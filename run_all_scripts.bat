@echo off
title Master Trading Automation Runner
color 0A

:: Force batch file to switch to current script folder
cd /d "%~dp0"

echo =======================================================
echo          SETTING UP TELEGRAM & API CREDENTIALS
echo =======================================================

# `.env` file load karein
load_dotenv()

# Fallback ke saath environment variables read karein
API_KEY = os.getenv("SMARTAPI_KEY", "N7XNbnkE")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE", "S885143")
PIN = os.getenv("SMARTAPI_PIN", "1989")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "ZH76UOCDHM4TITQGDKN32HBZEI")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8560792327:AAErjHTU4LlKxlueD4c-EXxS2KcqVwBrDN8"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1427460047")

echo.
echo =======================================================
echo        LAUNCHING ALL SCRIPTS WITH SAFE API DELAY...
echo =======================================================
echo.

echo [1/9] Launching Telegram Bot Listener...
start "Telegram Bot" cmd /k python telegram_bot.py
timeout /t 3 >nul

echo [2/9] Launching Nifty Algo Live...
start "Nifty Algo Live" cmd /k python nifty_algo.py
timeout /t 5 >nul

echo [3/9] Launching Advanced Stock Screener...
start "Stock Screener" cmd /k python advanced_stock_screener.py
timeout /t 5 >nul

echo [4/9] Launching Backtest Engine...
start "Backtest Engine" cmd /k python backtest.py
timeout /t 5 >nul

echo [5/9] Launching Nifty Algo Paper Trading...
start "Nifty Paper Algo" cmd /k python nifty_algo_paper.py
timeout /t 5 >nul

echo [6/9] Launching FnO 15Min Screener...
start "FnO 15Min Screener" cmd /k python fno_option_15min_screener.py
timeout /t 5 >nul

echo [7/9] Launching FnO Master Screener...
start "FnO Master Screener" cmd /k python nifty_stock_fno_master_screener.py
timeout /t 5 >nul

echo [8/9] Launching Fetch Trades...
start "Fetch Trades" cmd /k python fetch_trades.py
timeout /t 5 >nul

echo [9/9] Launching Daily Trade Logger...
start "Daily Trade Log" cmd /k python daily_trade_log.py

echo.
echo =======================================================
echo      ALL 9 SCRIPTS LAUNCHED SUCCESSFULLY!
echo =======================================================
pause