"""Explorer actions (master §55): links for a token, transaction, creator,
wallet, launchpad, DEX and block explorer, built for the token's own chain.

Rules:
- the chain decides every link; an address that is not valid for that
  chain gets no link (a Solana link is never built for an EVM token, nor
  an EVM link for a Solana mint);
- only URL formats confirmed from a primary or observed source are used;
  where none is confirmed the action is unavailable with the reason,
  never a guessed URL.

Formats and where they were confirmed (2026-10-03):
  Solscan            solscan.io/token|tx|account/<id>          (used by the live pages)
  BscScan            bscscan.com/token|tx|address/<id>
  Robinhood Chain    robinhoodchain.blockscout.com/token|tx|address/<id>
                     (Blockscout; launch_coordination reads its API)
  Pump.fun           pump.fun/coin/<mint>
  Four.meme          four.meme/token/<address>
  Flap               flap.sh/bnb/<address>
  Pons               ponsfamily.com/launchpad/<token address>  (route in ponsdotdev/ponsfamily pons-beta.md)
  DexScreener        dexscreener.com/solana|bsc/<address>      (no Robinhood Chain page confirmed)
"""

from __future__ import annotations

import re
from typing import Any

SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SOLANA_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")

EXPLORER = {
    "solana": ("Solscan", {"token": "https://solscan.io/token/{}", "tx": "https://solscan.io/tx/{}",
                           "address": "https://solscan.io/account/{}"}),
    "bsc": ("BscScan", {"token": "https://bscscan.com/token/{}", "tx": "https://bscscan.com/tx/{}",
                        "address": "https://bscscan.com/address/{}"}),
    "robinhood": ("Blockscout", {"token": "https://robinhoodchain.blockscout.com/token/{}",
                                 "tx": "https://robinhoodchain.blockscout.com/tx/{}",
                                 "address": "https://robinhoodchain.blockscout.com/address/{}"}),
}
LAUNCHPAD_PAGE = {
    "pumpfun": "https://pump.fun/coin/{}", "pumpswap": "https://pump.fun/coin/{}",
    "fourmeme": "https://four.meme/token/{}", "flap": "https://flap.sh/bnb/{}",
    "pons_v2": "https://ponsfamily.com/launchpad/{}",
}
LAUNCHPAD_CHAIN = {"pumpfun": "solana", "pumpswap": "solana", "fourmeme": "bsc", "flap": "bsc", "pons_v2": "robinhood"}
DEX_PAGE = {"solana": "https://dexscreener.com/solana/{}", "bsc": "https://dexscreener.com/bsc/{}"}


def is_address(chain: str, value: str | None) -> bool:
    if not value:
        return False
    return bool(SOLANA_ADDRESS.match(value) if chain == "solana" else EVM_ADDRESS.match(value))


def is_tx(chain: str, value: str | None) -> bool:
    if not value:
        return False
    return bool(SOLANA_SIGNATURE.match(value) if chain == "solana" else EVM_TX.match(value))


def internal_token_path(chain: str, token: str) -> str:
    """The dashboard's own token page: the Solana token terminal, or the EVM explorer page."""
    return f"/dashboard/tokens/{token}" if chain == "solana" else f"/dashboard/explorer/{chain}/{token}"


def links(chain: str, token: str | None = None, launchpad: str | None = None, creator: str | None = None,
          tx: str | None = None, wallet: str | None = None) -> dict[str, Any]:
    """{"chain", "explorer_name", "links": {action: url}, "unavailable": {action: reason}}
    for the actions OPEN TOKEN / TRANSACTION / CREATOR / WALLET / LAUNCHPAD /
    DEX / EXPLORER."""
    if chain not in EXPLORER:
        raise ValueError(f"unknown chain {chain!r}")
    name, ex = EXPLORER[chain]
    out: dict[str, str] = {}
    missing: dict[str, str] = {}

    def add(action: str, ok: bool, url: str | None, why: str) -> None:
        if ok and url:
            out[action] = url
        else:
            missing[action] = why

    token_ok = is_address(chain, token)
    add("token", token_ok, internal_token_path(chain, token) if token_ok else None,
        "no token" if not token else f"not a valid {chain} token address")
    add("explorer", token_ok, ex["token"].format(token) if token_ok else None,
        "no token" if not token else f"not a valid {chain} token address")
    add("transaction", is_tx(chain, tx), ex["tx"].format(tx) if is_tx(chain, tx) else None,
        "no transaction" if not tx else f"not a valid {chain} transaction id")
    add("creator", is_address(chain, creator), ex["address"].format(creator) if is_address(chain, creator) else None,
        "creator not known" if not creator else f"not a valid {chain} address")
    add("wallet", is_address(chain, wallet), ex["address"].format(wallet) if is_address(chain, wallet) else None,
        "no wallet" if not wallet else f"not a valid {chain} address")
    page = LAUNCHPAD_PAGE.get(launchpad or "")
    if page and LAUNCHPAD_CHAIN.get(launchpad) != chain:
        page = None  # a launchpad page of another chain is never linked
    add("launchpad", token_ok and page is not None, page.format(token) if token_ok and page else None,
        "no token" if not token_ok else f"no confirmed {launchpad or 'launchpad'} page format")
    dex = DEX_PAGE.get(chain)
    add("dex", token_ok and dex is not None, dex.format(token) if token_ok and dex else None,
        "no token" if not token_ok else f"no confirmed DEX page for {chain}")
    return {"chain": chain, "explorer_name": name, "links": out, "unavailable": missing}
