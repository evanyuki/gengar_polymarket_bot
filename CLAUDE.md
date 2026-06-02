# CLAUDE.md — PolyBot Project Context

## What this is

An algorithmic trading bot ("PolyBot") for Polymarket's 5-minute BTC Up/Down binary markets. The strategy exploits oracle lag between Binance real-time BTC prices and Polymarket's delayed repricing. Built in Python, runs locally, trades real collateral on Polygon.

## Owner

JLow (jlowplayground on Polymarket). Solo developer. Started from zero software development knowledge in January 2026, built this from scratch using Claude + Cursor. Treats this as a serious trading operation.

## Current version: v14 — Momentum-confirmed FAK taker + Hold-to-resolution

### Files

```
bot.py              → Main loop, position lifecycle, circuit breakers (v14)
strategy.py         → Brownian motion probability + Kelly criterion (recalibrated vol=0.12)
executor.py         → Polymarket CLOB order execution (balance-verified FAK taker market-order path)
market.py           → Market discovery via Gamma API
price_feed.py       → Binance WebSocket for real-time BTC
tracker.py          → Quant analytics logger (signals.csv, trades.csv, executions.csv)
telegram_notifier.py → Mobile alerts + hourly summaries
proxy.py            → Tor proxy for CLOB API geo-restrictions
```

### Key dependencies

- `py-clob-client-v2` v1.0.1+ — Polymarket CLOB V2 SDK (required after April 28, 2026 exchange upgrade)
- Binance WebSocket — real-time BTC price feed
- Polymarket Gamma API (`gamma-api.polymarket.com`) — market discovery

### Wallet

- Polymarket Safe (live, source of truth = `.env` `SAFE_ADDRESS`): `0xd374a6597e9bf6e279a2676bc043e0e80761e135`
  - Confirmed via on-chain data-api: this proxy holds the current 5-min BTC trades.
- Old/prior Safe (deprecated, last on-chain activity 2026-04-21): `0xbcd8Da52677827188A4c205dCC0D46eda3038A50`
- Signature type: 2 (Safe/proxy)

---

## Strategy — How it works

### The edge

Every 5 minutes, Polymarket opens a market: "Will BTC be higher or lower?" Shares pay $1 (correct) or $0 (wrong). BTC moves on Binance instantly, but Polymarket's order book reprices with a lag. The bot buys the correct side during that lag.

### Entry pipeline (four filters)

1. **Brownian motion model** (`estimate_true_probability` in strategy.py)
   - Input: `btc_delta_pct` (BTC move from window open) + `seconds_remaining`
   - Volatility parameter: `btc_5min_vol = 0.12` (recalibrated — was 0.08, see calibration section)
   - Output: probability that BTC will be above/below opening price at resolution

2. **Minimum probability gate**: live `MIN_PROB=0.86` (code default 0.80) — model must clear that confidence

3. **Fee-adjusted edge gate**: `fee_adjusted_edge(true_prob, market_price, fee) ≥ edge_required`
   - `edge_required` comes from the markov risk modifier (floor `MIN_EDGE=0.06`). The standalone `ENTRY_HUGE_EDGE_MIN` "huge-edge" gate was REMOVED (2026-06-02): official-settlement backtest proved edge is INVERSELY correlated with WR (edge<0.15 → 91% WR, edge≥0.15 → 61%). A minimum-edge gate selected fat-edge/low-price coin-flip losers. Live execution re-checks the actual ask against `edge_required` only (catches slippage between signal and fill).
   - The old `safety_factor` / `market_price ≤ true_prob × 0.85` mechanism was also REMOVED. No `safety_factor` in the code.

4. **Momentum-confirmation gate**: live `ENTRY_REQUIRE_MOMENTUM_ALIGN=true` — `momentum_15s_pct` must be the same sign as the signal side (move still pushing, not fading/flat). Anti-mean-reversion. Official-settlement backtest (90 windows): m15-aligned won 88.5% (+0.570/trade) vs m15-fading 72.7% (-0.359/trade). Replaced the removed `ENTRY_PRICE_FLOOR=0.78` gate, which discarded cheap m15-aligned winners (px<0.78 & aligned won 81.5% / +0.609/trade).

### Position sizing and execution

