"""
EA Riyan BTC — Terminal Algo Trading Bot untuk MetaTrader 5 (Windows).

Perintah:
  python main.py check                 cek koneksi MT5, simbol, margin, strategi & RiskGuard
  python main.py dry-run               simulasi live pakai data MT5 (tanpa kirim order asli)
  python main.py run                   jalankan algo trading LIVE di MT5 yang sedang terbuka
  python main.py once [--dry-run]      jalankan 1 siklus saja lalu berhenti
  python main.py status                lihat akun MT5, posisi terbuka bot, & status RiskGuard
  python main.py backtest              uji backtest & walk-forward strategi pada data historis
  python main.py close-all             tutup semua posisi terbuka milik bot ini
  python main.py reset-kill            buka kill-switch RiskGuard setelah peninjauan
"""
import argparse
import sys
from datetime import datetime, timezone

from bot.backtest import load_candles_csv, run_walkforward_backtest
from bot.config import ROOT, Settings
from bot.mt5_client import MT5Client, MT5Error, compute_lot, timeframe_info
from bot.risk_guard import RiskGuard, RiskStateError
from bot.runner import (
    TradingBot,
    calibrate_server_offset,
    compute_price_time_utc,
    instance_lock_path,
    single_instance_lock,
)
from bot.strategy import MIN_BARS, evaluate_signal

UTC = timezone.utc


def say(msg=""):
    print(msg, flush=True)


def cmd_check(cfg: Settings) -> int:
    say(f"Pemeriksaan Bot MT5 (folder: {ROOT})\n")
    ok_all = True

    def row(status, label, detail):
        nonlocal ok_all
        if status == "GAGAL":
            ok_all = False
        say(f"  [{status:<10}] {label}: {detail}")

    row("OK" if (ROOT / ".env").exists() else "PERINGATAN", "File .env",
        ".env ditemukan" if (ROOT / ".env").exists() else "belum ada .env (pakai nilai bawaan)")

    try:
        with MT5Client(cfg.mt5_path) as client:
            ti = client.terminal_info()
            acc = client.account_info()
            acct_type = "DEMO" if acc["is_demo"] else "REAL"
            row("OK", "Koneksi MT5", f"{ti['company']} | Login {acc['login']} ({acct_type}) @ {acc['server']}")
            row("OK" if ti["trade_allowed"] else "GAGAL", "Algo Trading MT5",
                "AKTIF" if ti["trade_allowed"] else "MATI -> aktifkan tombol 'Algo Trading' di toolbar MT5!")
            row("OK", "Saldo & Equity",
                f"Saldo ${acc['balance']:,.2f} | Equity ${acc['equity']:,.2f} | Free Margin ${acc['margin_free']:,.2f} | Lev 1:{acc['leverage']}")

            spec = client.symbol_spec(cfg.symbol)
            tick = client.tick(cfg.symbol)
            spread_pts = tick["spread"] / spec.point if spec.point > 0 else 0.0
            spread_ok = cfg.max_spread_points == 0 or spread_pts <= cfg.max_spread_points
            row("OK" if spread_ok else "PERINGATAN", f"Simbol {cfg.symbol}",
                f"Bid {tick['bid']:,.2f} / Ask {tick['ask']:,.2f} | Spread {spread_pts:.0f} pts | "
                f"Lot min {spec.volume_min} step {spec.volume_step}")

            candles = client.fetch_closed_candles(cfg.symbol, cfg.timeframe, count=max(MIN_BARS + 10, 600))
            sig = evaluate_signal(candles, cfg.strategy, cfg.params())
            atr_note = f" | ATR ${sig.atr:,.2f} | ADX {sig.adx:.1f}" if sig.atr > 0 else ""
            row("OK", f"Strategi ({cfg.strategy})",
                f"{len(candles)} candle {cfg.timeframe} | RSI {sig.rsi:.1f}{atr_note} | "
                f"Trend {'NAIK' if sig.trend_up else 'TURUN'} | Sinyal: {sig.action}")

            _, bar_sec = timeframe_info(cfg.timeframe)
            now_utc = datetime.now(UTC)
            latest_ts = candles[-1]["ts"]
            offset = calibrate_server_offset(tick["time"], latest_ts, bar_sec, now_utc, cfg.max_price_age_sec)
            price_time_utc = compute_price_time_utc(
                tick["time"], latest_ts, bar_sec, now_utc, offset, cfg.max_price_age_sec
            )
            offset_str = f"{offset:+.0f}s" if offset is not None else "GAGAL KALIBRASI"
            row("OK" if offset is not None else "GAGAL", "Sinkronisasi Jam MT5",
                f"Offset server vs PC: {offset_str} | Umur quote terkalibrasi: {(now_utc - price_time_utc).total_seconds():.0f}s")

            guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
            entry = tick["ask"]
            sl_dist = sig.sl_distance if sig.sl_distance > 0 else (sig.atr * cfg.atr_sl_mult if cfg.uses_atr_stops() and sig.atr > 0 else entry * cfg.sl_pct)
            tp_dist = sig.tp_distance if sig.tp_distance > 0 else (sig.atr * cfg.atr_tp_mult if cfg.uses_atr_stops() and sig.atr > 0 else entry * cfg.tp_pct)
            sl = round(entry - sl_dist, spec.digits)
            tp = round(entry + tp_dist, spec.digits) if tp_dist > 0 else None
            risk_entry = entry + cfg.deviation * spec.point
            v = guard.check_entry(
                "BUY", risk_entry, sl, tp,
                equity=acc["equity"], price_time=price_time_utc, now=now_utc, open_positions=0,
            )
            lot = compute_lot(spec, v.qty if v.qty > 0 else 0.0, cfg.fixed_lot, cfg.allow_min_lot)
            actual_risk = lot * spec.contract_size * abs(risk_entry - sl)
            actual_notional = lot * spec.contract_size * risk_entry
            actual_reasons = guard.check_actual_risk(actual_risk, acc["equity"], now=now_utc, actual_notional=actual_notional)
            ok_m, req_m, free_m = client.check_margin(cfg.symbol, "BUY", lot or spec.volume_min, risk_entry)

            sim_ok = ok_m and v.ok and not actual_reasons and lot > 0
            sim_err = "; ".join(v.reasons + actual_reasons)
            row("OK" if sim_ok else "GAGAL", "Simulasi Lot & Risk",
                f"Lot {lot} | Margin ${req_m:.2f} (bebas ${free_m:.2f}) | Risiko SL ${actual_risk:.2f}"
                + (f" | Ditolak: {sim_err}" if sim_err else ""))

            gs = guard.status()
            row("PERINGATAN" if gs["killed"] else "OK", "RiskGuard",
                f"KILL-SWITCH AKTIF ({gs['kill_reason']})" if gs["killed"]
                else f"Aktif | Trade hari ini: {gs['trades_today']}/{cfg.max_trades_per_day} | PnL hari ini: ${gs['pnl_today']:+.2f}")
    except MT5Error as e:
        row("GAGAL", "MetaTrader 5", str(e))
        return 1

    for w in cfg.config_warnings():
        row("PERINGATAN", "Konfigurasi", w)

    if ok_all:
        say("\nSemua siap! Jalankan 'python main.py dry-run' untuk uji live, atau 'python main.py run' untuk algo trading.")
    else:
        say("\nAda pemeriksaan yang GAGAL di atas. Perbaiki dulu sebelum menjalankan live trading.")
    return 0 if ok_all else 1


