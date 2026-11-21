"""Collects trade setups for the trade job and writes them to setups.json.

Sources, in this order:
  1. a setups list pushed in by the scanner API (PAYLOAD_SETUPS)
  2. the scanner API itself (SCANNER_API_URL + SCANNER_API_KEY), called with GET

Nothing here trades. Until the API address and key exist, it just reports that it is waiting."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MAX_AGE_MIN = int(os.getenv("MAX_AGE_MIN", "30") or "30")


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def extract(raw):
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict):
        data = data.get("setups", [])
    if not isinstance(data, list):
        raise ValueError("expected a list of setups or an object with a 'setups' list")
    return [s for s in data if isinstance(s, dict)]


def fresh(setups, now):
    keep = []
    for s in setups:
        created, expires = parse_time(s.get("created_at")), parse_time(s.get("expires"))
        if created and now - created > timedelta(minutes=MAX_AGE_MIN):
            print(f"  dropped {s.get('id')}: older than {MAX_AGE_MIN} minutes")
        elif expires and now > expires:
            print(f"  dropped {s.get('id')}: already expired")
        else:
            keep.append(s)
    return keep


def fetch_from_api(url, key, now):
    since = (now - timedelta(minutes=MAX_AGE_MIN + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sep = "&" if urllib.parse.urlparse(url).query else "?"
    request = urllib.request.Request(f"{url}{sep}since={since}", headers={
        "Authorization": f"Bearer {key}", "X-API-Key": key, "Accept": "application/json",
        "User-Agent": "mt5-api-trade/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return extract(response.read().decode("utf-8"))


def main():
    now = datetime.now(timezone.utc)
    pushed = os.getenv("PAYLOAD_SETUPS", "").strip()
    url, key = os.getenv("SCANNER_API_URL", "").strip(), os.getenv("SCANNER_API_KEY", "").strip()
    setups = []
    if pushed and pushed not in ("null", "[]", "{}"):
        print("Setups were pushed in by the scanner.")
        try:
            setups = extract(pushed)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"::error::The pushed setups are not valid JSON: {exc}")
            sys.exit(1)
    elif url and key:
        print("Asking the scanner API for setups.")
        try:
            setups = fetch_from_api(url, key, now)
        except urllib.error.HTTPError as exc:
            print(f"::warning::The scanner API answered HTTP {exc.code}. Nothing to trade this time.")
        except Exception as exc:  # network trouble, bad JSON, timeout
            print(f"::warning::Could not read the scanner API: {exc}")
    else:
        print("::notice::The scanner API is not configured yet (secrets SCANNER_API_URL and SCANNER_API_KEY). Waiting.")

    setups = fresh(setups, now)
    with open("setups.json", "w", encoding="utf-8") as handle:
        json.dump({"setups": setups}, handle)
    print(f"{len(setups)} setup(s) to trade")
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"count={len(setups)}\n")


if __name__ == "__main__":
    main()
