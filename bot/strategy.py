"""
Logika sinyal trading.
- scalp     : Wilder RSI 14 + EMA 9/21 + momentum 2 candle + filter RSI ekstrem (strategi utama bot)
- donchian  : Donchian channel breakout 55 bar berbasis High/Low (BUY & SELL)
- ema_cross : EMA 20/50 crossover pada bar persilangan (BUY & SELL)
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd

MIN_BARS = 300


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
    tp_pct: float = 0.015
    sl_pct: float = 0.005
    cooldown: int = 5


@dataclass
class SignalResult:
    action: str                  # BUY | SELL | EXIT | HOLD
    confidence: float
    rsi: float
    trend_up: bool
    detail: str = ""


def _extract_ohlc(prices) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Ekstrak seri high, low, close dari list dict candle, DataFrame, atau array close."""
    if isinstance(prices, pd.DataFrame):
        c = pd.Series(prices["close"].to_numpy(dtype=float))
        h = pd.Series(prices["high"].to_numpy(dtype=float)) if "high" in prices else c.copy()
        l = pd.Series(prices["low"].to_numpy(dtype=float)) if "low" in prices else c.copy()
        return h, l, c

    seq = list(prices)
    if seq and isinstance(seq[0], dict):
        c = pd.Series([float(x["close"]) for x in seq], dtype=float)
        h = pd.Series([float(x.get("high", x["close"])) for x in seq], dtype=float)
        l = pd.Series([float(x.get("low", x["close"])) for x in seq], dtype=float)
        return h, l, c

    c = pd.Series(np.asarray(seq, dtype=float))
    return c.copy(), c.copy(), c


def compute(close, p: Params = Params()) -> pd.DataFrame:
    """Hitung semua indikator + sinyal untuk setiap bar (tanpa lookahead)."""
    _, _, s = _extract_ohlc(close)
    d = s.diff()
    gain = d.clip(lower=0.0).ewm(alpha=1.0 / p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()
    loss = (-d.clip(upper=0.0)).ewm(alpha=1.0 / p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()

    rs = gain / loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(loss > 0.0, np.where(gain > 0.0, 100.0, 50.0))
    rsi.iloc[: p.rsi_period] = np.nan

    up = s.ewm(span=p.ema_fast, adjust=False).mean() > s.ewm(span=p.ema_slow, adjust=False).mean()
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


def evaluate_signal(prices, strategy: str = "scalp", p: Params = Params()) -> SignalResult:
    """Evaluasi sinyal terbaru sesuai strategi yang dipilih."""
    if len(prices) < MIN_BARS:
        raise ValueError(f"Butuh minimal {MIN_BARS} candle, baru ada {len(prices)}")

    window = list(prices[-MIN_BARS:])
    high, low, close = _extract_ohlc(window)
    df = compute(close, p)
    row = df.iloc[-1]
    rsi_val = float(row.rsi) if pd.notna(row.rsi) else float("nan")
    trend_up = bool(row.trend_up)

    if strategy == "scalp":
        action = {1: "BUY", -1: "SELL", 0: "HOLD"}[int(row.signal)]
        conf = abs(int(row.score)) / 2.0
        return SignalResult(
            action=action,
            confidence=conf,
            rsi=rsi_val,
            trend_up=trend_up,
            detail=f"score={int(row.score)} mom={float(row.momentum):+.2f}",
        )

    if strategy == "donchian":
        upper = high.rolling(55).max().shift(1)
        lower = low.rolling(55).min().shift(1)
        break_up = bool(close.iloc[-1] > upper.iloc[-1] and close.iloc[-2] <= upper.iloc[-2])
        break_dn = bool(close.iloc[-1] < lower.iloc[-1] and close.iloc[-2] >= lower.iloc[-2])
        if break_up:
            return SignalResult(action="BUY", confidence=1.0, rsi=rsi_val, trend_up=True, detail="donchian_break_up(55)")
        if break_dn:
            return SignalResult(action="SELL", confidence=1.0, rsi=rsi_val, trend_up=False, detail="donchian_break_dn(55)")
        return SignalResult(
            action="HOLD",
            confidence=0.0,
            rsi=rsi_val,
            trend_up=bool(close.iloc[-1] > (upper.iloc[-1] + lower.iloc[-1]) / 2.0),
            detail="donchian_wait(55)",
        )

    if strategy == "ema_cross":
        fast = close.ewm(span=20, adjust=False).mean()
        slow = close.ewm(span=50, adjust=False).mean()
        cross_up = bool(fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2])
        cross_dn = bool(fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2])
        is_up = bool(fast.iloc[-1] > slow.iloc[-1])
        detail = f"ema20={fast.iloc[-1]:.2f} ema50={slow.iloc[-1]:.2f}"
        if cross_up:
            return SignalResult(action="BUY", confidence=1.0, rsi=rsi_val, trend_up=True, detail=detail)
        if cross_dn:
            return SignalResult(action="SELL", confidence=1.0, rsi=rsi_val, trend_up=False, detail=detail)
        return SignalResult(action="HOLD", confidence=0.0, rsi=rsi_val, trend_up=is_up, detail=detail)

    raise ValueError(f"Strategi tidak dikenal: {strategy}. Pilih: scalp, donchian, ema_cross")