def cmd_status(cfg: Settings) -> int:
    guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
    try:
        with MT5Client(cfg.mt5_path) as client:
            acc = client.account_info()
            ti = client.terminal_info()
            positions = client.get_open_positions(cfg.symbol, cfg.magic)
            say(f"Akun MT5     : {acc['login']} ({'DEMO' if acc['is_demo'] else 'REAL'}) @ {acc['server']}")
            say(f"Algo Trading : {'AKTIF' if ti['trade_allowed'] else 'MATI'}")
            say(f"Saldo/Equity : ${acc['balance']:,.2f} / ${acc['equity']:,.2f} (floating ${acc['profit']:+,.2f})")
            say(f"Bot Config   : {cfg.symbol} {cfg.timeframe} | Strategi: {cfg.strategy} | Magic: {cfg.magic}")
            if positions:
                say(f"\nPosisi terbuka milik bot ({len(positions)}):")
                for p in positions:
                    say(f"  #{p['ticket']} {p['side']} {p['volume']} lot @ {p['entry_price']:,.2f} | "
                        f"Now {p['current_price']:,.2f} | SL {p['sl']:,.2f} | TP {p['tp']:,.2f} | PnL ${p['profit']:+.2f}")
            else:
                say("Posisi bot   : Tidak ada posisi terbuka.")
    except MT5Error as e:
        say(f"[MT5 ERROR] {e}")
        return 1

    g = guard.status()
    say(f"RiskGuard    : {'KILL-SWITCH: ' + g['kill_reason'] if g['killed'] else 'Normal'} | "
        f"Trade hari ini: {g['trades_today']} | PnL hari ini: ${g['pnl_today']:+.2f} | Jeda: {g['pause_until'] or '-'}")
    return 0