Quarter-Kelly criterion. `kelly_f = (b*p - q) / b` where `b = (1-price)/price`. Then `bet = bankroll × kelly_f × 0.25`. Bounded by configured `MIN_BET`/`MAX_BET`. CLOB `minimum_order_size` is shares (BTC 5m commonly 5 shares), not a fixed $5 notional; for FAK BUY market orders the bot ensures the dollar amount can buy at least `mos` shares at the worst-price cap.

Execution policy is intentionally narrow: FAK taker only. A signal that clears the four entry filters fires immediately at a worst-price cap (`ENTRY_FAK_SLIPPAGE_TICKS` above the live ask); the live ask is re-validated against `edge_required` first. GTD/post-only maker entry was removed because it conflicts with the oracle-lag thesis: if the edge is real, immediate execution is worth more than maker fee savings.

### Exit: hold to resolution

Default and intended policy is hold-to-resolution. This is hardcoded — there is NO env toggle (`STOP_LOSS_ENABLED` is not read by any code; do not document it as live config). Stop-loss/prob-stop code is not used in the live position manager. Sell prices and held-side Brownian probability are monitored for diagnostics/calibration only. Older analysis showed stops can destroy value in this market because BTC micro-bounces can trigger exits that later resolve correctly.

For LIVE resolution, Binance is not final truth. Reconcile using Polymarket claim/order result, official outcome/CSV, and collateral balance. Binance final price is logged only as diagnostic context or dry-run simulation fallback.

---

## Safety systems in bot.py

### 1. CLOB health check (circuit breaker)

Before every trade: `self.executor.client.get_ok()` — pings Polymarket's unauthenticated health endpoint.
- Fail → increment `_consecutive_buy_failures`, skip trade
- 3 consecutive failures → `_clob_halted = True`, Telegram alert, stop all trading
- Auto-recovery: each new 5-min window, probe `get_ok()` again. If OK, reset and resume.

### 2. Daily loss limit

`session_pnl = self.stats.bankroll - self._session_start_balance`
If `session_pnl ≤ -DAILY_LOSS_LIMIT` (default $30): halt all trading, Telegram alert.

### 3. Balance-verified buys

- Snapshot collateral before order
- Wait 5s + 3 verification rounds (balance check + order API check + 3s wait each)
- Ghost fills caught via balance drop even when API throws exception
- NEVER cancel on timeout — returns `UNVERIFIED_BUY` for pending detection

### 4. Pending buy safety net

If buy can't be verified in 14s, save order details. Next window boundary:
- Query real balance
- If balance dropped > $1 since buy attempt → retroactively track as filled position
- Resolve normally (claim if won, record loss if lost)

### 5. Window-boundary balance sync

Every new window: query real collateral balance, overwrite internal tracking. Logs drift > $0.50. This is the ultimate source of truth that corrects any accumulated errors.

### 6. Minimum notional guard

Before any sell: check `shares × price ≥ $5`. If below, don't attempt — hold to resolution. Polymarket rejects sells below $5 and the error previously stranded shares.

---

## Critical technical knowledge

### Two-book architecture and FAK execution

Polymarket has TWO order books per token:
- **Raw token book**: illiquid, $0.06/$0.94 spread, almost no volume.
- **Complement engine book**: tight 1¢ spreads, all real volume. This is where market makers and the UI trade.

Current v14 entry uses `create_market_order(MarketOrderArgsV2)` + `post_order(..., OrderType.FAK, post_only=False)` for taker buys only. Official docs: BUY market orders specify dollar amount; FAK executes immediately against resting liquidity and cancels the remainder. Always pass a worst-price cap and verify by balance/order status.

### Float precision warning

Older versions avoided BUY market orders because some py-clob-client paths divided `amount/price` and produced share precision artifacts such as `21.000000000004`, rejected as `"invalid amounts, max accuracy of 4 decimals"`. v14 deliberately accepts the market-order path only for FAK execution; if this error reappears in live logs, do not silently fall back to GTD/post-only. Either fix the SDK/order builder path or skip the trade.

### Gamma API parsing

`clobTokenIds` and `outcomes` fields return as JSON strings, not native JSON. Always parse with `json.loads()`.

### collateral decimals

Balance API returns 6 decimals (1e6). Conditional tokens are ERC-1155 requiring per-token approval.

