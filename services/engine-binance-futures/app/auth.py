import hashlib
import hmac
from urllib.parse import urlencode

# Binance's signed-endpoint scheme (HMAC-SHA256 over the literal query
# string, API key in the X-MBX-APIKEY header, `timestamp`/`signature` as
# query params) is one of the most stable, heavily-documented parts of
# their API surface and has been unchanged for years — implemented here
# with high confidence despite this environment having no live network
# access to verify against current docs (see ARCHITECTURE_AUDIT.md).
# `listenKey` endpoints are a deliberate exception: they need only the API
# key header, no signature — see client.py's _keyed_request.


def build_signed_query(params: dict, api_secret: str) -> str:
    """Returns the full query string (params + signature) to append to a
    signed endpoint's URL. The signature covers exactly this query string,
    so callers must send this string verbatim as the URL's query — never
    let httpx or anything else re-encode `params` separately afterward, or
    the signature and the actual request will silently diverge.
    """
    query_string = urlencode(params, doseq=True)
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"
