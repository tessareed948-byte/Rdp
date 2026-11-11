# UT FX AI — MT5 Python Bridge (demo-first)

A Windows GitHub Actions runner installs MetaTrader 5, authenticates to a broker demo account, polls a configured signal endpoint, validates trade levels and calculates position size using broker-side profit calculations before submitting orders through the MetaTrader5 Python package.

## Safety defaults

- DEMO_ONLY=true is mandatory. Startup aborts unless MT5 reports ACCOUNT_TRADE_MODE_DEMO.
- ALLOW_DEMO_TRADING=false by default. Signals are validated and logged, but no orders are sent.
- This branch is demo-only; live-account execution is blocked.
- Signals require BUY/SELL, stop loss, TP1, valid price ordering and a broker-listed symbol.
- Default risk sizing is 0.25% of demo balance per trade. This is a test setting, not a recommendation.
- Duplicate protection uses SQLite plus MT5 order/position history and a unique magic/comment fingerprint.

## GitHub Actions repository secrets

Required: MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, SIGNALS_URL, SIGNALS_ACCESS_KEY.
Set SIGNALS_URL to your Render bot URL plus /internal/mt5-signals. Set SIGNALS_ACCESS_KEY to the same value as the bot Render service's existing JOB_SECRET. The default request header is X-Job-Secret. Optional: SIGNALS_AUTH_HEADER, MT5_TERMINAL_PATH, MT5_SYMBOL_MAP (JSON), RDP_PASSWORD, TAILSCALE_AUTHKEY.

Never commit credentials or paste them into workflow files. Configure them under repository Settings → Secrets and variables → Actions.

## Signal contract expected by this initial parser

The endpoint must return a JSON array or an object with signals, data or results. A single signal object is accepted too. Fields: symbol/instrument; side/direction/action = BUY or SELL; sl/stop_loss; tp1/take_profit/tp/target. Market orders use the current MT5 quote. Pending orders additionally need entry/entry_price and order_type = buy_limit, sell_limit, buy_stop or sell_stop. An id or signal_id is recommended for stable deduplication.

The paired Aurora Trade Telegram Bot change adds an authenticated read-only /internal/mt5-signals feed over its existing aurora_bot_signals table. Merge PR #23 and redeploy the Render bot before starting this bridge. The feed returns only active validated setups. Do not enable demo execution until both changes are deployed and a validation-only run succeeds.

## Workflow

Open Actions → MT5 Python Bridge → Run workflow. A best-effort cron starts a new runner every six hours (UTC). GitHub-hosted runners are temporary; they are not persistent VPS instances. The bridge stops when the runner ends. Open broker positions remain at the broker but are unmanaged while the runner is offline. SQLite is uploaded as an artifact for diagnostics but is not automatically restored into a later runner. Do not use this for unattended live trading.

Tailscale is installed only when TAILSCALE_AUTHKEY exists. Optional RDP is protected by a secret password, keeps NLA enabled and restricts inbound RDP to the Tailscale CGNAT range. The bridge itself runs in the GitHub job session; RDP is for inspection, not required for trading.
