"""
Logika sinyal trading.
Strategi yang didukung:
- mtf_pullback : Multi-Timeframe (H1 bias + M30/M15 EMA 21/50/200 + ADX + pullback RSI 7 + ATR SL/TP) [rekomendasi]
- mtf_combo    : Kombinasi adaptif Session Box Breakout (05-08->16 UTC) + MTF Trend Pullback
- session_box  : Pre-London Session Box Breakout (05-08->16 UTC) dengan filter EMA 50/200 + Volume + H1 bias
- scalp        : Wilder RSI 14 + EMA 9/21 + momentum 2 candle + filter RSI ekstrem (legacy)
- donchian     : Donchian channel breakout 55 bar berbasis High/Low (BUY & SELL)
- ema_cross    : EMA 20/50 crossover pada bar persilangan (BUY & SELL)
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd

MIN_BARS = 300
STRATEGIES = ("mtf_pullback", "mtf_combo", "session_box", "scalp", "donchian", "ema_cross")


@dataclass
class Params:
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    mom_bars: int = 2
    mom_min: float = 0.001       # 0.1%
    rsi_extreme_hi: float = 80.0
    rsi_extreme_lo: float = 20.0
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # Parameter strategi ATR / MTF / Session Box
    adx_period: int = 14
    adx_min: float = 18.0
    rsi_pullback_period: int = 7
    rsi_pullback: float = 45.0
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 3.0
    box_start_hour: int = 5
    box_end_hour: int = 8
    box_trade_end_hour: int = 16
    box_vol_mult: float = 1.1
    box_tp_mult: float = 2.5
    max_atr_pct: float = 0.0


@dataclass
class SignalResult:
    action: str                  # BUY | SELL | EXIT | HOLD
    confidence: float
    rsi: float
    trend_up: bool
    detail: str = ""
    sl_distance: float = 0.0
    tp_distance: float = 0.0
    atr: float = 0.0
    adx: float = 0.0


def _extract_ohlcv(prices) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series | None]:
    """Ekstrak seri open, high, low, close, volume, dan timestamp (opsional)."""
    if isinstance(prices, pd.DataFrame):
        c = pd.Series(prices["close"].to_numpy(dtype=float))
        o = pd.Series(prices["open"].to_numpy(dtype=float)) if "open" in prices else c.copy()
        h = pd.Series(prices["high"].to_numpy(dtype=float)) if "high" in prices else c.copy()
        l = pd.Series(prices["low"].to_numpy(dtype=float)) if "low" in prices else c.copy()
        v = pd.Series(prices["volume"].to_numpy(dtype=float)) if "volume" in prices else pd.Series(np.ones(len(c)))
        ts = pd.Series(prices["ts"].to_numpy(dtype="int64")) if "ts" in prices else None
        return o, h, l, c, v, ts

    seq = list(prices)
    if seq and isinstance(seq[0], dict):
        c = pd.Series([float(x["close"]) for x in seq], dtype=float)
        o = pd.Series([float(x.get("open", x["close"])) for x in seq], dtype=float)
        h = pd.Series([float(x.get("high", x["close"])) for x in seq], dtype=float)
        l = pd.Series([float(x.get("low", x["close"])) for x in seq], dtype=float)
        v = pd.Series([float(x.get("volume", 1.0)) for x in seq], dtype=float)
        ts = pd.Series([int(x["ts"]) for x in seq], dtype="int64") if "ts" in seq[0] else None
        return o, h, l, c, v, ts

    c = pd.Series(np.asarray(seq, dtype=float))
    v = pd.Series(np.ones(len(c), dtype=float))
    return c.copy(), c.copy(), c.copy(), c, v, None


def _extract_ohlc(prices) -> tuple[pd.Series, pd.Series, pd.Series]:
    _, h, l, c, _, _ = _extract_ohlcv(prices)
    return h, l, c


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _wilder_rsi(s: pd.Series, period: int = 14) -> pd.Series:
    d = s.diff()
    gain = d.clip(lower=0.0).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    loss = (-d.clip(upper=0.0)).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(loss > 0.0, np.where(gain > 0.0, 100.0, 50.0))
    rsi.iloc[:period] = np.nan
    return rsi


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=close.index, dtype=float)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=close.index, dtype=float)
    atr_s = _atr(high, low, close, period).replace(0.0, np.nan)
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr_s
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr_s
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    adx_s = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return adx_s, pdi, mdi


def _htf_bias_series(
    open_s: pd.Series,
    high_s: pd.Series,
    low_s: pd.Series,
    close_s: pd.Series,
    ts_s: pd.Series | None,
) -> tuple[pd.Series, pd.Series]:
    """
    Hitung bias tren 1H secara kausal (tanpa lookahead).
    Hanya memakai bar 1H yang sudah SEPENUHNYA selesai sebelum jam bar saat ini (shift 1).
    Jika timestamp tidak tersedia atau bar sudah >= 1H, pakai proksi EMA 40/100 kausal.
    """
    if ts_s is None or len(ts_s) < 2:
        ef = _ema(close_s, 40)
        es = _ema(close_s, 100)
        return (ef > es) & (close_s > ef), (ef < es) & (close_s < ef)

    diffs = ts_s.diff().dropna()
    bar_sec = int(diffs.median()) if len(diffs) else 1800
    if bar_sec >= 3600:
        ef = _ema(close_s, 20)
        es = _ema(close_s, 50)
        return (ef > es) & (close_s > ef), (ef < es) & (close_s < ef)

    dt_idx = pd.to_datetime(ts_s, unit="s", utc=True)
    tmp = pd.DataFrame({
        "open": open_s.to_numpy(),
        "high": high_s.to_numpy(),
        "low": low_s.to_numpy(),
        "close": close_s.to_numpy(),
    }, index=dt_idx)
    h1 = tmp.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    if len(h1) < 25:
        ef = _ema(close_s, 40)
        es = _ema(close_s, 100)
        return (ef > es) & (close_s > ef), (ef < es) & (close_s < ef)

    h1_ef = _ema(h1["close"], 20)
    h1_es = _ema(h1["close"], 50)
    h1_bias = pd.DataFrame({
        "up": (h1_ef > h1_es) & (h1["close"] > h1_ef),
        "dn": (h1_ef < h1_es) & (h1["close"] < h1_ef),
    }, index=h1.index).shift(1)
    aligned = h1_bias.reindex(dt_idx, method="ffill")
    return (
        pd.Series(aligned["up"].to_numpy(), index=close_s.index).fillna(False).astype(bool),
        pd.Series(aligned["dn"].to_numpy(), index=close_s.index).fillna(False).astype(bool),
    )


def compute(close, p: Params = Params()) -> pd.DataFrame:
    """Hitung indikator + sinyal strategi legacy 'scalp' untuk setiap bar (tanpa lookahead)."""
    _, _, s = _extract_ohlc(close)
    rsi = _wilder_rsi(s, p.rsi_period)
    up = _ema(s, p.ema_fast) > _ema(s, p.ema_slow)
    mom = s - s.shift(p.mom_bars)
    mom = mom.where(mom.abs() >= s * p.mom_min, 0.0)
    hold = rsi.isna() | (rsi > p.rsi_extreme_hi) | (rsi < p.rsi_extreme_lo)
    score = (
        (rsi < p.rsi_oversold).astype(int)
        - (rsi > p.rsi_overbought).astype(int)
        + np.sign(mom.fillna(0.0)).astype(int)
    )
    sig = pd.Series(0, index=s.index, dtype="int8")
    sig[(~hold) & (score >= 1) & up] = 1
    sig[(~hold) & (score <= -1) & (~up)] = -1
    return pd.DataFrame({
        "close": s,
        "rsi": rsi,
        "trend_up": up,
        "momentum": mom,
        "score": score,
        "signal": sig,
    })


def signals_series(close, p: Params = Params()) -> np.ndarray:
    return compute(close, p)["signal"].values


def latest_signal(prices, p: Params = Params()):
    """Hasil kompatibel versi lama: (BUY/SELL/HOLD, confidence 0-1, rsi)."""
    if len(prices) < MIN_BARS:
        raise ValueError(f"Butuh minimal {MIN_BARS} candle, baru ada {len(prices)}")
    df = compute(prices[-MIN_BARS:], p)
    row = df.iloc[-1]
    name = {1: "BUY", -1: "SELL", 0: "HOLD"}[int(row.signal)]
    return name, abs(int(row.score)) / 2, float(row.rsi)


def compute_strategy_series(prices, strategy: str = "mtf_pullback", p: Params = Params()) -> pd.DataFrame:
    """Hitung sinyal vektor & jarak ATR SL/TP untuk strategi apa pun (untuk live & backtest)."""
    o, h, l, c, v, ts = _extract_ohlcv(prices)
    n = len(c)
    rsi14 = _wilder_rsi(c, p.rsi_period)
    atr_s = _atr(h, l, c, p.atr_period)
    adx_s, pdi, mdi = _adx(h, l, c, p.adx_period)

    sig = np.zeros(n, dtype=np.int8)
    sl_dist = np.zeros(n, dtype=float)
    tp_dist = np.zeros(n, dtype=float)
    trend_up = (_ema(c, 21) > _ema(c, 50)).to_numpy(dtype=bool)
    detail = np.full(n, "", dtype=object)

    if strategy == "scalp":
        df = compute(c, p)
        return pd.DataFrame({
            "close": c,
            "rsi": df["rsi"],
            "atr": atr_s,
            "adx": adx_s,
            "trend_up": df["trend_up"],
            "signal": df["signal"],
            "sl_distance": sl_dist,
            "tp_distance": tp_dist,
            "confidence": df["score"].abs() / 2.0,
            "detail": [f"score={int(sc)} mom={float(m):+.2f}" for sc, m in zip(df["score"], df["momentum"])],
        })

    if strategy == "donchian":
        upper = h.rolling(55).max().shift(1)
        lower = l.rolling(55).min().shift(1)
        break_up = ((c > upper) & (c.shift(1) <= upper.shift(1))).fillna(False).to_numpy()
        break_dn = ((c < lower) & (c.shift(1) >= lower.shift(1))).fillna(False).to_numpy()
        sig[break_up] = 1
        sig[break_dn] = -1
        mid_up = (c > (upper + lower) / 2.0).fillna(False).to_numpy()
        return pd.DataFrame({
            "close": c, "rsi": rsi14, "atr": atr_s, "adx": adx_s,
            "trend_up": mid_up, "signal": sig,
            "sl_distance": sl_dist, "tp_distance": tp_dist,
            "confidence": np.where(sig != 0, 1.0, 0.0),
            "detail": np.where(sig == 1, "donchian_break_up(55)", np.where(sig == -1, "donchian_break_dn(55)", "donchian_wait(55)")),
        })

    if strategy == "ema_cross":
        fast = _ema(c, 20)
        slow = _ema(c, 50)
        cross_up = ((fast > slow) & (fast.shift(1) <= slow.shift(1))).fillna(False).to_numpy()
        cross_dn = ((fast < slow) & (fast.shift(1) >= slow.shift(1))).fillna(False).to_numpy()
        sig[cross_up] = 1
        sig[cross_dn] = -1
        is_up = (fast > slow).to_numpy(dtype=bool)
        return pd.DataFrame({
            "close": c, "rsi": rsi14, "atr": atr_s, "adx": adx_s,
            "trend_up": is_up, "signal": sig,
            "sl_distance": sl_dist, "tp_distance": tp_dist,
            "confidence": np.where(sig != 0, 1.0, 0.0),
            "detail": [f"ema20={f:.2f} ema50={sl:.2f}" for f, sl in zip(fast, slow)],
        })

    if strategy not in ("mtf_pullback", "session_box", "mtf_combo"):
        raise ValueError(f"Strategi tidak dikenal: {strategy}. Pilih: {', '.join(STRATEGIES)}")

    ef = _ema(c, 21)
    em = _ema(c, 50)
    el = _ema(c, 200)
    h1_up, h1_dn = _htf_bias_series(o, h, l, c, ts)
    atr_np = atr_s.to_numpy(dtype=float)
    vol_cap_ok = np.ones(n, dtype=bool)
    if p.max_atr_pct > 0:
        atr_pct = atr_np / np.where(c.to_numpy() > 0, c.to_numpy(), np.nan)
        vol_cap_ok = np.isfinite(atr_pct) & (atr_pct <= p.max_atr_pct)

    # 1. Komponen MTF Trend Pullback
    if strategy in ("mtf_pullback", "mtf_combo"):
        rsi_pb = _wilder_rsi(c, p.rsi_pullback_period)
        t_up = h1_up & (ef > em) & (em > el) & (c > em)
        t_dn = h1_dn & (ef < em) & (em < el) & (c < em)
        buy_pb = (
            t_up & (adx_s >= p.adx_min) & (pdi > mdi)
            & (rsi_pb.shift(1) < p.rsi_pullback) & (rsi_pb >= p.rsi_pullback)
            & (c > o)
        ).fillna(False).to_numpy() & vol_cap_ok
        sell_pb = (
            t_dn & (adx_s >= p.adx_min) & (mdi > pdi)
            & (rsi_pb.shift(1) > (100.0 - p.rsi_pullback)) & (rsi_pb <= (100.0 - p.rsi_pullback))
            & (c < o)
        ).fillna(False).to_numpy() & vol_cap_ok
        pb_mask = buy_pb | sell_pb
        sig[buy_pb] = 1
        sig[sell_pb] = -1
        sl_dist[pb_mask] = atr_np[pb_mask] * p.atr_sl_mult
        tp_dist[pb_mask] = atr_np[pb_mask] * p.atr_tp_mult
        for idx in np.flatnonzero(pb_mask):
            detail[idx] = f"mtf_pb adx={adx_s.iloc[idx]:.1f} rsi{p.rsi_pullback_period}={rsi_pb.iloc[idx]:.1f} atr={atr_np[idx]:.1f}"

    # 2. Komponen Pre-London Session Box Breakout
    if strategy in ("session_box", "mtf_combo") and ts is not None:
        dt = pd.to_datetime(ts, unit="s", utc=True)
        hrs = dt.dt.hour.to_numpy()
        dates = dt.dt.date.to_numpy()
        vma = v.rolling(20).mean().to_numpy(dtype=float)
        h_np, l_np, c_np, v_np = h.to_numpy(), l.to_numpy(), c.to_numpy(), v.to_numpy()
        em_np, el_np = em.to_numpy(), el.to_numpy()
        h1_up_np, h1_dn_np = h1_up.to_numpy(dtype=bool), h1_dn.to_numpy(dtype=bool)

        cur_date = None
        hi_acc, lo_acc = -np.inf, np.inf
        box_bars = 0
        traded_today = False
        for i in range(n):
            d_i = dates[i]
            hr = hrs[i]
            if d_i != cur_date:
                cur_date = d_i
                hi_acc, lo_acc = -np.inf, np.inf
                box_bars = 0
                traded_today = False
            if p.box_start_hour <= hr < p.box_end_hour:
                hi_acc = max(hi_acc, h_np[i])
                lo_acc = min(lo_acc, l_np[i])
                box_bars += 1
            elif box_bars >= 2 and p.box_end_hour <= hr < p.box_trade_end_hour and not traded_today:
                av = atr_np[i]
                if not (np.isfinite(av) and av > 0 and np.isfinite(vma[i]) and vol_cap_ok[i]):
                    continue
                box_rng = hi_acc - lo_acc
                if box_rng < 0.8 * av or box_rng > 4.5 * av:
                    continue
                if v_np[i] < p.box_vol_mult * vma[i]:
                    continue

                up_ok = (c_np[i] > hi_acc) and (c_np[i] > el_np[i]) and (em_np[i] > el_np[i]) and h1_up_np[i]
                dn_ok = (c_np[i] < lo_acc) and (c_np[i] < el_np[i]) and (em_np[i] < el_np[i]) and h1_dn_np[i]
                if up_ok or dn_ok:
                    traded_today = True
                    if sig[i] == 0:
                        sig[i] = 1 if up_ok else -1
                        sl_dist[i] = av * p.atr_sl_mult
                        tp_dist[i] = av * p.box_tp_mult
                        detail[i] = f"session_box({p.box_start_hour}-{p.box_end_hour}) rng={box_rng:.1f} atr={av:.1f}"

    trend_up = (h1_up & (em > el)).fillna(False).to_numpy(dtype=bool)
    for i in range(n):
        if detail[i] == "":
            av = atr_np[i] if np.isfinite(atr_np[i]) else 0.0
            adxv = float(adx_s.iloc[i]) if pd.notna(adx_s.iloc[i]) else 0.0
            detail[i] = f"{strategy}_wait h1={'UP' if h1_up.iloc[i] else ('DN' if h1_dn.iloc[i] else 'FLAT')} adx={adxv:.1f} atr={av:.1f}"

    return pd.DataFrame({
        "close": c,
        "rsi": rsi14,
        "atr": atr_s,
        "adx": adx_s,
        "trend_up": trend_up,
        "signal": sig,
        "sl_distance": sl_dist,
        "tp_distance": tp_dist,
        "confidence": np.where(sig != 0, 1.0, 0.0),
        "detail": detail,
    })


def evaluate_signal(prices, strategy: str = "mtf_pullback", p: Params = Params()) -> SignalResult:
    """Evaluasi sinyal terbaru sesuai strategi yang dipilih."""
    if len(prices) < MIN_BARS:
        raise ValueError(f"Butuh minimal {MIN_BARS} candle, baru ada {len(prices)}")

    # Untuk MTF (H1 bias di M15/M30), pakai hingga 600 bar terakhir jika tersedia agar EMA50 H1 & EMA200 hangat
    window = list(prices[-max(MIN_BARS, 600):]) if not isinstance(prices, pd.DataFrame) else prices.iloc[-max(MIN_BARS, 600):]
    df = compute_strategy_series(window, strategy=strategy, p=p)
    row = df.iloc[-1]
    action = {1: "BUY", -1: "SELL", 0: "HOLD"}[int(row.signal)]
    return SignalResult(
        action=action,
        confidence=float(row.confidence),
        rsi=float(row.rsi) if pd.notna(row.rsi) else float("nan"),
        trend_up=bool(row.trend_up),
        detail=str(row.detail),
        sl_distance=float(row.sl_distance) if pd.notna(row.sl_distance) else 0.0,
        tp_distance=float(row.tp_distance) if pd.notna(row.tp_distance) else 0.0,
        atr=float(row.atr) if pd.notna(row.atr) else 0.0,
        adx=float(row.adx) if pd.notna(row.adx) else 0.0,
    )
