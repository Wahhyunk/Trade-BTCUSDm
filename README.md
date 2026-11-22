# EA Riyan BTC — Algo Trading Bot MT5 (Windows Terminal)

Bot algo trading Python yang langsung terhubung ke **MetaTrader 5 Windows yang sedang terbuka**, berjalan di terminal tanpa perlu XAMPP, PHP, atau MySQL.

## Fitur Utama
- **Langsung ke MT5 terbuka**: otomatis terhubung ke terminal MT5 aktif (`MetaTrader5` Python API), tanpa perlu isi password ulang.
- **3 pilihan strategi** (`STRATEGY` di `.env`):
  - `scalp` *(default)*: RSI 14 + EMA 9/21 + momentum 2 candle + filter RSI ekstrem (strategi asli bot, cocok M1/M5/M15).
  - `donchian`: Donchian breakout 55/20 (long/flat, cocok H1/H4/D1).
  - `ema_cross`: EMA 20/50 crossover (long/flat, cocok H1/H4/D1).
- **Eksekusi candle tertutup**: hanya membaca candle yang sudah close (`start_pos=1`) sehingga tidak repaint.
- **RiskGuard bawaan**: batas rugi harian, batas jumlah trade/hari, jeda rugi beruntun, minimal Reward:Risk, cek umur harga, dan drawdown kill-switch.
- **Magic Number terpisah** (`MT5_MAGIC=202611`): hanya mengelola posisi milik bot ini, tidak menyentuh posisi manual.
- **Log otomatis**: mencatat aktivitas ke `results/bot.log`, riwayat trade ke `results/trades.csv`, dan state risk guard ke `results/risk_state.json`.

## Struktur Folder
```
EA_Riyan_btc/
├── main.py              # Entry point CLI terminal
├── run.bat              # Pintasan Windows
├── .env                 # Konfigurasi aktif (jangan dibagikan)
├── .env.example         # Template konfigurasi
├── requirements.txt     # MetaTrader5, numpy, pandas
├── bot/
│   ├── config.py        # Loader konfigurasi .env
│   ├── strategy.py      # Logika sinyal (scalp, donchian, ema_cross)
│   ├── risk_guard.py    # Pengaman risiko pra-trade & kill-switch
│   ├── mt5_client.py    # Koneksi, data candle, lot, & eksekusi order MT5
│   └── runner.py        # Loop utama algo trading terminal
├── tests/
│   └── test_bot.py      # Unit test otomatis
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
5. Cek pengaturan di `.env` (simbol, timeframe, lot, TP/SL, RiskGuard).

## Cara Menjalankan di Terminal
```powershell
# 1. Cek koneksi MT5, simbol, margin, strategi, dan RiskGuard
python main.py check

# 2. Simulasi live dengan data MT5 asli (TIDAK mengirim order sungguhan)
python main.py dry-run

# 3. Jalankan algo trading LIVE di MT5 yang terbuka (tekan Ctrl+C untuk berhenti)
python main.py run

# Perintah tambahan
python main.py status          # Lihat saldo, equity, posisi terbuka bot, & RiskGuard
python main.py once --dry-run  # Jalankan 1 siklus saja
python main.py close-all       # Tutup semua posisi terbuka milik bot ini
python main.py reset-kill      # Reset kill-switch RiskGuard setelah ditinjau
```
Atau lewat pintasan Windows: `run.bat check`, `run.bat dry-run`, `run.bat run`.

## Menjalankan Tes Otomatis
```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

> **Catatan Risiko**: Strategi bawaan `scalp` (TP 1.5% / SL 0.5%) sebelumnya menunjukkan win rate historis di bawah breakeven pada backtest jangka panjang. Gunakan akun **Demo** terlebih dahulu untuk menguji perilaku bot dan penyesuaian parameter sebelum memakai dana riil.
"# Trade-BTCUSDm" 
