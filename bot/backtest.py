"""Backtest & walk-forward validator terintegrasi menggunakan logika sinyal bot/strategy.py."""
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config import Settings
from bot.strategy import MIN_BARS, compute_strategy_series

UTC = timezone.utc


@dataclass
class BacktestSummary:
    trades: int
    trades_per_day: float
    win_rate_pct: float
    profit_factor: float
    net_pnl_usd: float
    return_pct: float
    max_drawdown_pct: float
    avg_r: float
    max_loss_streak: int
    final_equity: float
    positive_folds: int
    total_folds: int
    fold_details: list[str]


def load_candles_csv(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append({
                "ts": int(float(row["ts"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 1.0) or 1.0),
            })
    rows.sort(key=lambda x: x["ts"])
    return rows


def _simulate_slice(
    df_bars: pd.DataFrame,
    strat_df: pd.DataFrame,
    cfg: Settings,
    initial_equity: float,
    spread_usd: float,
    slippage_usd: float,
    warmup_idx: int,
) -> dict:
    o = df_bars["open"].to_numpy(dtype=float)
    h = df_bars["high"].to_numpy(dtype=float)
    l = df_bars["low"].to_numpy(dtype=float)
    c = df_bars["close"].to_numpy(dtype=float)
    ts = df_bars["ts"].to_numpy(dtype="int64")

    sig = strat_df["signal"].to_numpy(dtype=np.int8)
    sl_d_arr = strat_df["sl_distance"].to_numpy(dtype=float)
    tp_d_arr = strat_df["tp_distance"].to_numpy(dtype=float)
    atr_arr = strat_df["atr"].to_numpy(dtype=float)

    n = len(df_bars)
    half_cost = spread_usd / 2.0 + slippage_usd
    equity = initial_equity
    peak = equity
    max_dd = 0.0

    pos = 0
    entry_px = sl_px = tp_px = init_risk = lot = 0.0
    entry_bar = -1
    last_exit_bar = -10**9
    be_moved = False
    trades_pnl = []
    trades_r = []

    for i in range(warmup_idx, n - 1):
        if pos != 0:
            exited = False
            exit_px = 0.0
            if pos == 1:
                if l[i] - half_cost <= sl_px:
                    exit_px = sl_px
                    exited = True
                elif tp_px > 0 and (h[i] - half_cost >= tp_px):
                    exit_px = tp_px
                    exited = True
            else:
                if h[i] + half_cost >= sl_px:
                    exit_px = sl_px
                    exited = True
                elif tp_px > 0 and (l[i] + half_cost <= tp_px):
                    exit_px = tp_px
                    exited = True

            if not exited and cfg.max_hold_bars > 0 and (i - entry_bar) >= cfg.max_hold_bars:
                exit_px = c[i] - half_cost if pos == 1 else c[i] + half_cost
                exited = True

            if not exited and cfg.strategy in ("donchian", "ema_cross") and sig[i] != 0 and sig[i] != pos:
                exit_px = o[i + 1] - half_cost if pos == 1 else o[i + 1] + half_cost
                exited = True

            if exited:
                pnl = pos * (exit_px - entry_px) * lot
                equity += pnl
                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
                trades_pnl.append(pnl)
                trades_r.append(pos * (exit_px - entry_px) / init_risk if init_risk > 0 else 0.0)
                pos = 0
                last_exit_bar = i
                be_moved = False
            else:
                if cfg.be_trigger_r > 0 and not be_moved and init_risk > 0:
                    fav = (c[i] - entry_px) if pos == 1 else (entry_px - c[i])
                    if fav >= cfg.be_trigger_r * init_risk:
                        buf = 0.05 * init_risk
                        cand = entry_px + buf if pos == 1 else entry_px - buf
                        if (pos == 1 and cand > sl_px) or (pos == -1 and cand < sl_px):
                            sl_px = cand
                        be_moved = True
                if cfg.trail_atr_mult > 0 and np.isfinite(atr_arr[i]) and atr_arr[i] > 0:
                    if cfg.be_trigger_r == 0 or be_moved:
                        cand = (c[i] - cfg.trail_atr_mult * atr_arr[i]) if pos == 1 else (c[i] + cfg.trail_atr_mult * atr_arr[i])
                        if (pos == 1 and cand > sl_px) or (pos == -1 and cand < sl_px):
                            sl_px = cand

        if pos == 0 and sig[i] != 0 and (i - last_exit_bar) >= cfg.cooldown_bars:
            d = int(sig[i])
            raw_open = o[i + 1]
            e_px = raw_open + half_cost if d == 1 else raw_open - half_cost
            sd = float(sl_d_arr[i]) if sl_d_arr[i] > 0 else e_px * cfg.sl_pct
            td = float(tp_d_arr[i]) if tp_d_arr[i] > 0 else (e_px * cfg.tp_pct if cfg.tp_pct > 0 else 0.0)
            if sd <= 0:
                continue
            trade_lot = cfg.fixed_lot if cfg.fixed_lot > 0 else max(0.01, round((equity * cfg.risk_per_trade) / sd, 2))
            if cfg.guard_enabled and trade_lot * sd > equity * cfg.risk_per_trade + 1e-9:
                continue
            pos = d
            entry_px = e_px
            sl_px = e_px - d * sd
            tp_px = (e_px + d * td) if td > 0 else 0.0
            init_risk = sd
            lot = trade_lot
            entry_bar = i + 1
            be_moved = False

    days = max(1.0, (int(ts[-1]) - int(ts[warmup_idx])) / 86400.0)
    if not trades_pnl:
        return {
            "trades": 0, "trades_per_day": 0.0, "win_rate": 0.0, "pf": 0.0,
            "net_pnl": 0.0, "ret_pct": 0.0, "max_dd": 0.0, "avg_r": 0.0,
            "max_ls": 0, "final_eq": equity,
        }
    arr = np.asarray(trades_pnl, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gw = float(wins.sum()) if len(wins) else 0.0
    gl = float(abs(losses.sum())) if len(losses) else 0.0
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    streak = max_streak = 0
    for p in arr:
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "trades": len(arr),
        "trades_per_day": len(arr) / days,
        "win_rate": 100.0 * len(wins) / len(arr),
        "pf": pf,
        "net_pnl": equity - initial_equity,
        "ret_pct": 100.0 * (equity - initial_equity) / initial_equity,
        "max_dd": 100.0 * max_dd,
        "avg_r": float(np.mean(trades_r)),
        "max_ls": max_streak,
        "final_eq": equity,
    }


def run_walkforward_backtest(
    candles: list[dict],
    cfg: Settings,
    initial_equity: float = 500.0,
    spread_usd: float = 10.0,
    slippage_usd: float = 2.0,
    folds: int = 4,
) -> BacktestSummary:
    if len(candles) < MIN_BARS + 20:
        raise ValueError(f"Butuh minimal {MIN_BARS + 20} candle untuk backtest (tersedia {len(candles)}).")
    df_bars = pd.DataFrame(candles)
    strat_df = compute_strategy_series(df_bars, strategy=cfg.strategy, p=cfg.params())
    warmup = 250
    full = _simulate_slice(df_bars, strat_df, cfg, initial_equity, spread_usd, slippage_usd, warmup)

    usable = len(df_bars) - warmup
    step = usable // folds
    pos_folds = 0
    fold_details = []
    if step >= 50:
        for k in range(folds):
            w_start = warmup + k * step
            w_end = warmup + (k + 1) * step if k < folds - 1 else len(df_bars)
            res = _simulate_slice(
                df_bars.iloc[:w_end].reset_index(drop=True),
                strat_df.iloc[:w_end].reset_index(drop=True),
                cfg, initial_equity, spread_usd, slippage_usd, w_start,
            )
            if res["trades"] > 0 and res["pf"] > 1.0:
                pos_folds += 1
            fold_details.append(f"F{k+1}: PF={res['pf']:.2f} WR={res['win_rate']:.0f}% Tr={res['trades']}")
    else:
        folds = 1
        pos_folds = 1 if full["pf"] > 1.0 and full["trades"] > 0 else 0
        fold_details.append(f"F1: PF={full['pf']:.2f} WR={full['win_rate']:.0f}% Tr={full['trades']}")

    return BacktestSummary(
        trades=full["trades"],
        trades_per_day=round(full["trades_per_day"], 2),
        win_rate_pct=round(full["win_rate"], 2),
        profit_factor=round(full["pf"], 3),
        net_pnl_usd=round(full["net_pnl"], 2),
        return_pct=round(full["ret_pct"], 2),
        max_drawdown_pct=round(full["max_dd"], 2),
        avg_r=round(full["avg_r"], 3),
        max_loss_streak=full["max_ls"],
        final_equity=round(full["final_eq"], 2),
        positive_folds=pos_folds,
        total_folds=folds,
        fold_details=fold_details,
    )