### VPN/geo restrictions

Tor is NOT required for placement. POST `/order` from the Hostinger MY VPS returns 401 (auth), not 403 (geo) — direct placement works. Tor (`proxy.py`) is now a lazy fallback: `executor.py` imports `ensure_tor`/`apply_proxy` only on a 403. If a real geo-block (403) appears, the proxy patch activates; restart Tor for a new circuit if its exit node is blocked. Header patching does not bypass a 403 — don't try.

### Order verification timing

FAK market orders should resolve immediately at the CLOB matching layer, but Polygon/balance settlement can still lag. The bot waits 5s, then performs 3 verification rounds at 3s intervals. If still unverified, return `UNVERIFIED_BUY` — never cancel based solely on local timeout; detect via balance sync at the next window boundary.

---

## Calibration history

### v1-v10 (vol=0.08): 60% WR, -35% ROC

The Brownian motion model with `btc_5min_vol = 0.08` was ~2x overconfident:
- Model said 60-75% → actual WR: 50% (barely better than coin flip)
- Model said 75-85% → actual WR: 40% (WORSE than random)
- Model said 85-100% → actual WR: 83% (roughly calibrated)

A 0.05% BTC move (~$37 on $74K) is noise in a 5-minute window. The old model treated it as "91% confident."

### v13 (vol=0.12): 100% WR on clean data, +55% ROC

Raising vol to 0.12 means a 0.05% move gives ~70% probability (not 91%). Only genuinely significant moves (0.10%+) reach the 80% threshold. Phase 1 of the first v13 session: 6 trades, 6 wins, +$45.73 on $83 deployed.

The 0.15 value was also tested — too conservative, zero trades in 2.5 hours. 0.12 is the sweet spot.

### Safety factor calibration

- 0.70 (Noisy article default for multi-day markets): too aggressive for 5-min markets, zero trades
- 0.85: allows trades when oracle lag creates genuine edge, filters out fully-priced moves
- Works because Polymarket's 5-min market mispricings are 5-15%, not 30-50%

---

## Bug history (resolved)

| Bug | Symptom | Fix | Version |
|-----|---------|-----|---------|
| Ghost orders | Buy "failed" but shares appeared, P&L diverged | Balance-verified buys | v11 |
| Partial fill trap | Sell below $5 minimum → error → shares stranded | Minimum notional guard | v11 |
| P&L tracking drift | Tracked -$6.46, real loss -$15.30 | Window-boundary balance sync | v11 |
| Decimal precision | `invalid amounts, max accuracy 4 decimals` | Integer shares via `create_order` | v10, re-applied v13 |
| Prob-stop destroying value | 4/5 stopped trades won at resolution | All stops removed | v12 |
| Model overconfidence | 60% WR despite "80% confident" signals | Vol recalibrated 0.08→0.12 | v13 |
| Safety factor too tight | Zero trades in 2.5 hours | Raised 0.70→0.85 | v13 |
| `take_profit_pct` crash | `'PolyBot' object has no attribute` | Removed all stop/TP references | v13 |
| CLOB outage losses | $42 lost trading on frozen/stale prices | `get_ok()` circuit breaker | v13 |
| `create_market_order` regression | Decimal precision error returned | Switched back to `create_order` | v13 |

---

## .env reference

