@echo off
rem Pintasan cepat: run.bat check | run.bat dry-run | run.bat run | run.bat status | run.bat backtest | run.bat close-all
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)
python main.py %*
