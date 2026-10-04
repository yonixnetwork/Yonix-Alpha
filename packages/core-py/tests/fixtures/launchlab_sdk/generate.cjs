// Reference trades from the OFFICIAL Raydium SDK (raydium-sdk-v2, LaunchLab
// "launchpad" curves), used to check that yonixalpha_core.solana.launchlab
// decodes the accounts and quotes trades exactly as the SDK does (master §7).
//
//   npm install @raydium-io/raydium-sdk-v2@0.2.73-alpha
//   node generate.cjs > fixtures.json
//
// Offline and deterministic: pools on all three curves (constant product,
// fixed price, linear price) are set up with the SDK's own getInitParam and
// advanced with its own buys, encoded with the SDK's account layouts (the
// bytes a real account holds) and traded with Curve.buyExactIn / buyExactOut /
// sellExactIn / sellExactOut under random fee rates.
const crypto = require("crypto");
const BN = require("bn.js");
const { Keypair } = require("@solana/web3.js");
const sdk = require("@raydium-io/raydium-sdk-v2");

let seed = 20261005;
const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const int = (lo, hi) => lo + Math.floor(rnd() * (hi - lo + 1));
const frac = (x, lo, hi) => x.muln(int(lo, hi)).divn(1000); // x * [lo, hi] / 1000
const key = (label) => Keypair.fromSeed(crypto.createHash("sha256").update(label).digest()).publicKey;
const zeros = (n) => Array(n).fill(0);
const disc = (name) => Buffer.from({ PoolState: [247, 237, 227, 245, 215, 195, 222, 70], GlobalConfig: [149, 8, 156, 202, 160, 252, 176, 217],
  PlatformConfig: [160, 78, 128, 0, 248, 83, 230, 160] }[name]);

function encode(layout, obj, name) {
  const buf = Buffer.alloc(layout.span);
  layout.encode(obj, buf); // the leading unnamed u64 (discriminator) is left out by the layout
  disc(name).copy(buf, 0);
  return buf;
}

function trade(fn, args) {
  try {
    const r = sdk.Curve[fn](args);
    return { amount_a: r.amountA.amount.toString(), amount_b: r.amountB.toString(), protocol_fee: r.splitFee.protocolFee.toString(),
      platform_fee: r.splitFee.platformFee.toString(), share_fee: r.splitFee.shareFee.toString(), creator_fee: r.splitFee.creatorFee.toString() };
  } catch (e) {
    return { error: String(e.message) };
  }
}

