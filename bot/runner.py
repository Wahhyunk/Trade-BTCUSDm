"""
Loop utama algo trading di terminal untuk MetaTrader 5.
Memproses candle yang SUDAH TUTUP, menjalankan strategi + RiskGuard,
mengelola breakeven/trailing SL, dan mengirim order ke MT5.
"""
import csv
import msvcrt
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from bot.config import ROOT, Settings
from bot.mt5_client import MT5Client, MT5Error, compute_lot, timeframe_info
from bot.risk_guard import RiskGuard, RiskStateError
from bot.strategy import MIN_BARS, evaluate_signal

UTC = timezone.utc


@contextmanager
def single_instance_lock(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+b")
    except OSError as e:
        raise MT5Error(f"Tidak dapat membuat lock instance '{path}': {e}") from e
    locked = False
    try:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise MT5Error("Instance algo trading lain sudah berjalan pada workspace ini.") from e
        locked = True
        yield
    finally:
        if not lock_file.closed:
            try:
                if locked:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                lock_file.close()


def instance_lock_path():
    return ROOT / "results" / ".live-bot.lock"


def calibrate_server_offset(tick_ts: int, bar_ts: int, bar_sec: int, now_utc: datetime, max_age_sec: int) -> float | None:
    """
    Estimasi selisih jam server MT5 terhadap jam lokal PC (dalam detik: server_ts - pc_utc_ts).
    Hanya menerima offset bila tick segar terhadap bar terakhir yang tutup, dan masuk akal
    sebagai drift jam PC (<= 600 dtk) atau zona waktu broker (kelipatan 30 menit + drift <= 600 dtk).
    Mengembalikan None jika tick terindikasi basi/tidak konsisten (fail-closed).
    """
    bar_close_ts = int(bar_ts) + int(bar_sec)
    server_lag = max(0.0, float(bar_close_ts - int(tick_ts)))
    if server_lag > max_age_sec:
        return None
    raw_offset = float(tick_ts) - now_utc.timestamp()
    if abs(raw_offset) <= 600.0:
        return raw_offset
    if -5 * 3600.0 <= raw_offset <= 14 * 3600.0:
        nearest_half_hour = round(raw_offset / 1800.0) * 1800.0
        if abs(raw_offset - nearest_half_hour) <= 600.0:
            return raw_offset
    return None


def compute_price_time_utc(
    tick_ts: int,
    bar_ts: int,
    bar_sec: int,
    now_utc: datetime,
    server_offset_sec: float | None,
    max_age_sec: int,
) -> datetime:
    """Konversi timestamp tick MT5 ke UTC terkalibrasi tanpa menutupi quote basi atau lonjakan masa depan."""
    bar_close_ts = int(bar_ts) + int(bar_sec)
    server_lag = max(0.0, float(bar_close_ts - int(tick_ts)))
    offset = server_offset_sec if server_offset_sec is not None else 0.0
    calibrated_ts = float(tick_ts) - offset
    now_ts = now_utc.timestamp()

    if calibrated_ts > now_ts + 5.0:
        return datetime.fromtimestamp(calibrated_ts, UTC)

    local_age = max(0.0, now_ts - calibrated_ts)
    effective_age = max(server_lag, local_age)
    if effective_age > max_age_sec:
        return now_utc - timedelta(seconds=effective_age)
    return datetime.fromtimestamp(calibrated_ts, UTC)


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
        self.spec = None
        self.server_offset_sec: float | None = None
        self._init_risk_by_ticket: dict[int, float] = {}
        self._be_done_tickets: set[int] = set()

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

    def _update_clock_offset(self, tick_ts: int, bar_ts: int, now_utc: datetime):
        cand = calibrate_server_offset(tick_ts, bar_ts, self.bar_sec, now_utc, self.cfg.max_price_age_sec)
        if cand is None:
            return
        if self.server_offset_sec is None:
            self.server_offset_sec = cand
        elif abs(cand - self.server_offset_sec) <= 60.0 and (int(tick_ts) - (int(bar_ts) + self.bar_sec)) >= 0:
            # Koreksi drift kecil bertahap hanya jika tick konsisten setelah bar tutup
            self.server_offset_sec = cand

    def _server_ts_to_utc(self, server_ts: int) -> datetime:
        offset = self.server_offset_sec if self.server_offset_sec is not None else 0.0
        return datetime.fromtimestamp(float(server_ts) - offset, UTC)

    def warmup(self):
        self.spec = self.client.symbol_spec(self.cfg.symbol)
        fetch_count = max(MIN_BARS + 20, 600)
        candles = self.client.fetch_closed_candles(self.cfg.symbol, self.cfg.timeframe, count=fetch_count)
        self.last_bar_ts = candles[-1]["ts"]
        tick = self.client.tick(self.cfg.symbol)
        now_utc = datetime.now(UTC)
        self._update_clock_offset(tick["time"], self.last_bar_ts, now_utc)
        acc = self.client.account_info()
        if not self.dry_run:
            for event in self.guard.check_equity(acc["equity"]):
                self.log(f"[RISK GUARD] {event}")
            if self.guard.status()["last_deal_time"] is None:
                deals_at_start = self.client.get_closed_deals_since(
                    self.cfg.symbol, self.cfg.magic, tick["time"]
                )
                same_second = [d["ticket"] for d in deals_at_start if d["time"] == tick["time"]]
                self.guard.initialize_deal_cursor(tick["time"], same_second)

        sig = evaluate_signal(candles, self.cfg.strategy, self.cfg.params())
        spread_pts = tick["spread"] / self.spec.point if self.spec.point > 0 else 0.0
        mode_label = "DRY-RUN (SIMULASI)" if self.dry_run else ("LIVE DEMO MT5" if acc["is_demo"] else "LIVE REAL MT5")
        offset_note = (
            f" | Offset Jam Server: {self.server_offset_sec:+.0f}s"
            if self.server_offset_sec is not None and abs(self.server_offset_sec) >= 5.0
            else ""
        )
        tp_sl_info = (
            f"ATR {self.cfg.atr_tp_mult:g}x / {self.cfg.atr_sl_mult:g}x (BE @{self.cfg.be_trigger_r:g}R)"
            if self.cfg.uses_atr_stops()
            else f"TP {self.cfg.tp_pct*100:.2f}% / SL {self.cfg.sl_pct*100:.2f}%"
        )
        self.log(
            f"=== Bot Dimulai [{mode_label}] ===\n"
            f"  Akun      : {acc['login']} @ {acc['server']} | Saldo ${acc['balance']:,.2f} | Equity ${acc['equity']:,.2f}\n"
            f"  Instrumen : {self.cfg.symbol} ({self.cfg.timeframe}) | Strategi: {self.cfg.strategy}\n"
            f"  TP/SL     : {tp_sl_info} | Cooldown: {self.cfg.cooldown_bars} bar | Magic: {self.cfg.magic}\n"
            f"  Harga     : Bid {tick['bid']:,.2f} / Ask {tick['ask']:,.2f} (Spread {spread_pts:.0f} pts){offset_note} | "
            f"RSI {sig.rsi:.1f} | Trend {'NAIK' if sig.trend_up else 'TURUN'} | Sinyal awal: {sig.action}"
        )
        for w in self.cfg.config_warnings():
            self.log(f"[PERINGATAN KONFIGURASI] {w}")

    def _sync_closed_deals(self, equity: float):
        if self.dry_run:
            return
        cursor = self.guard.status()["last_deal_time"] or 0
        deals = self.client.get_closed_deals_since(self.cfg.symbol, self.cfg.magic, cursor)
        for d in deals:
            if self.guard.is_processed_deal(d["ticket"]):
                continue
            self.since_exit_bars = 0
            self.held_bars = 0
            pos_id = int(d.get("position_id", 0) or 0)
            self._init_risk_by_ticket.pop(pos_id, None)
            self._be_done_tickets.discard(pos_id)
            now = self._server_ts_to_utc(d["time"])
            events = self.guard.record_close(
                d["net_pnl"], equity, now=now,
                deal_ticket=d["ticket"], deal_time=d["time"],
            )
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

    def _manage_trailing_sl(self, pos: dict, sig, latest_close: float, tick: dict):
        """Geser SL ke breakeven dan/atau trailing ATR pada penutupan bar."""
        if self.cfg.be_trigger_r <= 0 and self.cfg.trail_atr_mult <= 0:
            return
        ticket = int(pos["ticket"])
        entry = float(pos.get("entry_price", 0.0) or 0.0)
        cur_sl = float(pos.get("sl", 0.0) or 0.0)
        if entry <= 0:
            return

        init_risk = self._init_risk_by_ticket.get(ticket, 0.0)
        if init_risk <= 0 and cur_sl > 0:
            init_risk = abs(entry - cur_sl)
            self._init_risk_by_ticket[ticket] = init_risk
        if init_risk <= 0:
            return

        is_buy = pos["side"] == "BUY"
        fav = (latest_close - entry) if is_buy else (entry - latest_close)
        target_sl = None
        tag = ""

        # Cek kondisi breakeven sudah pernah tercapai (berdasar SL existing atau trigger R)
        be_already = ticket in self._be_done_tickets or (
            cur_sl > 0 and ((is_buy and cur_sl >= entry) or (not is_buy and cur_sl <= entry))
        )
        if be_already:
            self._be_done_tickets.add(ticket)

        if self.cfg.be_trigger_r > 0 and not be_already and fav >= self.cfg.be_trigger_r * init_risk:
            buf = 0.05 * init_risk
            cand = entry + buf if is_buy else entry - buf
            if cur_sl <= 0 or (is_buy and cand > cur_sl) or (not is_buy and cand < cur_sl):
                target_sl = cand
                tag = f"BE @{self.cfg.be_trigger_r:g}R"
            self._be_done_tickets.add(ticket)
            be_already = True

        atr_val = float(getattr(sig, "atr", 0.0) or 0.0)
        if self.cfg.trail_atr_mult > 0 and atr_val > 0 and (self.cfg.be_trigger_r == 0 or be_already):
            cand = (latest_close - self.cfg.trail_atr_mult * atr_val) if is_buy else (latest_close + self.cfg.trail_atr_mult * atr_val)
            base_sl = target_sl if target_sl is not None else cur_sl
            if base_sl <= 0 or (is_buy and cand > base_sl) or (not is_buy and cand < base_sl):
                target_sl = cand
                tag = f"Trail {self.cfg.trail_atr_mult:g}xATR"

        if target_sl is None:
            return

        if self.dry_run:
            self.log(f"[DRY-RUN] GESER SL #{ticket} {pos['side']} -> {target_sl:,.2f} ({tag})")
            return

        res = self.client.modify_position_sl(pos, target_sl, tick_data=tick)
        if res["ok"]:
            self.log(f"SL DIGESER #{ticket} {pos['side']} -> {res['sl']:,.2f} ({tag})")
        else:
            self.log(f"[INFO] SL #{ticket} belum digeser ({tag}): {res['comment']}")

    def _handle_open_position(self, pos: dict, sig, latest_bar_ts: int, latest_close: float = 0.0, tick: dict = None):
        self.held_bars += 1
        effective_held = self.held_bars
        if pos.get("time", 0) > 0 and latest_bar_ts >= pos["time"]:
            effective_held = max(self.held_bars, int((latest_bar_ts - pos["time"]) // self.bar_sec))

        should_close = False
        reason = ""

        if (
            self.cfg.strategy in ("donchian", "ema_cross")
            and sig.action in ("BUY", "SELL")
            and sig.action != pos["side"]
        ):
            should_close, reason = True, "sinyal berbalik arah"
        elif self.cfg.max_hold_bars > 0 and effective_held >= self.cfg.max_hold_bars:
            should_close, reason = True, f"max hold {self.cfg.max_hold_bars} bar"

        if not should_close:
            if latest_close > 0:
                self._manage_trailing_sl(pos, sig, latest_close, tick)
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

    def _compute_sl_tp(self, side: str, entry: float, sig) -> tuple[float, float | None]:
        d = 1 if side == "BUY" else -1
        sl_dist = float(getattr(sig, "sl_distance", 0.0) or 0.0)
        tp_dist = float(getattr(sig, "tp_distance", 0.0) or 0.0)
        if sl_dist > 0:
            sl = round(entry - d * sl_dist, self.spec.digits)
        else:
            sl = round(entry * (1 - d * self.cfg.sl_pct), self.spec.digits)

        if tp_dist > 0:
            tp = round(entry + d * tp_dist, self.spec.digits)
        elif self.cfg.tp_pct > 0:
            tp = round(entry * (1 + d * self.cfg.tp_pct), self.spec.digits)
        else:
            tp = None
        return sl, tp

    def _try_enter(self, side: str, sig, tick: dict, acc: dict, open_count: int, bar_ts: int | None = None):
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
        sl, tp = self._compute_sl_tp(side, entry, sig)
        risk_entry = entry + d * self.cfg.deviation * self.spec.point
        if risk_entry <= 0 or (tp is not None and d * (tp - risk_entry) <= 0):
            self.log("[GUARD TOLAK] Batas slippage MT5 dapat melewati target atau menghasilkan harga tidak valid.")
            return

        now_utc = datetime.now(UTC)
        ref_bar_ts = bar_ts if bar_ts is not None else (self.last_bar_ts if self.last_bar_ts is not None else tick["time"] - self.bar_sec)
        self._update_clock_offset(tick["time"], ref_bar_ts, now_utc)
        price_time_utc = compute_price_time_utc(
            tick["time"], ref_bar_ts, self.bar_sec, now_utc, self.server_offset_sec, self.cfg.max_price_age_sec
        )

        verdict = self.guard.check_entry(
            side=side,
            entry=risk_entry,
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

        actual_risk = lot * self.spec.contract_size * abs(risk_entry - sl)
        actual_notional = lot * self.spec.contract_size * risk_entry
        actual_reasons = self.guard.check_actual_risk(
            actual_risk, acc["equity"], now=now_utc, actual_notional=actual_notional
        )
        if actual_reasons:
            self.log(f"[GUARD TOLAK] Lot {lot} ({side}) ditolak: {'; '.join(actual_reasons)}")
            return

        ok_margin, req_margin, free_margin = self.client.check_margin(
            self.cfg.symbol, side, lot, risk_entry
        )
        if not ok_margin:
            self.log(f"[SKIP] Margin tidak cukup untuk {lot} lot (butuh ${req_margin:.2f}, tersedia ${free_margin:.2f})")
            return

        tp_str = f"{tp:,.2f}" if tp else "-"
        if self.dry_run:
            self.log(
                f"[DRY-RUN] AKAN BUKA {side} {lot} lot {self.cfg.symbol} @ {entry:,.2f} | "
                f"TP {tp_str} | SL {sl:,.2f} | est. risiko ${actual_risk:.2f} | {sig.detail}"
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
            comment=f"EA_{self.cfg.strategy}"[:31],
            tick_data=tick,
        )
        if res["ok"]:
            self.guard.record_entry(now=datetime.now(UTC))
            self.held_bars = 0
            order_id = int(res.get("order", 0) or 0)
            if order_id > 0:
                self._init_risk_by_ticket[order_id] = abs(float(res["price"]) - float(res["sl"]))
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
        if not self.dry_run:
            for event in self.guard.check_equity(acc["equity"]):
                self.log(f"[RISK GUARD] {event}")
        self._sync_closed_deals(acc["equity"])

        fetch_count = max(MIN_BARS + 20, 600)
        candles = self.client.fetch_closed_candles(self.cfg.symbol, self.cfg.timeframe, count=fetch_count)
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
            self._handle_open_position(positions[0], sig, latest["ts"], latest_close=latest["close"], tick=tick)
            return True

        if sig.action in ("BUY", "SELL") and self.since_exit_bars >= self.cfg.cooldown_bars:
            self._try_enter(sig.action, sig, tick, acc, len(positions), bar_ts=latest["ts"])
        elif sig.action in ("BUY", "SELL"):
            rem = self.cfg.cooldown_bars - self.since_exit_bars
            self.log(f"  -> Sinyal {sig.action} dilewati (cooldown sisa {rem} bar)")
        return True

    def run(self, once: bool = False):
        if self.dry_run:
            self._run(once)
            return
        with single_instance_lock(instance_lock_path()):
            self._run(once)

    def _run(self, once: bool):
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
                except RiskStateError:
                    raise
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
