"""The three trading chains of this phase and every launchpad on them, as
researched (docs/MULTICHAIN_AUDIT_2026.md §1). Addresses come from the
launchpads' own repositories / SDKs; each spec lists its sources. None of the
EVM addresses has been checked against the live chain from the build
environment — the on-server verification (tools/launchpad_verify) records
that evidence, and a launchpad cannot trade until it exists.
"""

from __future__ import annotations

from yonixalpha_core.chains.base import Chain, ChainSpec, LaunchpadSpec, Lifecycle

CHAINS: dict[Chain, ChainSpec] = {
    Chain.SOLANA: ChainSpec(Chain.SOLANA, "Solana", "SOL", "solana", explorer="https://solscan.io",
                            notes="Existing production path (pump stream, gate, PumpPortal / PumpSwap execution)."),
    Chain.BSC: ChainSpec(Chain.BSC, "BNB Smart Chain", "BNB", "evm", evm_chain_id=56, explorer="https://bscscan.com",
                         public_rpc=("https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com"),
                         notes="BSC and BNB Smart Chain are the same network: one adapter."),
    Chain.ROBINHOOD: ChainSpec(Chain.ROBINHOOD, "Robinhood Chain", "ETH", "evm", evm_chain_id=4663,
                               explorer="https://robinhoodchain.blockscout.com",
                               public_rpc=("https://rpc.mainnet.chain.robinhood.com",),
                               notes="Arbitrum Orbit L2, ETH gas; sequencer feed wss://feed.mainnet.chain.robinhood.com."),
}

# Robinhood Chain shared contracts (hoodchain SDK 0.1.1).
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
ROBINHOOD_UNISWAP_V3 = {"factory": "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA",
                        "quoter_v2": "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7",
                        "swap_router02": "0xCaf681a66D020601342297493863E78C959E5cb2"}
BSC_WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
# PancakeSwap V2 (docs.pancakeswap.finance): quotes for Four.meme tokens after
# their liquidity is added to PancakeSwap.
BSC_PANCAKE_V2 = {"router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
                  "factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"}

_FOUR = ("github.com/four-meme-community/four-meme-ai (skills/four-meme-integration, 2026-03-30)",)
_FLAP = ("github.com/CoolBB97/flap_sniper (built on docs.flap.sh, 2026-09-29)",)
_PONS = ("github.com/ponsdotdev/pons-labs (official Solidity source, 2026-09-29)",)
_HOOD = ("npm hoodchain 0.1.1 (github.com/nirholas/robinhood-chain-sdk)", "github.com/nirholas/hood-oracle (2026-09-15)")

