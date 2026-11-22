"""
EA Riyan BTC — Terminal Algo Trading Bot untuk MetaTrader 5 (Windows).

Perintah:
  python main.py check                 cek koneksi MT5, simbol, margin, strategi & RiskGuard
  python main.py dry-run               simulasi live pakai data MT5 (tanpa kirim order asli)
  python main.py run                   jalankan algo trading LIVE di MT5 yang sedang terbuka
  python main.py once [--dry-run]      jalankan 1 siklus saja lalu berhenti
  python main.py status                lihat akun MT5, posisi terbuka bot, & status RiskGuard
  python main.py close-all             tutup semua posisi terbuka milik bot ini
  python main.py reset-kill            buka kill-switch RiskGuard setelah peninjauan
"""
import argparse
import sys

from bot.config import ROOT, Settings
from bot.mt5_client import MT5Client, MT5Error, compute_lot
from bot.risk_guard import RiskGuard
from bot.runner import TradingBot
from bot.strategy import MIN_BARS, evaluate_signal


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
            row("OK", f"Simbol {cfg.symbol}",
                f"Bid {tick['bid']:,.2f} / Ask {tick['ask']:,.2f} | Spread {tick['spread']:.2f} | "
                f"Lot min {spec.volume_min} step {spec.volume_step}")

            candles = client.fetch_closed_candles(cfg.symbol, cfg.timeframe, count=max(MIN_BARS + 10, 320))
            sig = evaluate_signal([c["close"] for c in candles], cfg.strategy, cfg.params())
            row("OK", f"Strategi ({cfg.strategy})",
                f"{len(candles)} candle {cfg.timeframe} | RSI {sig.rsi:.1f} | Trend {'NAIK' if sig.trend_up else 'TURUN'} | Sinyal: {sig.action}")

            guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
            entry = tick["ask"]
            sl = entry * (1 - cfg.sl_pct)
            tp = entry * (1 + cfg.tp_pct)
            v = guard.check_entry("BUY", entry, sl, tp, equity=acc["equity"], open_positions=0)
            lot = compute_lot(spec, v.qty if v.qty > 0 else 0.0, cfg.fixed_lot, cfg.allow_min_lot)
            ok_m, req_m, free_m = client.check_margin(cfg.symbol, "BUY", lot or spec.volume_min, entry)
            row("OK" if ok_m else "GAGAL", "Simulasi Lot & Margin",
                f"Lot {lot} | Margin butuh ${req_m:.2f} (tersedia ${free_m:.2f}) | "
                f"Risiko SL {cfg.sl_pct*100:.2f}% = ${lot * spec.contract_size * abs(entry - sl):.2f}")

            gs = guard.status()
            row("PERINGATAN" if gs["killed"] else "OK", "RiskGuard",
                f"KILL-SWITCH AKTIF ({gs['kill_reason']})" if gs["killed"]
                else f"Aktif | Trade hari ini: {gs['trades_today']}/{cfg.max_trades_per_day} | PnL hari ini: ${gs['pnl_today']:+.2f}")
    except MT5Error as e:
        row("GAGAL", "MetaTrader 5", str(e))
        return 1

    say("\nSemua siap! Jalankan 'python main.py dry-run' untuk uji live, atau 'python main.py run' untuk algo trading.")
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
    try:
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
                    say(f"GAGAL tutup #{p['ticket']}: retcode={res['retcode']} ({res['comment']})")
    except MT5Error as e:
        say(f"[MT5 ERROR] {e}")
        return 1
    return 0


def cmd_reset_kill(cfg: Settings) -> int:
    guard = RiskGuard(cfg.risk_config(), cfg.guard_state_path)
    if not guard.status()["killed"]:
        say("Kill-switch sedang tidak aktif.")
        return 0
    new_peak = None
    try:
        with MT5Client(cfg.mt5_path) as client:
            new_peak = client.account_info()["equity"]
    except MT5Error:
        pass
    guard.reset_kill(new_peak=new_peak)
    say(f"Kill-switch berhasil direset (peak equity baru: {f'${new_peak:,.2f}' if new_peak else 'tetap'}).")
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

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    cfg = Settings.load()
    handlers = {
        "check": lambda: cmd_check(cfg),
        "dry-run": lambda: cmd_run(cfg, dry_run=True),
        "run": lambda: cmd_run(cfg, dry_run=False),
        "once": lambda: cmd_run(cfg, dry_run=args.dry_run, once=True),
        "status": lambda: cmd_status(cfg),
        "close-all": lambda: cmd_close_all(cfg),
        "reset-kill": lambda: cmd_reset_kill(cfg),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
