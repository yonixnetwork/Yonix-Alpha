"""Exchange keys encrypted at rest.

Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`), the approach the
platform already uses for provider URLs (yonixalpha_core.secretbox), with a
separate derived key so the two can never decrypt each other's values. The
source is CRYPTO_CREDENTIALS_KEY when set, otherwise JWT_SECRET (required,
server-only, at least 32 characters; the API and the crypto worker both
have it). Rotating the source makes saved keys unreadable: they are then
reported as needing re-entry, never replaced or guessed.

A keyed fingerprint (HMAC of the API key) lets the store refuse the same key
in two environments without ever storing or comparing plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENC_INFO = b"yonixalpha-crypto-exchange-credentials-v1"
_FP_INFO = b"yonixalpha-crypto-exchange-key-fingerprint-v1"


class NoEncryptionKey(RuntimeError):
    pass


def _source(env: Mapping[str, str] | None) -> bytes:
    env = os.environ if env is None else env
    source = env.get("CRYPTO_CREDENTIALS_KEY") or env.get("JWT_SECRET") or ""
    if len(source) < 32:
        raise NoEncryptionKey("no encryption key: JWT_SECRET (or CRYPTO_CREDENTIALS_KEY) must be at least 32 characters")
    return source.encode()


def _derive(env: Mapping[str, str] | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_source(env))


def encrypt(plaintext: str, env: Mapping[str, str] | None = None) -> str:
    return Fernet(base64.urlsafe_b64encode(_derive(env, _ENC_INFO))).encrypt(plaintext.encode()).decode()


def decrypt(token: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """The plaintext, or None when it cannot be decrypted (other key)."""
    if not token:
        return None
    try:
        return Fernet(base64.urlsafe_b64encode(_derive(env, _ENC_INFO))).decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def fingerprint(api_key: str, env: Mapping[str, str] | None = None) -> str:
    return hmac.new(_derive(env, _FP_INFO), api_key.encode(), hashlib.sha256).hexdigest()


def hint(api_key: str) -> str:
    """What the dashboard may show of a saved key: its last 4 characters."""
    return f"…{api_key[-4:]}" if len(api_key) >= 8 else "…"