def cmd_close_all(cfg: Settings) -> int:
    failed = False
    try:
        with single_instance_lock(instance_lock_path()):
            with MT5Client(cfg.mt5_path) as client:
                positions = client.get_open_positions(cfg.symbol, cfg.magic)
                if not positions:
                    say(f"Tidak ada posisi terbuka milik bot (magic {cfg.magic}) pada {cfg.symbol}.")
                    return 0
                for p in positions:
                    res = client.close_position(p, cfg.magic, cfg.deviation, comment="EA_CloseAll")
                    if res["ok"]:
                        say(f"DITUTUP #{p['ticket']} {p['side']} {p['volume']} lot @ {res['price']:,.2f}")
                    else:
                        failed = True
                        say(f"GAGAL tutup #{p['ticket']}: retcode={res['retcode']} ({res['comment']})")
    except MT5Error as e:
        say(f"[GAGAL] {e}")
        return 1
    return 1 if failed else 0


def cmd_reset_kill(cfg: Settings) -> int:
    try:
        with single_instance_lock(instance_lock_path()):
            guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
            if not guard.status()["killed"]:
                say("Kill-switch sedang tidak aktif.")
                return 0
            with MT5Client(cfg.mt5_path) as client:
                new_peak = client.account_info()["equity"]
            guard.reset_kill(new_peak=new_peak)
    except MT5Error as e:
        say(f"[GAGAL] Kill-switch tidak direset: {e}")
        return 1
    say(f"Kill-switch berhasil direset (peak equity baru: {f'${new_peak:,.2f}' if new_peak is not None else 'tetap'}).")
    return 0


def cmd_run(cfg: Settings, dry_run: bool = False, once: bool = False) -> int:
    try:
        with MT5Client(cfg.mt5_path) as client:
            ti = client.terminal_info()
            if not dry_run and not ti["trade_allowed"]:
                say("[GAGAL] Tombol 'Algo Trading' di MT5 sedang MATI. Aktifkan dulu di toolbar MT5 lalu jalankan ulang.")
                return 1
            guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
            bot = TradingBot(cfg, client, guard, dry_run=dry_run, log=say)
            bot.run(once=once)
    except MT5Error as e:
        say(f"[MT5 ERROR] {e}")
        return 1
    return 0


def cmd_backtest(cfg: Settings, csv_path: str | None, bars: int, spread: float, equity: float) -> int:
    try:
        if csv_path:
            candles = load_candles_csv(csv_path)
            source = csv_path
        else:
            with MT5Client(cfg.mt5_path) as client:
                candles = client.fetch_closed_candles(cfg.symbol, cfg.timeframe, count=bars)
                source = f"MT5 {cfg.symbol} {cfg.timeframe}"
    except (OSError, ValueError, MT5Error) as e:
        say(f"[BACKTEST ERROR] {e}")
        return 1

    res = run_walkforward_backtest(
        candles, cfg, initial_equity=equity, spread_usd=spread, slippage_usd=2.0, folds=4,
    )
    say(f"=== Hasil Backtest ({cfg.strategy} | {source} | {len(candles)} bar) ===")
    say(f"  Modal Awal   : ${equity:,.2f} -> Akhir ${res.final_equity:,.2f} ({res.return_pct:+.2f}%)")
    say(f"  Total Trade  : {res.trades} ({res.trades_per_day}/hari)")
    say(f"  Win Rate     : {res.win_rate_pct:.2f}% | Profit Factor: {res.profit_factor:.3f}")
    say(f"  Max Drawdown : {res.max_drawdown_pct:.2f}% | Max Loss Streak: {res.max_loss_streak} | Avg R: {res.avg_r:+.3f}")
    say(f"  Walk-Forward : {res.positive_folds}/{res.total_folds} fold profit ({' | '.join(res.fold_details)})")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="EA Riyan BTC — Terminal Algo Trading Bot untuk MT5 Windows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd")
    for name in ("check", "dry-run", "run", "status", "close-all", "reset-kill"):
        sub.add_parser(name)
    p_once = sub.add_parser("once")
    p_once.add_argument("--dry-run", action="store_true")
    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--csv", default=None, help="Path file CSV (ts,open,high,low,close,volume)")
    p_bt.add_argument("--bars", type=int, default=5000, help="Jumlah candle MT5 bila tanpa --csv")
    p_bt.add_argument("--spread", type=float, default=10.0, help="Asumsi spread USD")
    p_bt.add_argument("--equity", type=float, default=500.0, help="Modal awal simulasi USD")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    try:
        cfg = Settings.load()
    except ValueError as e:
        say(f"[CONFIG ERROR] {e}")
        return 2
    handlers = {
        "check": lambda: cmd_check(cfg),
        "dry-run": lambda: cmd_run(cfg, dry_run=True),
        "run": lambda: cmd_run(cfg, dry_run=False),
        "once": lambda: cmd_run(cfg, dry_run=args.dry_run, once=True),
        "status": lambda: cmd_status(cfg),
        "backtest": lambda: cmd_backtest(cfg, args.csv, args.bars, args.spread, args.equity),
        "close-all": lambda: cmd_close_all(cfg),
        "reset-kill": lambda: cmd_reset_kill(cfg),
    }
    try:
        return handlers[args.cmd]()
    except RiskStateError as e:
        say(f"[RISK ERROR] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
