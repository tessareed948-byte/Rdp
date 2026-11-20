# Scanner adapter integration contract

This is an additive compatibility layer. It does not change either scanner's strategy rules, calculations, thresholds, or output generation.

## XAU indicator adapter
Accepts only XAU/USD signals that explicitly identify a 45-minute setup and 1-hour confirmation, and carry entry, SL, TP, invalidation, timestamps, and an explicit market BUY/SELL order type. It copies the supplied levels without recalculation. Limit/stop orders are rejected because the existing Deriv executor only supports market orders; they must not be silently converted.

## Multi-engine adapter
Requires the existing router to return accepted=true, matching direction, and a separate complete trade_plan containing entry, SL, TP, invalidation, order type, timeframe, bar time, and expiry. The adapter does not manufacture a plan from directional votes. The existing router remains the authority for consensus and confidence thresholds.

## Safety boundary
The adapters return the legacy executor's normalized fxdesk_secondary contract only after structural validation. The executor must still perform its own symbol allowlist, freshness, broker quote, risk, margin, duplicate, DEMO-account, and order checks. No live-trading path is added.

## Not yet enabled for execution
The current Deriv executor accepts only market orders. The existing XAU scan workflow (xau_live.py) is a separate execution path with its own ATR/risk settings. This adapter branch deliberately does not modify or replace that workflow. A separate reviewed change is required to add pending-order support and route the adapter output into the executor; until then this code is a tested validation boundary, not an active trading integration.
