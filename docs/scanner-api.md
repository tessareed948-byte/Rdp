# Scanner API contract and triggers

The `API Trade Demo` workflow takes setups from your scanner and trades them on the Deriv **demo** account.

## 1. What your API returns (pull)

`GET <SCANNER_API_URL>?since=2026-11-21T10:00:00Z`, sent with the headers
`Authorization: Bearer <SCANNER_API_KEY>` and `X-API-Key: <SCANNER_API_KEY>`.

```json
{"setups": [
  {"id": "xau-h1-20261121-1400-buy",
   "symbol": "XAUUSD", "side": "buy", "type": "market",
   "sl_dist": 27.0, "tp_dist": 54.0,
   "risk_pct": 1.0, "strategy": "donchian",
   "created_at": "2026-11-21T14:01:00Z", "expires": "2026-11-21T16:00:00Z"}
]}
```

| field | needed | meaning |
|---|---|---|
| `id` | yes | unique per setup, letters, digits, `-`, `_`, max 24. The same id is never traded twice |
| `symbol`, `side` | yes | `XAUUSD`, `AUDJPY`...; `buy` or `sell`. The broker's suffix is found automatically |
| `sl` or `sl_dist` | yes | stop as a price, or as a distance from the entry price |
| `tp` or `tp_dist` | no | target as a price, or as a distance |
| `type` | no | `market` (default) or `limit` (then `entry` is required) |
| `entry` | no | limit price, or the reference price for a market setup (skipped if price ran too far) |
| `risk_pct` | no | percent of the balance to risk, sized from the stop (default `RISK_PCT`, capped by `MAX_RISK_PCT`) |
| `lots` | no | fixed lot size instead of `risk_pct` (capped by `XAU_MAX_LOTS`) |
| `created_at`, `expires` | no | older than 30 minutes or already expired: ignored |
| `strategy` | no | short tag, shown in the run summary |

Return only new or still-valid setups, or honour `since`. An empty list is fine. Any other answer is logged as a warning and nothing is traded.

## 2. Switch the schedule on (pull)

Repo settings, Secrets and variables, Actions:

* Secrets: `SCANNER_API_URL`, `SCANNER_API_KEY`, plus the Deriv demo secrets `DERIV_LOGIN`, `DERIV_PASSWORD`, `DERIV_SERVER`
  (the workflow also finds other names that contain DERIV or DEMO and prints the names it can see)
* Variable `API_POLL` = `on`
* Optional variables: `RISK_PCT` (1), `MAX_RISK_PCT` (2), `XAU_MAX_LOTS` (1), `MAX_OPEN` (10), `API_MAX_AGE_MIN` (30)
* Scheduled runs only start from the `main` branch, so merge this branch first.

## 3. Instant trigger (push), recommended

Your scanner calls GitHub the moment it finds a setup. Create a fine-grained token limited to this repo with
**Actions: Read and write**, and keep it in Render as `GITHUB_TOKEN`.

```python
import json, os, requests

def send_setups(setups):
    r = requests.post(
        "https://api.github.com/repos/tessareed948-byte/Rdp/actions/workflows/api-trade-demo.yml/dispatches",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"setups_json": json.dumps(setups)}},
        timeout=15)
    r.raise_for_status()   # 204 means GitHub accepted it
```

The trade appears about two to three minutes later. A run costs Windows minutes only when it has setups.

## Limits that always apply

Demo accounts only. Risk and lot caps, margin check, one trade per id, stop and target on the correct side
of the price, at most `MAX_OPEN` open trades and pending orders.
