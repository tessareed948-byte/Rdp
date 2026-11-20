"""Reporting helpers: lists the signals, compares them with known examples and back-tests them."""
import pandas as pd

from xau_scanner import Params, compute_signals, indicator_style_outcomes, limit_order_outcomes

# Setups taken from the TradingView chart: (side, entry). Stop and target sit 10 / 30 away.
EXAMPLES = [("sell", 3949.653), ("buy", 4104.375), ("buy", 3941.842),
            ("buy", 3899.788), ("sell", 3949.003)]

CONFIGS = [
    ("1h", 60, None),
    ("45m anchored 17:00 New York", 45, ("NY", 17, 0)),
    ("45m anchored 18:00 New York", 45, ("NY", 18, 0)),
    ("45m anchored 00:00 UTC", 45, ("UTC", 0, 0)),
]


def match_examples(sig: pd.DataFrame, examples=EXAMPLES):
    rows, near, close = [], 0, 0
    for side, entry in examples:
        pool = sig[sig["side"] == side] if len(sig) else sig
        if len(pool) == 0:
            rows.append(f"  {side:4s} {entry:9.3f}  no {side} signals at all")
            continue
        diffs = (pool["entry"] - entry).abs()
        best = diffs.idxmin()
        d = float(diffs[best])
        flag = "MATCH" if d <= 2.0 else ("near" if d <= 5.0 else "no")
        near += d <= 5.0
        close += d <= 2.0
        rows.append(f"  {side:4s} {entry:9.3f}  nearest {pool.at[best, 'entry']:9.3f}  "
                    f"diff {d:6.2f}  at {pool.at[best, 'end']:%Y-%m-%d %H:%M} UTC  [{flag}]")
    return rows, close, near


def outcome_lines(sig, chart, m15, minutes):
    out = []
    ind = indicator_style_outcomes(sig, chart)
    resolved = ind[ind != 0]
    if len(resolved):
        wins = int((resolved == 1).sum())
        out.append(f"  indicator-style (entry assumed filled): {wins}/{len(resolved)} wins = "
                   f"{100 * wins / len(resolved):.1f}%")
    for label, extra in (("limit order, real spread", 0.0), ("limit order, spread +0.40", 0.40)):
        lim = limit_order_outcomes(sig, m15, minutes, expiry_bars=4, extra_spread=extra)
        filled = lim[lim["filled"] & (lim["result"] != 0)]
        wins = int((filled["result"] == 1).sum())
        losses = int((filled["result"] == -1).sum())
        total = wins + losses
        if total == 0:
            out.append(f"  {label}: no filled and closed trades")
            continue
        expectancy = (3 * wins - losses) / total
        out.append(f"  {label}: {len(sig)} signals, {int(lim['filled'].sum())} filled "
                   f"({100 * lim['filled'].mean():.0f}%), {wins}W/{losses}L = "
                   f"{100 * wins / total:.1f}% wins, expectancy {expectancy:+.2f}R per trade "
                   f"(break-even is 25% wins)")
    return out


def analyse(m15, label, minutes, anchor, params=None):
    lines = [f"===== {label} ====="]
    kwargs = {} if anchor is None else {"anchor": anchor}
    sig, chart = compute_signals(m15, minutes, params or Params(), **kwargs)
    if len(sig) == 0:
        lines.append("  no signals")
        return lines, 0, 0
    span_days = max((m15.index[-1] - m15.index[0]).days, 1)
    lines.append(f"  chart bars {len(chart)}, signals {len(sig)} "
                 f"(buy {int((sig['side'] == 'buy').sum())}, sell {int((sig['side'] == 'sell').sum())}), "
                 f"about {len(sig) * 30 / span_days:.1f} per month")
    lines.append("  latest signals (close time UTC, side, entry, SL, TP, score, quality):")
    for r in sig.tail(6).itertuples():
        lines.append(f"    {r.end:%Y-%m-%d %H:%M} {r.side:4s} {r.entry:9.3f} {r.sl:9.3f} {r.tp:9.3f} "
                     f"{r.score:2d} {r.quality}")
    lines.append("  comparison with your chart examples:")
    rows, close, near = match_examples(sig)
    lines.extend(rows)
    lines.append(f"  examples within $2: {close}/5, within $5: {near}/5")
    lines.extend(outcome_lines(sig, chart, m15, minutes))
    return lines, close, near
