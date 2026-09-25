"""The live trading wallet.

The private key is read from `WALLET_PRIVATE_KEY` (base58 64-byte secret,
as Phantom/Solflare export it, or a JSON byte array as solana-keygen writes
it), used only to sign in this process, and never logged, returned by an
API, stored in the database or put in an error message. When
`WALLET_PUBLIC_KEY` is also set, the derived public key must match it, so a
mistyped key can't silently trade from an unexpected wallet.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from solders.keypair import Keypair

from yonixalpha_core.solana.codec import b58decode


class WalletError(Exception):
    """Wallet missing or malformed. Messages never contain key material."""


@dataclass(frozen=True)
class LiveWallet:
    pubkey: str
    _keypair: Keypair = field(repr=False)

    def __repr__(self) -> str:  # never show the keypair
        return f"LiveWallet(pubkey={self.pubkey})"

    @property
    def keypair(self) -> Keypair:
        return self._keypair


def _secret_bytes(raw: str) -> bytes:
    raw = raw.strip()
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except ValueError as exc:
            raise WalletError("WALLET_PRIVATE_KEY looks like a JSON array but does not parse") from exc
        if not isinstance(values, list) or not all(isinstance(v, int) and 0 <= v <= 255 for v in values):
            raise WalletError("WALLET_PRIVATE_KEY JSON array must contain byte values")
        return bytes(values)
    try:
        return b58decode(raw)
    except Exception as exc:  # noqa: BLE001 - never echo the input
        raise WalletError("WALLET_PRIVATE_KEY is not valid base58") from exc


def load_wallet(settings: Any) -> LiveWallet | None:
    """None when no private key is configured (paper-only deployments)."""
    secret = getattr(settings, "WALLET_PRIVATE_KEY", None)
    if secret is None:
        return None
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    if not raw.strip():
        return None
    data = _secret_bytes(raw)
    if len(data) != 64:
        raise WalletError(f"WALLET_PRIVATE_KEY must decode to 64 bytes, got {len(data)}")
    try:
        kp = Keypair.from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise WalletError("WALLET_PRIVATE_KEY is not a valid ed25519 keypair") from exc
    pub = str(kp.pubkey())
    expected = (getattr(settings, "WALLET_PUBLIC_KEY", None) or "").strip()
    if expected and expected != pub:
        raise WalletError(f"WALLET_PUBLIC_KEY {expected} does not match the private key's public key {pub}")
    return LiveWallet(pubkey=pub, _keypair=kp)


def wallet_status(settings: Any) -> dict[str, Any]:
    """Safe-to-display wallet configuration status (public data only)."""
    try:
        w = load_wallet(settings)
    except WalletError as exc:
        return {"configured": True, "valid": False, "pubkey": getattr(settings, "WALLET_PUBLIC_KEY", None), "error": str(exc)}
    if w is None:
        return {"configured": False, "valid": False, "pubkey": getattr(settings, "WALLET_PUBLIC_KEY", None),
                "error": "WALLET_PRIVATE_KEY not set"}
    return {"configured": True, "valid": True, "pubkey": w.pubkey, "error": None}
