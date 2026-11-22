"""
RISK GUARD - penjaga risiko pra-trade untuk algo trading 24/7 (BTC dll).
Wajib ada stop loss, batas rugi per trade, batas rugi harian, batas trade per hari,
minimal reward:risk, harga harus segar, jeda setelah rugi beruntun, dan kill-switch drawdown.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    max_loss_per_trade_usd: float = 0.0
    max_daily_loss_pct: float = 0.05
    max_trades_per_day: int = 10
    max_consecutive_losses: int = 4
    pause_minutes_after_streak: int = 60
    max_drawdown_pct: float = 0.15
    min_reward_risk: float = 1.5
    max_leverage: float = 10.0
    max_open_positions: int = 1
    require_stop: bool = True
    max_price_age_sec: int = 150
    min_notional_usd: float = 5.0


@dataclass
class Verdict:
    ok: bool
    qty: float = 0.0
    notional: float = 0.0
    risk_usd: float = 0.0
    rr: float = None
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class RiskGuard:
    def __init__(self, cfg: RiskConfig = None, state_path=None):
        self.cfg = cfg or RiskConfig()
        self.memory = state_path == ":memory:"
        self.path = None if self.memory else (
            Path(state_path) if state_path else Path(__file__).resolve().parent.parent / "results" / "risk_state.json"
        )
        self.s = self._default() if self.memory else self._load()

    @staticmethod
    def _default():
        return {
            "day": None,
            "day_start_equity": None,
            "trades_today": 0,
            "pnl_today": 0.0,
            "consec_losses": 0,
            "pause_until": None,
            "peak_equity": None,
            "killed": False,
            "kill_reason": "",
        }

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            s = self._default()
            s.update({k: data[k] for k in s if k in data})
            return s
        except (OSError, ValueError):
            return self._default()

    def _save(self):
        if self.memory:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    @classmethod
    def _now(cls, now):
        return cls._as_utc(now) if now is not None else datetime.now(UTC)

    def _roll_day(self, now, equity):
        today = self._now(now).date().isoformat()
        if self.s["day"] != today:
            self.s.update(day=today, trades_today=0, pnl_today=0.0, day_start_equity=equity)
        if self.s["day_start_equity"] is None:
            self.s["day_start_equity"] = equity
        if self.s["peak_equity"] is None:
            self.s["peak_equity"] = equity

    def size_position(self, equity, entry, stop):
        per_unit = abs(entry - stop)
        if per_unit <= 0:
            raise ValueError("Jarak antara entry dan stop harus lebih besar dari 0.")
        c = self.cfg
        risk_usd = equity * c.risk_per_trade
        if c.max_loss_per_trade_usd > 0:
            risk_usd = min(risk_usd, c.max_loss_per_trade_usd)
        qty = risk_usd / per_unit
        notes = []
        cap = equity * c.max_leverage / entry
        if qty > cap:
            qty = cap
            notes.append(f"ukuran dipangkas ke batas leverage {c.max_leverage:g}x")
        return qty, qty * entry, qty * per_unit, notes

    def check_actual_risk(self, actual_risk_usd: float, equity: float, now=None) -> list:
        """Validasi risiko aktual setelah pembulatan lot minimum/fixed_lot MT5."""
        c, now = self.cfg, self._now(now)
        self._roll_day(now, equity)
        reasons = []
        if c.max_loss_per_trade_usd > 0 and actual_risk_usd > c.max_loss_per_trade_usd + 1e-9:
            reasons.append(
                f"risiko aktual lot minimum ${actual_risk_usd:.2f} melebihi batas per trade ${c.max_loss_per_trade_usd:.2f}"
            )
        limit = c.max_daily_loss_pct * self.s["day_start_equity"]
        if self.s["pnl_today"] - actual_risk_usd < -limit:
            reasons.append(
                f"batas rugi harian ${limit:.2f}: rugi hari ini ${-self.s['pnl_today']:.2f} "
                f"+ risiko aktual trade ini ${actual_risk_usd:.2f} melewati batas"
            )
        return reasons

    def check_entry(self, side, entry, stop, target=None, equity=0.0, price_time=None, now=None, open_positions=0):
        c, now = self.cfg, self._now(now)
        v = Verdict(ok=False)
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            v.reasons.append("sisi harus BUY atau SELL")
            return v
        if not (entry and entry > 0 and equity and equity > 0):
            v.reasons.append("harga entry dan saldo harus > 0")
            return v
        self._roll_day(now, equity)
        long = side == "BUY"

        if self.s["killed"]:
            v.reasons.append(f"KILL-SWITCH aktif: {self.s['kill_reason']} (reset manual dengan: python main.py reset-kill)")
        if self.s["pause_until"]:
            pause_dt = self._as_utc(datetime.fromisoformat(self.s["pause_until"]))
            if now < pause_dt:
                v.reasons.append(f"jeda setelah rugi beruntun sampai {self.s['pause_until']}")
        if price_time is not None:
            age = (now - self._as_utc(price_time)).total_seconds()
            if age > c.max_price_age_sec:
                v.reasons.append(f"harga basi ({age:.0f} detik, batas {c.max_price_age_sec})")
        if open_positions >= c.max_open_positions:
            v.reasons.append(f"sudah ada {open_positions} posisi terbuka (maksimal {c.max_open_positions})")
        if self.s["trades_today"] >= c.max_trades_per_day:
            v.reasons.append(f"batas {c.max_trades_per_day} trade per hari sudah tercapai")

        if stop is None:
            if c.require_stop:
                v.reasons.append("stop loss wajib ada")
        elif (long and stop >= entry) or (not long and stop <= entry):
            v.reasons.append("stop berada di sisi yang salah dari entry")
        if target is not None and ((long and target <= entry) or (not long and target >= entry)):
            v.reasons.append("target berada di sisi yang salah dari entry")

        if stop is not None and not any("sisi yang salah" in r for r in v.reasons):
            qty, notional, risk_usd, notes = self.size_position(equity, entry, stop)
            v.qty, v.notional, v.risk_usd = qty, notional, risk_usd
            v.warnings += notes
            if notional < c.min_notional_usd:
                v.reasons.append(f"nilai posisi ${notional:.2f} di bawah minimum ${c.min_notional_usd:g}")
            limit = c.max_daily_loss_pct * self.s["day_start_equity"]
            if self.s["pnl_today"] - risk_usd < -limit:
                v.reasons.append(
                    f"batas rugi harian ${limit:.2f}: rugi hari ini ${-self.s['pnl_today']:.2f} "
                    f"+ risiko trade ini ${risk_usd:.2f} melewati batas"
                )
            if target is not None:
                v.rr = abs(target - entry) / abs(entry - stop)
                if v.rr < c.min_reward_risk:
                    v.reasons.append(f"reward:risk {v.rr:.2f} di bawah minimum {c.min_reward_risk:g}")
            else:
                v.warnings.append("tidak ada target, reward:risk tidak bisa diperiksa")
        v.ok = not v.reasons
        return v

    def record_entry(self, now=None):
        now = self._now(now)
        self._roll_day(now, self.s["day_start_equity"] or 0)
        self.s["trades_today"] += 1
        self._save()

    def record_close(self, pnl_usd, equity_after, now=None):
        c, now, events = self.cfg, self._now(now), []
        self._roll_day(now, equity_after - pnl_usd)
        self.s["pnl_today"] += pnl_usd
        if pnl_usd < 0:
            self.s["consec_losses"] += 1
            if self.s["consec_losses"] >= c.max_consecutive_losses:
                until = now + timedelta(minutes=c.pause_minutes_after_streak)
                self.s["pause_until"] = until.isoformat()
                events.append(f"{self.s['consec_losses']} rugi beruntun -> bot jeda sampai {until.isoformat()}")
                self.s["consec_losses"] = 0
        else:
            self.s["consec_losses"] = 0
        self.s["peak_equity"] = max(self.s["peak_equity"] or equity_after, equity_after)
        dd = 1 - equity_after / self.s["peak_equity"] if self.s["peak_equity"] > 0 else 0.0
        if dd >= c.max_drawdown_pct and not self.s["killed"]:
            self.s["killed"] = True
            self.s["kill_reason"] = f"drawdown {dd*100:.1f} persen dari puncak (batas {c.max_drawdown_pct*100:g})"
            events.append("KILL-SWITCH: " + self.s["kill_reason"])
        self._save()
        return events

    def reset_kill(self, new_peak=None):
        self.s["killed"], self.s["kill_reason"] = False, ""
        if new_peak:
            self.s["peak_equity"] = new_peak
        self._save()

    def status(self):
        return dict(self.s)