```env
# Required
PRIVATE_KEY=0x...
SAFE_ADDRESS=0x...
DRY_RUN=false

# Strategy (live values — .env is source of truth, not code defaults)
MIN_EDGE=0.06
MIN_PROB=0.86
ENTRY_WINDOW_START=210
ENTRY_WINDOW_END=10
KELLY_FRACTION=0.25
MIN_BET=1.0
MAX_BET=5.0
BANKROLL=20.0
MARKOV_INSUFFICIENT_EDGE=0.10

# Entry gates (momentum-confirmation + price/size risk)
# ENTRY_REQUIRE_MOMENTUM_ALIGN: momentum_15s must match the signal side (anti-mean-reversion).
#   Replaced the removed ENTRY_PRICE_FLOOR. ENTRY_HUGE_EDGE_MIN also removed (edge inversely predicts WR).
ENTRY_REQUIRE_MOMENTUM_ALIGN=true
ENTRY_MAX_PRICE=0.89          # skip above this; (1-price) payoff too thin for forced min size
ENTRY_MIN_SIZE_KELLY_RATIO=3.0  # skip if CLOB min-share floor > Kelly stake × this

# Realized-vol clamp (floor raised 0.06->0.12 so dead-quiet noise can't read as high confidence)
VOL_FLOOR=0.12

# Safety
DAILY_LOSS_LIMIT=10

# Cold-start gating (block trading until buffers hold real measurements)
ENTRY_REQUIRE_WARM=true
SEED_VOL_FROM_KLINES=true
WARM_MIN_SAMPLES=12
WARM_MIN_SPAN_S=30

# Source consensus (settlement = Chainlink; Binance is signal only)
SOURCE_CONSENSUS_ENABLED=true
SOURCE_REQUIRE_CHAINLINK=true
SOURCE_REQUIRE_RTDS_BINANCE=true
SOURCE_STALE_DOWNSIZE_SEC=10.0
SOURCE_STALE_SKIP_SEC=30.0
SOURCE_DOWNSIZE_FACTOR=0.50
SOURCE_RTDS_SOURCE_GAP_BPS=25.0
SOURCE_DIRECT_VS_RTDS_BINANCE_GAP_BPS=6.0

# Entry execution
ENTRY_FAK_SLIPPAGE_TICKS=5
ENTRY_MAX_SPREAD=0.08
ENTRY_MIN_EXIT_PRICE=0.50
CLOB_ORDERBOOK_CACHE_ENABLED=true
CLOB_ORDERBOOK_MAX_AGE_SEC=1.0

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Other
MARKET_PERIOD=5
LOG_DIR=logs
```

> Exit policy is hardcoded hold-to-resolution. There is NO `STOP_LOSS_ENABLED` env var (it was documented but never read by code).
> `ENTRY_HUGE_EDGE_MIN` and `ENTRY_PRICE_FLOOR` were removed from code+config on 2026-06-02 — do not re-add as live config.

---

## Working with this codebase

### Preferences

- **Discuss before coding** — JLow prefers to discuss issues before actioning fixes
- **Complete replacement files** — deliver full files, not diffs or partial edits (unless explicitly requested)
- **Forensic debugging** — cross-reference bot terminal logs against Polymarket CSV transaction history trade-by-trade
- **Iterates rapidly** through versioned rewrites when architectural issues are discovered

### Polymarket CSV export

Available from Polymarket UI. Columns: `marketName`, `action` (Buy/Sell/Redeem), `usdcAmount`, `tokenAmount`, `tokenName`, `timestamp` (unix), `hash`. The CSV uses BOM encoding (`utf-8-sig`).

### Key API endpoints

- CLOB API: `https://clob.polymarket.com`
  - `GET /` → health check (`get_ok()`)
  - `GET /time` → server time
  - `POST /order` → place order (requires auth + Tor routing)
- Gamma API: `https://gamma-api.polymarket.com`
  - `GET /markets` → market discovery
- Status page: `https://status.polymarket.com`
- Binance WS: `wss://stream.binance.com:9443/ws/btcusdt@trade`

### Testing

No automated tests yet. Validation is done via:
1. `python -c "import ast; ast.parse(open('file.py').read())"` for syntax
2. Dry run mode (`DRY_RUN=true`)
3. Live run with small bankroll + terminal log review
4. Post-session CSV analysis comparing tracker output to Polymarket history

---

## Open questions / future work

- **Model improvement**: ~~Adding momentum could filter out bounce-backs~~ — DONE (2026-06-02): `ENTRY_REQUIRE_MOMENTUM_ALIGN` requires `momentum_15s_pct` to align with the signal side. Next: forward-validate live (90-window/2.3-day backtest is small), then consider momentum strength tiers (m15 strong >0.02% won 90% / +0.612/trade) and `m15&m30 aligned & |d|≥0.10` (93.3% / +0.903/trade).
- **VPS deployment**: Running locally means process crash = lost position. A VPS with systemd/pm2 would add resilience.
- **Automated testing**: Backtesting framework against historical 5-min windows would allow rapid strategy iteration without risking capital.
- **Multi-market**: The strategy could theoretically work on ETH, SOL, or other assets' Up/Down markets if they have similar oracle lag.
