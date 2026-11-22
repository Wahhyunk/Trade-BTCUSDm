"""Unit test untuk strategi, RiskGuard, lot MT5, dan loop TradingBot (tanpa kirim order asli)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from bot.config import Settings
from bot.mt5_client import SymbolSpec, compute_lot, resolve_filling_mode, timeframe_info
from bot.risk_guard import RiskConfig, RiskGuard
from bot.runner import TradingBot
from bot.strategy import MIN_BARS, evaluate_signal, latest_signal

UTC = timezone.utc


class FakeMT5Client:
    def __init__(self, candles, tick_price=60000.0, equity=1000.0):
        self.candles = list(candles)
        self.tick_price = tick_price
        self.equity = equity
        self.positions = []
        self.deals = []
        self.orders_sent = []
        self.closed_tickets = []
        self.spec = SymbolSpec(
            name="BTCUSDm", digits=2, point=0.01,
            volume_min=0.01, volume_max=200.0, volume_step=0.01,
            contract_size=1.0, stops_level=0, filling_mode=3,
        )

    def symbol_spec(self, symbol):
        return self.spec

    def account_info(self):
        return {
            "login": 12345, "server": "Demo", "currency": "USD",
            "balance": self.equity, "equity": self.equity, "profit": 0.0,
            "margin_free": self.equity, "leverage": 2000, "is_demo": True, "trade_allowed": True,
        }

    def tick(self, symbol):
        return {
            "bid": self.tick_price,
            "ask": self.tick_price + 5.0,
            "time": self.candles[-1]["ts"] + 65,
            "spread": 5.0,
        }

    def fetch_closed_candles(self, symbol, timeframe, count=400):
        return self.candles[-count:]

    def get_open_positions(self, symbol, magic):
        return list(self.positions)

    def check_margin(self, symbol, side, lot, price):
        return True, 0.30, self.equity

    def open_market_order(self, symbol, side, lot, sl, tp, magic, deviation=30, comment=""):
        rec = {
            "ok": True, "retcode": 10009, "comment": "Done",
            "order": 999, "deal": 888, "price": self.tick_price,
            "volume": lot, "sl": round(sl, 2), "tp": round(tp, 2), "side": side,
        }
        self.orders_sent.append(rec)
        return rec

    def close_position(self, pos, magic, deviation=30, comment=""):
        self.closed_tickets.append(pos["ticket"])
        self.positions = [p for p in self.positions if p["ticket"] != pos["ticket"]]
        return {"ok": True, "retcode": 10009, "comment": "Done", "price": self.tick_price, "ticket": pos["ticket"]}

    def get_closed_deals_since(self, symbol, magic, since_ts):
        return [d for d in self.deals if d["time"] >= since_ts]


class TestStrategy(unittest.TestCase):
    def test_requires_min_bars(self):
        with self.assertRaises(ValueError):
            latest_signal([100.0] * 50)

    def test_scalp_and_trend_signals(self):
        rng = np.random.default_rng(42)
        prices = 60000 * np.exp(np.cumsum(rng.normal(0.0002, 0.002, MIN_BARS + 20)))
        sig_scalp = evaluate_signal(prices, "scalp")
        self.assertIn(sig_scalp.action, ("BUY", "SELL", "HOLD"))

        sig_don = evaluate_signal(prices, "donchian")
        self.assertIn(sig_don.action, ("BUY", "EXIT"))

        sig_ema = evaluate_signal(prices, "ema_cross")
        self.assertIn(sig_ema.action, ("BUY", "EXIT"))


class TestRiskGuard(unittest.TestCase):
    def test_blocks_bad_rr_and_kill_switch(self):
        g = RiskGuard(RiskConfig(min_reward_risk=1.5, max_drawdown_pct=0.10), state_path=":memory:")
        now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
        # RR 1.0 < 1.5 -> ditolak
        v_bad = g.check_entry("BUY", 100, 95, 105, equity=1000, price_time=now, now=now)
        self.assertFalse(v_bad.ok)
        # RR 3.0 -> lolos
        v_ok = g.check_entry("BUY", 100, 95, 115, equity=1000, price_time=now, now=now)
        self.assertTrue(v_ok.ok)
        # Drawdown 12% -> kill switch
        events = g.record_close(-120, 880, now=now)
        self.assertTrue(any("KILL-SWITCH" in e for e in events))
        self.assertTrue(g.status()["killed"])
        v_killed = g.check_entry("BUY", 100, 95, 115, equity=880, price_time=now, now=now)
        self.assertFalse(v_killed.ok)
        # Reset kill
        g.reset_kill(new_peak=880)
        self.assertFalse(g.status()["killed"])

    def test_stale_price_rejected(self):
        g = RiskGuard(RiskConfig(max_price_age_sec=60), state_path=":memory:")
        now = datetime(2026, 11, 21, 12, 5, tzinfo=UTC)
        old = now - timedelta(seconds=120)
        v = g.check_entry("BUY", 100, 95, 115, equity=1000, price_time=old, now=now)
        self.assertFalse(v.ok)


class TestMT5Utils(unittest.TestCase):
    def test_lot_calculation_and_filling(self):
        spec = SymbolSpec("BTCUSDm", 2, 0.01, 0.01, 200.0, 0.01, 1.0, 0, 3)
        self.assertEqual(compute_lot(spec, 0.003, fixed_lot=0.0, allow_min_lot=True), 0.01)
        self.assertEqual(compute_lot(spec, 0.003, fixed_lot=0.0, allow_min_lot=False), 0.0)
        self.assertEqual(compute_lot(spec, 0.037, fixed_lot=0.0, allow_min_lot=True), 0.03)
        self.assertEqual(compute_lot(spec, 0.5, fixed_lot=0.02, allow_min_lot=True), 0.02)
        self.assertEqual(timeframe_info("M1")[1], 60)
        self.assertEqual(timeframe_info("H1")[1], 3600)
        self.assertIsInstance(resolve_filling_mode(spec), int)


class TestRunner(unittest.TestCase):
    def _make_buy_candles(self):
        # Naik perlahan (EMA9 > EMA21, RSI ~60, momentum positif > 0.1%)
        base_ts = 1795000000
        prices = np.linspace(55000.0, 59000.0, MIN_BARS + 20)
        prices[-1] = prices[-3] * 1.003  # momentum +0.3%
        # Sedikit turun sebelumnya agar RSI tidak > 80
        for i in range(0, len(prices) - 4, 3):
            prices[i] -= 120.0
        return [
            {"ts": base_ts + i * 60, "open": float(p), "high": float(p + 10), "low": float(p - 10), "close": float(p), "volume": 100.0}
            for i, p in enumerate(prices)
        ]

    def test_live_order_and_dry_run(self):
        candles = self._make_buy_candles()
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(results_dir=Path(td), fixed_lot=0.01, strategy="ema_cross", cooldown_bars=1)
            # Dry-run tidak mengirim order
            c_dry = FakeMT5Client(candles[:-1], tick_price=candles[-1]["close"])
            b_dry = TradingBot(cfg, c_dry, RiskGuard(cfg.risk_config(), ":memory:"), dry_run=True, log=lambda *_: None)
            b_dry.warmup()
            c_dry.candles.append(candles[-1])
            self.assertTrue(b_dry.step())
            self.assertEqual(len(c_dry.orders_sent), 0)

            # Live mengirim order
            c_live = FakeMT5Client(candles[:-1], tick_price=candles[-1]["close"])
            b_live = TradingBot(cfg, c_live, RiskGuard(cfg.risk_config(), ":memory:"), dry_run=False, log=lambda *_: None)
            b_live.warmup()
            c_live.candles.append(candles[-1])
            self.assertTrue(b_live.step())
            self.assertEqual(len(c_live.orders_sent), 1)
            self.assertEqual(c_live.orders_sent[0]["side"], "BUY")
            self.assertEqual(c_live.orders_sent[0]["volume"], 0.01)


if __name__ == "__main__":
    unittest.main()
