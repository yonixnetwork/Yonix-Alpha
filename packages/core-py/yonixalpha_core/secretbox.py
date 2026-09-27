"""Encryption at rest for credentials entered in the dashboard (provider
URLs usually embed an API key).

Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package. The key
is CONFIG_ENCRYPTION_KEY when set, otherwise derived with HKDF from
JWT_SECRET (already a required, server-only secret of at least 32
characters), so no extra setup is needed. Rotating the source secret makes
stored values undecryptable: they are then reported as needing re-entry,
never silently replaced.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"yonixalpha-config-secrets-v1"


def _secret_value(v) -> str:
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v or "")


def _fernet(settings) -> Fernet:
    source = _secret_value(getattr(settings, "CONFIG_ENCRYPTION_KEY", None)) or _secret_value(settings.JWT_SECRET)
    if len(source) < 32:
        raise ValueError("no encryption key: JWT_SECRET (or CONFIG_ENCRYPTION_KEY) must be at least 32 characters")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(source.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(settings, token: str | None) -> str | None:
    """The plaintext, or None when it can't be decrypted (wrong key)."""
    if not token:
        return None
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
