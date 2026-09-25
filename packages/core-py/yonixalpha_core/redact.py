"""Keeps credentials out of logs, system events and alerts.

RPC providers commonly put the API key in the URL (Helius: ?api-key=...,
QuickNode/others: a token path segment), and httpx error messages embed
the request URL. So a provider URL is only ever shown as scheme://host.
"""

from urllib.parse import urlsplit


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
