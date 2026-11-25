"""
RISK GUARD - penjaga risiko pra-trade untuk algo trading 24/7 (BTC dll).
Wajib ada stop loss, batas rugi per trade, batas rugi harian, batas trade per hari,
minimal reward:risk, harga harus segar, jeda setelah rugi beruntun, dan kill-switch drawdown.
"""
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc


class RiskStateError(RuntimeError):
    pass


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
            "last_deal_time": None,
            "last_deal_tickets": [],
        }

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state harus berupa objek JSON")
            required = set(self._default()) - {"last_deal_time", "last_deal_tickets"}
            missing = required.difference(data)
            if missing:
                raise ValueError(f"field state hilang: {', '.join(sorted(missing))}")
            s = self._default()
            s.update({k: data[k] for k in s if k in data})
            for key in ("trades_today", "consec_losses"):
                if not isinstance(s[key], int) or isinstance(s[key], bool) or s[key] < 0:
                    raise ValueError(f"{key} harus integer non-negatif")
            for key in ("pnl_today", "day_start_equity", "peak_equity"):
                value = s[key]
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise ValueError(f"{key} harus angka finite atau null")
            if not isinstance(s["killed"], bool) or not isinstance(s["kill_reason"], str):
                raise ValueError("field kill-switch tidak valid")
            if s["day"] is not None and not isinstance(s["day"], str):
                raise ValueError("day harus string atau null")
            if s["day"] is not None:
                datetime.fromisoformat(s["day"])
            if s["pause_until"] is not None and not isinstance(s["pause_until"], str):
                raise ValueError("pause_until harus string atau null")
            if s["pause_until"] is not None:
                datetime.fromisoformat(s["pause_until"])
            if s["last_deal_time"] is not None and (
                not isinstance(s["last_deal_time"], int)
                or isinstance(s["last_deal_time"], bool)
                or s["last_deal_time"] < 0
            ):
                raise ValueError("last_deal_time harus integer atau null")
            if not isinstance(s["last_deal_tickets"], list) or not all(
                isinstance(ticket, int) and not isinstance(ticket, bool) and ticket >= 0
                for ticket in s["last_deal_tickets"]
            ):
                raise ValueError("last_deal_tickets harus berupa daftar integer")
            if any(
                s[key] is not None and s[key] < 0
                for key in ("day_start_equity", "peak_equity")
            ):
                raise ValueError("equity state tidak boleh negatif")
            return s
        except FileNotFoundError:
            return self._default()
        except (OSError, ValueError, TypeError) as e:
            raise RiskStateError(f"State RiskGuard tidak dapat dibaca ({self.path}): {e}") from e

    def _save(self):
        if self.memory:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                json.dump(self.s, tmp, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
        except (OSError, TypeError, ValueError) as e:
            raise RiskStateError(f"State RiskGuard tidak dapat disimpan ({self.path}): {e}") from e
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError as e:
                    raise RiskStateError(f"File sementara RiskGuard tidak dapat dihapus ({tmp_path}): {e}") from e

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    @classmethod
    def _now(cls, now):
        return cls._as_utc(now) if now is not None else datetime.now(UTC)

    def _roll_day(self, now, equity):
        today = self._now(now).date().isoformat()
        if self.s["day"] is None or today > self.s["day"]:
            self.s.update(day=today, trades_today=0, pnl_today=0.0, day_start_equity=equity)
        if self.s["day_start_equity"] is None:
            self.s["day_start_equity"] = equity
        if self.s["peak_equity"] is None:
            self.s["peak_equity"] = equity

    def initialize_deal_cursor(self, timestamp, seen_tickets=()):
        if self.s["last_deal_time"] is None:
            self.s["last_deal_time"] = int(timestamp)
            self.s["last_deal_tickets"] = [int(ticket) for ticket in seen_tickets]
            self._save()

    def is_processed_deal(self, ticket):
        return int(ticket) in self.s["last_deal_tickets"]

    def _day_pnl(self, equity):
        realized = self.s["pnl_today"]
        equity_change = equity - (self.s["day_start_equity"] or equity)
        return min(realized, equity_change)

    def check_equity(self, equity, now=None):
        now = self._now(now)
        old_day = self.s["day"]
        self._roll_day(now, equity)
        events = []
        peak = self.s["peak_equity"] or equity
        new_peak = max(peak, equity)
        changed = new_peak != self.s["peak_equity"] or self.s["day"] != old_day
        self.s["peak_equity"] = new_peak
        dd = 1 - equity / new_peak if new_peak > 0 else 0.0
        if dd >= self.cfg.max_drawdown_pct and not self.s["killed"]:
            self.s["killed"] = True
            self.s["kill_reason"] = (
                f"drawdown {dd*100:.1f} persen dari puncak "
                f"(batas {self.cfg.max_drawdown_pct*100:g})"
            )
            events.append("KILL-SWITCH: " + self.s["kill_reason"])
            changed = True
        if changed:
            self._save()
        return events

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

    def check_actual_risk(
        self, actual_risk_usd: float, equity: float, now=None, actual_notional: float = 0.0
    ) -> list:
        """Validasi risiko aktual setelah pembulatan lot minimum/fixed_lot MT5."""
        c, now = self.cfg, self._now(now)
        self._roll_day(now, equity)
        reasons = []
        risk_budget = equity * c.risk_per_trade
        if actual_risk_usd > risk_budget + 1e-9:
            reasons.append(
                f"risiko aktual ${actual_risk_usd:.2f} melebihi anggaran per trade ${risk_budget:.2f}"
            )
        if c.max_loss_per_trade_usd > 0 and actual_risk_usd > c.max_loss_per_trade_usd + 1e-9:
            reasons.append(
                f"risiko aktual lot minimum ${actual_risk_usd:.2f} melebihi batas per trade ${c.max_loss_per_trade_usd:.2f}"
            )
        leverage_limit = equity * c.max_leverage
        if actual_notional > leverage_limit + 1e-9:
            reasons.append(
                f"notional aktual ${actual_notional:.2f} melebihi batas leverage ${leverage_limit:.2f}"
            )
        limit = c.max_daily_loss_pct * self.s["day_start_equity"]
        day_pnl = self._day_pnl(equity)
        if day_pnl - actual_risk_usd < -limit:
            reasons.append(
                f"batas rugi harian ${limit:.2f}: rugi hari ini ${-day_pnl:.2f} "
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
            if age < -5:
                v.reasons.append(f"timestamp harga berada di masa depan ({-age:.0f} detik)")
            elif age > c.max_price_age_sec:
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
            day_pnl = self._day_pnl(equity)
            if day_pnl - risk_usd < -limit:
                v.reasons.append(
                    f"batas rugi harian ${limit:.2f}: rugi hari ini ${-day_pnl:.2f} "
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

    def record_close(self, pnl_usd, equity_after, now=None, deal_ticket=None, deal_time=None):
        c, now, events = self.cfg, self._now(now), []
        ticket = int(deal_ticket) if deal_ticket is not None else None
        timestamp = int(deal_time) if deal_time is not None else None
        if ticket is not None and timestamp is not None and self.is_processed_deal(ticket):
            return events
        self._roll_day(now, equity_after - pnl_usd)
        if self.s["day"] == now.date().isoformat():
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
        if ticket is not None and timestamp is not None:
            last_time = self.s["last_deal_time"]
            if last_time is None or timestamp > int(last_time):
                self.s["last_deal_time"] = timestamp
            if ticket not in self.s["last_deal_tickets"]:
                self.s["last_deal_tickets"].append(ticket)
                self.s["last_deal_tickets"] = self.s["last_deal_tickets"][-5000:]
        self._save()
        return events

    def reset_kill(self, new_peak=None):
        self.s["killed"], self.s["kill_reason"] = False, ""
        if new_peak:
            self.s["peak_equity"] = new_peak
        self._save()

    def status(self):
        return dict(self.s)