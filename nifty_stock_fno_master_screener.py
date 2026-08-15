# ==============================================================================
# F&O STOCKS MASTER SCREENER & TELEGRAM ALERT BOT (COMPLETE SINGLE FILE)
# ==============================================================================

import os
import sys
import time
import math
import logging
from dotenv import load_dotenv
import requests
import pyotp
import pandas as pd
import ta
from datetime import datetime, timedelta
from SmartApi import SmartConnect

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("StockScreener")

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
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

# STRATEGY THRESHOLDS (Strict Momentum Criteria)
MIN_RSI = 60.0        # RSI must be >= 60
MIN_ROC = 0.0        # ROC must be > 0
ENABLE_EMA_FILTER = True  # Enforce EMA 9 > EMA 21 & Close > EMA 44

# RISK PARAMETERS
SL_PCT = 0.045       # 4.5% Initial Stop Loss
TARGET_PCT = 0.1575   # 15.75% Target Gain
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% account capital risk per stock

LOG_FILE = "fno_scan_log.csv"

# F&O UNIVERSE BY SECTOR
FNO_UNIVERSE_BY_SECTOR = {
    "IT": [
        {"symbol": "TCS-EQ", "token": "11536"},
        {"symbol": "INFY-EQ", "token": "1594"},
        {"symbol": "PERSISTENT-EQ", "token": "18365"},
        {"symbol": "HCLTECH-EQ", "token": "7229"},
        {"symbol": "WIPRO-EQ", "token": "3787"},
        {"symbol": "COFORGE-EQ", "token": "11543"},
        {"symbol": "TECHM-EQ", "token": "13538"},
        {"symbol": "LTIM-EQ", "token": "17818"},
    ],
    "Auto": [
        {"symbol": "BAJAJ-AUTO-EQ", "token": "16669"},
        {"symbol": "M&M-EQ", "token": "2031"},
        {"symbol": "MARUTI-EQ", "token": "10999"},
        {"symbol": "TATAMOTORS-EQ", "token": "3456"},
        {"symbol": "HEROMOTOCO-EQ", "token": "1348"},
        {"symbol": "TVSMOTOR-EQ", "token": "8479"},
        {"symbol": "EICHERMOT-EQ", "token": "910"},
        {"symbol": "BHARATFORG-EQ", "token": "422"},
    ],
    "Pharma": [
        {"symbol": "SUNPHARMA-EQ", "token": "3351"},
        {"symbol": "CIPLA-EQ", "token": "694"},
        {"symbol": "DRREDDY-EQ", "token": "881"},
        {"symbol": "LUPIN-EQ", "token": "10440"},
        {"symbol": "DIVISLAB-EQ", "token": "10940"},
        {"symbol": "TORNTPHARM-EQ", "token": "3518"},
    ],
    "Banking": [
        {"symbol": "ICICIBANK-EQ", "token": "4963"},
        {"symbol": "SBIN-EQ", "token": "3045"},
        {"symbol": "HDFCBANK-EQ", "token": "1333"},
        {"symbol": "AXISBANK-EQ", "token": "5900"},
        {"symbol": "KOTAKBANK-EQ", "token": "1922"},
        {"symbol": "INDUSINDBK-EQ", "token": "5258"},
    ],
    "Metal": [
        {"symbol": "TATASTEEL-EQ", "token": "3499"},
        {"symbol": "HINDALCO-EQ", "token": "1363"},
        {"symbol": "JINDALSTEL-EQ", "token": "1732"},
        {"symbol": "VEDL-EQ", "token": "3063"},
    ],
    "Energy": [
        {"symbol": "RELIANCE-EQ", "token": "2885"},
        {"symbol": "NTPC-EQ", "token": "11630"},
        {"symbol": "POWERGRID-EQ", "token": "14977"},
        {"symbol": "ONGC-EQ", "token": "2475"},
    ],
}

smart_api_instance = None


