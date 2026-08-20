# ==========================================
# STRICT MOMENTUM SCREENER & TELEGRAM BOT
# ==========================================

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, time as dtime, timedelta
import logging
import os
import sys
import threading
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz
import requests
import ta
import telebot
from SmartApi import SmartConnect

# Logging Setup
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("StockScreener")

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

missing_vars = []
if not API_KEY:
    missing_vars.append("SMARTAPI_KEY / SMARTAPI_API_KEY")
if not CLIENT_CODE:
    missing_vars.append("SMARTAPI_CLIENT_CODE")
if not PIN:
    missing_vars.append("SMARTAPI_PIN")
if not TOTP_SECRET:
    missing_vars.append("SMARTAPI_TOTP_SECRET")

if missing_vars:
    logger.error(
        f"Missing critical environment variables: {', '.join(missing_vars)}"
    )
    sys.exit(1)

# TIMEZONE CONFIG
IST = pytz.timezone("Asia/Kolkata")

# Telegram Bot Initialization
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

if bot:
    @bot.message_handler(func=lambda message: True)
    def handle_telegram_messages(message):
        chat_id = message.chat.id
        user_name = message.from_user.first_name
        logger.info(
            f"Telegram Activity | User: {user_name} | Chat ID: {chat_id}"
        )
        bot.reply_to(
            message,
            f"Hello {user_name}! Aapki Chat ID: `{chat_id}`",
            parse_mode="Markdown",
        )

    def start_telegram_polling():
        try:
            logger.info("Telegram Bot listener active...")
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logger.error(f"Telegram Polling Exception: {e}")

    threading.Thread(target=start_telegram_polling, daemon=True).start()

# STRATEGY THRESHOLDS
MIN_RSI = 60.0
MIN_ROC = 0.0
ENABLE_EMA_FILTER = True

# RISK & TRAILING STOP-LOSS
SL_PCT = 0.045         # 4.5% Initial Stop Loss
TARGET_PCT = 0.125     # 12.5% Target
MAX_RISK_PER_TRADE_PCT = 0.015  # 1.5% Capital Risk Limit

LOG_FILE = "fno_scan_log.csv"
MAX_WORKERS = 2  # Rate Limit compliant (Strict 3 req/sec)
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"

smart_api_instance = None
http_session = requests.Session()


# ==========================================
# 2. TELEGRAM ALERT ENGINE
# ==========================================
def send_telegram_alert(trade_data):
    if not bot or not TELEGRAM_CHAT_ID:
        return

    msg = (
        f"<b>🚀 MOMENTUM SCANNER SIGNAL DETECTED</b>\n"
        f"-----------------------------------------\n"
        f"<b>Symbol:</b> {trade_data['Symbol']}\n"
        f"<b>Entry Price:</b> ₹{trade_data['LTP']}\n"
        f"<b>RSI (14):</b> {trade_data['RSI(14)']}\n"
        f"<b>ROC (12):</b> {trade_data['ROC(12)']}\n"
        f"-----------------------------------------\n"
        f"<b>Calculated Qty:</b> {trade_data['Alloc_Qty']} Shares\n"
        f"<b>Initial Stop Loss:</b> ₹{trade_data['Initial_SL']} (-4.5%)\n"
        f"<b>Target Price:</b> ₹{trade_data['Target']} (+12.5%)\n"
        f"-----------------------------------------\n"
        f"<i>Timestamp: {dt.now(IST).strftime('%d-%b-%Y %H:%M:%S')}</i>"
    )

    try:
        bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML"
        )
        logger.info(f"Telegram alert sent for {trade_data['Symbol']}")
    except Exception as e:
        logger.error(f"Failed Telegram alert for {trade_data['Symbol']}: {e}")


# ==========================================
# 3. LOGIN & UNIVERSE FETCH
# ==========================================
def initialize_smartapi():
    global smart_api_instance
    try:
        smart_api_instance = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api_instance.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get("status"):
            raw_token = data["data"]["jwtToken"]
            auth_token = (
                raw_token
                if raw_token.startswith("Bearer ")
                else f"Bearer {raw_token}"
            )
            logger.info(">>> SmartAPI Session Authenticated Successfully!")
            return auth_token
        else:
            logger.error(f"Login Failed: {data.get('message') if data else 'No response'}")
    except Exception as e:
        logger.error(f"Auth Exception: {e}")
    return None


def fetch_dynamic_fno_universe():
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    download_needed = True

    if os.path.exists(MASTER_FILE_LOCAL):
        file_time = dt.fromtimestamp(os.path.getmtime(MASTER_FILE_LOCAL))
        if file_time.date() == dt.now(IST).date():
            download_needed = False

    if download_needed:
        for scrip_url in urls:
            try:
                resp = http_session.get(scrip_url, timeout=30)
                if resp.status_code == 200:
                    with open(MASTER_FILE_LOCAL, "wb") as f:
                        f.write(resp.content)
                    break
            except Exception:
                continue

    if not os.path.exists(MASTER_FILE_LOCAL):
        return []

    try:
        df_master = pd.read_json(MASTER_FILE_LOCAL)
        df_master.columns = [c.lower() for c in df_master.columns]

        nfo_df = df_master[df_master["exch_seg"] == "NFO"]
        fno_symbols = set(nfo_df["name"].dropna().unique())

        nse_eq_df = df_master[
            (df_master["exch_seg"] == "NSE")
            & (df_master["symbol"].astype(str).str.endswith("-EQ"))
            & (df_master["name"].isin(fno_symbols))
        ]

        return [
            {"symbol": str(row["symbol"]), "token": str(row["token"])}
            for _, row in nse_eq_df.iterrows()
        ]
    except Exception as e:
        logger.error(f"Error reading Scrip Master: {e}")
        return []


