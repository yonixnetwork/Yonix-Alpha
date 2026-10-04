// Reference quotes from the OFFICIAL Meteora Dynamic Bonding Curve SDK, used to
// check that yonixalpha_core.solana.dbc decodes the accounts and quotes swaps
// exactly as the SDK does (master §7).
//
//   npm install @meteora-ag/dynamic-bonding-curve-sdk@1.5.13
//   node generate.cjs > fixtures.json
//
// Offline and deterministic: pools and configs are built here from a seeded
// PRNG (curves of 1-20 segments, every base fee mode, dynamic fee on / off,
// both fee collection modes), encoded with the SDK's own Anchor account coder
// (the bytes a real account holds), and quoted with the SDK's swapQuote; the
// curve walks (from input with an amount left over, from output) are taken
// from the SDK's calculate*FromAmountIn / calculate*FromAmountOut directly.
const crypto = require("crypto");
const BN = require("bn.js");
const { Connection, Keypair } = require("@solana/web3.js");
const sdk = require("@meteora-ag/dynamic-bonding-curve-sdk");

let seed = 20261004;
const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const int = (lo, hi) => lo + Math.floor(rnd() * (hi - lo + 1));
let seed2 = 7;
const rnd2 = () => { seed2 = (seed2 * 1103515245 + 12345) % 2147483648; return seed2 / 2147483648; };
const int2 = (lo, hi) => lo + Math.floor(rnd2() * (hi - lo + 1));
const big = (lo, hi) => new BN(lo).add(new BN(Math.floor(rnd() * 1e15)).mul(new BN(hi).sub(new BN(lo))).div(new BN(1e15)));
const key = (label) => Keypair.fromSeed(crypto.createHash("sha256").update(label).digest()).publicKey;
const Q64 = new BN(1).shln(64);
// the SDK's own program coder (camelCase fields, as the SDK reads real accounts);
// the connection is never used: encoding is local
const coder = sdk.createDbcProgram(new Connection("http://127.0.0.1:1")).program.coder.accounts;
const zeros = (n) => Array(n).fill(0);
// coder.encode allocates a fixed 1000-byte buffer and PoolConfig is larger:
// encode with the same layout the SDK decodes accounts with, into a big
// enough buffer, and check the SDK decodes the bytes back to the same state.
async function encode(name, obj) {
  const { layout } = coder.accountLayouts.get(name);
  const buf = Buffer.alloc(4096);
  const data = Buffer.concat([coder.accountDiscriminator(name), buf.subarray(0, layout.encode(obj, buf))]);
  const back = coder.decode(name, data);
  const flat = (o, p = "", out = {}) => {
    for (const [k, v] of Object.entries(o)) {
      if (BN.isBN(v)) out[p + k] = v.toString();
      else if (v && v.toBase58) out[p + k] = v.toBase58();
      else if (v && typeof v === "object") flat(v, p + k + ".", out);
      else out[p + k] = v;
    }
    return out;
  };
  const a = flat(obj), b = flat(back);
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (a[k] !== b[k]) throw new Error(`${name}.${k}: encoded ${a[k]}, the SDK decodes ${b[k]}`);
  }
  return data;
}

