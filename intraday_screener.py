# ==============================================================================
# MULTI-TIMEFRAME (DAILY + WEEKLY) LIVE INTRADAY SCREENER & TELEGRAM BOT
# ==============================================================================

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
from SmartApi import SmartConnect

# LOGGING CONFIGURATION
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("StockScreener")

# SECURE CONFIGURATION READ
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Dynamic Credentials Validation
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
    logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# TIMEZONE CONFIGURATION
IST = pytz.timezone("Asia/Kolkata")

# MULTI-TIMEFRAME MOMENTUM PARAMETERS
MIN_DAILY_RSI = 60.0
MIN_DAILY_ROC = 1.0
REQUIRE_VOL_SURGE = True
MIN_WEEKLY_RSI = 55.0
ENABLE_200_EMA_FILTER = True  # Institutional trend filter

# LIVE MONITORING CONFIG
MAX_WORKERS = 2  # Strict 3 req/sec rate limit compliance
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"

LOG_FILE = "fno_scan_log.csv"
smart_api_instance = None
http_session = requests.Session()
alerted_stocks_today = set()
alert_lock = threading.Lock()
last_alert_reset_date = dt.now(IST).date()
cached_fno_universe = None


# ==========================================
# TELEGRAM NOTIFICATION ENGINE
# ==========================================
def send_telegram_alert(message, bypass_time_check=False, max_retries=3):
    """Sends HTML formatted Telegram notifications safely"""
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
            response = http_session.post(url, data=payload, timeout=5)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Telegram Alert Error: {e}")
            time.sleep(1)


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
            return auth_token
        else:
            logger.error(
                f"Login Failed: {data.get('message') if data else 'No response'}"
            )
    except Exception as e:
        logger.error(f"Auth Exception: {e}")
    return None


def fetch_dynamic_fno_universe():
    global cached_fno_universe
    if cached_fno_universe:
        return cached_fno_universe

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

        cached_fno_universe = [
            {"symbol": str(row["symbol"]), "token": str(row["token"])}
            for _, row in nse_eq_df.iterrows()
        ]
        return cached_fno_universe
    except Exception as e:
        logger.error(f"Error reading Scrip Master File: {e}")
        return []


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

        if (
            resp_json.get("status")
            and resp_json.get("data")
            and len(resp_json["data"]) > 0
        ):
            df = pd.DataFrame(
                resp_json["data"],
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ],
            )
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)
            return df
    except Exception:
        pass
    return None


