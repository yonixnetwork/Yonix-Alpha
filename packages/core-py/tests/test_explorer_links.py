"""Explorer actions (master §55): every link is built for the token's own
chain; a Solana link is never built for an EVM token (or the reverse); an
unconfirmed format is reported unavailable, never guessed."""

from yonixalpha_core import explorer_links as el

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
SIG = "5" * 88
EVM = "0x" + "ab" * 20
TX = "0x" + "cd" * 32


def test_solana_links():
    r = el.links("solana", MINT, "pumpfun", creator=MINT, tx=SIG, wallet=MINT)
    assert r["links"] == {
        "token": f"/dashboard/tokens/{MINT}", "explorer": f"https://solscan.io/token/{MINT}",
        "transaction": f"https://solscan.io/tx/{SIG}", "creator": f"https://solscan.io/account/{MINT}",
        "wallet": f"https://solscan.io/account/{MINT}", "launchpad": f"https://pump.fun/coin/{MINT}",
        "dex": f"https://dexscreener.com/solana/{MINT}"}
    assert r["explorer_name"] == "Solscan" and r["unavailable"] == {}


def test_evm_links_use_the_evm_chains_own_explorer():
    bsc = el.links("bsc", EVM, "fourmeme", creator=EVM, tx=TX)
    assert bsc["links"]["explorer"] == f"https://bscscan.com/token/{EVM}"
    assert bsc["links"]["transaction"] == f"https://bscscan.com/tx/{TX}"
    assert bsc["links"]["launchpad"] == f"https://four.meme/token/{EVM}"
    assert bsc["links"]["dex"] == f"https://dexscreener.com/bsc/{EVM}"
    assert el.links("bsc", EVM, "flap")["links"]["launchpad"] == f"https://flap.sh/bnb/{EVM}"
    hood = el.links("robinhood", EVM, "pons_v2", wallet=EVM)
    assert hood["links"]["explorer"] == f"https://robinhoodchain.blockscout.com/token/{EVM}"
    assert hood["links"]["wallet"] == f"https://robinhoodchain.blockscout.com/address/{EVM}"
    assert hood["links"]["launchpad"] == f"https://ponsfamily.com/launchpad/{EVM}"
    assert "dex" in hood["unavailable"] and "no confirmed DEX page" in hood["unavailable"]["dex"]
    assert "launchpad" in el.links("bsc", EVM, "genius_fun")["unavailable"]  # no confirmed page format


def test_never_a_link_for_the_wrong_chain():
    for chain in ("bsc", "robinhood"):
        r = el.links(chain, MINT, "pumpfun", creator=MINT, tx=SIG)  # Solana ids on an EVM chain
        assert r["links"] == {} and all("solscan" not in u and "pump.fun" not in u for u in r["links"].values())
    r = el.links("solana", EVM, "fourmeme", creator=EVM, tx=TX)  # EVM ids on Solana
    assert r["links"] == {}
    r = el.links("robinhood", EVM, "fourmeme")  # a BSC launchpad page is never linked for a Robinhood token
    assert "launchpad" not in r["links"] and "launchpad" in r["unavailable"]
    all_urls = [u for c in ("bsc", "robinhood") for u in el.links(c, EVM, "fourmeme", EVM, TX, EVM)["links"].values()]
    assert not any("solscan" in u or "/solana/" in u or "pump.fun" in u for u in all_urls)
