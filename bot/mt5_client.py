"""
Wrapper MetaTrader 5 untuk Windows.
Terhubung langsung ke terminal MT5 yang sedang terbuka (tanpa perlu login ulang).
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

UTC = timezone.utc

TIMEFRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 60),
    "M5": (mt5.TIMEFRAME_M5, 300),
    "M15": (mt5.TIMEFRAME_M15, 900),
    "M30": (mt5.TIMEFRAME_M30, 1800),
    "H1": (mt5.TIMEFRAME_H1, 3600),
    "H4": (mt5.TIMEFRAME_H4, 14400),
    "D1": (mt5.TIMEFRAME_D1, 86400),
}


class MT5Error(Exception):
    pass


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    stops_level: int
    filling_mode: int
    trade_mode: int = mt5.SYMBOL_TRADE_MODE_FULL


def timeframe_info(tf_name: str):
    key = tf_name.strip().upper()
    if key not in TIMEFRAMES:
        raise MT5Error(f"Timeframe tidak didukung: {tf_name}. Pilih: {', '.join(TIMEFRAMES)}")
    return TIMEFRAMES[key]


def round_lot(volume: float, spec: SymbolSpec) -> float:
    step = spec.volume_step or 0.01
    steps = math.floor((volume + 1e-12) / step)
    rounded = round(steps * step, 8)
    return min(rounded, spec.volume_max)


def compute_lot(spec: SymbolSpec, raw_qty: float, fixed_lot: float = 0.0, allow_min_lot: bool = True) -> float:
    """Hitung lot MT5 yang valid dari ukuran posisi RiskGuard atau fixed_lot."""
    if fixed_lot and fixed_lot > 0:
        return max(round_lot(fixed_lot, spec), spec.volume_min)

    contract = spec.contract_size if spec.contract_size > 0 else 1.0
    target = raw_qty / contract
    lot = round_lot(target, spec)
    if lot < spec.volume_min:
        return spec.volume_min if allow_min_lot else 0.0
    return lot


def resolve_filling_mode(spec: SymbolSpec) -> int:
    # Bitmask MT5: 1 = SYMBOL_FILLING_FOK, 2 = SYMBOL_FILLING_IOC
    if spec.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if spec.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def filling_candidates(spec: SymbolSpec) -> list[int]:
    primary = resolve_filling_mode(spec)
    modes = [primary]
    for m in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        if m not in modes:
            modes.append(m)
    return modes


class MT5Client:
    def __init__(self, path: str = ""):
        self.path = path
        self.connected = False

    def connect(self):
        ok = mt5.initialize(path=self.path) if self.path else mt5.initialize()
        if not ok:
            code, msg = mt5.last_error()
            raise MT5Error(f"Gagal terhubung ke MT5 ({code}): {msg}. Pastikan MetaTrader 5 Windows sedang terbuka.")
        self.connected = True
        return self

    def disconnect(self):
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def reconnect(self):
        self.disconnect()
        return self.connect()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.disconnect()

    def terminal_info(self) -> dict:
        ti = mt5.terminal_info()
        if ti is None:
            raise MT5Error(f"Gagal membaca info terminal MT5: {mt5.last_error()}")
        return {
            "connected": bool(ti.connected),
            "trade_allowed": bool(ti.trade_allowed),
            "company": ti.company,
            "path": ti.path,
        }

    def account_info(self) -> dict:
        ai = mt5.account_info()
        if ai is None:
            raise MT5Error(f"Gagal membaca akun MT5: {mt5.last_error()}")
        return {
            "login": int(ai.login),
            "server": str(ai.server),
            "currency": str(ai.currency),
            "balance": float(ai.balance),
            "equity": float(ai.equity),
            "profit": float(ai.profit),
            "margin_free": float(ai.margin_free),
            "leverage": int(ai.leverage),
            "is_demo": ai.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
            "trade_allowed": bool(ai.trade_allowed),
        }

    def find_btc_symbols(self) -> list:
        syms = mt5.symbols_get() or []
        return [s.name for s in syms if "BTC" in s.name.upper()]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        info = mt5.symbol_info(symbol)
        if info is None:
            btc_list = self.find_btc_symbols()[:8]
            hint = f" Simbol BTC tersedia: {', '.join(btc_list)}" if btc_list else ""
            raise MT5Error(f"Simbol '{symbol}' tidak ditemukan di MT5.{hint}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise MT5Error(f"Gagal menampilkan simbol '{symbol}' di Market Watch.")
        return SymbolSpec(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            contract_size=float(info.trade_contract_size),
            stops_level=int(info.trade_stops_level),
            filling_mode=int(info.filling_mode),
            trade_mode=int(getattr(info, "trade_mode", mt5.SYMBOL_TRADE_MODE_FULL)),
        )

    def tick(self, symbol: str) -> dict:
        t = mt5.symbol_info_tick(symbol)
        if t is None or (t.bid <= 0 and t.ask <= 0):
            raise MT5Error(f"Quote live tidak tersedia untuk '{symbol}' (bid/ask = 0). Cek simbol di Market Watch.")
        return {
            "bid": float(t.bid),
            "ask": float(t.ask),
            "time": int(t.time),
            "spread": float(t.ask - t.bid),
        }

    def fetch_closed_candles(self, symbol: str, timeframe: str, count: int = 400) -> list:
        """Ambil candle yang SUDAH TUTUP (start_pos=1 melewati bar yang masih berjalan)."""
        tf_const, _ = timeframe_info(timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, count)
        if rates is None or len(rates) == 0:
            raise MT5Error(f"Gagal mengambil candle {symbol} {timeframe}: {mt5.last_error()}")
        return [
            {
                "ts": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["tick_volume"]),
            }
            for r in rates
        ]

    def get_open_positions(self, symbol: str, magic: int) -> list:
        rows = mt5.positions_get(symbol=symbol)
        if rows is None:
            return []
        result = []
        for p in rows:
            if int(p.magic) != magic:
                continue
            result.append({
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": float(p.volume),
                "entry_price": float(p.price_open),
                "current_price": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "swap": float(p.swap),
                "time": int(p.time),
                "magic": int(p.magic),
            })
        return result

    def check_margin(self, symbol: str, side: str, lot: float, price: float):
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        req_margin = mt5.order_calc_margin(order_type, symbol, lot, price)
        acc = self.account_info()
        if req_margin is None:
            return False, 0.0, acc["margin_free"]
        return acc["margin_free"] >= float(req_margin), float(req_margin), acc["margin_free"]

    def _send_with_fill_fallback(self, base_req: dict, spec: SymbolSpec):
        res = None
        for mode in filling_candidates(spec):
            req = dict(base_req, type_filling=mode)
            res = mt5.order_send(req)
            if res is None:
                raise MT5Error(f"order_send mengembalikan None: {mt5.last_error()}")
            if res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                break
        return res

    def open_market_order(
        self,
        symbol: str,
        side: str,
        lot: float,
        sl: float,
        tp: float,
        magic: int,
        deviation: int = 500,
        comment: str = "EA_Riyan_BTC",
    ) -> dict:
        spec = self.symbol_spec(symbol)
        t = self.tick(symbol)
        is_buy = side.upper() == "BUY"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = t["ask"] if is_buy else t["bid"]
        close_ref = t["bid"] if is_buy else t["ask"]

        # Validasi jarak minimal stops_level broker relatif terhadap harga eksekusi tutup (Bid untuk BUY, Ask untuk SELL)
        min_dist = spec.stops_level * spec.point
        sl_rounded = round(sl, spec.digits) if sl and sl > 0 else 0.0
        tp_rounded = round(tp, spec.digits) if tp and tp > 0 else 0.0
        if min_dist > 0:
            if sl_rounded > 0:
                if is_buy and (close_ref - sl_rounded) < min_dist:
                    sl_rounded = round(close_ref - min_dist, spec.digits)
                elif not is_buy and (sl_rounded - close_ref) < min_dist:
                    sl_rounded = round(close_ref + min_dist, spec.digits)
            if tp_rounded > 0:
                if is_buy and (tp_rounded - close_ref) < min_dist:
                    tp_rounded = round(close_ref + min_dist, spec.digits)
                elif not is_buy and (close_ref - tp_rounded) < min_dist:
                    tp_rounded = round(close_ref - min_dist, spec.digits)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": price,
            "sl": sl_rounded,
            "tp": tp_rounded,
            "deviation": int(deviation),
            "magic": int(magic),
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        res = self._send_with_fill_fallback(req, spec)
        ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
        return {
            "ok": ok,
            "retcode": int(res.retcode),
            "comment": str(res.comment),
            "order": int(res.order),
            "deal": int(res.deal),
            "price": float(res.price or price),
            "volume": float(res.volume or lot),
            "sl": sl_rounded,
            "tp": tp_rounded,
        }

    def close_position(self, pos: dict, magic: int, deviation: int = 500, comment: str = "EA_Close") -> dict:
        spec = self.symbol_spec(pos["symbol"])
        t = self.tick(pos["symbol"])
        is_buy = pos["side"] == "BUY"
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        price = t["bid"] if is_buy else t["ask"]

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos["symbol"],
            "volume": float(pos["volume"]),
            "type": close_type,
            "position": int(pos["ticket"]),
            "price": price,
            "deviation": int(deviation),
            "magic": int(magic),
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        res = self._send_with_fill_fallback(req, spec)
        ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
        return {
            "ok": ok,
            "retcode": int(res.retcode),
            "comment": str(res.comment),
            "price": float(res.price or price),
            "ticket": int(pos["ticket"]),
        }

    def get_closed_deals_since(self, symbol: str, magic: int, since_ts: int) -> list:
        """Ambil deal penutupan (TP/SL/manual) milik bot sejak since_ts."""
        start_dt = datetime.fromtimestamp(max(0, since_ts - 3600), UTC)
        end_dt = datetime.now(UTC) + timedelta(days=1)
        deals = mt5.history_deals_get(start_dt, end_dt)
        if not deals:
            return []
        out = []
        for d in deals:
            if int(d.magic) != magic or str(d.symbol) != symbol:
                continue
            if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
                continue
            if int(d.time) < since_ts:
                continue
            net_pnl = float(d.profit) + float(d.commission) + float(d.swap)
            out.append({
                "ticket": int(d.ticket),
                "position_id": int(d.position_id),
                "time": int(d.time),
                "price": float(d.price),
                "volume": float(d.volume),
                "profit": float(d.profit),
                "commission": float(d.commission),
                "swap": float(d.swap),
                "net_pnl": net_pnl,
                "comment": str(d.comment),
            })
        out.sort(key=lambda x: (x["time"], x["ticket"]))
        return out