def analyze_stock_multi_tf(stock, auth_token):
    global alerted_stocks_today, last_alert_reset_date

    today_ist = dt.now(IST).date()
    with alert_lock:
        if today_ist != last_alert_reset_date:
            alerted_stocks_today.clear()
            last_alert_reset_date = today_ist

    symbol = stock["symbol"]
    token = stock["token"]

    time.sleep(0.4)  # Rate limiting compliance sleep

    # 1. Fetch Daily Data
    df_daily = fetch_candle_data_direct(auth_token, token, "ONE_DAY", days=300)
    if df_daily is None or len(df_daily) < 30:
        return None, None

    ema_200_window = 200 if len(df_daily) >= 200 else len(df_daily) - 1

    df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
    df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
    df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
    df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
    df_daily["ema_200"] = ta.trend.ema_indicator(df_daily["close"], window=ema_200_window)
    df_daily["vol_sma20"] = df_daily["volume"].rolling(window=20).mean()

    curr_d = df_daily.iloc[-1]
    if pd.isna(curr_d["rsi"]) or pd.isna(curr_d["roc"]):
        return None, None

    entry_p = round(curr_d["close"], 2)

    vol_sma_val = curr_d["vol_sma20"] if pd.notna(curr_d["vol_sma20"]) and curr_d["vol_sma20"] > 0 else 1.0

    daily_rsi_pass = curr_d["rsi"] >= MIN_DAILY_RSI
    daily_roc_pass = curr_d["roc"] >= MIN_DAILY_ROC
    daily_ema_pass = curr_d["ema_9"] > curr_d["ema_21"]
    ema_200_pass = (
        curr_d["close"] > curr_d["ema_200"] if (ENABLE_200_EMA_FILTER and pd.notna(curr_d["ema_200"])) else True
    )
    vol_pass = (
        curr_d["volume"] >= vol_sma_val if REQUIRE_VOL_SURGE else True
    )

    if not (
        daily_rsi_pass
        and daily_roc_pass
        and daily_ema_pass
        and ema_200_pass
        and vol_pass
    ):
        return None, None

    # 2. Fetch Weekly Data ONLY if Daily parameters pass
    time.sleep(0.4)
    df_weekly = fetch_candle_data_direct(
        auth_token, token, "ONE_WEEK", days=250
    )
    if df_weekly is None or len(df_weekly) < 15:
        return None, None

    df_weekly["w_rsi"] = ta.momentum.rsi(df_weekly["close"], window=14)
    df_weekly["w_ema21"] = ta.trend.ema_indicator(
        df_weekly["close"], window=21
    )

    curr_w = df_weekly.iloc[-1]
    if pd.isna(curr_w["w_rsi"]) or pd.isna(curr_w["w_ema21"]):
        return None, None

    weekly_pass = (curr_w["w_rsi"] >= MIN_WEEKLY_RSI) and (
        curr_w["close"] > curr_w["w_ema21"]
    )

    if not weekly_pass:
        return None, None

    # Matches Found
    match_data = {
        "Symbol": symbol,
        "LTP": entry_p,
        "Daily_RSI": round(curr_d["rsi"], 2),
        "Weekly_RSI": round(curr_w["w_rsi"], 2),
        "Daily_ROC": round(curr_d["roc"], 2),
        "Signal": "🚀 DUAL-TF MOMENTUM BUY",
    }

    send_alert = False
    with alert_lock:
        if symbol not in alerted_stocks_today:
            alerted_stocks_today.add(symbol)
            send_alert = True

    if send_alert:
        tg_msg = (
            f"⚡ <b>LIVE BREAKOUT DETECTED (DAILY + WEEKLY)</b>\n\n"
            f"<b>Stock:</b> {symbol}\n"
            f"<b>LTP:</b> ₹{entry_p}\n"
            f"<b>Daily RSI:</b> {round(curr_d['rsi'], 2)}\n"
            f"<b>Weekly RSI:</b> {round(curr_w['w_rsi'], 2)}\n"
            f"<b>Daily ROC:</b> {round(curr_d['roc'], 2)}%\n"
            f"<b>Above 200 EMA:</b> YES (₹{round(curr_d['ema_200'], 2)})\n"
            f"<b>Status:</b> High-Probability Setup Active 🔥"
        )
        send_telegram_alert(tg_msg)

    return match_data, match_data


def run_live_scanner():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("SmartAPI login failed.")
        return

    fno_universe = fetch_dynamic_fno_universe()
    if not fno_universe:
        logger.error("F&O universe fetch returned empty.")
        return

    logger.info(
        f"[{dt.now(IST).strftime('%H:%M:%S')}] Scanning {len(fno_universe)} F&O stocks..."
    )

    qualified_matches = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(analyze_stock_multi_tf, stock, auth_token)
            for stock in fno_universe
        ]
        for future in as_completed(futures):
            try:
                _, match_data = future.result()
                if match_data:
                    qualified_matches.append(match_data)
            except Exception:
                continue

    if qualified_matches:
        df_match = pd.DataFrame(qualified_matches)
        print("\n" + "=" * 80)
        print("                  🔥 ACTIVE DUAL-TIMEFRAME MATCHES 🔥")
        print("=" * 80)
        print(df_match.to_string(index=False))
        print("=" * 80 + "\n")
    else:
        logger.info("Scan finished: Zero stocks matched dual timeframe momentum setup.")
        send_telegram_alert("📊 <b>Dual-TF Scanner Report:</b> Completed scan. No stock qualified strict Multi-Timeframe rules currently.", bypass_time_check=True)


if __name__ == "__main__":
    now = dt.now(IST)
    market_start = dtime(9, 15)
    market_cutoff = dtime(15, 15)

    if now.weekday() < 5 and (market_start <= now.time() <= market_cutoff):
        run_live_scanner()
    else:
        logger.info(
            "Outside live trading hours (Mon-Fri, 09:15-15:15 IST). Skipping scan execution."
        )

for item in qualified_matches:
            tg_msg = (
                f"📁 <b>FILE: intraday_screener.py</b>\n"
                f"⚡ <b>5-MIN INTRADAY BREAKOUT ALERT</b>\n\n"
                f"<b>Symbol:</b> {item['Symbol']}\n"
                f"<b>LTP:</b> ₹{item['LTP']}\n"
                f"<b>Change:</b> {item['Change%']}%\n"
                f"<b>RSI (14):</b> {item['RSI(14)']}\n"
                f"<b>Volume Surge:</b> {item['VolRatio']}x\n"
                f"<b>Signal:</b> {item['Signal']}"
            )
            send_telegram_alert(tg_msg)