function makeConfig(i) {
  const segments = int(1, 20);
  // sqrt prices: start below the first point, strictly increasing
  let sp = big("1000000000000", "400000000000000000"); // ~ 5e-14 .. 5e-4 price in Q64
  const start = sp;
  const curve = [];
  for (let s = 0; s < 20; s++) {
    if (s < segments) {
      sp = sp.add(sp.muln(int(1, 40)).divn(100)); // at most 1.4^20 x the start: inside MAX_SQRT_PRICE
      curve.push({ sqrtPrice: sp, liquidity: big("100000000000000000000000000", "900000000000000000000000000000000") });
    } else {
      curve.push({ sqrtPrice: new BN(0), liquidity: new BN(0) });
    }
  }
  const mode = i % 3; // 0 linear, 1 exponential scheduler, 2 rate limiter
  const cliff = new BN(int(2500000, 500000000));
  let baseFee;
  if (mode === 2) baseFee = { cliffFeeNumerator: cliff, firstFactor: int(1, 500), secondFactor: new BN(int(0, 3) === 0 ? 0 : int(10, 1000)), thirdFactor: new BN(int(1, 5000)).mul(new BN(1e6)), baseFeeMode: 2, padding0: zeros(5) };
  else baseFee = { cliffFeeNumerator: cliff, firstFactor: int(0, 100), secondFactor: new BN(int(0, 2) === 0 ? 0 : int(1, 600)), thirdFactor: new BN(mode === 0 ? int(0, Math.floor(cliff.toNumber() / 120)) : int(1, 2000)), baseFeeMode: mode, padding0: zeros(5) };
  const dyn = int(0, 1);
  const dynamicFee = { initialized: dyn, padding: zeros(7), maxVolatilityAccumulator: int(10000, 100000), variableFeeControl: int(1, 50000), binStep: int(1, 100), filterPeriod: 10, decayPeriod: 120, reductionFactor: 5000, padding2: zeros(8), binStepU128: new BN(0) };
  const vest = { isInitialized: 0, vestingPercentage: 0, padding: zeros(2), bpsPerPeriod: 0, numberOfPeriods: 0, frequency: 0, cliffDurationFromMigrationTime: 0 };
  const lastSp = curve[segments - 1].sqrtPrice;
  return {
    quoteMint: key("quote" + i), feeClaimer: key("claimer" + i), leftoverReceiver: key("leftover" + i),
    poolFees: { baseFee, dynamicFee }, partnerLiquidityVestingInfo: vest, creatorLiquidityVestingInfo: vest,
    padding0: zeros(14), padding1: 0, collectFeeMode: int(0, 1), migrationOption: 1, activationType: int(0, 1),
    tokenDecimal: 6, version: 1, tokenType: 0, quoteTokenFlag: 0, partnerPermanentLockedLiquidityPercentage: 0,
    partnerLiquidityPercentage: 50, creatorPermanentLockedLiquidityPercentage: 0, creatorLiquidityPercentage: 50,
    migrationFeeOption: 0, fixedTokenSupplyFlag: 0, creatorTradingFeePercentage: 0, tokenUpdateAuthority: 0,
    migrationFeePercentage: 0, creatorMigrationFeePercentage: 0, padding2: zeros(7), swapBaseAmount: new BN("800000000000000"),
    migrationQuoteThreshold: new BN("85000000000"), migrationBaseThreshold: new BN("200000000000000"), migrationSqrtPrice: lastSp,
    lockedVestingConfig: { amountPerPeriod: new BN(0), cliffDurationFromMigrationTime: new BN(0), frequency: new BN(0), numberOfPeriod: new BN(0), cliffUnlockAmount: new BN(0), padding: new BN(0) },
    preMigrationTokenSupply: new BN(0), postMigrationTokenSupply: new BN(0), migratedCollectFeeMode: 0, migratedDynamicFee: 0,
    migratedPoolFeeBps: 0, migratedPoolBaseFeeMode: 0, enableFirstSwapWithMinFee: 0, migratedCompoundingFeeBps: 0,
    poolCreationFee: new BN(0), migratedPoolBaseFeeBytes: zeros(16), sqrtStartPrice: start, curve,
  };
}

function makePool(i, config) {
  const pts = config.curve.filter((p) => !p.sqrtPrice.isZero());
  const lo = config.sqrtStartPrice, hi = pts[pts.length - 1].sqrtPrice;
  const at = i % 4 === 0 ? lo : lo.add(hi.sub(lo).muln(int(0, 95)).divn(100));
  return { poolState: {
    volatilityTracker: { lastUpdateTimestamp: new BN(0), padding: zeros(8), sqrtPriceReference: at, volatilityAccumulator: new BN(int(0, 90000)), volatilityReference: new BN(0) },
    config: key("config" + i), creator: key("creator" + i), baseMint: key("base" + i), baseVault: key("bv" + i), quoteVault: key("qv" + i),
    baseReserve: new BN("800000000000000"), quoteReserve: new BN(int(0, 80000000000)), protocolBaseFee: new BN(0), protocolQuoteFee: new BN(0),
    partnerBaseFee: new BN(0), partnerQuoteFee: new BN(0), sqrtPrice: at, activationPoint: new BN(1000000),
    poolType: 0, isMigrated: 0, isPartnerWithdrawSurplus: 0, isProtocolWithdrawSurplus: 0, migrationProgress: 0, isWithdrawLeftover: 0,
    isCreatorWithdrawSurplus: 0, migrationFeeWithdrawStatus: 0,
    metrics: { totalProtocolBaseFee: new BN(0), totalProtocolQuoteFee: new BN(0), totalTradingBaseFee: new BN(0), totalTradingQuoteFee: new BN(0) },
    finishCurveTimestamp: new BN(0), creatorBaseFee: new BN(0), creatorQuoteFee: new BN(0), legacyCreationFeeBits: 0, creationFeeBits: 0, hasSwap: 1,
    padding0: zeros(5), protocolLiquidityMigrationFeeBps: 0, padding1: zeros(6), protocolMigrationBaseFeeAmount: new BN(0),
    protocolMigrationQuoteFeeAmount: new BN(0), padding2: [new BN(0), new BN(0), new BN(0)],
  } };
}

