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


# --------------------------------------------------------------------------- entry-method study
def market_outcomes(sig, m15, params=None, extra_spread=0.0):
    """Enter at the open of the first M15 candle after the signal closes (buy at the ask,
    sell at the bid) with the stop and target the same distance away as the indicator uses."""
    import numpy as np
    p = params or Params()
    m = m15.copy()
    m.index = m.index.as_unit("ns")
    t = m.index.asi8
    o, h, l = (m[k].to_numpy(float) for k in ("open", "high", "low"))
    sp = m["spread"].to_numpy(float) + extra_spread
    rows = []
    for r in sig.itertuples():
        b0 = int(np.searchsorted(t, r.end.value, "left"))
        res = 0
        if b0 < len(t):
            buy = r.side == "buy"
            entry = o[b0] + (sp[b0] if buy else 0.0)
            sl = entry - p.stop_dist if buy else entry + p.stop_dist
            tp = entry + p.target_dist if buy else entry - p.target_dist
            for b in range(b0, len(t)):
                if buy:
                    stop, target = l[b] <= sl, h[b] >= tp
                else:
                    stop, target = h[b] + sp[b] >= sl, l[b] + sp[b] <= tp
                if stop or target:
                    res = -1 if stop else 1
                    break
        rows.append(res)
    return pd.Series(rows, index=sig.index, dtype=int)


def _score(results):
    r = results[results != 0]
    w, lo = int((r == 1).sum()), int((r == -1).sum())
    n = w + lo
    return n, w, lo, ((3 * w - lo) / n if n else float("nan"))


def _fmt(name, results):
    n, w, lo, ex = _score(results)
    if n == 0:
        return f"    {name:34s} no closed trades"
    return f"    {name:34s} {n:3d} trades  {w:2d}W/{lo:2d}L  {100 * w / n:4.1f}% wins  {ex:+.2f}R"


def entry_method_table(m15, label, minutes, anchor):
    lines = [f"===== ENTRY METHODS: {label} (R = risk of 10, win = +3R, break-even 25% wins) ====="]
    kwargs = {} if anchor is None else {"anchor": anchor}
    for mode in ("Original", "Balanced", "Strict"):
        sig, chart = compute_signals(m15, minutes, Params(accuracy=mode), **kwargs)
        if len(sig) == 0:
            lines.append(f"  {mode}: no signals")
            continue
        ind = indicator_style_outcomes(sig, chart)
        r = ind[ind != 0]
        lines.append(f"  {mode}: {len(sig)} signals, indicator-style win rate "
                     f"{100 * (r == 1).mean():.1f}% (assumes every limit order fills)")
        for exp in (4, 24):
            lim = limit_order_outcomes(sig, m15, minutes, expiry_bars=exp)
            res = lim["result"].where(lim["filled"], 0)
            lines.append(_fmt(f"limit at entry, expires {exp} bars", res)
                         + f"  (fill rate {100 * lim['filled'].mean():.0f}%)")
        mk = market_outcomes(sig, m15)
        mk40 = market_outcomes(sig, m15, extra_spread=0.40)
        lines.append(_fmt("market at next candle open", mk))
        lines.append(_fmt("market, spread +0.40", mk40))
        cut = sig["end"].median()
        first, second = mk[sig["end"] < cut], mk[sig["end"] >= cut]
        lines.append(f"    market, first half {_score(first)[3]:+.2f}R ({_score(first)[0]} trades) / "
                     f"second half {_score(second)[3]:+.2f}R ({_score(second)[0]} trades)")
    return lines


def search_example(m15, side, entry):
    """Look for one chart example under every alignment, accuracy mode and HTF option."""
    lines = [f"===== SEARCH FOR THE EXAMPLE {side} {entry} ====="]
    found = []
    for label, minutes, anchor in (CONFIGS[0], CONFIGS[1], CONFIGS[2]):
        kwargs = {} if anchor is None else {"anchor": anchor}
        for pname, params in (("Balanced", Params()), ("Original", Params(accuracy="Original")),
                              ("Strict", Params(accuracy="Strict")),
                              ("Balanced+closed HTF", Params(use_closed_htf=True)),
                              ("Original+closed HTF", Params(accuracy="Original", use_closed_htf=True)),
                              ("threshold 7", Params(score_threshold=7))):
            sig, _ = compute_signals(m15, minutes, params, **kwargs)
            pool = sig[sig["side"] == side] if len(sig) else sig
            if len(pool) == 0:
                continue
            d = (pool["entry"] - entry).abs()
            best = d.idxmin()
            found.append((float(d[best]), f"{label}, {pname}", pool.at[best, "entry"], pool.at[best, "end"]))
    for d, name, e, t in sorted(found)[:6]:
        lines.append(f"  diff {d:6.2f}  {name:46s} entry {e:9.3f} at {t:%Y-%m-%d %H:%M} UTC")
    return lines
