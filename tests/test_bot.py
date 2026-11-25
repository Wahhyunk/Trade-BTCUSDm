"""Unit test untuk strategi, RiskGuard, lot MT5, kalibrasi jam, breakeven, backtest, dan loop TradingBot."""
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from bot.backtest import run_walkforward_backtest
from bot.config import Settings, _bool
from bot.mt5_client import MT5Client, MT5Error, SymbolSpec, compute_lot, resolve_filling_mode, timeframe_info
from bot.risk_guard import RiskConfig, RiskGuard, RiskStateError
from bot.runner import TradingBot, calibrate_server_offset, compute_price_time_utc, single_instance_lock
from bot.strategy import MIN_BARS, compute_strategy_series, evaluate_signal, latest_signal

UTC = timezone.utc


class FakeMT5Client:
    def __init__(self, candles, tick_price=60000.0, equity=1000.0, tick_time_offset=0, server_clock_offset=0):
        self.candles = list(candles)
        self.tick_price = tick_price
        self.equity = equity
        self.tick_time_offset = tick_time_offset
        self.server_clock_offset = server_clock_offset
        self.positions = []
        self.deals = []
        self.orders_sent = []
        self.sl_modifications = []
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
            "time": self.candles[-1]["ts"] + 65 + self.tick_time_offset,
            "spread": 5.0,
        }

    def fetch_closed_candles(self, symbol, timeframe, count=600):
        return self.candles[-count:]

    def get_open_positions(self, symbol, magic):
        return list(self.positions)

    def check_margin(self, symbol, side, lot, price):
        return True, 0.30, self.equity

    def open_market_order(self, symbol, side, lot, sl, tp, magic, deviation=30, comment="", tick_data=None):
        rec = {
            "ok": True, "retcode": 10009, "comment": "Done",
            "order": 999, "deal": 888, "price": self.tick_price,
            "volume": lot, "sl": round(sl, 2), "tp": round(tp, 2), "side": side,
            "tick_time": tick_data["time"] if tick_data else 0,
        }
        self.orders_sent.append(rec)
        return rec

    def modify_position_sl(self, pos, new_sl, tick_data=None):
        rec = {"ok": True, "retcode": 10009, "comment": "Done", "ticket": pos["ticket"], "sl": round(new_sl, 2)}
        self.sl_modifications.append(rec)
        pos["sl"] = round(new_sl, 2)
        return rec

    def close_position(self, pos, magic, deviation=30, comment=""):
        self.closed_tickets.append(pos["ticket"])
        self.positions = [p for p in self.positions if p["ticket"] != pos["ticket"]]
        return {"ok": True, "retcode": 10009, "comment": "Done", "price": self.tick_price, "ticket": pos["ticket"]}

    def get_closed_deals_since(self, symbol, magic, since_ts):
        return [d for d in self.deals if d["time"] >= since_ts]


def _make_ohlcv_candles(count=650, step_sec=1800, clock_offset=0):
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0001, 0.0025, count)
    closes = 60000.0 * np.exp(np.cumsum(rets))
    base_ts = int(datetime.now(UTC).timestamp()) + clock_offset - (count - 1) * step_sec - step_sec - 5
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        hi = max(o, c) + abs(rng.normal(40, 20))
        lo = min(o, c) - abs(rng.normal(40, 20))
        out.append({
            "ts": base_ts + i * step_sec,
            "open": float(o),
            "high": float(hi),
            "low": float(lo),
            "close": float(c),
            "volume": float(rng.uniform(50, 200)),
        })
    return out


