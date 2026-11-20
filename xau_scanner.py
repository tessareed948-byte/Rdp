"""Python port of the TradingView indicator "Xau scalper by ut" (XAU Smart Money Bot).

The indicator is evaluated the way a live TradingView alert sees it: at the close of every
chart bar, with the higher-timeframe (60m and 15m) values taken from the data available at
that moment (including the still-forming 60m bar when the chart is 45m).

Input data are 15-minute candles (UTC, bar OPEN time as index) with the columns
open, high, low, close, volume, spread. Every chart timeframe used here (45m and 60m) is
a whole number of 15-minute candles, so all of it can be built exactly from M15.
"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

M15 = pd.Timedelta(minutes=15)


@dataclass
class Params:
    score_threshold: int = 8
    block_asia: bool = True
    session_tz: str = "Europe/Budapest"
    equal_tol: float = 1.5
    fib_tol: float = 5.0
    use_closed_htf: bool = False
    accuracy: str = "Balanced"  # Original, Balanced or Strict
    min_trend: float = 0.15
    stop_dist: float = 10.0
    target_dist: float = 30.0


# --------------------------------------------------------------------------- indicators
def pine_ema(values, length):
    """ta.ema: seeded with the first value, alpha = 2 / (length + 1)."""
    alpha = 2.0 / (length + 1)
    return pd.Series(np.asarray(values, dtype=float)).ewm(alpha=alpha, adjust=False).mean().to_numpy()


def pine_atr(high, low, close, length=14):
    """ta.atr: Wilder RMA of the true range, seeded with the SMA of the first `length` values."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    tr[0] = high[0] - low[0]
    out = np.full(len(tr), np.nan)
    if len(tr) >= length:
        out[length - 1] = tr[:length].mean()
        for i in range(length, len(tr)):
            out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def shift(a, k=1):
    a = np.asarray(a)
    if a.dtype == bool:
        out = np.zeros(len(a), dtype=bool)
        out[k:] = a[:-k]
        return out
    out = np.full(len(a), np.nan)
    out[k:] = a[:-k]
    return out


def roll_max(a, n):
    return pd.Series(a).rolling(n).max().to_numpy()


def roll_min(a, n):
    return pd.Series(a).rolling(n).min().to_numpy()


# --------------------------------------------------------------------------- resampling
def bucket_starts(index: pd.DatetimeIndex, minutes: int, anchor=("NY", 17, 0)) -> pd.DatetimeIndex:
    """Start time (UTC) of the chart bar that each M15 candle belongs to."""
    if minutes % 60 == 0:
        return index.floor(f"{minutes}min")
    kind, hh, mm = anchor
    tz = ZoneInfo("UTC") if kind == "UTC" else ZoneInfo("America/New_York")
    local = index.tz_convert(tz).tz_localize(None)
    shifted = local - pd.Timedelta(hours=hh, minutes=mm)
    day0_local = shifted.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    day0_utc = day0_local.tz_localize(tz).tz_convert("UTC")
    steps = (index - day0_utc) // pd.Timedelta(minutes=minutes)
    return day0_utc + steps * pd.Timedelta(minutes=minutes)


def aggregate(m15: pd.DataFrame, bucket: pd.DatetimeIndex) -> pd.DataFrame:
    key = pd.Series(bucket, index=m15.index)
    grouped = m15.groupby(key)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
        "spread": grouped["spread"].last(),
    })
    out.index.name = "start"
    return out


