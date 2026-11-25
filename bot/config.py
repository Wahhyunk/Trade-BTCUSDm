"""Konfigurasi terpusat dari file .env untuk MT5 Algo Trading Bot."""
import math
import os
from dataclasses import dataclass
from pathlib import Path

from bot.risk_guard import RiskConfig
from bot.strategy import STRATEGIES, Params

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None) -> dict:
    p = Path(path) if path else ROOT / ".env"
    env = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _bool(v, default=False) -> bool:
    if v is None or v == "":
        return default
    value = str(v).strip().lower()
    if value in ("1", "true", "yes", "ya", "on"):
        return True
    if value in ("0", "false", "no", "tidak", "off"):
        return False
    raise ValueError(f"Nilai boolean konfigurasi tidak valid: {v!r}.")


@dataclass
class Settings:
    # --- MT5 ---
    symbol: str = "BTCUSDT"
    timeframe: str = "M30"
    poll_seconds: float = 5.0
    magic: int = 202611
    deviation: int = 500
    max_spread_points: int = 2500
    fixed_lot: float = 0.0
    allow_min_lot: bool = True
    mt5_path: str = ""

    # --- Strategi ---
    strategy: str = "mtf_pullback"   # mtf_pullback | mtf_combo | session_box | scalp | donchian | ema_cross
    tp_pct: float = 0.015            # Fallback % TP (jika strategi tidak pakai ATR)
    sl_pct: float = 0.005            # Fallback % SL (jika strategi tidak pakai ATR)
    cooldown_bars: int = 2
    max_hold_bars: int = 0
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    mom_bars: int = 2
    mom_min: float = 0.001           # 0.1%
    adx_min: float = 18.0
    rsi_pullback: float = 45.0
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 3.0
    be_trigger_r: float = 1.0
    trail_atr_mult: float = 0.0
    box_start_hour: int = 5
    box_end_hour: int = 8
    box_trade_end_hour: int = 16
    box_vol_mult: float = 1.1
    box_tp_mult: float = 2.5
    max_atr_pct: float = 0.0

    # --- Risk Guard ---
    guard_enabled: bool = True
    risk_per_trade: float = 0.02
    max_daily_loss_pct: float = 0.05
    max_trades_per_day: int = 10
    max_consecutive_losses: int = 4
    pause_minutes: int = 60
    max_drawdown_pct: float = 0.15
    min_rr: float = 1.5
    max_leverage: float = 10.0
    max_price_age_sec: int = 150

    # --- Penyimpanan lokal ---
    results_dir: Path = ROOT / "results"

    def __post_init__(self):
        float_values = (
            self.poll_seconds, self.fixed_lot, self.tp_pct, self.sl_pct, self.mom_min,
            self.adx_min, self.rsi_pullback, self.atr_sl_mult, self.atr_tp_mult,
            self.be_trigger_r, self.trail_atr_mult, self.box_vol_mult, self.box_tp_mult,
            self.max_atr_pct, self.risk_per_trade, self.max_daily_loss_pct,
            self.max_drawdown_pct, self.min_rr, self.max_leverage,
        )
        if not all(math.isfinite(value) for value in float_values):
            raise ValueError("Nilai konfigurasi numerik harus finite (bukan NaN atau infinity).")
        if self.sl_pct <= 0:
            raise ValueError("SL_PCT harus lebih besar dari 0 (misal 0.005 untuk 0.5%).")
        if self.tp_pct < 0:
            raise ValueError("TP_PCT tidak boleh negatif (isi 0 jika tanpa fixed TP).")
        if self.tp_pct > 0.5 or self.sl_pct > 0.5:
            raise ValueError("TP_PCT / SL_PCT > 50%. Gunakan format desimal (misal 0.015 untuk 1.5%).")
        if self.poll_seconds <= 0:
            raise ValueError("POLL_SECONDS harus lebih besar dari 0.")
        if self.max_spread_points < 0:
            raise ValueError("MT5_MAX_SPREAD tidak boleh negatif.")
        if self.strategy not in STRATEGIES:
            raise ValueError(f"STRATEGY harus salah satu dari: {', '.join(STRATEGIES)}.")
        if self.rsi_period <= 0 or self.ema_fast <= 0 or self.ema_slow <= 0 or self.atr_period <= 0:
            raise ValueError("RSI_PERIOD, EMA_FAST, EMA_SLOW, dan ATR_PERIOD harus lebih besar dari 0.")
        if self.mom_bars <= 0 or self.mom_min < 0:
            raise ValueError("MOM_BARS harus lebih besar dari 0 dan MOM_MIN tidak boleh negatif.")
        if self.atr_sl_mult <= 0 or self.atr_tp_mult < 0 or self.box_tp_mult < 0:
            raise ValueError("ATR_SL_MULT harus > 0 dan ATR_TP_MULT / BOX_TP_MULT tidak boleh negatif.")
        if self.be_trigger_r < 0 or self.trail_atr_mult < 0 or self.max_atr_pct < 0:
            raise ValueError("BE_TRIGGER_R, TRAIL_ATR_MULT, dan MAX_ATR_PCT tidak boleh negatif.")
        if not 0 < self.rsi_pullback < 50:
            raise ValueError("RSI_PULLBACK harus antara 0 dan 50 (misal 45).")
        if not (0 <= self.box_start_hour < self.box_end_hour <= self.box_trade_end_hour <= 24):
            raise ValueError("Jam Session Box tidak valid (0 <= start < end <= trade_end <= 24).")
        if self.cooldown_bars < 0 or self.max_hold_bars < 0:
            raise ValueError("COOLDOWN_BARS dan MAX_HOLD_BARS tidak boleh negatif.")
        if not 0 < self.risk_per_trade <= 1:
            raise ValueError("RISK_PER_TRADE harus lebih besar dari 0 dan paling besar 1.")
        if not 0 <= self.max_daily_loss_pct <= 1 or not 0 <= self.max_drawdown_pct <= 1:
            raise ValueError("Batas rugi harian dan drawdown harus berada antara 0 dan 1.")
        if self.max_trades_per_day <= 0 or self.max_consecutive_losses <= 0:
            raise ValueError("Batas trade harian dan rugi beruntun harus lebih besar dari 0.")
        if self.pause_minutes < 0 or self.max_leverage <= 0 or self.max_price_age_sec <= 0:
            raise ValueError("PAUSE_MINUTES tidak boleh negatif; leverage dan umur harga harus lebih besar dari 0.")
        if self.min_rr < 0 or self.deviation < 0:
            raise ValueError("MIN_REWARD_RISK dan MT5_DEVIATION tidak boleh negatif.")
        if self.fixed_lot < 0:
            raise ValueError("MT5_FIXED_LOT tidak boleh negatif.")

    def uses_atr_stops(self) -> bool:
        return self.strategy in ("mtf_pullback", "mtf_combo", "session_box")

    def config_warnings(self) -> list[str]:
        warns = []
        if self.guard_enabled:
            if self.uses_atr_stops() and self.atr_tp_mult > 0:
                rr = self.atr_tp_mult / self.atr_sl_mult
                if rr < self.min_rr:
                    warns.append(
                        f"Rasio ATR TP/SL ({rr:.2f}) < MIN_REWARD_RISK ({self.min_rr}); sinyal entry dapat ditolak RiskGuard."
                    )
            elif not self.uses_atr_stops() and self.tp_pct > 0:
                rr = self.tp_pct / self.sl_pct
                if rr < self.min_rr:
                    warns.append(
                        f"Rasio TP/SL ({rr:.2f}) < MIN_REWARD_RISK ({self.min_rr}); semua sinyal entry akan ditolak RiskGuard."
                    )
        if self.deviation < 100:
            warns.append(f"MT5_DEVIATION={self.deviation} terlalu kecil untuk BTCUSDT dan berisiko memicu requote.")
        return warns

    @property
    def guard_state_path(self) -> Path:
        return self.results_dir / "risk_state.json"

    @property
    def trades_csv_path(self) -> Path:
        return self.results_dir / "trades.csv"

    @property
    def log_path(self) -> Path:
        return self.results_dir / "bot.log"

    @classmethod
    def load(cls, env_path=None, **overrides) -> "Settings":
        e = load_env(env_path)
        g = lambda k, d: os.environ.get(k, e.get(k, d))

        raw = dict(
            symbol=str(g("MT5_SYMBOL", cls.symbol)).strip(),
            timeframe=str(g("MT5_TIMEFRAME", cls.timeframe)).strip().upper(),
            poll_seconds=float(g("POLL_SECONDS", cls.poll_seconds)),
            magic=int(g("MT5_MAGIC", cls.magic)),
            deviation=int(g("MT5_DEVIATION", cls.deviation)),
            max_spread_points=int(g("MT5_MAX_SPREAD", cls.max_spread_points)),
            fixed_lot=float(g("MT5_FIXED_LOT", cls.fixed_lot)),
            allow_min_lot=_bool(g("MT5_ALLOW_MIN_LOT", "true"), True),
            mt5_path=str(g("MT5_PATH", cls.mt5_path)).strip(),
            strategy=str(g("STRATEGY", cls.strategy)).strip().lower(),
            tp_pct=float(g("TP_PCT", cls.tp_pct)),
            sl_pct=float(g("SL_PCT", cls.sl_pct)),
            cooldown_bars=int(g("COOLDOWN_BARS", cls.cooldown_bars)),
            max_hold_bars=int(g("MAX_HOLD_BARS", cls.max_hold_bars)),
            rsi_period=int(g("RSI_PERIOD", cls.rsi_period)),
            ema_fast=int(g("EMA_FAST", cls.ema_fast)),
            ema_slow=int(g("EMA_SLOW", cls.ema_slow)),
            mom_bars=int(g("MOM_BARS", cls.mom_bars)),
            mom_min=float(g("MOM_MIN", cls.mom_min)),
            adx_min=float(g("ADX_MIN", cls.adx_min)),
            rsi_pullback=float(g("RSI_PULLBACK", cls.rsi_pullback)),
            atr_period=int(g("ATR_PERIOD", cls.atr_period)),
            atr_sl_mult=float(g("ATR_SL_MULT", cls.atr_sl_mult)),
            atr_tp_mult=float(g("ATR_TP_MULT", cls.atr_tp_mult)),
            be_trigger_r=float(g("BE_TRIGGER_R", cls.be_trigger_r)),
            trail_atr_mult=float(g("TRAIL_ATR_MULT", cls.trail_atr_mult)),
            box_start_hour=int(g("BOX_START_HOUR", cls.box_start_hour)),
            box_end_hour=int(g("BOX_END_HOUR", cls.box_end_hour)),
            box_trade_end_hour=int(g("BOX_TRADE_END_HOUR", cls.box_trade_end_hour)),
            box_vol_mult=float(g("BOX_VOL_MULT", cls.box_vol_mult)),
            box_tp_mult=float(g("BOX_TP_MULT", cls.box_tp_mult)),
            max_atr_pct=float(g("MAX_ATR_PCT", cls.max_atr_pct)),
            guard_enabled=_bool(g("GUARD_ENABLED", "true"), True),
            risk_per_trade=float(g("RISK_PER_TRADE", cls.risk_per_trade)),
            max_daily_loss_pct=float(g("MAX_DAILY_LOSS_PCT", cls.max_daily_loss_pct)),
            max_trades_per_day=int(g("MAX_TRADES_PER_DAY", cls.max_trades_per_day)),
            max_consecutive_losses=int(g("MAX_CONSECUTIVE_LOSSES", cls.max_consecutive_losses)),
            pause_minutes=int(g("PAUSE_MINUTES", cls.pause_minutes)),
            max_drawdown_pct=float(g("MAX_DRAWDOWN_PCT", cls.max_drawdown_pct)),
            min_rr=float(g("MIN_REWARD_RISK", cls.min_rr)),
            max_leverage=float(g("MAX_LEVERAGE", cls.max_leverage)),
            max_price_age_sec=int(g("MAX_PRICE_AGE_SEC", cls.max_price_age_sec)),
        )
        raw.update(overrides)
        return cls(**raw)

    def params(self) -> Params:
        return Params(
            rsi_period=self.rsi_period,
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            mom_bars=self.mom_bars,
            mom_min=self.mom_min,
            adx_min=self.adx_min,
            rsi_pullback=self.rsi_pullback,
            atr_period=self.atr_period,
            atr_sl_mult=self.atr_sl_mult,
            atr_tp_mult=self.atr_tp_mult,
            box_start_hour=self.box_start_hour,
            box_end_hour=self.box_end_hour,
            box_trade_end_hour=self.box_trade_end_hour,
            box_vol_mult=self.box_vol_mult,
            box_tp_mult=self.box_tp_mult,
            max_atr_pct=self.max_atr_pct,
        )

    def risk_config(self) -> RiskConfig:
        if not self.guard_enabled:
            return RiskConfig(
                risk_per_trade=self.risk_per_trade,
                max_daily_loss_pct=1e9,
                max_trades_per_day=10**9,
                max_consecutive_losses=10**9,
                max_drawdown_pct=1e9,
                min_reward_risk=0.0,
                max_leverage=1e9,
                max_price_age_sec=10**12,
                min_notional_usd=0.0,
            )
        return RiskConfig(
            risk_per_trade=self.risk_per_trade,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_trades_per_day=self.max_trades_per_day,
            max_consecutive_losses=self.max_consecutive_losses,
            pause_minutes_after_streak=self.pause_minutes,
            max_drawdown_pct=self.max_drawdown_pct,
            min_reward_risk=self.min_rr,
            max_leverage=self.max_leverage,
            max_price_age_sec=self.max_price_age_sec,
        )
