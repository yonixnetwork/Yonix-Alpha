# Strategy library, Strategy Center and backtesting (2026-10-16)

This is the report for the "multi-strategy trading platform expansion". It
covers what was built, the research behind the 20 modules, how they are
validated, and what is NOT yet known.

## 1. Honest status in one paragraph

All 20 modules, the Strategy Center, the backtest pipeline, portfolio risk,
paper and exchange-demo modes exist in code and pass automated tests.
**No backtest has been run on real market data yet**, so no strategy has a
YonixAlpha result: every module shows "backtest pending". This session's
network cannot reach any exchange or market-data host (the session's own
proxy refuses them). The server can reach Binance.US public candles
(verified 2026-10-16), so the first real batch runs there, queued and
throttled (section 7). Every number shown in tests or screenshots during
development came from SYNTHETIC data and is not a result. Live trading stays
locked in code (`LIVE_ORDERS_UNLOCKED = False`).

## 2. What was built (all in `packages/crypto-py`, the API and the web app; Memecoin untouched)

| Part | Where | What it does |
|---|---|---|
| Indicators | `strategies/indicators.py` | Causal SMA, EMA, RSI / ATR / ADX (Wilder), MACD, ROC, Bollinger, Donchian over the PREVIOUS bars, confirmed swing highs, OBV, rolling VWAP, higher-timeframe resampling from completed bars. |
| Strategy library | `strategies/library.py` | 20 modules behind one interface (`Module.decide` / `should_exit`), each with exact rules, parameter bounds, default exits, a research record and a documented short rule (not executed: spot is long-only). The live runner uses the same code (`LibraryAdapter`). |
| Backtest engine | `backtest/engine.py` | Next-open fills; stop assumed first when a bar touches stop and target; gaps fill at the open; fills capped by bar volume (partial fills); taker / maker fees, spread, slippage, funding; trade management reused from the live executor. |
| Metrics | `backtest/metrics.py` | One definition for every metric; small samples labeled "insufficient evidence"; ratios need 30 days. |
| Validation | `backtest/validation.py` | Train / validation / out-of-sample (60 / 20 / 20) in time order; defaults never fitted; a 3 x 3 grid chosen on train only; 4-fold anchored walk-forward; sensitivity; buy-and-hold benchmark; verdict tiers. |
| Data and queue | `backtest/data.py`, `jobs.py`, `worker.py` | Candle store with provenance (bounded); batch / run queue with claim (skip-locked), cancel, stale recovery, code fingerprint; a low-priority worker that refuses while backtests are switched off. |
| Risk | `risk.py`, `portfolio.py` | Per strategy: fixed or % of equity, position / exposure / daily loss / open positions, spread, slippage, cooldown after N losses, minimum candle value. Portfolio: total open risk, positions, total and per-coin exposure, daily loss, drawdown shutdown (sets the emergency stop). |
| Modes | `guard.py`, `config_store.py`, `runner.py`, `private/service.py` | DISABLED, DEMO (paper: simulated fills on live data), EXCHANGE_DEMO (official demo / testnet account through the existing executor); LIVE is not selectable. Paper positions get trailing stops, partial exits and breakeven. |
| API | `apps/api/app/api/routes/crypto_center.py` | Strategy Center, backtests, comparison, portfolio, alerts and audit endpoints. |
| Dashboard | `apps/web/app/dashboard/crypto/{center,backtests,compare,portfolio,alerts}` | Strategy Center (search, filters, sort; cards on phones), strategy pages, backtests, comparison, portfolio risk, alerts and audit. |
| Migration | `c0004` | Additive (section 9). |

## 3. Research: sources and how they are used

Published research is cited for each module, but **it is not evidence for
this implementation**: the papers test other markets, periods or rule
variants. Only YonixAlpha's own out-of-sample results (after the batch
runs) count as evidence, and even those are not proof of future returns.

