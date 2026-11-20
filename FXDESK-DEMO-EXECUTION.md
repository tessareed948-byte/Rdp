# FX Desk to Deriv DEMO executor

This branch adds a separate one-signal executor. It does not edit or replace the legacy mt5_bridge.py, demo_gold_limit.py, existing workflow, or existing secrets.

- Accepts only signal_source=fxdesk_secondary.
- Restricts execution to the eight FX Desk instruments.
- Rejects expired signals, malformed SL/TP, wrong direction, and excessive entry drift.
- Uses the broker's minimum permitted lot and never rounds volume upward.
- Rejects minimum-lot trades exceeding 0.25% demo balance risk or 10% of free margin.
- Verifies the configured login/server, DEMO trade mode, and Deriv server identity.
- Checks duplicate order comments before sending.
- One workflow run handles one signal, then shuts down its Windows runner/MT5 process. Broker-side SL/TP remains active.

The trusted Render caller must dispatch this workflow with signal_key and signal_payload. It needs a GitHub token with Actions write permission. Never pass broker credentials in workflow inputs.

Run manually with empty inputs for a no-order Deriv DEMO connection test.
