from yonixalpha_core.redact import redact_text, redact_url
from yonixalpha_core.solana.rpc import RpcManager


def test_urls_keep_only_scheme_and_host():
    assert redact_url("https://mainnet.helius-rpc.com/?api-key=SECRET") == "https://mainnet.helius-rpc.com/…"
    assert redact_url("wss://x.quiknode.pro/TOKEN123/") == "wss://x.quiknode.pro/…"
    assert redact_url("https://api.mainnet-beta.solana.com") == "https://api.mainnet-beta.solana.com"
    assert redact_url(None) == ""


def test_error_text_is_scrubbed():
    url = "https://rpc.example/?api-key=SECRET"
    msg = f"Client error '403 Forbidden' for url '{url}'"
    assert "SECRET" not in redact_text(msg, [url, None])


def test_rpc_health_snapshot_never_contains_key():
    import httpx

    rpc = RpcManager.create(client=httpx.AsyncClient(), primary_url="https://h.example/?api-key=SECRET")
    assert "SECRET" not in str(rpc.health_snapshot())
