// Reference instructions from the OFFICIAL Pump SDKs, used to check that
// yonixalpha_core.solana.pump_tx builds byte-identical instructions.
//
//   npm install @pump-fun/pump-sdk@2.0.0 @pump-fun/pump-swap-sdk@1.20.0 @solana/web3.js @solana/spl-token bn.js
//   node generate.cjs > fixtures.json
//
// Offline: nothing here talks to the network. Keys are derived from fixed
// seeds; fee recipients are passed explicitly (the SDKs pick them at
// random otherwise).
const crypto = require("crypto");
const { Keypair, PublicKey } = require("@solana/web3.js");
const { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, NATIVE_MINT, getAssociatedTokenAddressSync } = require("@solana/spl-token");
const BN = require("bn.js");
const { PUMP_SDK } = require("@pump-fun/pump-sdk");
const { PumpAmmSdk, canonicalPumpPoolPda } = require("@pump-fun/pump-swap-sdk");

const key = (label) => Keypair.fromSeed(crypto.createHash("sha256").update(label).digest()).publicKey;
const ix = (i) => ({
  program: i.programId.toBase58(),
  keys: i.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable]),
  data: Buffer.from(i.data).toString("hex"),
});
const USER = key("user"), MINT = key("mint"), CREATOR = key("creator");
const FEE = new PublicKey("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV");
const BUYBACK = new PublicKey("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD");

async function curve() {
  const out = {};
  for (const [name, tp] of [["token", TOKEN_PROGRAM_ID], ["token2022", TOKEN_2022_PROGRAM_ID]]) {
    out[`curve_buy_${name}`] = ix(await PUMP_SDK.getBuyInstructionRaw({
      user: USER, mint: MINT, creator: CREATOR, amount: new BN("123456789000"), solAmount: new BN("250000000"),
      feeRecipient: FEE, tokenProgram: tp, buybackFeeRecipient: BUYBACK }));
    for (const cashback of [false, true]) {
      out[`curve_sell_${name}${cashback ? "_cashback" : ""}`] = ix(await PUMP_SDK.getSellInstructionRaw({
        user: USER, mint: MINT, creator: CREATOR, amount: new BN("123456789000"), solAmount: new BN("200000000"),
        feeRecipient: FEE, tokenProgram: tp, buybackFeeRecipient: BUYBACK, cashback }));
    }
  }
  return out;
}

async function amm() {
  const sdk = new PumpAmmSdk();
  const out = {};
  const protocolFee = key("protocol-fee"), buyback = key("amm-buyback"), coinCreator = key("coin-creator");
  const globalConfig = {
    admin: key("admin"), lpFeeBasisPoints: new BN(20), protocolFeeBasisPoints: new BN(5), disableFlags: 0,
    protocolFeeRecipients: Array(8).fill(protocolFee), coinCreatorFeeBasisPoints: new BN(5),
    adminSetCoinCreatorAuthority: key("a2"), whitelistPda: key("wl"), reservedFeeRecipient: key("rf"),
    mayhemModeEnabled: false, reservedFeeRecipients: Array(7).fill(key("rf")), isCashbackEnabled: false,
    buybackFeeRecipients: Array(8).fill(buyback), buybackBasisPoints: new BN(0),
  };
  const poolKey = canonicalPumpPoolPda(MINT);
  for (const [name, tp, cashback, size] of [["token", TOKEN_PROGRAM_ID, false, 300], ["token2022", TOKEN_2022_PROGRAM_ID, false, 300],
                                            ["token_cashback", TOKEN_PROGRAM_ID, true, 300], ["token_short_pool", TOKEN_PROGRAM_ID, false, 243]]) {
    const pool = { poolBump: 255, index: 0, creator: key("pool-creator"), baseMint: MINT, quoteMint: NATIVE_MINT,
      lpMint: key("lp"), poolBaseTokenAccount: key("pool-base"), poolQuoteTokenAccount: key("pool-quote"),
      lpSupply: new BN(1000), coinCreator, isMayhemMode: false, isCashbackCoin: cashback, virtualQuoteReserves: new BN(0) };
    const state = {
      globalConfig, feeConfig: null, poolKey, poolAccountInfo: { data: Buffer.alloc(size), owner: key("x"), lamports: 1, executable: false },
      pool, poolBaseAmount: new BN("1000000000000"), poolQuoteAmount: new BN("80000000000"),
      baseTokenProgram: tp, quoteTokenProgram: TOKEN_PROGRAM_ID, baseMint: MINT, baseMintAccount: null, user: USER,
      userBaseTokenAccount: getAssociatedTokenAddressSync(MINT, USER, true, tp),
      userQuoteTokenAccount: getAssociatedTokenAddressSync(NATIVE_MINT, USER, true, TOKEN_PROGRAM_ID),
      userBaseAccountInfo: null, userQuoteAccountInfo: null,
    };
    out[`amm_buy_${name}`] = (await sdk.buyInstructions(state, new BN("5000000000"), new BN("260000000"))).map(ix);
    out[`amm_sell_${name}`] = (await sdk.sellInstructions(state, new BN("5000000000"), new BN("190000000"))).map(ix);
  }
  return out;
}