Academic sources (citations checked 2026-10-16):
- Brock, Lakonishok & LeBaron (1992), *Simple technical trading rules and the stochastic properties of stock returns*, Journal of Finance 47(5) 1731-1764: moving-average and trading-range-break rules on the DJIA. [RePEc](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html)
- Sullivan, Timmermann & White (1999), *Data-snooping, technical trading rule performance, and the bootstrap*, Journal of Finance 54(5) 1647-1691: corrects for data snooping; much apparent rule performance does not survive. [FMG discussion paper](https://ideas.repec.org/p/fmg/fmgdps/dp303.html)
- Hudson & Urquhart (2021), *Technical trading and cryptocurrencies*, Annals of Operations Research 297 191-220: ~15,000 rules on crypto; predictability in-sample, but no out-of-sample predictability for Bitcoin. [Hull repository](https://hull-repository.worktribe.com/output/2328042)
- Moskowitz, Ooi & Pedersen (2012), *Time series momentum*, Journal of Financial Economics 104(2) 228-250; a later critique attributes much of the effect to volatility scaling. [SSRN](https://papers.ssrn.com/abstract=2089463)
- Liu & Tsyvinski (2021), *Risks and returns of cryptocurrency*, Review of Financial Studies 34(6) 2689-2727: time-series momentum in crypto. [SSRN](https://papers.ssrn.com/abstract=3226806)
- Bailey, Borwein, Lopez de Prado & Zhu, *The probability of backtest overfitting*, Journal of Computational Finance: why the validation here never tunes on the reporting period. [SSRN](https://papers.ssrn.com/abstract=2326253)
- Indicator definitions: Wilder (1978) RSI / ATR / ADX; Appel MACD; Bollinger (2001); Granville (1963) OBV; Donchian / Turtle rules as published by Faith (2007); Connors & Alvarez (2008) RSI(2).

Repositories (licences re-checked 2026-10-16 from each repository's LICENSE file; no code copied from any of them):

| Repository | Licence | Use here |
|---|---|---|
| freqtrade/freqtrade | GPL-3.0 | ideas only (architecture, testing practice) |
| freqtrade/freqtrade-strategies | GPL-3.0 | ideas only (catalogue of indicator rules) |
| EA31337/EA31337-Libre | GPL-3.0 | ideas only (MQL) |
| EA31337/EA31337-strategies | GPL-3.0 | ideas only (MQL indicator strategies) |
| ArturSepp/TrendFollowingSystems | GPL-3.0 | ideas only (trend-following research) |
| grinay/geektrade-strategies | MIT | read; its README's win rate / profit factor are NOT verified and not used |
| arwensorcenelli/trend-bot @ab75bb1 | none | rule idea (SMA 10/50, 3 x ATR stop), independent implementation |
| hoziertom44-arch/Meta_Muse_Crossover_strategy @a163ff7 | none | rules re-implemented (already in the engine) |
| hoziertom44-arch/hyperliquid_grid_trading_bot @710cebb | none | read; not selected (section 11) |
| siriusforex-ai/confluence_matrix_forex @f3beff9 | none | pivot-breakout idea for `sr_breakout`; its backtest has no costs |

GPL code is not copied because YonixAlpha has no licence file and is public:
copying GPL code would impose GPL terms on the project. Unlicensed code is
not copied because no permission exists.

## 4. The 20 modules (generated from the code)

| # | id | Name | Category | Default timeframe |
|---|---|---|---|---|
| 1 | `meta_muse` | Meta Muse Crossover (BTC trend vs ETH trend) | trend divergence | 5m |
| 2 | `sma_cross` | SMA crossover 20/50 | trend following | 4h |
| 3 | `ema_cross` | EMA crossover 12/26 with 200 filter | trend following | 1h |
| 4 | `adx_trend` | ADX / DMI trend strength | trend following | 4h |
| 5 | `mtf_trend` | Multi-timeframe trend confirmation | trend following | 1h |
| 6 | `rsi_momentum` | RSI momentum continuation | momentum | 1h |
| 7 | `macd_momentum` | MACD momentum | momentum | 4h |
| 8 | `roc_momentum` | Rate-of-change (time-series) momentum | momentum | 4h |
| 9 | `donchian_breakout` | Donchian channel breakout (20/10) | breakout | 4h |
| 10 | `atr_breakout` | ATR volatility breakout (Keltner-style) | breakout | 1h |
| 11 | `sr_breakout` | Resistance breakout with volume expansion | breakout | 1h |
| 12 | `breakout_retest` | Breakout retest | breakout | 1h |
| 13 | `ema_pullback` | EMA pullback in an uptrend | trend pullback | 1h |
| 14 | `trend_pullback` | Trend pullback SMA 10/50 | trend pullback | 1h |
| 15 | `vwap_pullback` | Rolling VWAP pullback | trend pullback | 1h |
| 16 | `bollinger_reversion` | Bollinger Band reversion | mean reversion | 1h |
| 17 | `rsi_reversion` | RSI(2) reversion above the 200 average | mean reversion | 4h |
| 18 | `zscore_reversion` | Rolling z-score reversion (range filter) | mean reversion | 1h |
| 19 | `obv_trend` | OBV-confirmed trend | volume | 4h |
| 20 | `regime_switch` | Volatility / trend regime switch | hybrid | 1h |

#### 1. Meta Muse Crossover (BTC trend vs ETH trend) (`meta_muse`)

- **How it works:** Trades the second symbol when two strong EMA trends diverge (pair rule of the original bot).
- **Entry (LONG, closed bars only):** Long ETH when BTC's EMA 9/21 trend is strongly DOWN and ETH's is strongly UP (separation >= 0.15%).
- **Exit:** Stop 1%, target 2%; exit early when either trend is no longer strong or the signal is no longer LONG.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `fast` 9 (2..100); `slow` 21 (3..400); `trend_threshold` 0.0015 (0..0.05); `stop_pct` 0.01 (0.001..0.2); `target_pct` 0.02 (0.001..0.5); `candles` 150 (30..1000)
- **Designed for:** Binance USD-M futures, BTC/ETH, 5m
- **Short rule:** short ETH when BTC strong UP and ETH strong DOWN (not executed: spot is long-only)
- **May work when:** the two assets decouple for a while. **Fails when:** correlation snaps back; whipsaws on 5m.
- **Published evidence (not evidence for this implementation):** none published; the author's README makes no verified performance claim
- **Sources:** [hoziertom44-arch/Meta_Muse_Crossover_strategy @a163ff7 (no licence): the original bot, rules read and re-implemented](https://github.com/hoziertom44-arch/Meta_Muse_Crossover_strategy)
- **Code / licence:** hoziertom44-arch/Meta_Muse_Crossover_strategy @a163ff7, no licence: rules re-implemented
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 2. SMA crossover 20/50 (`sma_cross`)

- **How it works:** Classic moving-average crossover: follows a trend once the faster average crosses the slower one.
- **Entry (LONG, closed bars only):** Long when SMA 20 crosses above SMA 50 on a closed bar.
- **Exit:** Exit when SMA 20 crosses below SMA 50, or at the 2.5 x ATR stop; ATR trailing stop after +1 R.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `fast` 20 (2..200); `slow` 50 (5..400); `stop_atr` 2.5 (0.5..10); `atr_period` 14 (5..50); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** equity index (daily); applied here to crypto 4h
- **Short rule:** short when SMA 20 crosses below SMA 50 (not executed: spot is long-only)
- **May work when:** long, persistent trends. **Fails when:** sideways markets (many small losses from whipsaws).
- **Published evidence (not evidence for this implementation):** BLL found MA rules informative on the DJIA 1897-1986; STW showed that much of it does not survive data-snooping corrections out of sample; Hudson & Urquhart report predictability in crypto with no out-of-sample predictability for Bitcoin
- **Sources:** [Brock, Lakonishok & LeBaron (1992), Simple technical trading rules and the stochastic properties of stock returns, Journal of Finance 47(5) 1731-1764](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html); [Sullivan, Timmermann & White (1999), Data-snooping, technical trading rule performance, and the bootstrap, Journal of Finance 54(5) 1647-1691](https://ideas.repec.org/p/fmg/fmgdps/dp303.html); [Hudson & Urquhart (2021), Technical trading and cryptocurrencies, Annals of Operations Research 297 191-220 (no out-of-sample predictability for Bitcoin; some for other coins)](https://hull-repository.worktribe.com/output/2328042)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 3. EMA crossover 12/26 with 200 filter (`ema_cross`)

- **How it works:** Faster EMA crossover, taken only above the 200-bar EMA (long-term trend filter) to avoid counter-trend entries.
- **Entry (LONG, closed bars only):** Long when EMA 12 crosses above EMA 26 and the close is above EMA 200.
- **Exit:** Exit when EMA 12 crosses below EMA 26, or at the 2 x ATR stop; ATR trailing after +1 R.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `fast` 12 (2..100); `slow` 26 (5..300); `trend` 200 (50..400); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general; EMA variant of the BLL moving-average family
- **Short rule:** short when EMA 12 < EMA 26 below EMA 200 (not executed: spot is long-only)
- **May work when:** trending markets above the long average. **Fails when:** range-bound markets; late entries after sharp moves.
- **Published evidence (not evidence for this implementation):** same family evidence and caveats as the SMA crossover
- **Sources:** [Brock, Lakonishok & LeBaron (1992), Simple technical trading rules and the stochastic properties of stock returns, Journal of Finance 47(5) 1731-1764](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html); [Sullivan, Timmermann & White (1999), Data-snooping, technical trading rule performance, and the bootstrap, Journal of Finance 54(5) 1647-1691](https://ideas.repec.org/p/fmg/fmgdps/dp303.html); [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 4. ADX / DMI trend strength (`adx_trend`)

- **How it works:** Wilder's directional movement: enter when buyers take control (+DI over -DI) while trend strength (ADX) is high.
- **Entry (LONG, closed bars only):** Long when +DI crosses above -DI and ADX(14) is at least 25.
- **Exit:** Exit when -DI rises above +DI or ADX falls below 20; 2 x ATR stop.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `adx_period` 14 (5..50); `adx_min` 25 (10..50); `adx_exit` 20 (5..40); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** commodities (daily), Wilder 1978
- **Short rule:** short when -DI crosses above +DI with ADX >= 25 (not executed: spot is long-only)
- **May work when:** strong directional moves. **Fails when:** choppy markets: DI crossovers flip often.
- **Published evidence (not evidence for this implementation):** definition source; no robust independent crypto evidence found
- **Sources:** Wilder (1978), New Concepts in Technical Trading Systems (RSI, ATR, ADX/DMI definitions); [EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only](https://github.com/EA31337/EA31337-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 5. Multi-timeframe trend confirmation (`mtf_trend`)

- **How it works:** Trades the lower timeframe only in the direction of a rising higher-timeframe trend (built from the same bars).
- **Entry (LONG, closed bars only):** Long when the last COMPLETED higher-timeframe bar (4 x the interval) closes above its rising EMA 50 and the lower-timeframe close crosses above EMA 20.
- **Exit:** Exit on two closes below EMA 20, a higher-timeframe close below its EMA, or the 2 x ATR stop.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `htf_factor` 4 (2..12); `htf_ema` 50 (10..200); `ltf_ema` 20 (5..100); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general practice
- **Short rule:** short when the higher trend falls and the close crosses below EMA 20 (not executed: spot is long-only)
- **May work when:** aligned trends on both timeframes. **Fails when:** turning points (the higher timeframe lags).
- **Published evidence (not evidence for this implementation):** time-series momentum evidence (MOP) is at monthly horizons; intraday transfer is unverified
- **Sources:** [Moskowitz, Ooi & Pedersen (2012), Time series momentum, Journal of Financial Economics 104(2) 228-250](https://papers.ssrn.com/abstract=2089463); [ArturSepp/TrendFollowingSystems (GPL-3.0): trend-following research code, ideas only](https://github.com/ArturSepp/TrendFollowingSystems)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 6. RSI momentum continuation (`rsi_momentum`)

- **How it works:** Uses RSI as a momentum gauge (not as overbought/oversold): buy when momentum turns positive inside an uptrend.
- **Entry (LONG, closed bars only):** Long when RSI(14) crosses above 55 and the close is above EMA 50.
- **Exit:** Exit when RSI falls below 45, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `rsi_period` 14 (2..50); `rsi_entry` 55 (50..80); `rsi_exit` 45 (20..50); `trend` 50 (10..300); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general
- **Short rule:** short when RSI crosses below 45 below EMA 50 (not executed: spot is long-only)
- **May work when:** persistent moves after consolidation. **Fails when:** fast reversals.
- **Published evidence (not evidence for this implementation):** no robust independent evidence for this exact rule
- **Sources:** Wilder (1978), New Concepts in Technical Trading Systems (RSI, ATR, ADX/DMI definitions); [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 7. MACD momentum (`macd_momentum`)

- **How it works:** Appel's MACD: enter when short-term momentum accelerates above the signal line in positive territory.
- **Entry (LONG, closed bars only):** Long when the MACD line (12, 26) crosses above its 9-period signal line while MACD is above zero.
- **Exit:** Exit when MACD crosses below its signal line, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `fast` 12 (2..50); `slow` 26 (5..100); `signal` 9 (2..50); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general
- **Short rule:** short when MACD crosses below the signal below zero (not executed: spot is long-only)
- **May work when:** accelerating trends. **Fails when:** flat markets around zero.
- **Published evidence (not evidence for this implementation):** no robust independent evidence for this rule
- **Sources:** G. Appel, MACD (12, 26, 9); [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies); [EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only](https://github.com/EA31337/EA31337-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 8. Rate-of-change (time-series) momentum (`roc_momentum`)

- **How it works:** Time-series momentum: buy when the asset's own return over the lookback turns strongly positive.
- **Entry (LONG, closed bars only):** Long when ROC(24) rises through +2% (was below on the previous bar), is higher than 3 bars ago, and the close is above SMA 100.
- **Exit:** Exit when ROC(24) turns negative, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `roc_period` 24 (3..200); `roc_min` 2 (0..50); `trend` 100 (20..400); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** futures, monthly (MOP); crypto, weekly (Liu & Tsyvinski)
- **Short rule:** short when ROC falls through -2% (not executed: spot is long-only)
- **May work when:** persistent trends. **Fails when:** sharp reversals after extended moves.
- **Published evidence (not evidence for this implementation):** time-series momentum is documented in futures and in crypto at weekly-monthly horizons; a critique attributes much of the futures result to volatility scaling; intraday horizons are not covered
- **Sources:** [Moskowitz, Ooi & Pedersen (2012), Time series momentum, Journal of Financial Economics 104(2) 228-250](https://papers.ssrn.com/abstract=2089463); [Liu & Tsyvinski (2021), Risks and returns of cryptocurrency, Review of Financial Studies 34(6) 2689-2727 (time-series momentum in crypto)](https://papers.ssrn.com/abstract=3226806)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 9. Donchian channel breakout (20/10) (`donchian_breakout`)

- **How it works:** Turtle-style channel breakout: buy a new 20-bar high, exit on a 10-bar low.
- **Entry (LONG, closed bars only):** Long when the close is above the highest high of the previous 20 bars.
- **Exit:** Exit when the close is below the lowest low of the previous 10 bars, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `entry_len` 20 (5..200); `exit_len` 10 (3..100); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** futures (daily), the Turtle rules
- **Short rule:** short below the 20-bar low (not executed: spot is long-only)
- **May work when:** trend starts after ranges. **Fails when:** false breakouts in ranges.
- **Published evidence (not evidence for this implementation):** trading-range-break rules are part of BLL's study (same snooping caveats); channel breakouts are standard in trend-following research
- **Sources:** Donchian channel / Turtle rules (20-bar entry, 10-bar exit, 2N stop), as published by C. Faith, Way of the Turtle (2007); [Brock, Lakonishok & LeBaron (1992), Simple technical trading rules and the stochastic properties of stock returns, Journal of Finance 47(5) 1731-1764](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html); [ArturSepp/TrendFollowingSystems (GPL-3.0): trend-following research code, ideas only](https://github.com/ArturSepp/TrendFollowingSystems); [Hudson & Urquhart (2021), Technical trading and cryptocurrencies, Annals of Operations Research 297 191-220 (no out-of-sample predictability for Bitcoin; some for other coins)](https://hull-repository.worktribe.com/output/2328042)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 10. ATR volatility breakout (Keltner-style) (`atr_breakout`)

- **How it works:** Buys when price moves more than k ATRs above its average: a volatility-adjusted breakout.
- **Entry (LONG, closed bars only):** Long when the close crosses above EMA 20 + 2 x ATR(14).
- **Exit:** Exit when the close falls back below EMA 20, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `ema_period` 20 (5..200); `band_atr` 2 (0.5..5); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general (Keltner channels)
- **Short rule:** short below EMA 20 - 2 x ATR (not executed: spot is long-only)
- **May work when:** volatility expansion into a trend. **Fails when:** spikes that revert.
- **Published evidence (not evidence for this implementation):** no robust independent evidence for this rule
- **Sources:** Wilder (1978), New Concepts in Technical Trading Systems (RSI, ATR, ADX/DMI definitions); [EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only](https://github.com/EA31337/EA31337-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 11. Resistance breakout with volume expansion (`sr_breakout`)

- **How it works:** Breaks the last CONFIRMED swing high (a pivot known only after it formed) and requires a volume surge.
- **Entry (LONG, closed bars only):** Long when the close breaks above the last confirmed swing high (5 bars each side) + 0.1 x ATR, with volume at least 1.5 x its 20-bar average.
- **Exit:** Exit after two closes back below the broken level - 0.5 x ATR (failed breakout), at 2 R, or the 2 x ATR stop.
- **Default exits plan:** trailing none; partials [('1', '0.5', 'breakeven')]
- **Parameters (default, allowed range):** `pivot_n` 5 (2..20); `buffer_atr` 0.1 (0..2); `volume_mult` 1.5 (1..5); `target_r` 2 (0..10); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** forex M15 (confluence repository's pivot breakout)
- **Short rule:** short below the last confirmed swing low (not executed: spot is long-only)
- **May work when:** range ends with participation. **Fails when:** low-volume fake-outs.
- **Published evidence (not evidence for this implementation):** the source repository's backtest charges no spread or commission: not evidence
- **Sources:** [siriusforex-ai/confluence_matrix_forex @f3beff9 (no licence): pivot breakout, rule idea only; its backtest charges no costs](https://github.com/siriusforex-ai/confluence_matrix_forex); J. Granville (1963), On-balance volume
- **Code / licence:** rule idea from siriusforex-ai/confluence_matrix_forex @f3beff9 (no licence); independent implementation
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 12. Breakout retest (`breakout_retest`)

- **How it works:** Waits for a broken level to be retested and hold before buying, instead of chasing the breakout bar.
- **Entry (LONG, closed bars only):** After a close above the previous 20-bar high, within 10 bars: a bar whose low comes back to the broken level (within 0.25 x ATR) and closes above it and above its open.
- **Exit:** Stop 0.5 x ATR below the retest low / level; exit below the prior 10-bar low; target 2 R.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `entry_len` 20 (5..200); `retest_window` 10 (2..50); `retest_atr` 0.25 (0..2); `exit_len` 10 (3..100); `target_r` 2 (0..10); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general practice
- **Short rule:** short on a retest of a broken low (not executed: spot is long-only)
- **May work when:** breakouts that become support. **Fails when:** breakouts that fail without a retest (missed) or fail after it.
- **Published evidence (not evidence for this implementation):** no robust independent evidence
- **Sources:** Donchian channel / Turtle rules (20-bar entry, 10-bar exit, 2N stop), as published by C. Faith, Way of the Turtle (2007); [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 13. EMA pullback in an uptrend (`ema_pullback`)

- **How it works:** Buys a dip to the fast EMA while the medium EMA is above the slow EMA.
- **Entry (LONG, closed bars only):** Long when EMA 50 > EMA 200, the close is above EMA 50, the bar's low touches EMA 20 and it closes above EMA 20 as a bullish bar.
- **Exit:** Exit when the close falls below EMA 50, or at the 2 x ATR stop; ATR trailing after +1 R.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `pull_ema` 20 (5..100); `mid_ema` 50 (10..200); `slow_ema` 200 (50..400); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general practice
- **Short rule:** short on a rally to EMA 20 in a downtrend (not executed: spot is long-only)
- **May work when:** orderly trends with shallow pullbacks. **Fails when:** trend ends (the pullback keeps going).
- **Published evidence (not evidence for this implementation):** no robust independent evidence
- **Sources:** [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies); [EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only](https://github.com/EA31337/EA31337-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 14. Trend pullback SMA 10/50 (`trend_pullback`)

- **How it works:** Buys the first recovery above SMA 10 after a dip, while SMA 10 is above SMA 50 (the rule selected earlier).
- **Entry (LONG, closed bars only):** Long when SMA 10 > SMA 50, the close is above SMA 50, the previous close was below SMA 10 and this close is above it.
- **Exit:** Stop 3 x ATR; exit on a close below SMA 50; ATR trailing after +1 R; time stop 48 bars below +0.5 R.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'}); time stop 48 bars
- **Parameters (default, allowed range):** `fast` 10 (2..100); `slow` 50 (10..300); `stop_atr` 3 (0.5..10); `atr_period` 14 (5..50); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** index futures, daily (trend-bot)
- **Short rule:** short on a recovery below SMA 10 in an SMA 10 < SMA 50 downtrend (not executed: spot is long-only)
- **May work when:** steady trends. **Fails when:** trend reversals.
- **Published evidence (not evidence for this implementation):** the source repository publishes no verifiable results
- **Sources:** [arwensorcenelli/trend-bot @ab75bb1 (no licence): 10/50 MA trend with a 3 x ATR stop, rule idea only](https://github.com/arwensorcenelli/trend-bot); [Brock, Lakonishok & LeBaron (1992), Simple technical trading rules and the stochastic properties of stock returns, Journal of Finance 47(5) 1731-1764](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html)
- **Code / licence:** rule idea from arwensorcenelli/trend-bot @ab75bb1 (no licence); independent implementation
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 15. Rolling VWAP pullback (`vwap_pullback`)

- **How it works:** Buys a touch of a rising volume-weighted average price (24-bar rolling, since crypto has no session).
- **Entry (LONG, closed bars only):** Long when the 24-bar VWAP is higher than 6 bars ago and the bar's low touches the VWAP while it closes above.
- **Exit:** Exit when the close is more than 0.5 x ATR below the VWAP, or at the 2 x ATR stop.
- **Default exits plan:** trailing none
- **Parameters (default, allowed range):** `vwap_window` 24 (6..200); `slope_bars` 6 (1..50); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** equity intraday (session VWAP); here a rolling window
- **Short rule:** short on a touch of a falling VWAP (not executed: spot is long-only)
- **May work when:** orderly up-moves with volume. **Fails when:** high-volatility sell-offs.
- **Published evidence (not evidence for this implementation):** VWAP is an execution benchmark; no robust evidence for this entry rule
- **Sources:** [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 16. Bollinger Band reversion (`bollinger_reversion`)

- **How it works:** Buys when price closes back inside the lower band after an excursion below it, expecting a return to the mean.
- **Entry (LONG, closed bars only):** Long when the previous close was below the lower band (20, 2 sd) and this close is back above it (optionally only above the 200-bar average).
- **Exit:** Exit at the middle band, at the 2 x ATR stop, or after 24 bars (time stop).
- **Default exits plan:** trailing none; time stop 24 bars
- **Parameters (default, allowed range):** `bb_period` 20 (5..200); `bb_std` 2 (1..4); `trend_filter` 1 (0..1); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general
- **Short rule:** short when price closes back inside the upper band (not executed: spot is long-only)
- **May work when:** ranges and orderly pullbacks. **Fails when:** strong down-trends (bands walk down).
- **Published evidence (not evidence for this implementation):** no robust independent evidence for crypto
- **Sources:** J. Bollinger (2001), Bollinger on Bollinger Bands (20-period, 2 standard deviations); [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 17. RSI(2) reversion above the 200 average (`rsi_reversion`)

- **How it works:** Connors' short-term reversion: buy a deeply oversold 2-period RSI while the long-term trend is up.
- **Entry (LONG, closed bars only):** Long when RSI(2) is below 10 and the close is above SMA 200.
- **Exit:** Exit when the close is above SMA 5; 3 x ATR stop (a protective addition: the published rule has no stop); time stop 24 bars.
- **Default exits plan:** trailing none; time stop 24 bars
- **Parameters (default, allowed range):** `rsi_period` 2 (2..14); `rsi_entry` 10 (1..40); `trend` 200 (50..400); `exit_sma` 5 (2..50); `stop_atr` 3 (0.5..10); `atr_period` 14 (5..50); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** US equities and ETFs, daily (Connors & Alvarez)
- **Short rule:** short when RSI(2) > 90 below SMA 200 (not executed: spot is long-only)
- **May work when:** pullbacks inside long uptrends. **Fails when:** crashes (the published rule has no stop).
- **Published evidence (not evidence for this implementation):** the authors' published statistics are their own, on equities; not independently verified for crypto
- **Sources:** Connors & Alvarez (2008), Short Term Trading Strategies That Work (RSI(2) < 10 above the 200-day average, exit above the 5-day average)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 18. Rolling z-score reversion (range filter) (`zscore_reversion`)

- **How it works:** Statistical reversion: buy when price recovers from more than 2 standard deviations below its 50-bar mean, only when ADX says the market is ranging.
- **Entry (LONG, closed bars only):** Long when the 50-bar z-score crosses back above -2 and ADX(14) is below 25.
- **Exit:** Exit when the z-score returns to 0, at the 2 x ATR stop, or after 24 bars.
- **Default exits plan:** trailing none; time stop 24 bars
- **Parameters (default, allowed range):** `z_window` 50 (10..300); `z_entry` 2 (0.5..4); `z_exit` 0 (-1..2); `adx_max` 25 (10..50); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general (pairs / stat-arb practice)
- **Short rule:** short when the z-score falls back below +2 (not executed: spot is long-only)
- **May work when:** ranges. **Fails when:** regime change into a trend.
- **Published evidence (not evidence for this implementation):** no robust independent evidence for single-asset crypto
- **Sources:** [freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied](https://github.com/freqtrade/freqtrade-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 19. OBV-confirmed trend (`obv_trend`)

- **How it works:** Granville's on-balance volume: enter when accumulation (OBV above its average) confirms a rising price trend.
- **Entry (LONG, closed bars only):** Long when OBV crosses above its 20-bar EMA while the close is above a rising EMA 50.
- **Exit:** Exit when OBV is below its EMA and the close is below EMA 50, or at the 2 x ATR stop.
- **Default exits plan:** trailing atr ({'method': 'atr', 'activate_r': '1', 'atr_mult': '3'})
- **Parameters (default, allowed range):** `obv_ema` 20 (5..100); `trend` 50 (10..300); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** equities (Granville)
- **Short rule:** short when OBV crosses below its EMA under a falling EMA 50 (not executed: spot is long-only)
- **May work when:** trends with participation. **Fails when:** volume spikes from liquidations or wash trading.
- **Published evidence (not evidence for this implementation):** no robust independent evidence
- **Sources:** J. Granville (1963), On-balance volume; [EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only](https://github.com/EA31337/EA31337-strategies)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### 20. Volatility / trend regime switch (`regime_switch`)

- **How it works:** Chooses its rule by regime: breakouts when ADX shows a trend, band reversion when ADX shows a range, nothing in between.
- **Entry (LONG, closed bars only):** ADX(14) >= 25: long on a close above the prior 20-bar high. ADX <= 20: long when the close re-enters the lower Bollinger band (20, 2). Otherwise no trade.
- **Exit:** Exit below the prior 10-bar low; in a range regime also at the 20-bar mean; 2 x ATR stop.
- **Default exits plan:** trailing none; time stop 48 bars
- **Parameters (default, allowed range):** `trend_adx` 25 (15..50); `range_adx` 20 (5..40); `atr_period` 14 (5..50); `stop_atr` 2 (0.5..10); `target_r` 0 (0..10); `min_volume_ratio` 0 (0..5)
- **Designed for:** general
- **Short rule:** mirror rules for shorts (not executed: spot is long-only)
- **May work when:** clear regimes. **Fails when:** transitions between regimes.
- **Published evidence (not evidence for this implementation):** no robust independent evidence; regime filters reduce but do not remove whipsaws
- **Sources:** Wilder (1978), New Concepts in Technical Trading Systems (RSI, ATR, ADX/DMI definitions); J. Bollinger (2001), Bollinger on Bollinger Bands (20-period, 2 standard deviations); Donchian channel / Turtle rules (20-bar entry, 10-bar exit, 2N stop), as published by C. Faith, Way of the Turtle (2007)
- **Code / licence:** independent implementation; no third-party code copied
- **Status:** implemented, unit-tested (entry, exit, no look-ahead); backtest pending; paper not started; exchange demo not started; live locked.

#### Candidates not in the library

| Candidate | Status | Reason |
|---|---|---|
| Momentum with volume confirmation | MERGED | not a distinct rule: every module has the shared min_volume_ratio filter, and sr_breakout requires volume expansion |
| Volume expansion breakout | MERGED | implemented as sr_breakout (breakout of a confirmed level with a volume surge) |
| ATR / volatility regime strategy | MERGED | covered by regime_switch (ADX regime) and atr_breakout (ATR bands) |
| Trend plus momentum confluence | REJECTED | cosmetic duplicate of rsi_momentum / macd_momentum with a trend filter (both already filter on trend) |
| Multi-timeframe pullback confirmation | MERGED | the higher-timeframe filter of mtf_trend applied to a pullback duplicates ema_pullback's EMA 50 > 200 regime |
| Multi-timeframe confluence | REJECTED | no distinct, testable rule beyond mtf_trend |
| Hyperliquid grid | NOT IMPLEMENTED | separate venue; the source pauses (does not close) on drawdown, leaving inventory exposed; its trade log cannot be verified |
| Confluence Matrix (forex) | BLOCKED | no forex data source; MetaTrader 5 only; its backtest charges no costs |
| Gold vs BTC | BLOCKED | gold source not chosen |

## 5. Settings: per strategy, then portfolio-wide

Per strategy (each its own row and version; one save never touches another):
- risk budget: **fixed USDT** or **% of account equity** (`risk_pct`, 0 = fixed). The budget is the loss if the stop is hit, after costs; the size is budget / stop distance, capped by the maximum position and the free balance. It is not the amount invested and not leverage (spot has none);
- max position, max strategy exposure, max open positions, max daily loss, max spread, slippage allowance, max price age;
- cooldown: pause new entries for N minutes after K losses in a row;
- minimum traded value of the signal candle;
- exits: trailing (none / percent / ATR / swing low) with activation in R; partial targets in R with an optional move to breakeven; time stop; the module's own stop (ATR multiple) and target (R multiple);
- strategy parameters, validated against their bounds;
- mode (DISABLED / PAPER / EXCHANGE DEMO with a DEMO or TESTNET connection of the same exchange). LIVE is not selectable.

Portfolio-wide (`/dashboard/crypto/portfolio`), checked before every entry of
every strategy in every mode: total open risk, open positions, total
exposure, exposure per coin (three BTC strategies count as one BTC
exposure), daily loss, drawdown shutdown (sets the emergency stop; open
positions keep their protection). Plus the switches for backtests and
parameter optimisation. Every change is audited (`crypto.config_audit`).

A reliable risk boundary is required: missing ATR, a stop not below the
entry, unknown precision, stale data or a size below the exchange minimum
reject the trade with the reason shown on the strategy page.

## 6. Backtesting method and metrics

- Bars: closed candles only; indicators are causal (tested by truncation for every module and indicator).
- Execution: next-open fills with half-spread and slippage; stop first on an ambiguous bar; gaps at the open; volume-capped partial fills; taker fees on entries and stop / market exits, maker on targets.
- Validation: defaults run on train, validation and out-of-sample; a train-only 3 x 3 grid; 4-fold anchored walk-forward; sensitivity over the grid; buy-and-hold benchmark.
- Verdict tiers: **candidate** (positive out-of-sample expectancy, profit factor >= 1, >= 60% positive folds, robust sensitivity, drawdown <= 25%), **weak**, **insufficient evidence** (< 30 out-of-sample trades), **failed**. Ranking: tier, then out-of-sample expectancy in R, then drawdown. **Win rate is never used to rank.** A candidate is only "approved for consideration" for paper / demo testing.
- Every run stores its code fingerprint, configuration, effective costs and risk, data provenance (exchange, host, first / last candle, gaps) and timestamps.
- Metrics: net profit / return, gross profit / loss, realized vs closed-at-end, win / loss rate, average win / loss, expectancy (USDT and R), profit factor, max drawdown, Sharpe / Sortino (30+ days), holding time, fees, slippage, funding, exposure, capital utilisation, partial fills, consecutive wins / losses, monthly and weekly returns, exit reasons, skipped signals. Short side: n/a on spot.

## 7. Running the first real batch (server, after an approved deploy)

1. Turn on backtests: Crypto Trading > Portfolio Risk > Backtests = On.
2. Queue a batch: Crypto Trading > Backtests > New batch (all strategies; BTC/USDT, ETH/USDT, SOL/USDT; 1h; Binance.US; two years). That is 58 runs (19 single-symbol modules x 3 symbols, plus meta_muse once as a pair).
3. Run the queue with the crypto-trading container's limits (128 MB, 0.25 CPU), lowest priority:
   `cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml run --rm crypto-trading python -m yonixalpha_crypto.backtest.worker --max-runs 60`
   Expected cost (measured locally on 8,000 bars: about 15 s and 37 MB per study at full speed): roughly one minute per run at 0.25 CPU, about one hour for the batch. Data: about 17,500 candles per symbol (well under the 40,000 limit). It can be cancelled from the page; a stopped worker's run is re-queued (twice at most).

## 8. Tests (commands and results)

All run in this session on 2026-10-16 (local Postgres 16 and Redis; no exchange reachable):

| Command | Result |
|---|---|
| `cd packages/crypto-py && ruff check yonixalpha_crypto tests` | clean |
| `cd packages/crypto-py && python -m pytest -q` | **244 passed** (after the PR review fixes; see `docs/PR82_REVIEW.md`) |
| `cd services/crypto-trading && python -m pytest -q` | 2 passed |
| `cd apps/api && ruff check app tests && python -m pytest -q` (full suite, Memecoin routes included) | clean; **211 passed** (after the PR review fixes) |
| `cd apps/web && npx tsc --noEmit && npx next lint && npx next build` | clean |
| gitleaks on the staged changes | no leaks |
| protected-path check (`git diff origin/main --stat -- packages/core-py services/{Memecoin workers} scripts apps/api/migrations apps/web/lib .env.example`) | empty |

What the crypto-py tests cover: every module's entry and exit rule on scenarios built for it; no look-ahead for every
module and every indicator (decisions on data cut at each bar equal decisions on the full data); the live adapter
deciding like the backtester; next-open fills, stop-first ambiguity, gaps, volume-capped partial fills, fees on both
legs, equity == sum of trade P&L; metrics and their sample labels; chronological splits, train-only selection,
walk-forward and the verdict (a high win rate with negative expectancy is "failed"); the queue (batch, provenance,
cancel, stale recovery, pair skip, refusal while switched off); the bounded candle store; per-strategy fixed / %
budgets, cooldown, per-coin and drawdown portfolio limits (shutdown sets the emergency stop); mode rules (no LIVE,
EXCHANGE_DEMO only with a same-exchange DEMO / TESTNET connection); paper trailing / partial / breakeven accounting; an
EXCHANGE_DEMO strategy entry through the executor to the (simulated) Binance testnet with no duplicate on a repeated
candle; plus the earlier execution, connection and lock tests. API tests cover login on every endpoint, honest empty
states, lifecycle status, ranking order, comparison, portfolio versions and refusal of LIVE.

**Simulation versus real exchanges:** every exchange in these tests is simulated (MockTransport). No test ran
against a real exchange demo or testnet endpoint in this session.

**Browser check:** the dashboard pages were rendered at 1400 px and 390 px against responses captured from the real
API and the real backtest worker running on SYNTHETIC candles (not results), with no console errors and no horizontal
page overflow on the phone width.

## 9. Database migration and rollback

`c0004` is additive and backward-compatible:
- new tables: `crypto.ohlcv`, `crypto.backtest_batches`, `crypto.backtest_runs`, `crypto.portfolio_settings`;
- new columns: `crypto.strategy_configs.exits` (JSONB, default `'{}'`), `crypto.strategy_configs.connection_id` (nullable, FK `fk_strategy_configs_connection_id` ON DELETE SET NULL); `crypto.demo_positions.initial_stop`, `highest_price`, `exits_state` (nullable).
- Applied by the API at start (`python -m yonixalpha_crypto.migrate upgrade`), like c0001-c0003. The main (Memecoin) Alembic chain is untouched.
- Rollback: `python -m yonixalpha_crypto.migrate downgrade c0003` drops exactly these objects (stored candles and backtest results are lost: export them first). The upgrade / check / downgrade / upgrade cycle is tested (`tests/test_migrations.py`).
- Indexes: run lookups by batch, status and strategy; batches by creation time. No full-table scans on Memecoin tables (none are read).

## 10. Resources and operational limits

- Worker: the strategy library adds no measurable idle memory (pure Python, loaded with the engine); the execution code loads only while an exchange trade is active (+8 MB measured earlier). A paper strategy reads up to 3 x its longest window of candles once per closed bar (at most 624 candles, for `mtf_trend`).
- Backtests: one study at a time, `nice 19`, inside the 128 MB / 0.25 CPU container; peak about 40 MB locally for 8,000 bars; candles capped at 40,000 per symbol and interval; at most 120 runs per batch.
- Not measured on the server yet: actual run times and the effect on the API during a batch. Check `docker stats` during the first batch.

## 11. Unresolved issues, blockers and rejected candidates

- No real backtest, paper or exchange-demo result exists yet (section 1).
- Exchange demo depends on the server reaching the practice hosts (still to be checked from the server) and on a practice key; Bybit and Binance.com public data are refused from the US server, so EXCHANGE_DEMO strategies there would see "stale / unavailable" data and not trade. Binance.US paper trading works.
- Spot is long-only: every short rule is documented, none is executed; perpetual futures are not implemented.
- `meta_muse` in the backtester uses the original rule on aligned BTC / ETH candles; the live engine keeps its original class (both read the same EMA definition; a test checks they agree).
- Correlation and overlap on the Compare page use the stored out-of-sample curve (300 points) and trade sample (last 100 trades): enough to flag duplicates, not a full portfolio simulation.
- Rejected / merged: see the table in section 4 (volume-confirmed momentum, volume-expansion breakout, ATR regime, trend + momentum confluence, multi-timeframe pullback and confluence, Hyperliquid grid, Confluence on forex, Gold vs BTC).
- Live trading: locked. Before it can even be considered: real practice-account lifecycle tests, verified key permissions, reconciliation evidence, working protective exits, risk-limit enforcement on a practice account, the kill switch, and the operator's written authorisation.