const cases = [];
for (let i = 0; i < 45; i++) {
  const curveType = i % 3;
  const supply = new BN(int(1, 1000)).mul(new BN("1000000000000")); // 1e6 .. 1e9 tokens (6 decimals)
  const totalFundRaising = new BN(int(5, 500)).mul(new BN(1e9));    // 5 .. 500 SOL
  const migrateFee = curveType === 0 ? new BN(0) : new BN(int(0, 2)).mul(new BN(1e8));
  const totalLockedAmount = new BN(0);
  let totalSell = frac(supply, 500, 800);
  const curve = sdk.Curve.getCurve(curveType);
  const init = curve.getInitParam({ supply, totalFundRaising, totalSell, totalLockedAmount, migrateFee });
  if (curveType !== 0) totalSell = init.c; // the program requires the curve's own total sell for these
  const poolInfo = { virtualA: init.a, virtualB: init.b, realA: new BN(0), realB: new BN(0), totalSellA: totalSell, totalFundRaisingB: totalFundRaising };
  // advance the pool with the SDK's own buys (no fee), from empty to most of the curve
  const steps = int(0, 6);
  for (let s = 0; s < steps; s++) {
    const left = totalFundRaising.sub(poolInfo.realB);
    if (left.lten(1)) break;
    const r = sdk.Curve.buyExactIn({ poolInfo, amountB: frac(left, 10, 400), protocolFeeRate: new BN(0), platformFeeRate: new BN(0),
      curveType, shareFeeRate: new BN(0), creatorFeeRate: new BN(0), transferFeeConfigA: undefined, transferFeeConfigB: undefined, slot: 0 });
    poolInfo.realA = poolInfo.realA.add(r.amountA.amount);
    poolInfo.realB = poolInfo.realB.add(r.amountB);
  }
  const rates = { protocolFeeRate: new BN(int(0, 10000)), platformFeeRate: new BN(int(0, 20000)), creatorFeeRate: new BN(int(0, 10000)),
    shareFeeRate: new BN(i % 4 === 0 ? int(1, 3000) : 0) };
  const pool = {
    epoch: new BN(int(700, 900)), bump: 255, status: 0, mintDecimalsA: 6, mintDecimalsB: 9, migrateType: int(0, 1),
    supply, totalSellA: totalSell, virtualA: poolInfo.virtualA, virtualB: poolInfo.virtualB, realA: poolInfo.realA, realB: poolInfo.realB,
    totalFundRaisingB: totalFundRaising, protocolFee: new BN(0), platformFee: new BN(0), migrateFee,
    vestingSchedule: { totalLockedAmount, cliffPeriod: new BN(0), unlockPeriod: new BN(0), startTime: new BN(0), totalAllocatedShare: new BN(0) },
    configId: key("config" + i), platformId: key("platform" + i), mintA: key("mintA" + i), mintB: key("mintB" + i),
    vaultA: key("vaultA" + i), vaultB: key("vaultB" + i), creator: key("creator" + i), mintProgramFlag: 0, cpmmCreatorFeeOn: 0,
    platformVestingShare: new BN(0),
  };
  const config = {
    epoch: new BN(1), curveType, index: i, migrateFee, tradeFeeRate: rates.protocolFeeRate, maxShareFeeRate: new BN(10000),
    minSupplyA: new BN(10000000), maxLockRate: new BN(300000), minSellRateA: new BN(200000), minMigrateRateA: new BN(200000),
    minFundRaisingB: new BN(1e9), mintB: key("mintB" + i), protocolFeeOwner: key("pfo" + i), migrateFeeOwner: key("mfo" + i),
    migrateToAmmWallet: key("amm" + i), migrateToCpmmWallet: key("cpmm" + i),
  };
  const name = Array.from(Buffer.from(("site " + i).padEnd(64, "\0")));
  const platform = {
    epoch: new BN(1), platformClaimFeeWallet: key("pcw" + i), platformLockNftWallet: key("pln" + i), platformScale: new BN(0),
    creatorScale: new BN(0), burnScale: new BN(1000000), feeRate: rates.platformFeeRate, name, web: zeros(256), img: zeros(256),
    cpConfigId: key("cpc" + i), creatorFeeRate: rates.creatorFeeRate, transferFeeExtensionAuth: key("tfa" + i),
    platformVestingWallet: key("pvw" + i), platformVestingScale: new BN(0), platformCpCreator: key("pcc" + i),
    restrictGlobalConfig: 0, restrictCurveParam: 0, curveRuleManager: key("crm" + i),
  };
  const base = { poolInfo, curveType, ...rates, transferFeeConfigA: undefined, transferFeeConfigB: undefined, slot: 0 };
  const remainingA = totalSell.sub(poolInfo.realA);
  const remainingB = totalFundRaising.sub(poolInfo.realB);
  const trades = [];
  const add = (fn, amount, args) => trades.push({ fn, amount: amount.toString(), ...trade(fn, { ...base, ...args }) });
  for (const amount of [frac(remainingB.addn(1000), 1, 300), frac(remainingB.addn(1000), 500, 3000)]) add("buyExactIn", amount, { amountB: amount });
  for (const amount of [frac(remainingA.addn(1000), 1, 500), frac(remainingA.addn(1000), 900, 1500)]) add("buyExactOut", amount, { amountA: amount });
  if (!poolInfo.realA.isZero()) {
    for (const amount of [frac(poolInfo.realA, 1, 300), frac(poolInfo.realA, 500, 1000)]) add("sellExactIn", amount, { amountA: amount });
    for (const amount of [frac(poolInfo.realB, 1, 300), frac(poolInfo.realB, 900, 1300)]) add("sellExactOut", amount, { amountB: amount });
  }
  cases.push({ curve_type: curveType, pool: encode(sdk.LaunchpadPool, pool, "PoolState").toString("base64"),
    config: encode(sdk.LaunchpadConfig, config, "GlobalConfig").toString("base64"),
    platform: encode(sdk.PlatformConfig, platform, "PlatformConfig").toString("base64"),
    share_fee_rate: rates.shareFeeRate.toString(), trades });
}
process.stdout.write(JSON.stringify({ sdk: "@raydium-io/raydium-sdk-v2@0.2.73-alpha", cases }, null, 0));