function accounts() {
  // Global / GlobalConfig encoded by Anchor from the SDK's own IDLs.
  const { BorshAccountsCoder } = require("@coral-xyz/anchor");
  const idl = (pkg, f) => JSON.parse(require("fs").readFileSync(require("path").join("node_modules", pkg, "src", "idl", f), "utf8"));
  const pumpIdl = idl("@pump-fun/pump-sdk", "pump.json");
  const ammIdl = idl("@pump-fun/pump-swap-sdk", "pump_amm.json");
  const k = (n) => key(n);
  const global = {
    initialized: true, authority: k("g-auth"), fee_recipient: k("g-fee"), initial_virtual_token_reserves: new BN(1),
    initial_virtual_sol_reserves: new BN(2), initial_real_token_reserves: new BN(3), token_total_supply: new BN(4),
    fee_basis_points: new BN(95), withdraw_authority: k("g-w"), enable_migrate: true, pool_migration_fee: new BN(5),
    creator_fee_basis_points: new BN(30), fee_recipients: [1, 2, 3, 4, 5, 6, 7].map((i) => k("g-fee" + i)),
    set_creator_authority: k("g-sc"), admin_set_creator_authority: k("g-asc"), create_v2_enabled: true,
    whitelist_pda: k("g-wl"), reserved_fee_recipient: k("g-res"), mayhem_mode_enabled: true,
    reserved_fee_recipients: [1, 2, 3, 4, 5, 6, 7].map((i) => k("g-res" + i)), is_cashback_enabled: false,
    buyback_fee_recipients: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => k("g-bb" + i)), buyback_basis_points: new BN(0),
    initial_virtual_quote_reserves: new BN(6), whitelisted_quote_mints: [k("g-q")], creator_fee_configurable: false,
    max_configurable_creator_fee_bps: new BN(0), holder_reward_claim_authority: k("g-h"), is_holder_reward_enabled: false,
  };
  const config = {
    admin: k("c-admin"), lp_fee_basis_points: new BN(20), protocol_fee_basis_points: new BN(5), disable_flags: 0,
    protocol_fee_recipients: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => k("c-fee" + i)), coin_creator_fee_basis_points: new BN(5),
    admin_set_coin_creator_authority: k("c-a2"), whitelist_pda: k("c-wl"), reserved_fee_recipient: k("c-res"),
    mayhem_mode_enabled: false, reserved_fee_recipients: [1, 2, 3, 4, 5, 6, 7].map((i) => k("c-res" + i)),
    is_cashback_enabled: false, buyback_fee_recipients: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => k("c-bb" + i)),
    buyback_basis_points: new BN(0), boost_authority: k("c-boost"), boost_enabled: false, creator_fee_configurable: false,
    max_configurable_creator_fee_bps: new BN(0),
  };
  // BorshAccountsCoder.encode allocates 1000 bytes; Global is longer.
  const enc = (idl, name, v) => {
    const layout = new BorshAccountsCoder(idl).accountLayouts.get(name);
    const buf = Buffer.alloc(8192);
    const len = layout.layout.encode(v, buf);
    const disc = Buffer.from(idl.accounts.find((a) => a.name === name).discriminator);
    return Buffer.concat([disc, buf.subarray(0, len)]);
  };
  return Promise.resolve([enc(pumpIdl, "Global", global), enc(ammIdl, "GlobalConfig", config)]).then(([g, c]) => ({
    global_account: { hex: g.toString("hex"), fee_recipient: global.fee_recipient.toBase58(),
      fee_recipients: global.fee_recipients.map((x) => x.toBase58()), reserved_fee_recipient: global.reserved_fee_recipient.toBase58(),
      reserved_fee_recipients: global.reserved_fee_recipients.map((x) => x.toBase58()),
      buyback_fee_recipients: global.buyback_fee_recipients.map((x) => x.toBase58()) },
    global_config_account: { hex: c.toString("hex"), protocol_fee_recipients: config.protocol_fee_recipients.map((x) => x.toBase58()),
      reserved_fee_recipient: config.reserved_fee_recipient.toBase58(),
      reserved_fee_recipients: config.reserved_fee_recipients.map((x) => x.toBase58()),
      buyback_fee_recipients: config.buyback_fee_recipients.map((x) => x.toBase58()) },
  }));
}

(async () => {
  const fixtures = {
    ...(await accounts()),
    generated_with: { "@pump-fun/pump-sdk": "2.0.0", "@pump-fun/pump-swap-sdk": "1.20.0" },
    inputs: { user: USER.toBase58(), mint: MINT.toBase58(), creator: CREATOR.toBase58(), fee_recipient: FEE.toBase58(),
              buyback_fee_recipient: BUYBACK.toBase58(), amm_protocol_fee_recipient: key("protocol-fee").toBase58(),
              amm_buyback_fee_recipient: key("amm-buyback").toBase58(), amm_coin_creator: key("coin-creator").toBase58(),
              pool_base: key("pool-base").toBase58(), pool_quote: key("pool-quote").toBase58() },
    ...(await curve()), ...(await amm()),
  };
  process.stdout.write(JSON.stringify(fixtures, null, 1));
})().catch((e) => { console.error(e); process.exit(1); });
