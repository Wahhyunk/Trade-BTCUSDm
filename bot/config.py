"""Konfigurasi terpusat dari file .env untuk MT5 Algo Trading Bot."""
import os
from dataclasses import dataclass
from pathlib import Path

from bot.risk_guard import RiskConfig
from bot.strategy import Params

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
    return str(v).strip().lower() in ("1", "true", "yes", "ya", "on")


@dataclass
class Settings:
    # --- MT5 ---
    symbol: str = "BTCUSDT"
    timeframe: str = "M1"
    poll_seconds: float = 5.0
    magic: int = 202611
    deviation: int = 500
    max_spread_points: int = 500
    fixed_lot: float = 0.0
    allow_min_lot: bool = True
    mt5_path: str = ""

    # --- Strategi ---
    strategy: str = "scalp"          # scalp | donchian | ema_cross
    tp_pct: float = 0.015            # 1.5%
    sl_pct: float = 0.005            # 0.5%
    cooldown_bars: int = 5
    max_hold_bars: int = 0
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    mom_bars: int = 2
    mom_min: float = 0.001           # 0.1%

    # --- Risk Guard ---
    guard_enabled: bool = True
    risk_per_trade: float = 0.01
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

    def config_warnings(self) -> list[str]:
        warns = []
        if self.guard_enabled and self.tp_pct > 0:
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
            tp_pct=self.tp_pct,
            sl_pct=self.sl_pct,
            cooldown=self.cooldown_bars,
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