LAUNCHPADS: dict[str, LaunchpadSpec] = {s.key: s for s in (
    # --- Solana (existing implementation) --------------------------------------------------------
    LaunchpadSpec(
        "pumpfun", Chain.SOLANA, "Pump.fun", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="constant product on virtual reserves (Mayhem tokens excepted)",
        liquidity_model="SOL in the bonding curve", migration_model="curve complete + Migration event → PumpSwap pool",
        execution_model="PumpPortal trade-local / pump program (route 'pump'); migrated positions switch to PumpSwap",
        safety_model="mint/freeze authority, Token-2022 extensions, holders, creator, curve math, executable quote",
        supported_events=("Create", "Trade", "Complete", "Migration"),
        contracts={"program": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}, quote_asset="SOL",
        sources=("pump-fun/pump-public-docs",), notes="Production path; adapters describe it, they do not re-route it."),
    LaunchpadSpec(
        "pumpswap", Chain.SOLANA, "PumpSwap", Lifecycle.DIRECT_DEX,
        curve_model="constant-product AMM pool", liquidity_model="pool reserves (WSOL / token)",
        migration_model="none (destination of Pump.fun migrations)",
        execution_model="PumpPortal / pump-amm program (route 'pump-amm')",
        safety_model="as Pump.fun + pool liquidity and migrated-liquidity minimum",
        supported_events=("BuyEvent", "SellEvent"), contracts={"program": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"},
        quote_asset="SOL", sources=("pump-fun/pump-public-docs",)),
    # --- BSC --------------------------------------------------------------------------------------
    LaunchpadSpec(
        "fourmeme", Chain.BSC, "Four.meme", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="TokenManager bonding curve (offers / funds up to maxFunds)",
        liquidity_model="BNB (or a BEP-20 quote) in the curve, then a PancakeSwap pair",
        migration_model="LiquidityAdded(base, offers, quote, funds) on TokenManager2; getTokenInfo().liquidityAdded",
        execution_model="V2: TokenManager2 buyTokenAMAP / sellToken (ERC-20 approve first); "
                        "V1 (tokens before 2024-09-05): purchaseTokenAMAP / saleToken",
        safety_model="TokenManagerHelper3.tryBuy / trySell round trip, TaxToken (creatorType 5) fee config, "
                     "X Mode / AntiSniperFeeMode flags, ERC-20 owner / proxy checks",
        supported_events=("TokenCreate", "TokenPurchase", "TokenSale", "LiquidityAdded"),
        contracts={"manager_v1": "0xEC4549caDcE5DA21Df6E6422d448034B5233bFbC",
                   "manager_v2": "0x5c952063c7fc8610FFDB798152D69F0B9550762b",
                   "helper3": "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"},
        quote_asset="BNB (some tokens use a BEP-20 quote)", sources=_FOUR,
        notes="Events are emitted by TokenManager2 (V2) only; V1 tokens are traded, not discovered."),
    LaunchpadSpec(
        "flap", Chain.BSC, "Flap", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="constant product with parameters r, h, k (getTokenV8Safe); DEX at dexSupplyThresh (80% sold)",
        liquidity_model="quote reserve in the Portal curve, then a DEX pool",
        migration_model="status becomes DEX (4) in getTokenV8Safe; pool field set",
        execution_model="Portal swapExactInput (native BNB in; sells with ERC-2612 permit)",
        safety_model="status Tradable, buy/sell tax (FlapTokenTaxSet / FlapTokenAsymmetricTaxSet), extensions, "
                     "quoteExactInput round trip",
        supported_events=("TokenCreated", "TokenQuoteSet", "FlapTokenTaxSet", "FlapTokenAsymmetricTaxSet",
                          "TokenExtensionEnabled", "TokenBought", "TokenSold"),
        contracts={"portal": "0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0"}, quote_asset="BNB (other quotes skipped)",
        sources=_FLAP, notes="Only native-BNB quote tokens on the curve are supported in this phase."),
    # --- Robinhood Chain --------------------------------------------------------------------------
    LaunchpadSpec(
        "pons_v2", Chain.ROBINHOOD, "Pons (V2)", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="constant-product bonding curve per token (getReserves)",
        liquidity_model="pair token (WETH / quote) in the curve, then a locked full-range Uniswap V4 pool with the Pons hook",
        migration_model="PoolGraduated(token, positionId, tokenAmount, pairTokenAmount) on the factory; CurveCompleted on the curve",
        execution_model="curve buy(quoteIn, minTokensOut, recipient) / sell(tokensIn, minQuoteOut, recipient); "
                        "after graduation: Uniswap V4 (NOT IMPLEMENTED — graduated V2 tokens are observe-only)",
        safety_model="curve reserves round trip, creator tax, snipe tax window, ERC-20 checks",
        supported_events=("TokenLaunched", "CurveBuy", "CurveSell", "CurveCompleted", "PoolGraduated", "LaunchSwept"),
        contracts={"factory": "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"}, quote_asset="ETH (pair token per launch)",
        sources=_PONS),
    LaunchpadSpec(
        "pons_v1", Chain.ROBINHOOD, "Pons (V1)", Lifecycle.INSTANT_POOL,
        curve_model="none", liquidity_model="one-sided Uniswap V3 position, locked",
        migration_model="none (no graduation event)", execution_model="Uniswap V3 SwapRouter02 (QuoterV2 quotes)",
        safety_model="same-block block, max wallet, cumulative buy cap (launch restrictions), V3 round trip",
        supported_events=("TokenLaunched",),
        contracts={"factory": "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB", **ROBINHOOD_UNISWAP_V3},
        quote_asset="ETH", sources=_PONS + _HOOD,
        notes="hood-oracle lists another Pons launch factory proxy 0xf4fc0cd27fc8ecf17e55ee4c3f7201897df3eb75 "
              "(NOXA-codebase TokenLaunched); which one is current must be settled by on-chain verification."),
    LaunchpadSpec(
        "odyssey_curve", Chain.ROBINHOOD, "The Odyssey (curve)", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="native-ETH constant product on virtual reserves (quoteBuy / quoteSell)",
        liquidity_model="ETH in the curve, then a Uniswap V3 pool, LP locked one year",
        migration_model="PoolCompleted then PoolMigrated(token, pool, tokenId, liquidity, tokenUsed, quoteUsed)",
        execution_model="factory buy(token, tokensOut) payable / sell(token, tokensIn, minQuoteOut); "
                        "after migration Uniswap V3 SwapRouter02",
        safety_model="quoteBuy / quoteSell round trip, feeBps, antiSnipeBlocks, maxWalletBps",
        supported_events=("TokenCreated", "Traded", "PoolCompleted", "PoolMigrated"),
        contracts={"bonding_curve_factory": "0xEb3FeeD2716cF0eEAda05B22e67424794e1f5a80",
                   "legacy_factory": "0xAf9f3ce1d34909F59E88c23027f89d5807B0F915", **ROBINHOOD_UNISWAP_V3},
        quote_asset="ETH", sources=_HOOD),
    LaunchpadSpec(
        "odyssey_instant", Chain.ROBINHOOD, "The Odyssey (instant)", Lifecycle.INSTANT_POOL,
        curve_model="none", liquidity_model="Uniswap V3 pool created at launch",
        migration_model="none", execution_model="Uniswap V3 SwapRouter02 (QuoterV2 quotes)",
        safety_model="V3 round trip, ERC-20 checks",
        supported_events=("InstantTokenCreated", "InstantFirstBuy"),
        contracts={"instant_factory": "0xD7601cEe401306fdea5833c6898181D9c770F800", **ROBINHOOD_UNISWAP_V3},
        quote_asset="ETH", sources=_HOOD),
    LaunchpadSpec(
        "odyssey_reflection", Chain.ROBINHOOD, "The Odyssey (reflection)", Lifecycle.BONDING_CURVE_TO_DEX,
        curve_model="curve paying reflections in a reward token", liquidity_model="curve, then a Uniswap V4 pool",
        migration_model="PoolMigratedV4(token, poolId, ...)", execution_model="not supported (observe only)",
        safety_model="observe only", supported_events=("TokenCreated", "PoolMigratedV4"),
        contracts={"reflection_factory": "0x6Ce85c4b7cE12903E5867652C265bCcce57f935F"},
        supports_trading=False, supports_copy_trading=False, quote_asset="ETH", sources=_HOOD,
        notes="Reflection mechanics change balances; the reference executor refuses curve buys here."),
    LaunchpadSpec(
        "noxa", Chain.ROBINHOOD, "NOXA", Lifecycle.INSTANT_POOL,
        curve_model="none", liquidity_model="single-sided Uniswap V3 1% pool, locked",
        migration_model="none", execution_model="Uniswap V3 SwapRouter02",
        safety_model="launch restrictions until restrictionsEndBlock, V3 round trip",
        supported_events=("TokenLaunched",),
        contracts={"launch_factory": "0xD9eC2db5f3D1b236843925949fe5bd8a3836FCcB",
                   "locker": "0x7F03effbd7ceB22A3f80Dd468f67eF27826acD85", **ROBINHOOD_UNISWAP_V3},
        active=False, inactive_reason="no launches since block ~5.25M (hood-oracle scan 2026-09-03); new launches paused "
                                      "(CoinDesk 2026-07-15)",
        quote_asset="ETH", sources=_HOOD + ("coindesk.com 2026-07-15",)),
)}


def launchpads_for(chain: Chain) -> list[LaunchpadSpec]:
    return [s for s in LAUNCHPADS.values() if s.chain == chain]