# --------------------------------------------------------------------------- main port
def compute_signals(m15: pd.DataFrame, minutes: int, params: Params | None = None,
                    anchor=("NY", 17, 0)):
    """Returns (signals, chart). `signals` has one row per confirmed BUY/SELL signal."""
    p = params or Params()
    m15 = m15.copy()
    m15.index = m15.index.as_unit("ns")
    chart = aggregate(m15, bucket_starts(m15.index, minutes, anchor))
    step = pd.Timedelta(minutes=minutes)
    last_m15_close = m15.index[-1] + M15
    # The last chart bar only counts once all of its time has passed (or the market closed).
    if chart.index[-1] + step > last_m15_close:
        chart = chart.iloc[:-1]
    n = len(chart)
    if n < 120:
        return pd.DataFrame(), chart

    o, h, l, c = (chart[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = chart["volume"].to_numpy(float)
    starts = chart.index
    ends = starts + step

    ema9, ema21 = pine_ema(c, 9), pine_ema(c, 21)
    atr = pine_atr(h, l, c, 14)
    atr_sma20 = pd.Series(atr).rolling(20).mean().to_numpy()

    hi10_prev = shift(roll_max(h, 10))
    lo10_prev = shift(roll_min(l, 10))
    swing_hi, swing_lo = roll_max(h, 20), roll_min(l, 20)
    range_eq = (roll_max(h, 50) + roll_min(l, 50)) / 2

    h1, h2, l1, l2 = shift(h, 1), shift(h, 2), shift(l, 1), shift(l, 2)
    c1, c2, o1, o2 = shift(c, 1), shift(c, 2), shift(o, 1), shift(o, 2)

    with np.errstate(invalid="ignore"):
        ema_bull, ema_bear = ema9 > ema21, ema9 < ema21
        ema_reject_sell = (h >= ema9) & (c < ema9)
        ema_reject_buy = (l <= ema9) & (c > ema9)
        bull_structure = (h > h1) & (h1 > h2) & (c > ema21)
        bear_structure = (l < l1) & (l1 < l2) & (c < ema21)
        liq_sell = (np.abs(h - h1) < p.equal_tol) & (c < h)
        liq_buy = (np.abs(l - l1) < p.equal_tol) & (c > l)
        premium, discount = c > range_eq, c < range_eq
        bull_disp = (c > o) & (c - o > atr * 0.7)
        bear_disp = (c < o) & (o - c > atr * 0.7)
        strong_bull = (c > o) & (c - o > atr)
        strong_bear = (c < o) & (o - c > atr)
        high_vol = atr > atr_sma20
        obv_bull = (c > c1) & (v > 0)
        obv_bear = (c < c1) & (v > 0)
        bull_sweep = (l < lo10_prev) & (c > l + (h - l) * 0.5)
        bear_sweep = (h > hi10_prev) & (c < h - (h - l) * 0.5)
        bull_bos, bear_bos = c > hi10_prev, c < lo10_prev
        trending = np.abs(ema9 - ema21) > atr * p.min_trend
        fib_buy = swing_hi - (swing_hi - swing_lo) * 0.618
        fib_sell = swing_lo + (swing_hi - swing_lo) * 0.618
        near_fib_buy = np.abs(c - fib_buy) < p.fib_tol
        near_fib_sell = np.abs(c - fib_sell) < p.fib_tol

    local_hour = starts.tz_convert(ZoneInfo(p.session_tz)).hour.to_numpy()
    asia = (local_hour >= 0) & (local_hour < 6)
    session_allowed = np.ones(n, dtype=bool) if not p.block_asia else ~asia

    # ----- higher-timeframe values as seen at each chart-bar close
    t15 = m15.index.asi8
    o15, h15, l15, c15 = (m15[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    h60 = aggregate(m15, m15.index.floor("60min"))
    s60 = h60.index.asi8
    O60, H60, L60, C60 = (h60[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    E9_60, E21_60 = pine_ema(C60, 9), pine_ema(C60, 21)
    E9_15, E21_15 = pine_ema(c15, 9), pine_ema(c15, 21)
    a9, a21 = 2.0 / 10, 2.0 / 22
    hour_ns = 3_600_000_000_000

    names = ["htf_bull", "htf_bear", "bias_bull", "bias_bear", "bull_fvg", "bear_fvg", "bull_ob",
             "bear_ob", "c_htf_bull", "c_htf_bear", "c_bias_bull", "c_bias_bear",
             "c_bull_fvg", "c_bear_fvg", "c_bull_ob", "c_bear_ob"]
    htf = {k: np.zeros(n, dtype=bool) for k in names}

    for j in range(n):
        end_ns = ends[j].value
        cur_start = ((end_ns - 1) // hour_ns) * hour_ns
        k = int(np.searchsorted(s60, cur_start))
        if k >= len(s60) or s60[k] != cur_start:
            continue
        lo = int(np.searchsorted(t15, cur_start, "left"))
        hi = int(np.searchsorted(t15, end_ns, "left"))
        if hi <= lo or hi < 2 or k < 4:
            continue
        ok_, hk, lk, ck = o15[lo], h15[lo:hi].max(), l15[lo:hi].min(), c15[hi - 1]
        e9p = a9 * ck + (1 - a9) * E9_60[k - 1]
        e21p = a21 * ck + (1 - a21) * E21_60[k - 1]
        htf["htf_bull"][j], htf["htf_bear"][j] = e9p > e21p, e9p < e21p
        htf["c_htf_bull"][j] = E9_60[k - 1] > E21_60[k - 1]
        htf["c_htf_bear"][j] = E9_60[k - 1] < E21_60[k - 1]
        # 60m fair value gaps and order blocks, with the forming bar as bar k
        seq_bull = ck > ok_ and C60[k - 1] > O60[k - 1] and C60[k - 2] > O60[k - 2]
        seq_bear = ck < ok_ and C60[k - 1] < O60[k - 1] and C60[k - 2] < O60[k - 2]
        htf["bull_fvg"][j] = seq_bull and lk > H60[k - 2]
        htf["bear_fvg"][j] = seq_bear and hk < L60[k - 2]
        htf["bull_ob"][j] = C60[k - 1] < O60[k - 1] and ck > H60[k - 1]
        htf["bear_ob"][j] = C60[k - 1] > O60[k - 1] and ck < L60[k - 1]
        # same patterns evaluated on the last completed 60m bar (the "closed HTF" option)
        htf["c_bull_fvg"][j] = (C60[k - 1] > O60[k - 1] and C60[k - 2] > O60[k - 2] and
                                C60[k - 3] > O60[k - 3] and L60[k - 1] > H60[k - 3])
        htf["c_bear_fvg"][j] = (C60[k - 1] < O60[k - 1] and C60[k - 2] < O60[k - 2] and
                                C60[k - 3] < O60[k - 3] and H60[k - 1] < L60[k - 3])
        htf["c_bull_ob"][j] = C60[k - 2] < O60[k - 2] and C60[k - 1] > H60[k - 2]
        htf["c_bear_ob"][j] = C60[k - 2] > O60[k - 2] and C60[k - 1] < L60[k - 2]
        # 15m bias: the 15m bar that closes at the chart-bar close
        i15 = hi - 1
        htf["bias_bull"][j], htf["bias_bear"][j] = E9_15[i15] > E21_15[i15], E9_15[i15] < E21_15[i15]
        htf["c_bias_bull"][j] = E9_15[i15 - 1] > E21_15[i15 - 1]
        htf["c_bias_bear"][j] = E9_15[i15 - 1] < E21_15[i15 - 1]

    pick = (lambda a, b: htf[b]) if p.use_closed_htf else (lambda a, b: htf[a])
    h_bull_fvg, h_bear_fvg = pick("bull_fvg", "c_bull_fvg"), pick("bear_fvg", "c_bear_fvg")
    h_bull_ob, h_bear_ob = pick("bull_ob", "c_bull_ob"), pick("bear_ob", "c_bear_ob")
    bias_bull, bias_bear = pick("bias_bull", "c_bias_bull"), pick("bias_bear", "c_bias_bear")

    i = lambda *arrs: sum(a.astype(int) for a in arrs)  # noqa: E731
    buy_score = i(ema_bull, bull_sweep, strong_bull, h_bull_fvg, h_bull_ob, obv_bull, high_vol,
                  near_fib_buy, ema_reject_buy, bull_structure, liq_buy, discount, bull_disp)
    sell_score = i(ema_bear, bear_sweep, strong_bear, h_bear_fvg, h_bear_ob, obv_bear, high_vol,
                   near_fib_sell, ema_reject_sell, bear_structure, liq_sell, premium, bear_disp)

    base_buy = (buy_score >= p.score_threshold) & session_allowed & bias_bull
    base_sell = (sell_score >= p.score_threshold) & session_allowed & bias_bear
    legacy_buy = base_buy & ~shift(base_buy)
    legacy_sell = base_sell & ~shift(base_sell)

    with np.errstate(invalid="ignore"):
        buy_quality = i(htf["c_htf_bull"], htf["c_bias_bull"], trending, c > ema9,
                        bull_sweep | bull_bos, bull_disp | strong_bull, discount)
        sell_quality = i(htf["c_htf_bear"], htf["c_bias_bear"], trending, c < ema9,
                         bear_sweep | bear_bos, bear_disp | strong_bear, premium)
    need = {"Strict": 6, "Balanced": 4}.get(p.accuracy, 0)
    buy_raw = base_buy & ((buy_quality >= need) if need else True)
    sell_raw = base_sell & ((sell_quality >= need) if need else True)
    buy_sig = buy_raw & ~shift(buy_raw)
    sell_sig = sell_raw & ~shift(sell_raw)

    bear_prev = c1 < o1
    bull_prev = c1 > o1
    lbh = np.where(bear_prev, h1, h2)
    lbl = np.where(bear_prev, l1, l2)
    lbuh = np.where(bull_prev, h1, h2)
    lbul = np.where(bull_prev, l1, l2)
    buy_entry = (lbh + lbl) / 2
    sell_entry = (lbuh + lbul) / 2

    chart = chart.copy()
    chart["buy_score"], chart["sell_score"] = buy_score, sell_score
    chart["htf_bull"], chart["htf_bear"] = htf["htf_bull"], htf["htf_bear"]
    chart["bias_bull"], chart["bias_bear"] = bias_bull, bias_bear
    chart["buy_signal"], chart["sell_signal"] = buy_sig, sell_sig

    rows = []
    for j in np.flatnonzero(buy_sig | sell_sig):
        buy = bool(buy_sig[j])
        entry = float(buy_entry[j] if buy else sell_entry[j])
        if np.isnan(entry):
            continue
        rows.append({
            "bar": int(j), "start": starts[j], "end": ends[j], "side": "buy" if buy else "sell",
            "entry": round(entry, 3),
            "sl": round(entry - p.stop_dist if buy else entry + p.stop_dist, 3),
            "tp": round(entry + p.target_dist if buy else entry - p.target_dist, 3),
            "score": int(buy_score[j] if buy else sell_score[j]),
            "quality": int(buy_quality[j] if buy else sell_quality[j]),
            "candidate_only": False,
        })
    return pd.DataFrame(rows), chart


# --------------------------------------------------------------------------- outcomes
def indicator_style_outcomes(signals: pd.DataFrame, chart: pd.DataFrame) -> pd.Series:
    """The indicator's own win-rate logic: first stop/target touch on later chart bars,
    stop wins a tie, and the entry is assumed to have been filled."""
    h, l = chart["high"].to_numpy(float), chart["low"].to_numpy(float)
    result = []
    for r in signals.itertuples():
        out = 0
        for b in range(r.bar + 1, len(chart)):
            if r.side == "buy":
                stop, target = l[b] <= r.sl, h[b] >= r.tp
            else:
                stop, target = h[b] >= r.sl, l[b] <= r.tp
            if stop or target:
                out = -1 if stop else 1
                break
        result.append(out)
    return pd.Series(result, index=signals.index, dtype=int)


def limit_order_outcomes(signals: pd.DataFrame, m15: pd.DataFrame, minutes: int,
                         expiry_bars: int = 4, extra_spread: float = 0.0) -> pd.DataFrame:
    """Realistic outcome of the limit order the alert would place: it must fill first, it
    expires after `expiry_bars` chart bars, and on a tie within one M15 candle the stop wins.
    Buy limits fill when the ask (bid + spread) reaches the entry; sell limits when the bid does."""
    m15 = m15.copy()
    m15.index = m15.index.as_unit("ns")
    t15 = m15.index.asi8
    h15, l15 = m15["high"].to_numpy(float), m15["low"].to_numpy(float)
    spread = m15["spread"].to_numpy(float) + extra_spread
    rows = []
    for r in signals.itertuples():
        start = int(np.searchsorted(t15, r.end.value, "left"))
        deadline = (r.end + expiry_bars * pd.Timedelta(minutes=minutes)).value
        state, filled_at, result = "pending", None, 0
        for b in range(start, len(t15)):
            if state == "pending":
                if t15[b] >= deadline:
                    state = "expired"
                    break
                touched = (l15[b] + spread[b] <= r.entry) if r.side == "buy" else (h15[b] >= r.entry)
                if touched:
                    state, filled_at = "open", b
                    # the order is filled inside this candle: only the stop is checked here
                    if (r.side == "buy" and l15[b] <= r.sl) or (r.side == "sell" and h15[b] + spread[b] >= r.sl):
                        state, result = "closed", -1
                        break
            else:
                if r.side == "buy":
                    stop, target = l15[b] <= r.sl, h15[b] >= r.tp
                else:
                    stop, target = h15[b] + spread[b] >= r.sl, l15[b] + spread[b] <= r.tp
                if stop or target:
                    state, result = "closed", (-1 if stop else 1)
                    break
        rows.append({"state": state if state != "pending" else "open", "result": result,
                     "filled": filled_at is not None})
    return pd.DataFrame(rows, index=signals.index)
