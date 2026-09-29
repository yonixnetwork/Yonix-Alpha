"""Wallet relationships among a launch's traders, and what they mean for
how much of its demand is independent.

Raw buyer counts and raw volume overstate demand when several "buyers" are
one actor. This module groups a launch's traders into relationship
clusters from evidence, then recomputes buyers, volume and flow with each
cluster counted as what it is.

Evidence (each edge says which kind it is):

  creator          the wallet is the creator, was funded by the creator, or
                   shares the creator's own (non-exchange) funder
  common_funder    two or more fresh wallets whose first SOL came from the
                   same funder, and that funder is NOT a busy address
                   (exchange / bridge / payment processor: >= 1000
                   transactions, solana.funding) — a shared exchange
                   withdrawal is not a relationship
  co_dump_history  wallets that sold early together in earlier collapsed
                   launches (wallet_intel cohorts, causal)
  timing           same-second buys / sells in THIS launch. Timing alone is
                   never an edge (block-0 snipers all buy in the same
                   second); it only upgrades a cluster that already has
                   another kind of evidence

Classifications, each with its evidence, never an accusation:
  CREATOR_RELATED, COORDINATED (funding or co-dump history + timing),
  FUNDING_RELATED, SMART_MONEY_CLUSTER (>= 2 historically proven wallets,
  no funding/creator link), POTENTIAL_COORDINATION (co-dump history only),
  PROTOCOL_CONTROLLED (pump.fun / PumpSwap program accounts), UNKNOWN.

Coverage is explicit. Funding sources come from solana.funding's cache
(filled by the gate's RPC checks, a week per wallet; the funder -> wallet
edges persist 30 days in yx:wg:children:*), so most traders of most launches
are UNATTRIBUTED: nothing is known about them either way. Unattributed
volume is never counted as organic; the organic-demand ratio is reported as
a lower and an upper bound, and as a single number only when enough of the
volume is attributed.

Everything here is a feature for the gate's configurable actions and for
ML, whose predictive value must be validated; none of it is a BUY or
REJECT rule by itself.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from yonixalpha_core.solana import funding as funding_mod
from yonixalpha_core.solana.flow import Trade

GRAPH_VERSION = "wg-1"
LAMPORTS = 1_000_000_000
FLOW_WINDOWS = (5, 10, 30, 60, 300)
ACCEL_WINDOW = 30
MAX_WALLETS_LISTED = 25

CREATOR, CREATOR_LINKED, PROTOCOL = "CREATOR", "CREATOR_LINKED", "PROTOCOL_CONTROLLED"
CLUSTERED, INDEPENDENT, UNATTRIBUTED = "CLUSTERED", "INDEPENDENT", "UNATTRIBUTED"
CREATOR_RELATED, COORDINATED, FUNDING_RELATED = "CREATOR_RELATED", "COORDINATED", "FUNDING_RELATED"
SMART_MONEY_CLUSTER, POTENTIAL_COORDINATION = "SMART_MONEY_CLUSTER", "POTENTIAL_COORDINATION"
# Clusters that act as one buyer: effective buyers count each as one.
DEPENDENT = (COORDINATED, FUNDING_RELATED)
DISCLAIMER = ("relationships are evidence of wallet dependence or coordination, not proof of manipulation; "
              "every number here is a feature whose predictive value must be validated")


def _sol(lamports: float) -> float:
    return round(lamports / LAMPORTS, 4)


def _cid(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:10]}"


def protocol_addresses(mint: str | None) -> set[str]:
    """pump.fun / PumpSwap program accounts that can appear next to a
    token's trades: never a creator's or a holder's wallet."""
    from yonixalpha_core.solana import pumpswap
    from yonixalpha_core.solana.creator_history import bonding_curve_pda
    from yonixalpha_core.solana.pumpfun import PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID
    out = {PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID}
    if mint:
        try:
            out |= {bonding_curve_pda(mint), pumpswap.pool_authority(mint), pumpswap.canonical_pool(mint)}
        except Exception:  # noqa: BLE001 - an invalid mint has no program accounts
            pass
    return out


