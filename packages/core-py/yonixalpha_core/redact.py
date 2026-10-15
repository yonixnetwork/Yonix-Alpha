"""Keeps credentials out of logs, system events and alerts.

RPC providers commonly put the API key in the URL (Helius: ?api-key=...,
QuickNode/others: a token path segment), and httpx error messages embed
the request URL. So a provider URL is only ever shown as scheme://host.
"""

import re
from urllib.parse import parse_qsl, urlsplit


def redact_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    host = parts.hostname or "?"
    return f"{parts.scheme}://{host}" + ("/…" if (parts.path not in ("", "/") or parts.query) else "")


def redact_text(text: str, urls: list[str | None]) -> str:
    """Replaces every occurrence of each full URL in `text` with its
    redacted form (longest first, so a URL that prefixes another can't
    leave part of the longer one behind)."""
    for url in sorted((u for u in urls if u), key=len, reverse=True):
        text = text.replace(url, redact_url(url))
    return text


# -- credential-bearing URLs inside stored text (2026-10-15) ----------------
# A Chainstack RPC URL with its key was found inside a stored decision
# reason (an exception message copied into RiskAssessment.assessment). Text
# that is saved to the database goes through scrub_credential_urls (see
# yonixalpha_core.db.types): every URL that can carry a credential is cut to
# scheme://host/…, every other URL (token metadata, IPFS, explorers) is kept.
_URL_IN_TEXT = re.compile(r"(?:https?|wss?)://[^\s'\"<>]+")
# Hosts of RPC / data providers that put the key in the path or query.
_PROVIDER_HOST = re.compile(
    r"rpc|helius|quiknode|quicknode|chainstack|alchemy|ankr|syndica|triton|shyft|getblock|drpc|infura|nodereal"
    r"|publicnode|blockdaemon|blastapi|nownodes|tatum|moralis|bitquery|api\.telegram\.org", re.I)
_KEY_PARAM = re.compile(r"key|token|secret|sig|pass|auth|jwt|credential", re.I)


def has_credential(url: str) -> bool:
    """Whether this URL can carry a credential: user:password@, a key-like
    query parameter, or a provider host (keys often live in their path)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if parts.username or parts.password:
        return True
    if any(_KEY_PARAM.search(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return True
    return bool(_PROVIDER_HOST.search(parts.hostname or ""))


def scrub_credential_urls(text: str) -> str:
    if "://" not in text:
        return text
    new = _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)) if has_credential(m.group(0)) else m.group(0), text)
    return text if new == text else new  # the same object when nothing changed


def scrub_credentials(value):
    """scrub_credential_urls applied to every string inside a JSON-like
    value (dicts, lists, keys and values). Returns the same object when
    nothing changed."""
    if isinstance(value, str):
        return scrub_credential_urls(value)
    if isinstance(value, dict):
        out, changed = {}, False
        for k, v in value.items():
            k2 = scrub_credential_urls(k) if isinstance(k, str) else k
            v2 = scrub_credentials(v)
            changed = changed or k2 is not k or v2 is not v
            out[k2] = v2
        return out if changed else value
    if isinstance(value, (list, tuple)):
        items = [scrub_credentials(v) for v in value]
        if any(a is not b for a, b in zip(items, value)):
            return items if isinstance(value, list) else tuple(items)
        return value
    return value