# ==========================================
# 2. TELEGRAM NOTIFICATION ENGINE
# ==========================================
def send_telegram_alert(message):
    """Sends HTML formatted Telegram notifications"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot Token ya Chat ID missing hai.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Alert Exception: {e}")


# ==========================================
# 3. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
    global smart_api_instance
    try:
        if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
            logger.error("SmartAPI Credentials missing hain!")
            return None

        smart_api_instance = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api_instance.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get("status"):
            raw_token = data["data"]["jwtToken"]
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            logger.info(">>> SmartAPI Authenticated Successfully!")
            return auth_token
    except Exception as e:
        logger.error(f"Authentication Exception: {e}")
    return None


# ==========================================
# 4. DYNAMIC POSITION SIZING
# ==========================================
def calculate_dynamic_position_size(entry_price):
    """Calculates quantity based on account capital risk"""
    try:
        if smart_api_instance:
            rms_data = smart_api_instance.rmsLimit()
            if rms_data and rms_data.get("status") and "data" in rms_data:
                net_capital = float(rms_data["data"].get("net", 0.0))
                if net_capital > 0:
                    max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
                    risk_per_share = entry_price * SL_PCT
                    quantity = int(max_risk_amount // risk_per_share)
                    return max(1, quantity), net_capital
    except Exception as e:
        logger.error(f"Error Fetching RMS Data: {e}")

    return 10, 0.0


# ==========================================
# 5. HISTORICAL DATA FETCH
# ==========================================
def fetch_candle_data_direct(auth_token, token, interval, days, exchange="NSE"):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": API_KEY,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "MAC_ADDRESS",
        "Authorization": auth_token,
    }

    now = datetime.now()
    last_trade_date = now - timedelta(days=1) if now.hour < 9 else now
    to_date = last_trade_date.strftime("%Y-%m-%d 15:30")
    from_date = (last_trade_date - timedelta(days=days)).strftime("%Y-%m-%d 09:15")

    payload = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        resp_json = resp.json()

        if resp_json.get("status") and resp_json.get("data"):
            df = pd.DataFrame(
                resp_json["data"],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)
            return df
    except Exception:
        pass
    return None


# ==========================================
# 6. STOCK SCANNER ENGINE & TELEGRAM ALERTS
# ==========================================
def scan_fno_universe(auth_token):
    logger.info(">>> Scanning F&O Stocks with Momentum Filters...")
    all_scanned = []
    qualified_matches = []

    for sector_name, stock_list in FNO_UNIVERSE_BY_SECTOR.items():
        print(f" Scanning [{sector_name} Sector] ({len(stock_list)} F&O stocks)...")

        for stock in stock_list:
            symbol = stock["symbol"]
            token = stock["token"]

            df_daily = fetch_candle_data_direct(auth_token, token, "ONE_DAY", days=100)
            if df_daily is None or len(df_daily) < 45:
                continue

            # Compute Technical Indicators
            df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
            df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
            df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
            df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
            df_daily["ema_44"] = ta.trend.ema_indicator(df_daily["close"], window=44)
            df_daily["vol_sma20"] = df_daily["volume"].rolling(window=20).mean()

            curr = df_daily.iloc[-1]
            entry_p = round(curr["close"], 2)

            # Conditions Check
            rsi_pass = curr["rsi"] >= MIN_RSI
            roc_pass = curr["roc"] > MIN_ROC
            ema_pass = (
                (curr["ema_9"] > curr["ema_21"]) and (curr["close"] > curr["ema_44"])
                if ENABLE_EMA_FILTER
                else True
            )
            vol_pass = curr["volume"] > curr["vol_sma20"]

            status_data = {
                "Sector": sector_name,
                "Symbol": symbol,
                "LTP": entry_p,
                "RSI(14)": round(curr["rsi"], 2),
                "ROC(12)": round(curr["roc"], 2),
                "RSI_>=_60": "PASS" if rsi_pass else "FAIL",
                "ROC_>_0": "PASS" if roc_pass else "FAIL",
                "EMA_Trend": "PASS" if ema_pass else "FAIL",
                "VolSurge": "YES" if vol_pass else "NO",
            }
            all_scanned.append(status_data)

            # Push to Telegram if all criteria match
            if rsi_pass and roc_pass and ema_pass:
                qty, capital = calculate_dynamic_position_size(entry_p)
                initial_sl = round(entry_p * (1 - SL_PCT), 2)
                tgt_p = round(entry_p * (1 + TARGET_PCT), 2)

                match_data = status_data.copy()
                match_data["Signal"] = "🚀 MOMENTUM BUY"
                match_data["Alloc_Qty"] = qty
                match_data["Initial_SL"] = initial_sl
                match_data["Target"] = tgt_p
                qualified_matches.append(match_data)

                # Send Telegram Notification
                tg_msg = (
                    f"🎯 <b>F&O STOCK MOMENTUM SIGNAL</b>\n\n"
                    f"<b>Stock:</b> {symbol} ({sector_name})\n"
                    f"<b>LTP:</b> ₹{entry_p}\n"
                    f"<b>RSI(14):</b> {round(curr['rsi'], 2)}\n"
                    f"<b>ROC(12):</b> {round(curr['roc'], 2)}\n"
                    f"<b>Volume Surge:</b> {'YES ⚡' if vol_pass else 'NO'}\n\n"
                    f"<b>Suggested Qty:</b> {qty}\n"
                    f"<b>Stop Loss (4.5%):</b> ₹{initial_sl}\n"
                    f"<b>Target (12.5%):</b> ₹{tgt_p}"
                )
                send_telegram_alert(tg_msg)

            time.sleep(0.08)

    return all_scanned, qualified_matches


def log_scan_results(qualified_matches):
    """Exports scanned results into CSV file"""
    if qualified_matches:
        df_log = pd.DataFrame(qualified_matches)
        df_log["Scan_Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
        logger.info(f"Saved {len(qualified_matches)} setup(s) to {LOG_FILE}")


# ==========================================
# 7. MAIN PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("Session initialization failed. Exiting.")
        return

    send_telegram_alert("🔍 <b>F&O Stock Master Screener Started!</b> Scanning sectors...")
    all_scanned, qualified_matches = scan_fno_universe(auth_token)

    print("\n" + "=" * 95)
    print("                    RAW METRICS LOG (ALL SCANNED F&O STOCKS)")
    print("=" * 95)
    if all_scanned:
        df_all = pd.DataFrame(all_scanned)
        print(df_all.to_string(index=False))
    else:
        print(">>> No stock data retrieved.")
    print("=" * 95 + "\n")

    print("=" * 95)
    print("         🔥 FINAL FILTERED CANDIDATES (RSI >= 60 & ROC > 0 & EMA ALIGNED) 🔥")
    print("=" * 95)
    if qualified_matches:
        df_match = pd.DataFrame(qualified_matches)
        print(df_match.to_string(index=False))
        log_scan_results(qualified_matches)
        send_telegram_alert(f"✅ <b>Scan Complete!</b> Found {len(qualified_matches)} qualified setup(s).")
    else:
        print(">>> ZERO STOCKS MATCHED STRICT RSI + ROC + EMA FILTERS TODAY.")
        send_telegram_alert("ℹ️ <b>Scan Complete!</b> No stocks matched strict RSI+ROC filters today.")
    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