class TestStrategy(unittest.TestCase):
    def test_requires_min_bars(self):
        with self.assertRaises(ValueError):
            latest_signal([100.0] * 50)

    def test_scalp_and_trend_and_mtf_signals(self):
        rng = np.random.default_rng(42)
        prices = 60000 * np.exp(np.cumsum(rng.normal(0.0002, 0.002, MIN_BARS + 20)))
        for strat in ("scalp", "donchian", "ema_cross", "mtf_pullback", "session_box", "mtf_combo"):
            sig = evaluate_signal(prices, strat)
            self.assertIn(sig.action, ("BUY", "SELL", "HOLD"))

    def test_mtf_pullback_no_lookahead_and_atr_stops(self):
        candles = _make_ohlcv_candles(650, step_sec=1800)
        df_full = compute_strategy_series(candles, strategy="mtf_combo")
        df_prefix = compute_strategy_series(candles[:-10], strategy="mtf_combo")
        idx = len(df_prefix) - 1
        self.assertEqual(int(df_full["signal"].iloc[idx]), int(df_prefix["signal"].iloc[idx]))
        self.assertAlmostEqual(float(df_full["atr"].iloc[idx]), float(df_prefix["atr"].iloc[idx]), places=6)

    def test_backtest_engine_runs_cleanly(self):
        candles = _make_ohlcv_candles(700, step_sec=1800)
        cfg = Settings(strategy="mtf_pullback", timeframe="M30", fixed_lot=0.01, risk_per_trade=0.02)
        summary = run_walkforward_backtest(candles, cfg, initial_equity=500.0, folds=2)
        self.assertEqual(summary.total_folds, 2)
        self.assertGreaterEqual(summary.trades, 0)


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

    def test_actual_risk_and_leverage_caps_apply_to_fixed_lots(self):
        g = RiskGuard(
            RiskConfig(risk_per_trade=0.01, max_leverage=2.0),
            state_path=":memory:",
        )
        reasons = g.check_actual_risk(20, 1000, actual_notional=3000)
        self.assertTrue(any("anggaran per trade" in reason for reason in reasons))
        self.assertTrue(any("batas leverage" in reason for reason in reasons))

    def test_equity_drawdown_trips_kill_switch(self):
        g = RiskGuard(RiskConfig(max_drawdown_pct=0.10), state_path=":memory:")
        now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
        g.check_equity(1000, now=now)
        events = g.check_equity(890, now=now + timedelta(minutes=1))
        self.assertTrue(any("KILL-SWITCH" in event for event in events))

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "risk_state.json"
            for content in ("{invalid", "{}"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(RiskStateError):
                        RiskGuard(state_path=path)

    def test_close_deal_is_deduplicated_after_restart(self):
        now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
        deal_time = int(now.timestamp())
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "risk_state.json"
            first = RiskGuard(RiskConfig(), state_path=path)
            first.check_equity(1000, now=now)
            first.record_close(-10, 990, now=now, deal_ticket=123, deal_time=deal_time)
            restarted = RiskGuard(RiskConfig(), state_path=path)
            restarted.record_close(-10, 990, now=now, deal_ticket=123, deal_time=deal_time)
            self.assertEqual(restarted.status()["pnl_today"], -10)


class TestMT5Utils(unittest.TestCase):
    def test_lot_calculation_and_filling(self):
        spec = SymbolSpec("BTCUSDm", 2, 0.01, 0.01, 200.0, 0.01, 1.0, 0, 3)
        self.assertEqual(compute_lot(spec, 0.003, fixed_lot=0.0, allow_min_lot=True), 0.01)
        self.assertEqual(compute_lot(spec, 0.003, fixed_lot=0.0, allow_min_lot=False), 0.0)
        self.assertEqual(compute_lot(spec, 0.037, fixed_lot=0.0, allow_min_lot=True), 0.03)
        self.assertEqual(compute_lot(spec, 0.5, fixed_lot=0.02, allow_min_lot=True), 0.02)
        self.assertEqual(compute_lot(spec, 0.5, fixed_lot=0.005, allow_min_lot=False), 0.0)
        self.assertEqual(timeframe_info("M1")[1], 60)
        self.assertEqual(timeframe_info("M30")[1], 1800)
        self.assertEqual(timeframe_info("H1")[1], 3600)
        self.assertIsInstance(resolve_filling_mode(spec), int)

    def test_invalid_quote_and_position_lookup_fail_closed(self):
        client = MT5Client()
        with patch("bot.mt5_client.mt5.symbol_info_tick", return_value=SimpleNamespace(
            bid=0, ask=100, time=1,
        )):
            with self.assertRaises(MT5Error):
                client.tick("BTCUSDm")
        with patch("bot.mt5_client.mt5.positions_get", return_value=None), patch(
            "bot.mt5_client.mt5.last_error", return_value=(-1, "test error")
        ):
            with self.assertRaises(MT5Error):
                client.get_open_positions("BTCUSDm", 202611)

    def test_order_is_rejected_instead_of_widening_stops(self):
        client = MT5Client()
        spec = SymbolSpec("BTCUSDm", 2, 0.01, 0.01, 200.0, 0.01, 1.0, 200, 3)
        client.symbol_spec = lambda _: spec
        client.tick = lambda _: {"bid": 100.0, "ask": 100.1, "time": 1, "spread": 0.1}
        with patch("bot.mt5_client.mt5.order_send") as send:
            result = client.open_market_order("BTCUSDm", "BUY", 0.1, 99.5, 105, 202611)
        self.assertFalse(result["ok"])
        self.assertIn("terlalu dekat", result["comment"])
        send.assert_not_called()

    def test_modify_position_sl_only_tightens_profitably(self):
        client = MT5Client()
        spec = SymbolSpec("BTCUSDm", 2, 0.01, 0.01, 200.0, 0.01, 1.0, 100, 3)
        client.symbol_spec = lambda _: spec
        pos = {"ticket": 11, "symbol": "BTCUSDm", "side": "BUY", "sl": 59500.0, "tp": 61000.0}
        tick = {"bid": 60500.0, "ask": 60505.0, "time": 100}
        # Mencoba melonggarkan SL BUY -> ditolak lokal
        res_worse = client.modify_position_sl(pos, 59400.0, tick_data=tick)
        self.assertFalse(res_worse["ok"])
        # Memperketat SL BUY ke breakeven -> terkirim
        with patch("bot.mt5_client.mt5.order_send", return_value=SimpleNamespace(retcode=10009, comment="Done")) as send:
            res_ok = client.modify_position_sl(pos, 60025.0, tick_data=tick)
        self.assertTrue(res_ok["ok"])
        send.assert_called_once()


class TestRunner(unittest.TestCase):
    def _make_buy_candles(self, clock_offset=0):
        prices = np.full(MIN_BARS + 20, 59000.0)
        prices[-1] = 60000.0
        base_ts = int(datetime.now(UTC).timestamp()) + clock_offset - (len(prices) - 1) * 60 - 65
        return [
            {"ts": base_ts + i * 60, "open": float(p), "high": float(p + 10), "low": float(p - 10), "close": float(p), "volume": 100.0}
            for i, p in enumerate(prices)
        ]

    def test_live_order_and_dry_run(self):
        candles = self._make_buy_candles()
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(results_dir=Path(td), timeframe="M1", fixed_lot=0.01, strategy="ema_cross", cooldown_bars=1)
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

    def test_server_clock_skew_180s_and_broker_timezone_do_not_block_orders(self):
        # Simulasi persis bug forward-test: server MT5 +180 detik & +7200 detik (UTC+2) di depan jam PC
        for offset in (180, 7200):
            with self.subTest(server_offset=offset):
                candles = self._make_buy_candles(clock_offset=offset)
                with tempfile.TemporaryDirectory() as td:
                    cfg = Settings.load(results_dir=Path(td), timeframe="M1", fixed_lot=0.01, strategy="ema_cross", cooldown_bars=1)
                    client = FakeMT5Client(candles[:-1], tick_price=candles[-1]["close"])
                    bot = TradingBot(cfg, client, RiskGuard(cfg.risk_config(), ":memory:"), dry_run=False, log=lambda *_: None)
                    bot.warmup()
                    client.candles.append(candles[-1])
                    self.assertTrue(bot.step())
                    self.assertEqual(len(client.orders_sent), 1)

    def test_frozen_feed_and_future_jump_still_rejected(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
        bar_ts = int(now.timestamp()) + 180 - 60
        tick_ts = int(now.timestamp()) + 180
        offset = calibrate_server_offset(tick_ts, bar_ts, 60, now, 150)
        self.assertIsNotNone(offset)
        # Feed membeku 200 detik setelah kalibrasi -> harga basi
        later = now + timedelta(seconds=200)
        pt_stale = compute_price_time_utc(tick_ts, bar_ts, 60, later, offset, 150)
        self.assertGreater((later - pt_stale).total_seconds(), 150)
        # Tick melonjak +120 detik ke masa depan setelah kalibrasi -> ditolak
        pt_future = compute_price_time_utc(tick_ts + 120, bar_ts, 60, now, offset, 150)
        self.assertLess((now - pt_future).total_seconds(), -5)

    def test_oversized_fixed_lot_and_stale_tick_are_rejected(self):
        candles = self._make_buy_candles()
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(
                results_dir=Path(td), timeframe="M1", fixed_lot=0.1, strategy="ema_cross", cooldown_bars=1
            )
            client = FakeMT5Client(candles[:-1], tick_price=59000.0)
            bot = TradingBot(cfg, client, RiskGuard(cfg.risk_config(), ":memory:"), log=lambda *_: None)
            bot.warmup()
            client.candles.append(candles[-1])
            bot.step()
            self.assertEqual(client.orders_sent, [])

            cfg.fixed_lot = 0.01
            stale = FakeMT5Client(
                candles[:-1], tick_price=59000.0, tick_time_offset=-1000
            )
            stale_bot = TradingBot(
                cfg, stale, RiskGuard(cfg.risk_config(), ":memory:"), log=lambda *_: None
            )
            stale_bot.warmup()
            stale.candles.append(candles[-1])
            stale_bot.step()
            self.assertEqual(stale.orders_sent, [])

    def test_breakeven_sl_moves_on_profitable_bar_close(self):
        candles = self._make_buy_candles()
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(
                results_dir=Path(td), timeframe="M1", strategy="mtf_pullback",
                be_trigger_r=1.0, trail_atr_mult=0.0,
            )
            client = FakeMT5Client(candles)
            bot = TradingBot(cfg, client, RiskGuard(cfg.risk_config(), ":memory:"), log=lambda *_: None)
            pos = {
                "ticket": 555, "symbol": "BTCUSDm", "side": "BUY", "volume": 0.01,
                "entry_price": 59000.0, "sl": 58700.0, "tp": 59900.0, "time": candles[-2]["ts"],
            }
            # Close 59400 = +400 > 1.0 * 300 init_risk -> geser SL ke 59000 + 0.05*300 = 59015
            bot._handle_open_position(
                pos, SimpleNamespace(action="HOLD", atr=200.0), candles[-1]["ts"],
                latest_close=59400.0, tick=client.tick("BTCUSDm"),
            )
            self.assertEqual(len(client.sl_modifications), 1)
            self.assertAlmostEqual(client.sl_modifications[0]["sl"], 59015.0, places=2)

    def test_trend_strategy_closes_on_opposite_signal(self):
        candles = self._make_buy_candles()
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(results_dir=Path(td), timeframe="M1", strategy="ema_cross")
            client = FakeMT5Client(candles)
            bot = TradingBot(cfg, client, RiskGuard(cfg.risk_config(), ":memory:"), log=lambda *_: None)
            pos = {
                "ticket": 123, "side": "BUY", "volume": 0.01, "time": candles[-2]["ts"],
            }
            bot._handle_open_position(pos, SimpleNamespace(action="SELL"), candles[-1]["ts"])
            self.assertEqual(client.closed_tickets, [123])

    def test_closed_deal_during_downtime_is_synced_once(self):
        candles = self._make_buy_candles()
        now = datetime.now(UTC)
        deal_time = int(now.timestamp()) - 120
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings.load(results_dir=Path(td), timeframe="M1", strategy="ema_cross")
            guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
            guard.check_equity(1000, now=now)
            guard.initialize_deal_cursor(deal_time - 10)
            client = FakeMT5Client(candles, tick_price=59000.0)
            client.deals.append({
                "ticket": 777, "time": deal_time, "position_id": 88,
                "volume": 0.01, "price": 58900.0, "net_pnl": -15.0, "comment": "SL",
            })
            bot = TradingBot(cfg, client, guard, log=lambda *_: None)
            bot.warmup()
            bot.step()
            bot.step()
            self.assertEqual(guard.status()["pnl_today"], -15.0)
            self.assertEqual(guard.status()["last_deal_tickets"], [777])

    def test_live_instance_lock_rejects_another_process(self):
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from bot.runner import single_instance_lock\n"
            "from bot.mt5_client import MT5Error\n"
            "try:\n"
            " with single_instance_lock(Path(sys.argv[1])): pass\n"
            "except MT5Error:\n"
            " sys.exit(7)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.lock"
            with single_instance_lock(path):
                result = subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    cwd=Path(__file__).resolve().parent.parent,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(result.returncode, 7, result.stderr)


class TestConfiguration(unittest.TestCase):
    def test_invalid_indicator_period_is_rejected_at_load(self):
        with self.assertRaises(ValueError):
            Settings(rsi_period=0)

    def test_invalid_boolean_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            _bool("treu")


if __name__ == "__main__":
    unittest.main()
