import concurrent.futures
from datetime import datetime, time as dtime, timedelta
import os
import sys
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# STRATEGY THRESHOLDS (Strict Momentum & 200 EMA Criteria)
MIN_RSI = 60.0        # System standard RSI >= 60
MIN_ROC = 0.0         # ROC must be > 0
MIN_VOL_SURGE_RATIO = 1.5  # Volume >= 1.5x of 21-period SMA Volume
TIMEFRAME = "FIVE_MINUTE"  # Live Intraday Scanning (5-Min Candles)
MAX_WORKERS = 10      # Multi-threading for high-speed scanning


# ==========================================
# 2. MARKET HOURS CHECK (IST TIMEZONE FIX)
# ==========================================
def is_market_open():
    """Ensures alerts are sent only during live trading hours (09:15 to 15:25 IST)."""
    IST = pytz.timezone("Asia/Kolkata")
    now = datetime.now(IST).time()
    market_start = dtime(9, 15)
    market_end = dtime(15, 25)
    return market_start <= now <= market_end


# ==========================================
# 3. DYNAMIC F&O UNIVERSE FETCH
# ==========================================
def fetch_dynamic_fno_universe():
    print(">>> Downloading Angel One Master Instrument File for Dynamic F&O Universe...")
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    headers = {"User-Agent": "Mozilla/5.0"}

    for scrip_url in urls:
        try:
            resp = requests.get(scrip_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                df_master = pd.DataFrame(data)

                nfo_df = df_master[df_master["exch_seg"] == "NFO"]
                fno_symbols = set(nfo_df["name"].dropna().unique())

                nse_eq_df = df_master[
                    (df_master["exch_seg"] == "NSE")
                    & (df_master["symbol"].astype(str).str.endswith("-EQ"))
                    & (df_master["name"].isin(fno_symbols))
                ]

                fno_list = []
                for _, row in nse_eq_df.iterrows():
                    fno_list.append({"symbol": str(row["symbol"]), "token": str(row["token"])})

                print(f">>> Successfully loaded {len(fno_list)} F&O stocks dynamically!\n")
                return fno_list
        except Exception:
            continue

    print(">>> Dynamic Universe Fetch Failed.")
    return []


# ==========================================
# 4. TELEGRAM NOTIFICATION ENGINE
# ==========================================
def send_telegram_alert(message, max_retries=3):
    """Sends HTML formatted Telegram notifications safely with Time-Filter"""
    IST = pytz.timezone("Asia/Kolkata")
    current_time = datetime.now(IST).time()
    cutoff_time = dtime(15, 15)

    if current_time > cutoff_time:
        print(f"Alert Skipped: Current IST time ({current_time.strftime('%H:%M')}) is past market cutoff (15:15).")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=payload, timeout=5)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"Telegram Alert Error: {e}")
            time.sleep(1)


# ==========================================
# 5. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
    try:
        missing_vars = []
        if not API_KEY:
            missing_vars.append("SMARTAPI_KEY/SMARTAPI_API_KEY")
        if not CLIENT_CODE:
            missing_vars.append("SMARTAPI_CLIENT_CODE")
        if not PIN:
            missing_vars.append("SMARTAPI_PIN")
        if not TOTP_SECRET:
            missing_vars.append("SMARTAPI_TOTP_SECRET")

        if missing_vars:
            print(f">>> Error: Missing environment variables: {', '.join(missing_vars)}")
            return None

        smart_api = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get("status"):
            raw_token = data["data"]["jwtToken"]
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            print("\n" + "=" * 85)
            print(">>> SmartAPI Authenticated! Strict 5-Min 200 EMA & Momentum Active...")
            print("=" * 85 + "\n")
            return auth_token
        else:
            print(">>> Authentication failed response:", data)
    except Exception as e:
        print(">>> Authentication Exception:", e)
    return None


