"""Multi-chain layer: chain and launchpad specs behind common interfaces.

Solana keeps its existing, production-verified path (solana/*); the Solana
adapters here describe and wrap it, they do not re-route it. EVM chains
(BSC, Robinhood Chain) are implemented per launchpad in chains/evm/.
"""