(async () => {
  const cases = [];
  for (let i = 0; i < 60; i++) {
    const config = makeConfig(i);
    const pool = makePool(i, config);
    const configBytes = await encode("poolConfig", config);
    const poolBytes = await encode("virtualPool", pool);
    const quotes = [];
    for (const [baseForQuote, amount] of [[false, new BN(int(1, 1000)).muln(1000000)], [false, new BN(int(1, 50)).mul(new BN(1000000000))],
                                          [true, new BN(int(1, 1000)).mul(new BN(1e9))], [true, new BN(int(1, 300)).mul(new BN(1e12))]]) {
      const point = new BN(1000000 + int(0, 2000));
      try {
        const r = sdk.swapQuote(pool, config, baseForQuote, amount, 0, false, point, false);
        quotes.push({ base_for_quote: baseForQuote, amount_in: amount.toString(), current_point: point.toString(),
          actual_input_amount: r.actualInputAmount.toString(), output_amount: r.outputAmount.toString(),
          next_sqrt_price: r.nextSqrtPrice.toString(), trading_fee: r.tradingFee.toString(),
          protocol_fee: r.protocolFee.toString(), referral_fee: r.referralFee.toString() });
      } catch (e) {
        quotes.push({ base_for_quote: baseForQuote, amount_in: amount.toString(), current_point: point.toString(), error: String(e.message) });
      }
    }
    // the curve walks alone, both ways (swap2: partial fill leaves an amount
    // unswapped, exact out walks from the output); own PRNG stream, so the
    // quotes above stay as they were
    const walks = [];
    const sp = pool.poolState.sqrtPrice;
    const walk = (fn, baseForQuote, amount, call) => {
      try {
        const r = call();
        walks.push({ fn, base_for_quote: baseForQuote, sqrt_price: sp.toString(), amount: amount.toString(),
          result: r.outputAmount.toString(), next_sqrt_price: r.nextSqrtPrice.toString(), amount_left: r.amountLeft.toString() });
      } catch (e) {
        walks.push({ fn, base_for_quote: baseForQuote, sqrt_price: sp.toString(), amount: amount.toString(), error: String(e.message) });
      }
    };
    for (const amount of [new BN(int2(1, 1000)).muln(1000000), new BN(int2(1, 900)).mul(new BN(1e9)), new BN(int2(1, 5)).mul(new BN(1e15))]) {
      walk("quote_to_base_from_amount_in", false, amount, () => sdk.calculateQuoteToBaseFromAmountIn(config, sp, amount, config.migrationSqrtPrice));
      walk("base_to_quote_from_amount_in", true, amount, () => sdk.calculateBaseToQuoteFromAmountIn(config, sp, amount));
    }
    for (const amount of [new BN(int2(1, 1000)).muln(1000000), new BN(int2(1, 900)).mul(new BN(1e9)), new BN(int2(1, 900)).mul(new BN(1e12))]) {
      walk("quote_to_base_from_amount_out", false, amount, () => sdk.calculateQuoteToBaseFromAmountOut(config, sp, amount));
      walk("base_to_quote_from_amount_out", true, amount, () => sdk.calculateBaseToQuoteFromAmountOut(config, sp, amount));
    }
    cases.push({ config: Buffer.from(configBytes).toString("base64"), pool: Buffer.from(poolBytes).toString("base64"), quotes, walks });
  }
  process.stdout.write(JSON.stringify({ sdk: "@meteora-ag/dynamic-bonding-curve-sdk@1.5.13", cases }, null, 0));
})();