# ==========================================
# 6. INTRADAY DATA FETCH & PROCESSING
# ==========================================
def fetch_candle_data_direct(auth_token, token, interval=TIMEFRAME, days=7, exchange="NSE"):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": API_KEY,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "FE:80:00:00:00:00",
        "Authorization": auth_token,
    }

    now = datetime.now()
    to_date = now.strftime("%Y-%m-%d %H:%M")
    from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15")

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
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            df["volume"] = df["volume"].astype(float)
            return df
    except Exception:
        pass
    return None


def evaluate_stock_setup(auth_token, stock):
    symbol = stock["symbol"]
    token = stock["token"]

    df = fetch_candle_data_direct(auth_token, token, interval=TIMEFRAME, days=7)
    if df is None or len(df) < 200:
        return None

    # Indicator Calculation
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["roc"] = ta.momentum.roc(df["close"], window=9)
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["vol_sma21"] = df["volume"].rolling(window=21).mean()

    curr = df.iloc[-1]
    prev_close = df.iloc[-2]["close"]

    vol_ratio = curr["volume"] / curr["vol_sma21"] if curr["vol_sma21"] > 0 else 0
    day_change_pct = ((curr["close"] - prev_close) / prev_close) * 100

    # STRICT 200 EMA & MOMENTUM RULES
    ema_200_pass = curr["close"] > curr["ema_200"]
    rsi_pass = curr["rsi"] >= MIN_RSI
    roc_pass = curr["roc"] > MIN_ROC
    vol_pass = vol_ratio >= MIN_VOL_SURGE_RATIO
    ema_cross_pass = curr["ema_9"] > curr["ema_21"]

    if ema_200_pass and rsi_pass and roc_pass and vol_pass and ema_cross_pass:
        return {
            "Symbol": symbol,
            "LTP": round(curr["close"], 2),
            "Change%": round(day_change_pct, 2),
            "RSI(14)": round(curr["rsi"], 2),
            "VolRatio": round(vol_ratio, 2),
            "Signal": "BUY CALL / LONG",
        }
    return None


# ==========================================
# 7. MAIN PIPELINE (FAST MULTI-THREADED SCANNER)
# ==========================================
def main():
    if not is_market_open():
        print(">>> Market Closed! Skipping Execution to Avoid Post-Market False Signals.")
        return

    auth_token = initialize_smartapi()
    if not auth_token:
        print(">>> Session initialization failed. Exiting.")
        return

    fno_stock_list = fetch_dynamic_fno_universe()
    if not fno_stock_list:
        print(">>> F&O Stock List fetch failed. Exiting.")
        return

    print(f">>> SCANNING {len(fno_stock_list)} F&O STOCKS ON 5-MIN TIMEFRAME WITH MULTI-THREADING...")
    qualified_matches = []

    # Parallel processing for 3-5 second scanning
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(evaluate_stock_setup, auth_token, stock) for stock in fno_stock_list]
        for future in concurrent.futures.as_completed(futures):
            try:
                match = future.result()
                if match:
                    qualified_matches.append(match)
            except Exception:
                continue

    if qualified_matches:
        df_match = pd.DataFrame(qualified_matches)
        print("\n" + "=" * 80)
        print(" 🔥 HIGH CONVICTION BREAKOUT CANDIDATES MATCHED RULES 🔥")
        print("=" * 80)
        print(df_match.to_string(index=False))

        for item in qualified_matches:
            tg_msg = (
                f"🚀 <b>5-MIN HIGH CONVICTION BREAKOUT</b>\n\n"
                f"<b>Symbol:</b> {item['Symbol']}\n"
                f"<b>LTP:</b> ₹{item['LTP']}\n"
                f"<b>Change:</b> {item['Change%']}%\n"
                f"<b>RSI (14):</b> {item['RSI(14)']}\n"
                f"<b>Volume Surge:</b> {item['VolRatio']}x\n"
                f"<b>Signal:</b> {item['Signal']}"
            )
            send_telegram_alert(tg_msg)
    else:
        print("\n>>> ZERO STOCKS MATCHED STRICT 200 EMA + RSI (>60) + VOL SURGE RULES.")


if __name__ == "__main__":
    main()
