"""Every strategy the Crypto Trading engine knows, with its provenance and
honest status. Only status IMPLEMENTED can be enabled; a BLOCKED or
NOT_IMPLEMENTED entry is listed so the page shows why it is not running.

Rules are re-implemented from the inspected source (no files copied: the
source repositories carry no licence). Each entry records the repository
and commit that was read."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable

IMPLEMENTED = "IMPLEMENTED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
BLOCKED = "BLOCKED"
RESEARCH_ONLY = "RESEARCH_ONLY"

SPOT_EXCHANGES = ("bybit", "binance", "binance_us")


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    version: str
    category: str
    status: str
    status_reason: str
    provenance: dict[str, str]
    exchanges: tuple[str, ...]
    intervals: tuple[str, ...]
    default_exchange: str
    default_symbols: tuple[str, ...]
    default_interval: str
    # key -> (default, kind, lo, hi) where kind is "int" | "dec" | "bool" | "choice:<a>|<b>"
    params: dict[str, tuple[Any, str, Any, Any]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    max_symbols: int = 3
    # Extra validation of a merged config (returns error strings).
    check: Callable[[dict[str, Any]], list[str]] | None = None
    # Strategy Center: plain-language rules and the research record (strategies.library).
    description: str = ""
    entry_rules: str = ""
    exit_rules: str = ""
    research: dict[str, Any] = field(default_factory=dict)
    default_exits: dict[str, Any] = field(default_factory=dict)
    library: bool = False

    def default_params(self) -> dict[str, Any]:
        return {k: (str(v[0]) if isinstance(v[0], Decimal) else v[0]) for k, v in self.params.items()}


_REGISTRY: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> StrategySpec:
    _REGISTRY[spec.id] = spec
    return spec


def get(strategy_id: str) -> StrategySpec | None:
    return _REGISTRY.get(strategy_id)


__all__ = ["replace"]  # dataclasses.replace, re-exported for strategies.library


def all_specs() -> list[StrategySpec]:
    return list(_REGISTRY.values())


# --- built-in entries -------------------------------------------------------------------------

def _meta_muse_check(cfg: dict[str, Any]) -> list[str]:
    errors = []
    if len(cfg.get("symbols") or []) != 2:
        errors.append("meta_muse needs exactly two symbols: [leading (watched), traded]")
    p = cfg.get("params") or {}
    if int(p.get("fast", 9)) >= int(p.get("slow", 21)):
        errors.append("params.fast must be smaller than params.slow")
    if int(p.get("candles", 150)) < 2 * int(p.get("slow", 21)):
        errors.append("params.candles must be at least twice params.slow (EMA warm-up)")
    return errors


register(StrategySpec(
    id="meta_muse", name="Meta Muse Crossover (BTC trend vs ETH trend)", version="1", category="trend divergence",
    status=IMPLEMENTED, status_reason="",
    provenance={"repo": "https://github.com/hoziertom44-arch/Meta_Muse_Crossover_strategy", "commit": "a163ff7",
                "copy": "yonixnetwork/meta-muse-crossover-strategy@ab3b52d", "licence": "none (rules re-implemented)"},
    exchanges=SPOT_EXCHANGES, intervals=("5m", "15m", "1h"), default_exchange="binance_us",
    default_symbols=("BTC/USDT", "ETH/USDT"), default_interval="5m", max_symbols=2,
    params={"fast": (9, "int", 2, 100), "slow": (21, "int", 3, 400),
            "trend_threshold": (Decimal("0.0015"), "dec", "0", "0.05"),
            "stop_pct": (Decimal("0.01"), "dec", "0.001", "0.2"), "target_pct": (Decimal("0.02"), "dec", "0.001", "0.5"),
            "candles": (150, "int", 30, 1000)},
    notes=("original trades Binance USD-M futures both ways at 10x with a $50 margin; here spot DEMO, LONG entries "
           "only (SHORT signals are recorded, not simulated), size from the risk settings",
           "original reads the still-forming candle; here closed candles only",
           "original default history: 150 candles (config.MAX_KLINES)"),
    check=_meta_muse_check))

register(StrategySpec(
    id="gold_vs_btc", name="Gold vs BTC dual trend", version="0", category="cross-asset filter",
    status=BLOCKED,
    status_reason="needs a gold market: Binance futures XAUUSDT is refused from this server (HTTP 451); "
                  "the gold source (Hyperliquid PAXG or xyz:GOLD) is an operator decision",
    provenance={"repo": "yonixnetwork/goldvsbtc-binance-future", "commit": "332bbca", "licence": "none"},
    exchanges=(), intervals=("15m",), default_exchange="", default_symbols=(), default_interval="15m"))

register(StrategySpec(
    id="hyperliquid_grid", name="Hyperliquid grid", version="0", category="grid",
    status=NOT_IMPLEMENTED, status_reason="Hyperliquid is a separate venue with its own adapter (later increment)",
    provenance={"repo": "https://github.com/hoziertom44-arch/hyperliquid_grid_trading_bot", "commit": "710cebb",
                "licence": "none"},
    exchanges=(), intervals=(), default_exchange="", default_symbols=(), default_interval=""))

register(StrategySpec(
    id="confluence_matrix", name="Confluence Matrix (MSB + confluence score)", version="0", category="structure breakout",
    status=BLOCKED, status_reason="forex (EURUSD, GBPUSD) has no data source here and the original needs MetaTrader 5 "
                                  "(Windows); the adapted gold demo waits for the gold source decision",
    provenance={"repo": "https://github.com/siriusforex-ai/confluence_matrix_forex", "commit": "f3beff9",
                "licence": "none"},
    exchanges=(), intervals=("15m",), default_exchange="", default_symbols=(), default_interval="15m"))