# ==========================================
# 4. POSITION SIZING & HISTORICAL DATA
# ==========================================
def calculate_dynamic_position_size(entry_price):
    try:
        if smart_api_instance:
            rms_data = smart_api_instance.rmsLimit()
            if rms_data and rms_data.get("status") and "data" in rms_data:
                net_capital = float(rms_data["data"].get("net", 0.0))
                if net_capital > 0:
                    max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
                    risk_per_share = entry_price * SL_PCT
                    if risk_per_share > 0:
                        quantity = int(max_risk_amount // risk_per_share)
                        return max(1, quantity), net_capital
    except Exception as e:
        logger.error(f"Error Fetching RMS Data: {e}")

    return 1, 0.0


def fetch_candle_data_direct(auth_token, token, interval, days):
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

    now = dt.now(IST)
    to_date = now.strftime("%Y-%m-%d %H:%M")
    from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15")

    payload = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = http_session.post(url, headers=headers, json=payload, timeout=5)
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
# 5. STOCK ANALYSIS & SCAN ENGINE
# ==========================================
def process_single_stock(stock, auth_token):
    symbol = stock["symbol"]
    token = stock["token"]

    time.sleep(0.4)  # REST API Rate Limit Governor

    df_daily = fetch_candle_data_direct(auth_token, token, "ONE_DAY", days=100)
    if df_daily is None or len(df_daily) < 45:
        return None, None

    df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
    df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
    df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
    df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
    df_daily["ema_44"] = ta.trend.ema_indicator(df_daily["close"], window=44)
    df_daily["vol_sma20"] = df_daily["volume"].rolling(window=20).mean()

    curr = df_daily.iloc[-1]
    if pd.isna(curr["rsi"]) or pd.isna(curr["roc"]):
        return None, None

    entry_p = round(curr["close"], 2)

    vol_sma_val = curr["vol_sma20"] if pd.notna(curr["vol_sma20"]) and curr["vol_sma20"] > 0 else 1.0

    rsi_pass = curr["rsi"] >= MIN_RSI
    roc_pass = curr["roc"] > MIN_ROC
    ema_pass = (
        (curr["ema_9"] > curr["ema_21"]) and (curr["close"] > curr["ema_44"])
        if ENABLE_EMA_FILTER
        else True
    )
    vol_pass = curr["volume"] > vol_sma_val

    status_data = {
        "Symbol": symbol,
        "LTP": entry_p,
        "RSI(14)": round(curr["rsi"], 2),
        "ROC(12)": round(curr["roc"], 2),
        "RSI_>=_60": "PASS" if rsi_pass else "FAIL",
        "ROC_>_0": "PASS" if roc_pass else "FAIL",
        "EMA_Trend": "PASS" if ema_pass else "FAIL",
        "VolSurge": "YES" if vol_pass else "NO",
    }

    match_data = None
    if rsi_pass and roc_pass and ema_pass:
        qty, _ = calculate_dynamic_position_size(entry_p)
        initial_sl = round(entry_p * (1 - SL_PCT), 2)
        tgt_p = round(entry_p * (1 + TARGET_PCT), 2)

        match_data = status_data.copy()
        match_data["Signal"] = "🚀 STRICT MOMENTUM BUY"
        match_data["Alloc_Qty"] = qty
        match_data["Initial_SL"] = initial_sl
        match_data["Target"] = tgt_p

        send_telegram_alert(match_data)

    return status_data, match_data


def scan_fno_universe(auth_token):
    fno_universe = fetch_dynamic_fno_universe()
    if not fno_universe:
        logger.error("Dynamic universe list is empty.")
        return [], []

    logger.info(
        f">>> SCANNING {len(fno_universe)} F&O STOCKS (RSI >= {MIN_RSI}, ROC > {MIN_ROC})..."
    )
    all_scanned = []
    qualified_matches = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(process_single_stock, stock, auth_token)
            for stock in fno_universe
        ]
        for future in as_completed(futures):
            try:
                status_data, match_data = future.result()
                if status_data:
                    all_scanned.append(status_data)
                if match_data:
                    qualified_matches.append(match_data)
            except Exception:
                continue

    return all_scanned, qualified_matches


def log_scan_results(qualified_matches):
    if qualified_matches:
        df_log = pd.DataFrame(qualified_matches)
        df_log["Scan_Timestamp"] = dt.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
        logger.info(f"Saved {len(qualified_matches)} setup(s) to {LOG_FILE}")


# ==========================================
# 6. MAIN PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("Session initialization failed. Exiting.")
        return

    all_scanned, qualified_matches = scan_fno_universe(auth_token)

    print("\n" + "=" * 95)
    print("                    RAW METRICS LOG (SCANNED F&O UNIVERSE)")
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
    else:
        print(">>> ZERO STOCKS MATCHED STRICT RSI + ROC + EMA FILTERS TODAY.")
    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
