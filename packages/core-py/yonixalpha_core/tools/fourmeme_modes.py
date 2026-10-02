"""Four.meme token modes on real tokens (read-only, master §8 / M10c).

four-meme-ai names the on-chain flags but not the layout of the getters
that hold them:
  X Mode             TokenManager2._tokenInfos[token].template & 0x10000
  TaxToken           (template >> 10) & 0x3F == 5
  Agent creator      template & (1 << 85)
  AntiSniperFeeMode  TokenManager2._tokenInfoEx1s[token].feeSetting > 0

Decoding a guessed layout could misread every token, so this tool collects
the evidence that fixes it, for the newest --tokens Four.meme tokens still
on the curve:
  1. the plain-buy simulation (the X Mode check safety now runs): reverted
     "A" = X Mode, by the contract's own answer;
  2. TaxToken from feeRate() on the token (what we already use);
  3. the raw 32-byte words _tokenInfos / _tokenInfoEx1s return;
  4. with --api, four.meme's own token API (version "V8" = X Mode,
     feePlan = AntiSniperFeeMode, taxInfo = TaxToken), as a cross-check;
  5. with ETHERSCAN_API_KEY set, the verified TokenManager2 ABI (via the
     EIP-1967 implementation), which names the fields outright.

Second run (2026-10-02, 40 newest tokens): 13 words; word 0 = the token,
word 1 = the quote (non-zero exactly for the 32 BEP-20-quoted tokens),
word 3 = 1e27 (total supply), words 4 / 7 = 8e26 (max offers / offers), so
the layout looks like base, quote, template, totalSupply, maxOffers, ...;
but the sample held no TaxToken, X Mode or agent token, so the template
bits were not tested. This version also samples tokens that safety already
found X Mode and tokens with a stored tax, and tests word 2 directly.

Then, for every word of _tokenInfos, how often its template bits agree
with the TaxToken and X Mode evidence, and for _tokenInfoEx1s which words
are non-zero and whether they follow the API's feePlan. A word that agrees
on every token is the template; nothing is decoded in production until this
output has been read. Nothing is written.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        exec -T data-evm python -m yonixalpha_core.tools.fourmeme_modes [--tokens 40] [--api]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any

import httpx

INFO_GETTER = "_tokenInfos(address)"
EX1_GETTER = "_tokenInfoEx1s(address)"
FOUR_API = "https://four.meme/meme-api/v1/private/token/get/v2"
X_MODE_BIT = 0x10000
AGENT_BIT = 1 << 85
TAX_TEMPLATE = 5


def words(raw: str | None) -> list[int]:
    h = (raw or "0x")[2:]
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - len(h) % 64, 64)]


def analyse(rows: list[dict[str, Any]]) -> list[str]:
    """Report lines from per-token evidence (pure; tested)."""
    out: list[str] = []
    status = Counter(r["plain_buy"] for r in rows)
    out.append(f"tokens: {len(rows)}; plain-buy simulation: " + ", ".join(f"{k} {v}" for k, v in status.most_common()))
    taxed = [r for r in rows if r.get("tax_bps") is not None]
    out.append(f"TaxToken by feeRate(): {sum(1 for r in taxed if r['tax_bps'] > 0)} of {len(taxed)} read")
    xknown = [r for r in rows if r["plain_buy"] in ("X_MODE", "PLAIN_BUY_OK")]
    n_info = max((len(r["info_words"]) for r in rows), default=0)
    out.append(f"{INFO_GETTER}: returns {sorted({len(r['info_words']) for r in rows})} words")
    for i in range(n_info):
        have = [r for r in rows if len(r["info_words"]) > i]
        tax_agree = sum(1 for r in have if r.get("tax_bps") is not None
                        and (((r["info_words"][i] >> 10) & 0x3F) == TAX_TEMPLATE) == (r["tax_bps"] > 0))
        tax_n = sum(1 for r in have if r.get("tax_bps") is not None)
        x_agree = sum(1 for r in have if r in xknown
                      and bool(r["info_words"][i] & X_MODE_BIT) == (r["plain_buy"] == "X_MODE"))
        x_n = sum(1 for r in have if r in xknown)
        agent = sum(1 for r in have if r["info_words"][i] & AGENT_BIT)
        nonzero = sum(1 for r in have if r["info_words"][i])
        out.append(f"    word {i:>2}: TaxToken bits agree {tax_agree}/{tax_n}; X Mode bit agrees {x_agree}/{x_n}; "
                   f"agent bit set {agent}; non-zero {nonzero}/{len(have)}")
    n_ex = max((len(r["ex1_words"]) for r in rows), default=0)
    out.append(f"{EX1_GETTER}: returns {sorted({len(r['ex1_words']) for r in rows})} words")
    api = [r for r in rows if r.get("api_fee_plan") is not None]
    for i in range(n_ex):
        have = [r for r in rows if len(r["ex1_words"]) > i]
        nonzero = sum(1 for r in have if r["ex1_words"][i])
        fp = [r for r in api if len(r["ex1_words"]) > i]
        agree = sum(1 for r in fp if bool(r["ex1_words"][i]) == bool(r["api_fee_plan"]))
        out.append(f"    word {i:>2}: non-zero {nonzero}/{len(have)}" + (f"; non-zero agrees with API feePlan {agree}/{len(fp)}"
                                                                        if fp else ""))
    if api:
        v = Counter(str(r.get("api_version")) for r in rows if r.get("api_version") is not None)
        x_api = [r for r in xknown if r.get("api_version") is not None]
        out.append("four.meme API versions: " + ", ".join(f"{k} {n}" for k, n in v.most_common())
                   + f"; simulation X_MODE agrees with version V8 on {sum(1 for r in x_api if (r['api_version'] == 'V8') == (r['plain_buy'] == 'X_MODE'))}/{len(x_api)}")
    out += template_lines(rows)
    return out


TEMPLATE_WORD = 2  # hypothesis from the 2026-10-02 run (base, quote, template, ...)


def template_lines(rows: list[dict[str, Any]], i: int = TEMPLATE_WORD) -> list[str]:
    """The hypothesis 'word 2 is the template', tested against the evidence."""
    have = [r for r in rows if len(r["info_words"]) > i]
    if not have:
        return []
    ct = Counter((r["info_words"][i] >> 10) & 0x3F for r in have)
    out = [f"template hypothesis (word {i}): creator types " + ", ".join(f"{k}: {v}" for k, v in sorted(ct.items()))]
    taxed = [r for r in have if (r.get("tax_bps") or 0) > 0]
    x = [r for r in have if r["plain_buy"] == "X_MODE"]
    plain = [r for r in have if r["plain_buy"] == "PLAIN_BUY_OK"]
    out.append(f"    TaxTokens (feeRate > 0): {len(taxed)}; with creator type 5: "
               f"{sum(1 for r in taxed if ((r['info_words'][i] >> 10) & 0x3F) == TAX_TEMPLATE)}")
    out.append(f"    X Mode by simulation: {len(x)}; with bit 16: {sum(1 for r in x if r['info_words'][i] & X_MODE_BIT)}; "
               f"plain buy OK: {len(plain)}; with bit 16: {sum(1 for r in plain if r['info_words'][i] & X_MODE_BIT)}")
    out.append(f"    agent bit 85 set: {sum(1 for r in have if r['info_words'][i] & AGENT_BIT)} of {len(have)}")
    quotes = Counter(hex(r["info_words"][1]) if len(r["info_words"]) > 1 else "?" for r in rows)
    out.append("quote assets (word 1; 0x0 = BNB): " + ", ".join(f"{q} {n}" for q, n in quotes.most_common(8)))
    errs = Counter(r["api_error"] for r in rows if r.get("api_error"))
    if errs:
        out.append("four.meme API errors: " + ", ".join(f"{e} {n}" for e, n in errs.most_common()))
    return out


async def _api(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    try:
        r = await client.get(FOUR_API, params={"address": token}, headers={"accept": "application/json"})
        if r.status_code != 200:
            return {"api_error": f"HTTP {r.status_code}"}
        body = r.json() or {}
        d = body.get("data") or {}
        if not d:
            return {"api_error": f"no data (code {body.get('code')}, msg {str(body.get('msg'))[:40]})"}
        return {"api_version": d.get("version"), "api_fee_plan": d.get("feePlan"), "api_tax": bool(d.get("taxInfo")),
                "api_ai_creator": d.get("aiCreator")}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return {"api_error": type(exc).__name__}


async def verified_abi(client: httpx.AsyncClient, rpc, manager: str, key: str) -> list[str]:
    from yonixalpha_core.chains.evm.safety import EIP1967_IMPLEMENTATION
    from yonixalpha_core.launch_coordination import ETHERSCAN_V2

    slot = await rpc.get_storage_at(manager, EIP1967_IMPLEMENTATION)
    impl = "0x" + slot[-40:] if slot and int(slot, 16) else None
    out = [f"TokenManager2 {manager}: EIP-1967 implementation {impl or 'not set'}"]
    for addr in [a for a in (impl, manager) if a]:
        r = await client.get(ETHERSCAN_V2, params={"chainid": 56, "module": "contract", "action": "getabi",
                                                   "address": addr, "apikey": key})
        body = r.json()
        if str(body.get("status")) != "1":
            out.append(f"  {addr}: no verified ABI ({str(body.get('result'))[:120]})")
            continue
        for fn in json.loads(body["result"]):
            if fn.get("name") in ("_tokenInfos", "_tokenInfoEx1s"):
                fields = ", ".join(f"{o.get('type')} {o.get('name')}" for o in fn.get("outputs") or [])
                out.append(f"  {addr} {fn['name']}(address) -> ({fields})")
        break
    return out


async def collect(session, rpc, n: int, probe: int, use_api: bool) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from yonixalpha_core.chains.evm.abi import encode_call
    from yonixalpha_core.chains.evm.fourmeme import FourMeme
    from yonixalpha_core.db.models import EvmToken

    lp = FourMeme(rpc)
    manager = lp.spec.contracts["manager_v2"]
    base = (EvmToken.chain == "bsc", EvmToken.launchpad == "fourmeme")
    newest = (await session.execute(select(EvmToken.token).where(*base, EvmToken.stage == "CURVE")
                                    .order_by(EvmToken.created_at.desc()).limit(n))).scalars().all()
    # tokens safety already found X Mode, and tokens with a stored tax: the evidence the newest lack
    x_mode = (await session.execute(select(EvmToken.token).where(
        *base, EvmToken.safety["findings"].contains([{"code": "FOURMEME_X_MODE"}]))
        .order_by(EvmToken.safety_at.desc()).limit(n))).scalars().all()
    taxed = (await session.execute(select(EvmToken.token).where(
        *base, EvmToken.state["buy_tax_bps"].astext.op("~")("^[1-9][0-9]*$"))
        .order_by(EvmToken.state_at.desc()).limit(n))).scalars().all()
    tokens = list(dict.fromkeys([*newest, *x_mode, *taxed]))
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for t in tokens:
            row: dict[str, Any] = {"token": t}
            row["plain_buy"] = (await lp.plain_buy(t, probe))["status"]
            try:
                tax, _src = await lp._tax_bps(t)
                row["tax_bps"] = tax
            except Exception:  # noqa: BLE001 - unread, not zero
                row["tax_bps"] = None
            for key, sig in (("info_words", INFO_GETTER), ("ex1_words", EX1_GETTER)):
                try:
                    row[key] = words(await rpc.eth_call(manager, encode_call(sig, t)))
                except Exception as exc:  # noqa: BLE001
                    row[key], row[key + "_error"] = [], f"{type(exc).__name__}: {str(exc)[:80]}"
            if use_api:
                row.update(await _api(client, t))
            rows.append(row)
    return rows


async def main() -> int:
    from yonixalpha_core.chains.evm import rpc_registry
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tokens", type=int, default=60, help="per group: newest, X Mode by safety, taxed")
    ap.add_argument("--probe-bnb", type=float, default=0.01)
    ap.add_argument("--api", action="store_true", help="cross-check with four.meme's token API")
    args = ap.parse_args()
    settings = get_settings()
    engine = make_engine(settings)
    rpc = None
    try:
        async with make_session_factory(engine)() as session:
            rpc = await rpc_registry.rpc_for(session, settings, "bsc")
            rows = await collect(session, rpc, args.tokens, int(args.probe_bnb * 10 ** 18), args.api)
            for line in analyse(rows):
                print(line)
            errs = Counter(r.get("info_words_error") or r.get("ex1_words_error") for r in rows
                           if r.get("info_words_error") or r.get("ex1_words_error"))
            for e, k in errs.items():
                print(f"getter errors: {k} x {e}")
            for r in rows[:5]:
                print(f"sample {r['token']}: plain_buy {r['plain_buy']}, tax {r.get('tax_bps')}, "
                      f"info {[hex(w) for w in r['info_words']]}, ex1 {[hex(w) for w in r['ex1_words']]}"
                      + (f", api {r.get('api_version')} feePlan {r.get('api_fee_plan')}" if args.api else ""))
            if settings.ETHERSCAN_API_KEY:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    from yonixalpha_core.chains.registry import LAUNCHPADS

                    for line in await verified_abi(client, rpc, LAUNCHPADS["fourmeme"].contracts["manager_v2"],
                                                   settings.ETHERSCAN_API_KEY):
                        print(line)
            else:
                print("verified ABI: not fetched (ETHERSCAN_API_KEY not set)")
    finally:
        if rpc is not None:
            await rpc.aclose()
        await engine.dispose()
    print("\nSend this output back. The template / feeSetting layout is decoded only after it is read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
