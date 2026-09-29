"""EVM chains (BSC, Robinhood Chain): JSON-RPC client, ABI helpers and the
launchpad adapters. Nothing here signs or sends a transaction."""

from __future__ import annotations

from yonixalpha_core.chains.evm.rpc import EvmRpc


def adapter_for(key: str, rpc: EvmRpc):
    """The LaunchpadAdapter for an EVM launchpad key from the registry."""
    from yonixalpha_core.chains.evm import flap, fourmeme, odyssey, pons

    factories = {
        "fourmeme": fourmeme.FourMeme, "flap": flap.Flap, "pons_v2": pons.PonsV2, "pons_v1": pons.pons_v1,
        "noxa": pons.noxa, "odyssey_curve": odyssey.OdysseyCurve, "odyssey_instant": odyssey.OdysseyInstant,
        "odyssey_reflection": odyssey.OdysseyReflection,
    }
    if key not in factories:
        raise KeyError(f"no EVM adapter for launchpad {key!r}")
    return factories[key](rpc)


EVM_LAUNCHPADS = ("fourmeme", "flap", "pons_v2", "pons_v1", "noxa", "odyssey_curve", "odyssey_instant",
                  "odyssey_reflection")
