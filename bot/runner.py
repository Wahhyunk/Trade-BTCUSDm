"""
Loop utama algo trading di terminal untuk MetaTrader 5.
Memproses candle yang SUDAH TUTUP, menjalankan strategi + RiskGuard, dan mengirim order ke MT5.
"""
import csv
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from bot.config import Settings
from bot.mt5_client import MT5Client, MT5Error, compute_lot, timeframe_info
from bot.risk_guard import RiskGuard
from bot.strategy import MIN_BARS, evaluate_signal

UTC = timezone.utc


class TradingBot:
    def __init__(self, cfg: Settings, client: MT5Client, guard: RiskGuard, dry_run: bool = False, log=print):
        self.cfg = cfg
        self.client = client
        self.guard = guard
        self.dry_run = dry_run
        self._out = log
        _, self.bar_sec = timeframe_info(cfg.timeframe)
        self.last_bar_ts = None
        self.since_exit_bars = 10**9
        self.held_bars = 0
        self.seen_deals = set()
        self.start_sync_ts = int(time.time())
        self.spec = None

    def log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        self._out(line)
        try:
            self.cfg.results_dir.mkdir(parents=True, exist_ok=True)
            with self.cfg.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _record_trade_csv(self, row: dict):
        self.cfg.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.trades_csv_path
        exists = path.exists()
        fields = ["time_utc", "mode", "event", "symbol", "side", "lot", "price", "sl", "tp", "pnl_usd", "note"]
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if not exists:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in fields})

    def warmup(self):
        self.spec = self.client.symbol_spec(self.cfg.symbol)
        candles = self.client.fetch_closed_candles(self.cfg.symbol, self.cfg.timeframe, count=max(MIN_BARS + 20, 350))
        self.last_bar_ts = candles[-1]["ts"]
        tick = self.client.tick(self.cfg.symbol)
        self.start_sync_ts = tick["time"]

        # Catat deal yang sudah ada sebelum bot mulai agar tidak dihitung ulang
        for d in self.client.get_closed_deals_since(self.cfg.symbol, self.cfg.magic, self.start_sync_ts):
            self.seen_deals.add(d["ticket"])

        sig = evaluate_signal(candles, self.cfg.strategy, self.cfg.params())
        acc = self.client.account_info()
        spread_pts = tick["spread"] / self.spec.point if self.spec.point > 0 else 0.0
        mode_label = "DRY-RUN (SIMULASI)" if self.dry_run else ("LIVE DEMO MT5" if acc["is_demo"] else "LIVE REAL MT5")
        self.log(
            f"=== Bot Dimulai [{mode_label}] ===\n"
            f"  Akun      : {acc['login']} @ {acc['server']} | Saldo ${acc['balance']:,.2f} | Equity ${acc['equity']:,.2f}\n"
            f"  Instrumen : {self.cfg.symbol} ({self.cfg.timeframe}) | Strategi: {self.cfg.strategy}\n"
            f"  TP/SL     : TP {self.cfg.tp_pct*100:.2f}% / SL {self.cfg.sl_pct*100:.2f}% | Cooldown: {self.cfg.cooldown_bars} bar | Magic: {self.cfg.magic}\n"
            f"  Harga     : Bid {tick['bid']:,.2f} / Ask {tick['ask']:,.2f} (Spread {spread_pts:.0f} pts) | "
            f"RSI {sig.rsi:.1f} | Trend {'NAIK' if sig.trend_up else 'TURUN'} | Sinyal awal: {sig.action}"
        )
        for w in self.cfg.config_warnings():
            self.log(f"[PERINGATAN KONFIGURASI] {w}")

    def _sync_closed_deals(self, equity: float):
        if self.dry_run:
            return
        deals = self.client.get_closed_deals_since(self.cfg.symbol, self.cfg.magic, self.start_sync_ts)
        for d in deals:
            if d["ticket"] in self.seen_deals:
                continue
            self.seen_deals.add(d["ticket"])
            if d["time"] > self.start_sync_ts:
                self.start_sync_ts = d["time"]
            self.since_exit_bars = 0
            self.held_bars = 0
            now = datetime.now(UTC)
            events = self.guard.record_close(d["net_pnl"], equity, now=now)
            self.log(
                f"POSISI TERTUTUP #{d['position_id']} | lot {d['volume']} @ {d['price']:,.2f} | "
                f"PnL bersih {d['net_pnl']:+.2f} USD | Equity ${equity:,.2f}"
            )
            self._record_trade_csv({
                "time_utc": now.isoformat(timespec="seconds"),
                "mode": "LIVE",
                "event": "CLOSE",
                "symbol": self.cfg.symbol,
                "side": "",
                "lot": d["volume"],
                "price": d["price"],
                "pnl_usd": round(d["net_pnl"], 2),
                "note": d["comment"],
            })
            for ev in events:
                self.log(f"[RISK GUARD] {ev}")

    def _handle_open_position(self, pos: dict, sig, latest_bar_ts: int):
        self.held_bars += 1
        effective_held = self.held_bars
        if pos.get("time", 0) > 0 and latest_bar_ts >= pos["time"]:
            effective_held = max(self.held_bars, int((latest_bar_ts - pos["time"]) // self.bar_sec))

        should_close = False
        reason = ""

        if self.cfg.strategy in ("donchian", "ema_cross") and sig.action == "EXIT":
            should_close, reason = True, "sinyal EXIT tren"
        elif self.cfg.max_hold_bars > 0 and effective_held >= self.cfg.max_hold_bars:
            should_close, reason = True, f"max hold {self.cfg.max_hold_bars} bar"

        if not should_close:
            return

        if self.dry_run:
            self.log(f"[DRY-RUN] AKAN TUTUP #{pos['ticket']} {pos['side']} ({reason})")
            return

        res = self.client.close_position(pos, self.cfg.magic, self.cfg.deviation, comment=f"EA_{reason[:12]}")
        if res["ok"]:
            self.log(f"TUTUP #{pos['ticket']} {pos['side']} @ {res['price']:,.2f} ({reason})")
            self.since_exit_bars = 0
            self.held_bars = 0
        else:
            self.log(f"[ERROR] Gagal tutup #{pos['ticket']}: retcode={res['retcode']} {res['comment']}")

    def _try_enter(self, side: str, sig, bar_ts: int, tick: dict, acc: dict, open_count: int):
        self.spec = self.client.symbol_spec(self.cfg.symbol)
        if self.spec.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            self.log(f"[SKIP] Pasar {self.cfg.symbol} sedang tidak membuka posisi baru (trade_mode={self.spec.trade_mode})")
            return

        if self.cfg.max_spread_points > 0 and self.spec.point > 0:
            spread_pts = tick["spread"] / self.spec.point
            if spread_pts > self.cfg.max_spread_points:
                self.log(
                    f"[SKIP] Spread terlalu lebar ({spread_pts:.0f} pts > batas {self.cfg.max_spread_points} pts)"
                )
                return

        d = 1 if side == "BUY" else -1
        entry = tick["ask"] if side == "BUY" else tick["bid"]
        sl = entry * (1 - d * self.cfg.sl_pct)
        tp = entry * (1 + d * self.cfg.tp_pct) if self.cfg.tp_pct > 0 else None

        # Hitung umur data relatif terhadap server MT5 lalu jangkarkan ke UTC murni agar sinkron dengan RiskGuard
        now_utc = datetime.now(UTC)
        data_age_sec = max(0, int(tick["time"]) - (int(bar_ts) + self.bar_sec))
        price_time_utc = now_utc - timedelta(seconds=data_age_sec)

        verdict = self.guard.check_entry(
            side=side,
            entry=entry,
            stop=sl,
            target=tp,
            equity=acc["equity"],
            price_time=price_time_utc,
            now=now_utc,
            open_positions=open_count,
        )
        if not verdict.ok:
            self.log(f"[GUARD TOLAK] Sinyal {side} ditolak: {'; '.join(verdict.reasons)}")
            return

        lot = compute_lot(self.spec, verdict.qty, fixed_lot=self.cfg.fixed_lot, allow_min_lot=self.cfg.allow_min_lot)
        if lot <= 0:
            self.log(
                f"[SKIP] Lot hasil hitung terlalu kecil (< {self.spec.volume_min}). "
                f"Atur MT5_ALLOW_MIN_LOT=true atau MT5_FIXED_LOT={self.spec.volume_min} di .env"
            )
            return

        actual_risk = lot * self.spec.contract_size * abs(entry - sl)
        actual_reasons = self.guard.check_actual_risk(actual_risk, acc["equity"], now=now_utc)
        if actual_reasons:
            self.log(f"[GUARD TOLAK] Lot {lot} ({side}) ditolak: {'; '.join(actual_reasons)}")
            return

        ok_margin, req_margin, free_margin = self.client.check_margin(self.cfg.symbol, side, lot, entry)
        if not ok_margin:
            self.log(f"[SKIP] Margin tidak cukup untuk {lot} lot (butuh ${req_margin:.2f}, tersedia ${free_margin:.2f})")
            return

        tp_str = f"{tp:,.2f}" if tp else "-"
        if self.dry_run:
            self.log(
                f"[DRY-RUN] AKAN BUKA {side} {lot} lot {self.cfg.symbol} @ {entry:,.2f} | "
                f"TP {tp_str} | SL {sl:,.2f} | est. risiko ${actual_risk:.2f} | RSI {sig.rsi:.1f}"
            )
            self._record_trade_csv({
                "time_utc": now_utc.isoformat(timespec="seconds"),
                "mode": "DRY-RUN",
                "event": "OPEN",
                "symbol": self.cfg.symbol,
                "side": side,
                "lot": lot,
                "price": entry,
                "sl": round(sl, self.spec.digits),
                "tp": round(tp, self.spec.digits) if tp else 0,
                "note": sig.detail,
            })
            return

        res = self.client.open_market_order(
            symbol=self.cfg.symbol,
            side=side,
            lot=lot,
            sl=sl,
            tp=tp or 0.0,
            magic=self.cfg.magic,
            deviation=self.cfg.deviation,
            comment=f"EA_{self.cfg.strategy}",
        )
        if res["ok"]:
            self.guard.record_entry(now=datetime.now(UTC))
            self.held_bars = 0
            self.log(
                f"ORDER TERBUKA #{res['order']} | {side} {res['volume']} lot {self.cfg.symbol} @ {res['price']:,.2f} | "
                f"TP {res['tp']:,.2f} | SL {res['sl']:,.2f} | est. risiko ${actual_risk:.2f}"
            )
            self._record_trade_csv({
                "time_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "mode": "LIVE",
                "event": "OPEN",
                "symbol": self.cfg.symbol,
                "side": side,
                "lot": res["volume"],
                "price": res["price"],
                "sl": res["sl"],
                "tp": res["tp"],
                "note": f"order={res['order']} {sig.detail}",
            })
        else:
            self.log(f"[ORDER GAGAL] {side} {lot} lot: retcode={res['retcode']} ({res['comment']})")

    def step(self) -> bool:
        """Jalankan satu siklus polling. Mengembalikan True jika ada candle baru yang diproses."""
        acc = self.client.account_info()
        self._sync_closed_deals(acc["equity"])

        candles = self.client.fetch_closed_candles(self.cfg.symbol, self.cfg.timeframe, count=max(MIN_BARS + 20, 350))
        latest = candles[-1]
        if self.last_bar_ts is not None and latest["ts"] <= self.last_bar_ts:
            return False

        self.last_bar_ts = latest["ts"]
        self.since_exit_bars += 1
        sig = evaluate_signal(candles, self.cfg.strategy, self.cfg.params())
        tick = self.client.tick(self.cfg.symbol)
        positions = self.client.get_open_positions(self.cfg.symbol, self.cfg.magic)

        pos_info = "FLAT"
        if positions:
            p = positions[0]
            pos_info = f"{p['side']} {p['volume']}lot ({p['profit']:+.2f}$)"

        bar_str = datetime.fromtimestamp(latest["ts"], UTC).strftime("%H:%M UTC")
        self.log(
            f"BAR {bar_str} | {self.cfg.symbol} {latest['close']:,.2f} | RSI {sig.rsi:5.1f} | "
            f"EMA {'UP' if sig.trend_up else 'DN'} | Sig {sig.action:<4} | Pos: {pos_info} | Eq ${acc['equity']:,.2f}"
        )

        if positions:
            self._handle_open_position(positions[0], sig, latest["ts"])
            return True

        if sig.action in ("BUY", "SELL") and self.since_exit_bars >= self.cfg.cooldown_bars:
            self._try_enter(sig.action, sig, latest["ts"], tick, acc, len(positions))
        elif sig.action in ("BUY", "SELL"):
            rem = self.cfg.cooldown_bars - self.since_exit_bars
            self.log(f"  -> Sinyal {sig.action} dilewati (cooldown sisa {rem} bar)")
        return True

    def run(self, once: bool = False):
        self.warmup()
        if once:
            # Pada mode once, paksa evaluasi bar terakhir dengan reset last_bar_ts
            self.last_bar_ts = None
            self.step()
            return

        self.log(f"Menunggu candle baru ({self.cfg.timeframe}), cek tiap {self.cfg.poll_seconds:g} detik. Tekan Ctrl+C untuk berhenti.")
        try:
            while True:
                try:
                    self.step()
                except MT5Error as e:
                    self.log(f"[MT5 ERROR] {e} -> mencoba sambung ulang...")
                    try:
                        self.client.reconnect()
                    except MT5Error:
                        pass
                except Exception as e:
                    self.log(f"[ERROR] {type(e).__name__}: {e}")
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self.log("Bot dihentikan oleh pengguna (Ctrl+C).")