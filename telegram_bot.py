from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import logging
import os
import sys
import threading
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
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
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
# `.env` file se environment variables load karein
load_dotenv()

# STRICT ENVIRONMENT READ (No hardcoded credentials)
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validation check for missing variables
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
        f"Missing critical environment variables in .env file: {', '.join(missing_vars)}"
    )
    logger.error(
        "Please create a .env file with your credentials before running."
    )
    sys.exit(1)

# Telegram Bot Initialisation
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None


# ==========================================
# 2. TELEGRAM CHAT ID HANDLER & POLLING THREAD
# ==========================================
if bot:

    @bot.message_handler(func=lambda message: True)
    def handle_telegram_messages(message):
        """Telegram par message aane par Chat ID reply karega"""
        chat_id = message.chat.id
        user_name = message.from_user.first_name
        logger.info(
            f"Telegram Activity | Message from {user_name} | Chat ID: {chat_id}"
        )
        bot.reply_to(
            message,
            f"Hello {user_name}! Aapki Telegram Chat ID hai: `{chat_id}`",
            parse_mode="Markdown",
        )

    def start_telegram_polling():
        """Polling in background thread"""
        try:
            logger.info("Telegram Bot listener started...")
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logger.error(f"Telegram Polling Exception: {e}")

    polling_thread = threading.Thread(target=start_telegram_polling, daemon=True)
    polling_thread.start()


# STRATEGY THRESHOLDS (Strict Momentum Criteria)
MIN_RSI = 60.0  # RSI >= 60
MIN_ROC = 0.0  # ROC > 0
ENABLE_EMA_FILTER = True  # Enforce EMA 9 > EMA 21 & Close > EMA 44

# RISK & TRAILING STOP-LOSS PARAMETERS
SL_PCT = 0.045  # 4.5% Initial Stop Loss
TARGET_PCT = 0.125  # 12.5% Target Gain
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade

ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04  # Trailing activates after +4% profit
TSL_STEP_TRIGGER_PCT = 0.02  # Step trigger every +2% gain
TSL_STEP_MOVE_PCT = 0.015  # Move SL up by +1.5% per step

LOG_FILE = "fno_scan_log.csv"
MAX_WORKERS = 3  # Complies with Angel One REST API rate limits

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
http_session = requests.Session()


# ==========================================
# 3. TELEGRAM ALERT SENDER
# ==========================================
def send_telegram_alert(trade_data):
    if not bot or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot or Chat ID not configured.")
        return

    msg = (
        f"<b>🚀 MOMENTUM SCANNER SIGNAL DETECTED</b>\n"
        f"-----------------------------------------\n"
        f"<b>Symbol:</b> {trade_data['Symbol']}\n"
        f"<b>Sector:</b> {trade_data['Sector']}\n"
        f"<b>Entry Price:</b> ₹{trade_data['LTP']}\n"
        f"<b>RSI (14):</b> {trade_data['RSI(14)']}\n"
        f"<b>ROC (12):</b> {trade_data['ROC(12)']}\n"
        f"-----------------------------------------\n"
        f"<b>Calculated Quantity:</b> {trade_data['Alloc_Qty']} Shares\n"
        f"<b>Initial Stop Loss:</b> ₹{trade_data['Initial_SL']} (-4.5%)\n"
        f"<b>Target Price:</b> ₹{trade_data['Target']} (+12.5%)\n"
        f"-----------------------------------------\n"
        f"<i>Timestamp: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}</i>"
    )

    try:
        bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML"
        )
        logger.info(f"Telegram alert sent for {trade_data['Symbol']}!")
    except Exception as e:
        logger.error(
            f"Failed to send Telegram alert for {trade_data['Symbol']}: {e}"
        )


# ==========================================
# 4. LOGIN & AUTHENTICATION
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
            logger.info(">>> SmartAPI Authenticated Successfully!")
            return auth_token
        else:
            logger.error(
                f"Login Failed: {data.get('message') if data else 'No response'}"
            )
    except Exception as e:
        logger.error(f"Authentication Exception: {e}")
    return None


# ==========================================
# 5. DYNAMIC POSITION SIZING & TRAILING SL
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
                    quantity = int(max_risk_amount // risk_per_share)
                    return max(1, quantity), net_capital
    except Exception as e:
        logger.error(f"Error Fetching RMS Data: {e}")

    return 10, 0.0


def calculate_trailing_sl(entry_price, highest_price_seen):
    base_sl = round(entry_price * (1 - SL_PCT), 2)
    if not ENABLE_TRAILING_SL or highest_price_seen <= entry_price:
        return base_sl

    gain_pct = (highest_price_seen - entry_price) / entry_price
    if gain_pct >= TSL_ACTIVATION_PCT:
        steps = int((gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT)
        trailed_sl = round(entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2)
        return max(base_sl, trailed_sl)

    return base_sl


# ==========================================
# 6. HISTORICAL DATA FETCH
# ==========================================
def fetch_candle_data_direct(
    auth_token, token, interval, days, exchange="NSE"
):
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
    if now.hour < 9:
        last_trade_date = now - timedelta(days=1)
    else:
        last_trade_date = now

    to_date = last_trade_date.strftime("%Y-%m-%d 15:30")
    from_date = (last_trade_date - timedelta(days=days)).strftime(
        "%Y-%m-%d 09:15"
    )

    payload = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = http_session.post(url, headers=headers, json=payload, timeout=8)
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
# 7. INDIVIDUAL STOCK PROCESSOR
# ==========================================
def process_single_stock(stock, sector_name, auth_token):
    symbol = stock["symbol"]
    token = stock["token"]

    time.sleep(0.35)  # Enforce REST API rate limit

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
    entry_p = round(curr["close"], 2)

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

    match_data = None
    if rsi_pass and roc_pass and ema_pass:
        qty, capital = calculate_dynamic_position_size(entry_p)
        initial_sl = round(entry_p * (1 - SL_PCT), 2)
        tgt_p = round(entry_p * (1 + TARGET_PCT), 2)

        match_data = status_data.copy()
        match_data["Signal"] = "🚀 STRICT MOMENTUM BUY"
        match_data["Alloc_Qty"] = qty
        match_data["Initial_SL"] = initial_sl
        match_data["Target"] = tgt_p

        send_telegram_alert(match_data)

    return status_data, match_data


# ==========================================
# 8. PARALLEL SCANNER ENGINE
# ==========================================
def scan_fno_universe(auth_token):
    logger.info(
        f">>> SCANNING F&O STOCKS WITH STRICT FILTERS (RSI >= {MIN_RSI}, ROC >"
        f" {MIN_ROC})..."
    )
    all_scanned = []
    qualified_matches = []

    tasks = []
    for sector_name, stock_list in FNO_UNIVERSE_BY_SECTOR.items():
        for stock in stock_list:
            tasks.append((stock, sector_name))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(process_single_stock, stock, sector, auth_token)
            for stock, sector in tasks
        ]
        for future in as_completed(futures):
            status_data, match_data = future.result()
            if status_data:
                all_scanned.append(status_data)
            if match_data:
                qualified_matches.append(match_data)

    return all_scanned, qualified_matches


def log_scan_results(qualified_matches):
    if qualified_matches:
        df_log = pd.DataFrame(qualified_matches)
        df_log["Scan_Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
        logger.info(f"Saved {len(qualified_matches)} setup(s) to {LOG_FILE}")


# ==========================================
# 9. MAIN PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("Session initialization failed. Exiting.")
        return

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
    print(
        "         🔥 FINAL FILTERED CANDIDATES (RSI >= 60 & ROC > 0 & EMA"
        " ALIGNED) 🔥"
    )
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
