"""Volatility-scaled stop study: does a stop sized to the market's own ATR work better than the
indicator's fixed $10 stop and $30 target?"""
import numpy as np
import pandas as pd

from scanner_report import Params, compute_signals
from xau_scanner import pine_atr


def atr_outcomes(sig, chart, m15, sl_mult, rr, extra_spread=0.0):
    """Market entry at the next M15 open with the stop `sl_mult` x ATR away and the target
    `rr` times the stop distance. Returns +1 / -1 / 0 (still open) per signal."""
    atr = pine_atr(chart["high"], chart["low"], chart["close"], 14)
    m = m15.copy()
    m.index = m.index.as_unit("ns")
    t = m.index.asi8
    o, h, l = (m[k].to_numpy(float) for k in ("open", "high", "low"))
    sp = m["spread"].to_numpy(float) + extra_spread
    rows = []
    for r in sig.itertuples():
        b0 = int(np.searchsorted(t, r.end.value, "left"))
        res = 0
        if b0 < len(t) and not np.isnan(atr[r.bar]):
            dist = sl_mult * atr[r.bar]
            buy = r.side == "buy"
            entry = o[b0] + (sp[b0] if buy else 0.0)
            sl = entry - dist if buy else entry + dist
            tp = entry + rr * dist if buy else entry - rr * dist
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


def _expectancy(res, rr):
    c = res[res != 0]
    if len(c) == 0:
        return float("nan"), 0, 0
    w = int((c == 1).sum())
    return (rr * w - (len(c) - w)) / len(c), len(c), w


def atr_study(m15, label, minutes, anchor):
    kwargs = {} if anchor is None else {"anchor": anchor}
    sig, chart = compute_signals(m15, minutes, Params(), **kwargs)
    lines = [f"===== VOLATILITY-SCALED STOPS: {label}, Balanced mode ====="]
    if len(sig) == 0:
        return lines + ["  no signals"]
    atr = pine_atr(chart["high"], chart["low"], chart["close"], 14)
    mean_atr = float(atr[sig["bar"].to_numpy()].mean())
    lines.append(f"  typical ATR at the signals ${mean_atr:.1f}; the indicator's fixed $10 stop is "
                 f"{10 / mean_atr:.2f} x ATR")
    cut = sig["end"].median()
    lines.append("  stop size, target:  trades  win%  expectancy   (first half / second half)")
    for mult in (0.75, 1.0, 1.5, 2.0):
        for rr in (2.0, 3.0):
            res = atr_outcomes(sig, chart, m15, mult, rr)
            ex, n, w = _expectancy(res, rr)
            e1 = _expectancy(res[sig["end"] < cut], rr)[0]
            e2 = _expectancy(res[sig["end"] >= cut], rr)[0]
            lines.append(f"    {mult:4.2f} x ATR, {rr:.0f}:1   {n:3d}  {100 * w / max(n, 1):4.1f}%  "
                         f"{ex:+.2f}R   ({e1:+.2f} / {e2:+.2f})")
    return lines