# --- decision time: Redis lookups only (no RPC) -------------------------------------------

async def gather(redis, wallets: list[str], creator: str | None) -> dict[str, Any]:
    """Cached funding facts for `wallets` (and the creator): whatever the
    gate's funding checks have already learned. Unchecked wallets are None."""
    ws = list(dict.fromkeys(w for w in wallets if w))
    if creator and creator not in ws:
        ws.append(creator)
    if redis is None or not ws:
        return {"funding": {}, "busy": {}, "fanout": {}, "creator_funding": None}
    raw = await redis.mget([funding_mod.funder_key(w) for w in ws])
    info: dict[str, dict | None] = {}
    for w, r in zip(ws, raw):
        try:
            info[w] = json.loads(r) if r else None
        except (ValueError, TypeError):
            info[w] = None
    funders = sorted({i["funder"] for i in info.values() if i and i.get("funder")})
    busy: dict[str, bool | None] = {}
    fanout: dict[str, int] = {}
    if funders:
        braw = await redis.mget([funding_mod.busy_key(f) for f in funders])
        pipe = redis.pipeline()
        for f in funders:
            pipe.zcard(funding_mod.CHILDREN + f)
        cards = await pipe.execute()
        for f, b, n in zip(funders, braw, cards):
            try:
                busy[f] = bool(json.loads(b)["busy"]) if b else None
            except (ValueError, TypeError, KeyError):
                busy[f] = None
            fanout[f] = int(n or 0)
    return {"funding": info, "busy": busy, "fanout": fanout, "creator_funding": info.get(creator) if creator else None}


# --- analysis (pure) ------------------------------------------------------------------------

