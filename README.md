# EA Riyan BTC — Algo Trading Bot MT5 (Windows Terminal)

Bot algo trading Python yang langsung terhubung ke **MetaTrader 5 Windows yang sedang terbuka**, berjalan di terminal tanpa perlu XAMPP, PHP, atau MySQL.

## Fitur Utama
- **Langsung ke MT5 terbuka**: otomatis terhubung ke terminal MT5 aktif (`MetaTrader5` Python API), tanpa perlu isi password ulang.
- **Pilihan strategi** (`STRATEGY` di `.env`):
  - `mtf_pullback` *(default, rekomendasi untuk `M30`)*: Multi-Timeframe trend pullback — bias `H1` (`EMA 20/50`) + tren `EMA 21/50/200` + filter kekuatan tren `ADX(14) >= 18` + pemicu pullback `RSI(7)` + SL/TP adaptif berbasis `ATR(14)` (`1.5x` SL / `3.0x` TP) + otomatis geser SL ke Breakeven di `+1.0R`.
  - `mtf_combo`: Kombinasi adaptif `session_box` (Pre-London breakout `05:00–08:00` -> `08:00–16:00 UTC`) + `mtf_pullback`.
  - `session_box`: Breakout kotak konsolidasi sesi pra-London (`05:00–08:00 UTC`) dengan filter tren `H1`, `EMA 50/200`, dan volume.
  - `scalp`, `donchian`, `ema_cross`: Strategi bawaan versi awal (legacy).
- **Kalibrasi Jam Server MT5 vs PC**: otomatis mengkompensasi perbedaan jam PC Windows terhadap server MT5 (seperti drift jam PC atau zona waktu server broker UTC+2/+3) tanpa mematikan deteksi quote basi (*stale feed*).
- **Eksekusi candle tertutup & tanpa lookahead**: hanya membaca candle yang sudah close (`start_pos=1`), termasuk resampling `H1` yang hanya memakai bar 1 jam yang sudah selesai.
- **Breakeven & Trailing SL otomatis**: pada penutupan bar saat posisi sudah profit `>= BE_TRIGGER_R * risk`, bot otomatis menggeser SL ke area aman (`entry + 0.05R`) melalui `TRADE_ACTION_SLTP`.
- **RiskGuard bawaan**: batas risiko aktual per trade dan leverage setelah pembulatan lot, rugi harian, jumlah trade/hari, jeda rugi beruntun, minimal Reward:Risk, cek kesegaran quote, dan drawdown kill-switch.
- **Backtest & Walk-Forward CLI**: uji strategi langsung terhadap data historis MT5 atau file CSV lewat `python main.py backtest`.
- **Satu instance live**: hanya satu proses live dalam workspace ini, karena state risiko disimpan bersama.
- **Magic Number terpisah** (`MT5_MAGIC=202611`): hanya mengelola posisi milik bot ini, tidak menyentuh posisi manual.
- **Log otomatis**: mencatat aktivitas ke `results/bot.log`, riwayat trade ke `results/trades.csv`, dan state risk guard ke `results/risk_state.json`.

## Struktur Folder
```
EA_Riyan_btc/
├── main.py              # Entry point CLI terminal
├── run.bat              # Pintasan Windows (otomatis pakai .venv)
├── .env                 # Konfigurasi aktif (jangan dibagikan)
├── .env.example         # Template konfigurasi
├── requirements.txt     # MetaTrader5, numpy, pandas
├── bot/
│   ├── config.py        # Loader konfigurasi .env
│   ├── strategy.py      # Logika sinyal (mtf_pullback, mtf_combo, session_box, legacy)
│   ├── risk_guard.py    # Pengaman risiko pra-trade & kill-switch
│   ├── mt5_client.py    # Koneksi, data candle, lot, modifikasi SL, & eksekusi order MT5
│   ├── backtest.py      # Engine backtest & walk-forward terintegrasi
│   └── runner.py        # Loop utama algo trading terminal
├── tests/
│   └── test_bot.py      # Unit test & uji paritas otomatis
└── results/             # Log & state lokal bot
```

## Persiapan
1. Buka **MetaTrader 5** di Windows dan login ke akun (disarankan uji dulu di **Demo**).
2. Pastikan tombol **Algo Trading** di toolbar MT5 menyala hijau (aktif).
3. Pastikan simbol (mis. `BTCUSDm`) aktif di Market Watch.
4. Instal dependensi jika belum ada:
   ```powershell
   pip install -r requirements.txt
   ```
5. Cek pengaturan di `.env` (simbol, timeframe `M30`, strategi `mtf_pullback`, lot, RiskGuard).

## Cara Menjalankan di Terminal
```powershell
# 1. Cek koneksi MT5, sinkronisasi jam server, simbol, margin, strategi, dan RiskGuard
python main.py check

# 2. Simulasi live dengan data MT5 asli (TIDAK mengirim order sungguhan)
python main.py dry-run

# 3. Jalankan algo trading LIVE di MT5 yang terbuka (tekan Ctrl+C untuk berhenti)
python main.py run

# Perintah tambahan
python main.py status                       # Lihat saldo, equity, posisi terbuka bot, & RiskGuard
python main.py backtest --bars 5000         # Backtest & walk-forward 4 fold pakai data MT5
python main.py backtest --csv data.csv      # Backtest pakai file CSV offline
python main.py once --dry-run               # Jalankan 1 siklus saja
python main.py close-all                    # Tutup semua posisi terbuka milik bot ini
python main.py reset-kill                   # Reset kill-switch RiskGuard setelah ditinjau
```
Atau lewat pintasan Windows: `run.bat check`, `run.bat dry-run`, `run.bat run`, `run.bat backtest`.

Perintah `close-all` dan `reset-kill` memerlukan bot live dalam workspace ini sudah berhenti.
State RiskGuard yang rusak akan menghentikan bot dan harus diperiksa/dipulihkan secara manual; bot tidak
akan menginisialisasi ulang batas risiko secara diam-diam.

## Menjalankan Tes Otomatis
```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```