class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.p[self.find(a)] = self.find(b)


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def analyse(trades: list[Trade], now: datetime, ctx: dict[str, Any] | None, *, creator: str | None, mint: str | None,
            smart: dict[str, Any] | None = None, cohorts: list[list[str]] | None = None,
            min_attribution: float = 0.5) -> dict[str, Any]:
    """Relationship clusters, effective buyers, organic demand, flows and
    smart-money context at `now` (trades up to `now` only)."""
    held = sorted((t for t in trades if t.at <= now), key=lambda t: t.at)
    base = {"graph_version": GRAPH_VERSION, "as_of": now.isoformat(), "note": DISCLAIMER,
            "source": "pump stream trades + cached funding lookups (solana.funding) + wallet-intel cohorts"}
    if not held:
        return {**base, "status": "UNAVAILABLE", "reason": "no trades held for this token"}
    ctx = ctx or {}
    funding: dict[str, dict | None] = ctx.get("funding") or {}
    busy: dict[str, bool | None] = ctx.get("busy") or {}
    fanout: dict[str, int] = ctx.get("fanout") or {}
    creator_funder = (ctx.get("creator_funding") or {}).get("funder")
    protocol = protocol_addresses(mint)
    proven = {p["wallet"]: p for p in ((smart or {}).get("proven") or []) if p.get("wallet")}

    traders = list(dict.fromkeys(t.trader for t in held))
    buyers = list(dict.fromkeys(t.trader for t in held if t.is_buy))
    uf = _UF()
    kinds: dict[str, set[str]] = defaultdict(set)  # component root -> evidence kinds (recomputed below)
    edge_log: list[tuple[str, str, str]] = []  # (a, b, kind)
    evidence: dict[str, list[str]] = defaultdict(list)

    # creator group
    creator_group: set[str] = set()
    for w in traders:
        info = funding.get(w) or {}
        f = info.get("funder")
        if creator and w == creator:
            creator_group.add(w)
            evidence[w].append("the token's creator")
        elif w in protocol:
            continue
        elif creator and f == creator:
            creator_group.add(w)
            evidence[w].append("first SOL came from the creator")
        elif creator_funder and f == creator_funder and busy.get(f) is False:
            creator_group.add(w)
            evidence[w].append(f"shares the creator's funder {f[:6]}… (not an exchange-like address)")
    anchor = creator or next(iter(creator_group), None)
    for w in creator_group:
        if anchor and w != anchor:
            uf.union(w, anchor)
            edge_log.append((w, anchor, "creator"))

    # common funders (fresh wallets, non-busy funder)
    by_funder: dict[str, list[str]] = defaultdict(list)
    shared_unchecked: set[str] = set()
    for w in traders:
        info = funding.get(w) or {}
        f = info.get("funder")
        if w in protocol or not info.get("fresh") or not f or f == creator:
            continue
        by_funder[f].append(w)
    for f, ws in by_funder.items():
        if len(ws) < 2:
            continue
        if busy.get(f) is None:
            shared_unchecked.update(ws)
            continue
        if busy[f]:
            continue  # exchange-like: not a relationship
        for w in ws[1:]:
            uf.union(w, ws[0])
            edge_log.append((w, ws[0], "common_funder"))
        for w in ws:
            evidence[w].append(f"funded by {f[:6]}… together with {len(ws) - 1} other trader(s) of this token")

    # co-dump history cohorts
    present = set(traders)
    for g in cohorts or []:
        members = [w for w in g if w in present and w not in protocol]
        for w in members[1:]:
            uf.union(w, members[0])
            edge_log.append((w, members[0], "co_dump_history"))
        for w in members:
            evidence[w].append(f"sold early with {len(members) - 1} of this token's traders in earlier collapsed launches")

    # same-second timing in this launch
    buy_secs: dict[int, set[str]] = defaultdict(set)
    sell_secs: dict[int, set[str]] = defaultdict(set)
    for t in held:
        (buy_secs if t.is_buy else sell_secs)[int(t.at.timestamp())].add(t.trader)

    comps: dict[str, set[str]] = defaultdict(set)
    for w in traders:
        if w in uf.p:
            comps[uf.find(w)].add(w)
    for a, b, k in edge_log:
        kinds[uf.find(a)].add(k)

    # per-wallet flows
    buy_l: dict[str, int] = defaultdict(int)
    sell_l: dict[str, int] = defaultdict(int)
    tok: dict[str, int] = defaultdict(int)
    for t in held:
        if t.is_buy:
            buy_l[t.trader] += t.sol_lamports
            tok[t.trader] += t.token_raw
        else:
            sell_l[t.trader] += t.sol_lamports
            tok[t.trader] -= t.token_raw

    clusters: list[dict[str, Any]] = []
    cluster_of: dict[str, dict[str, Any]] = {}
    for root, members in comps.items():
        if len(members) < 2 and not (members & creator_group):
            continue
        ks = kinds.get(root, set())
        coord_buys = sum(1 for ws in buy_secs.values() if len(ws & members) >= 2)
        coord_sells = sum(1 for ws in sell_secs.values() if len(ws & members) >= 2)
        timing = coord_buys > 0 or coord_sells > 0
        n_proven = len(members & set(proven))
        if members & creator_group:
            cls = CREATOR_RELATED
        elif "common_funder" in ks:
            cls = COORDINATED if timing else FUNDING_RELATED
        elif "co_dump_history" in ks:
            cls = COORDINATED if timing else (SMART_MONEY_CLUSTER if n_proven >= 2 else POTENTIAL_COORDINATION)
        else:
            cls = POTENTIAL_COORDINATION
        tags = sorted({cls} | ({SMART_MONEY_CLUSTER} if n_proven >= 2 else set()))
        funders = sorted({(funding.get(w) or {}).get("funder") for w in members} - {None})
        shared = [f for f in funders if sum(1 for w in members if (funding.get(w) or {}).get("funder") == f) >= 2]
        funded = [(funding.get(w) or {}).get("funded_at") for w in members]
        funded = [x for x in funded if x]
        cluster_buy = sum(buy_l[w] for w in members)
        top_funder_buy = max((sum(buy_l[w] for w in members if (funding.get(w) or {}).get("funder") == f) for f in shared),
                             default=0)
        if cls == CREATOR_RELATED:
            cid = _cid("crt", creator or sorted(members)[0])
        elif shared:
            cid = _cid("fund", shared[0])
        else:
            cid = _cid("grp", ",".join(sorted(members)))
        ev = [f"evidence kinds: {', '.join(sorted(ks)) or 'creator'}"]
        if coord_buys or coord_sells:
            ev.append(f"same-second activity in this launch: {coord_buys} buy second(s), {coord_sells} sell second(s)")
        for w in sorted(members):
            ev.extend(f"{w[:6]}…: {e}" for e in evidence.get(w, [])[:2])
        c = {
            "cluster_id": cid, "classification": cls, "tags": tags, "size": len(members),
            "members": sorted(members), "buyers": len(members & set(buyers)),
            "parents": shared or funders[:3], "funding_fanout": {f: fanout.get(f) for f in (shared or funders)[:3]},
            "funding_concentration": _ratio(top_funder_buy, cluster_buy),
            "funding_window_seconds": (max(funded) - min(funded)) if len(funded) >= 2 else None,
            "coordinated_buy_seconds": coord_buys, "coordinated_sell_seconds": coord_sells,
            "proven_wallets": n_proven,
            "buy_sol": _sol(cluster_buy), "sell_sol": _sol(sum(sell_l[w] for w in members)),
            "net_sol": _sol(cluster_buy - sum(sell_l[w] for w in members)),
            "net_tokens_raw": sum(tok[w] for w in members),
            "evidence": ev[:12],
        }
        clusters.append(c)
        for w in members:
            cluster_of[w] = c
    clusters.sort(key=lambda c: (c["classification"] != CREATOR_RELATED, -c["size"]))

    # roles
    role: dict[str, str] = {}
    for w in traders:
        info = funding.get(w)
        c = cluster_of.get(w)
        if w in protocol:
            role[w] = PROTOCOL
        elif creator and w == creator:
            role[w] = CREATOR
        elif w in creator_group or (c and c["classification"] == CREATOR_RELATED):
            role[w] = CREATOR_LINKED
        elif c and c["classification"] in DEPENDENT:
            role[w] = CLUSTERED
        elif w in shared_unchecked:
            role[w] = UNATTRIBUTED  # shares a funder whose activity was never checked
        elif info is not None and not (c and c["classification"] in (POTENTIAL_COORDINATION, SMART_MONEY_CLUSTER)):
            role[w] = INDEPENDENT  # checked: established wallet, or its own / an exchange-like funder
        else:
            role[w] = UNATTRIBUTED

    def klass(w: str) -> str:
        r = role[w]
        if r == CREATOR:
            return "creator"
        if r == CREATOR_LINKED:
            return "creator_linked"
        if r == PROTOCOL:
            return "protocol"
        if r == CLUSTERED:
            return "coordinated" if cluster_of[w]["classification"] == COORDINATED else "funding_cluster"
        if r == INDEPENDENT:
            return "independent"
        c = cluster_of.get(w)
        return "potential_coordination" if c else "unattributed"

    cls_of = {w: klass(w) for w in traders}
    classes = ("creator", "creator_linked", "funding_cluster", "coordinated", "potential_coordination", "protocol",
               "independent", "unattributed")
    vol = dict.fromkeys(classes, 0)
    buyvol = dict.fromkeys(classes, 0)
    for t in held:
        vol[cls_of[t.trader]] += t.sol_lamports
        if t.is_buy:
            buyvol[cls_of[t.trader]] += t.sol_lamports
    total = sum(vol.values())
    total_buy = sum(buyvol.values())
    known_related = vol["creator"] + vol["creator_linked"] + vol["funding_cluster"] + vol["coordinated"] + vol["protocol"]
    attributed_share = _ratio(total - vol["unattributed"] - vol["potential_coordination"], total)
    lower = _ratio(vol["independent"], total)
    upper = _ratio(vol["independent"] + vol["unattributed"] + vol["potential_coordination"], total)
    measured = attributed_share is not None and attributed_share >= min_attribution
    demand = {
        "total_volume_sol": _sol(total), **{f"{k}_volume_sol": _sol(v) for k, v in vol.items()},
        "known_related_volume_sol": _sol(known_related), "unattributed_volume_sol": _sol(vol["unattributed"]),
        "attributed_volume_share": attributed_share,
        "organic_demand_ratio": lower if measured else None,
        "organic_demand_ratio_lower": lower, "organic_demand_ratio_upper": upper,
        "organic_buy_ratio_lower": _ratio(buyvol["independent"], total_buy),
        "organic_buy_ratio_upper": _ratio(buyvol["independent"] + buyvol["unattributed"] + buyvol["potential_coordination"],
                                          total_buy),
        "status": "MEASURED" if measured else "PARTIAL" if attributed_share else "UNATTRIBUTED",
        "why": (None if measured else
                f"only {attributed_share or 0:.0%} of volume is attributed (needs {min_attribution:.0%}); "
                "unattributed volume is not assumed organic, see the lower/upper bounds"),
    }

    # effective buyers
    creator_buyers = [w for w in buyers if role[w] in (CREATOR, CREATOR_LINKED)]
    protocol_buyers = [w for w in buyers if role[w] == PROTOCOL]
    dep = [c for c in clusters if c["classification"] in DEPENDENT]
    excess = sum(max(0, sum(1 for w in c["members"] if w in set(buyers)) - 1) for c in dep)
    effective = len(buyers) - len(creator_buyers) - len(protocol_buyers) - excess
    explain = [f"{len(buyers)} apparent buyers"]
    if creator_buyers:
        explain.append(f"− {len(creator_buyers)} creator / creator-linked")
    if protocol_buyers:
        explain.append(f"− {len(protocol_buyers)} protocol-controlled")
    for c in dep:
        b = sum(1 for w in c["members"] if w in set(buyers))
        if b > 1:
            explain.append(f"− {b - 1} ({c['classification']} cluster {c['cluster_id']} of {b} buyers counts as one)")
    buyers_block = {
        "raw_unique_buyers": len(buyers), "effective_unique_buyers": max(effective, 0),
        "creator_related_buyers": len(creator_buyers), "protocol_buyers": len(protocol_buyers),
        "coordinated_buyers": sum(1 for w in buyers if role[w] == CLUSTERED),
        "potential_coordination_buyers": sum(1 for w in buyers if cls_of[w] == "potential_coordination"),
        "independent_buyers": sum(1 for w in buyers if role[w] == INDEPENDENT),
        "unattributed_buyers": sum(1 for w in buyers if cls_of[w] == "unattributed"),
        "explanation": " ".join(explain) + f" = {max(effective, 0)} effective",
        "note": "potential-coordination and unattributed buyers are not subtracted: only clusters with funding or "
                "creator evidence count as one buyer",
    }

    # flows
    def actor(w: str) -> str | None:
        r = role[w]
        if r in (CREATOR, CREATOR_LINKED, PROTOCOL):
            return None
        return cluster_of[w]["cluster_id"] if r == CLUSTERED else w

    non_related = {"independent", "unattributed", "potential_coordination"}
    flows: dict[str, Any] = {}
    for win in FLOW_WINDOWS:
        xs = [t for t in held if t.at > now - timedelta(seconds=win)]
        agg = defaultdict(int)
        for t in xs:
            side = "buy" if t.is_buy else "sell"
            c = cls_of[t.trader]
            agg[side] += t.sol_lamports
            grp = ("creator" if c in ("creator", "creator_linked") else "cluster" if c in ("funding_cluster", "coordinated")
                   else "independent" if c == "independent" else "other")
            agg[f"{grp}_{side}"] += t.sol_lamports
            if c in non_related:
                agg[f"non_related_{side}"] += t.sol_lamports
        flows[f"{win}s"] = {
            "net_sol_flow": _sol(agg["buy"] - agg["sell"]), "gross_buy_flow": _sol(agg["buy"]), "gross_sell_flow": _sol(agg["sell"]),
            "independent_buy_flow": _sol(agg["independent_buy"]), "independent_sell_flow": _sol(agg["independent_sell"]),
            "cluster_buy_flow": _sol(agg["cluster_buy"]), "cluster_sell_flow": _sol(agg["cluster_sell"]),
            "creator_buy_flow": _sol(agg["creator_buy"]), "creator_sell_flow": _sol(agg["creator_sell"]),
            "organic_net_sol_flow": _sol(agg["independent_buy"] - agg["independent_sell"]),
            "non_related_net_sol_flow": _sol(agg["non_related_buy"] - agg["non_related_sell"]),
        }

    # accelerations: last ACCEL_WINDOW s vs the ACCEL_WINDOW s before
    r0, r1 = now - timedelta(seconds=2 * ACCEL_WINDOW), now - timedelta(seconds=ACCEL_WINDOW)
    first_buy: dict[str, datetime] = {}
    for t in held:
        if t.is_buy:
            first_buy.setdefault(t.trader, t.at)
    first_actor: dict[str, datetime] = {}
    for w, at in first_buy.items():
        a = actor(w)
        if a is not None and (a not in first_actor or at < first_actor[a]):
            first_actor[a] = at

    def window_stats(lo: datetime, hi: datetime) -> dict[str, int]:
        xs = [t for t in held if lo < t.at <= hi]
        return {"raw_vol": sum(t.sol_lamports for t in xs),
                "org_vol": sum(t.sol_lamports for t in xs if cls_of[t.trader] in non_related),
                "raw_trades": len(xs), "ind_trades": sum(1 for t in xs if cls_of[t.trader] in non_related),
                "raw_buyers": sum(1 for at in first_buy.values() if lo < at <= hi),
                "eff_buyers": sum(1 for at in first_actor.values() if lo < at <= hi)}

    prev, cur = window_stats(r0, r1), window_stats(r1, now)
    accel = {f"{name}_acceleration": _ratio(cur[k], prev[k]) for name, k in (
        ("raw_volume", "raw_vol"), ("organic_volume", "org_vol"), ("raw_buyer", "raw_buyers"),
        ("effective_buyer", "eff_buyers"), ("raw_trade", "raw_trades"), ("independent_trade", "ind_trades"))}
    accel.update({"window_seconds": ACCEL_WINDOW, "recent": cur, "previous": prev,
                  "note": "recent window / previous window; None when the previous window was empty. "
                          "'organic' and 'independent' here exclude only KNOWN related wallets"})

    # creator adjustment
    creator_trades = [t for t in held if cls_of[t.trader] == "creator"]
    related_trades = [t for t in held if cls_of[t.trader] in ("creator", "creator_linked")]
    sells_total = total - total_buy
    creator_block = {
        "creator_known": bool(creator),
        "creator_volume_ratio": _ratio(vol["creator"], total),
        "creator_buy_ratio": _ratio(buyvol["creator"], total_buy),
        "creator_sell_ratio": _ratio(vol["creator"] - buyvol["creator"], sells_total),
        "creator_related_volume_ratio": _ratio(vol["creator"] + vol["creator_linked"], total),
        "creator_related_buyer_ratio": _ratio(len(creator_buyers), len(buyers)),
        "creator_related_trade_ratio": _ratio(len(related_trades), len(held)),
        "creator_trades": len(creator_trades),
        "raw_volume_sol": _sol(total), "adjusted_volume_sol": _sol(total - known_related),
        "adjustment_reason": (f"removed {_sol(known_related)} SOL of creator, creator-linked, funding-cluster, coordinated "
                              "and protocol volume" if known_related else "no known related volume"),
    }

    # smart-money context
    sm_status = (smart or {}).get("status")
    by_class = {"independent": [], "clustered": [], "creator_related": [], "unknown": []}
    for w, p in proven.items():
        r = role.get(w)
        key = ("creator_related" if r in (CREATOR, CREATOR_LINKED) else "clustered" if r == CLUSTERED
               else "independent" if r == INDEPENDENT else "unknown")
        by_class[key].append(w)
    ind_actors = len(by_class["independent"]) + len({cluster_of[w]["cluster_id"] for w in by_class["clustered"]})
    n = len(proven)
    if sm_status != "MEASURED":
        context = "UNKNOWN"
    elif n == 0:
        context = "NONE"
    elif n == 1:
        context = "ONE_SMART_WALLET"
    elif len(by_class["independent"]) >= 2:
        context = "MULTIPLE_INDEPENDENT"
    elif by_class["creator_related"]:
        context = "CREATOR_RELATED"
    elif by_class["clustered"]:
        context = "MULTIPLE_RELATED"
    else:
        context = "MULTIPLE_UNATTRIBUTED"
    smart_block = {
        "status": sm_status or "UNKNOWN", "context": context, "smart_money_count": n,
        "smart_money_independent": len(by_class["independent"]), "smart_money_clustered": len(by_class["clustered"]),
        "smart_money_creator_related": len(by_class["creator_related"]), "smart_money_unknown": len(by_class["unknown"]),
        "smart_money_quality": round(sum(p.get("lower", 0) for p in proven.values()) / n, 4) if n else None,
        "smart_money_independence": _ratio(ind_actors, n) if n else None,
        "smart_money_signal_strength": ind_actors,
        "still_holding": sum(1 for w in proven if tok[w] > 0),
        "note": "smart-wallet activity is an observable feature whose predictive value must be validated; never a BUY trigger. "
                "Quality = mean posterior lower bound of the win rate of launches bought early (this system's history)",
    }

    checked = sum(1 for w in traders if funding.get(w) is not None)
    wallets_out = sorted(traders, key=lambda w: -(buy_l[w] + sell_l[w]))[:MAX_WALLETS_LISTED]
    return {
        **base, "status": "MEASURED",
        "coverage": {"traders": len(traders), "funding_known": checked,
                     "funding_known_share": _ratio(checked, len(traders)),
                     "shared_funder_activity_unchecked": len(shared_unchecked),
                     "cohort_history_used": bool(cohorts)},
        "buyers": buyers_block, "demand": demand, "creator": creator_block, "flows": flows,
        "acceleration": accel, "smart_money": smart_block,
        "clusters": clusters[:10], "cluster_count": len(clusters),
        "largest_dependent_cluster": max((c["size"] for c in dep), default=0),
        "wallets": [{"wallet": w, "role": role[w], "class": cls_of[w],
                     "cluster_id": cluster_of[w]["cluster_id"] if w in cluster_of else None,
                     "buy_sol": _sol(buy_l[w]), "sell_sol": _sol(sell_l[w]), "proven": w in proven,
                     "evidence": evidence.get(w, [])[:3]} for w in wallets_out],
    }


def compact(rel: dict[str, Any], *, wallets: int = 0, clusters: int = 5, members: int = 5) -> dict[str, Any]:
    """A smaller copy for storage on every ledger row: all aggregate numbers
    kept, the per-wallet table and long member lists trimmed (their counts
    stay: size, buyers)."""
    out = dict(rel)
    if "wallets" in out:
        out["wallets"] = out["wallets"][:wallets]
    if "clusters" in out:
        out["clusters"] = [{**c, "members": c["members"][:members], "evidence": c["evidence"][:4]}
                           for c in out["clusters"][:clusters]]
    if "flows" in out:
        out["flows"] = {k: {n: v for n, v in f.items() if v} for k, f in out["flows"].items()}
    # constant explanatory text is in the code and the dashboard, not on every row
    out.pop("note", None)
    for k in ("buyers", "smart_money", "acceleration"):
        if isinstance(out.get(k), dict):
            out[k] = {n: v for n, v in out[k].items() if n != "note"}
    return out
