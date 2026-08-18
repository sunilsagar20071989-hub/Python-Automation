# ==============================================================================
# DYNAMIC F&O STOCKS MASTER SCREENER & TELEGRAM ALERT BOT (200 EMA & SWING ENHANCED)
# ==============================================================================

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
import logging
import os
import sys
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import requests
import ta
from SmartApi import SmartConnect

# Logging Setup
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("StockScreener")

# ==========================================
# 1. SECURE CONFIGURATION (STRICT .ENV READ)
# ==========================================
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

# STRATEGY THRESHOLDS (Strict Breakout, 200 EMA & Momentum)
MIN_RSI = 65.0  # RSI >= 65 for strong momentum
MIN_ROC = 1.5  # ROC >= 1.5%
MIN_VOL_SURGE_RATIO = 1.5  # Volume >= 1.5x of 20-day SMA Volume
MIN_DAY_CHANGE_PCT = 2.0  # At least 2% intraday price surge
ENABLE_EMA_FILTER = True  # Enforces 200 EMA + 9/21 EMA Alignment

# RISK PARAMETERS (CASH EQUITY / SWING FOCUS)
SL_PCT = 0.045  # 4.5% Initial Stop Loss
TARGET_PCT = 0.1575  # 15.75% Target Gain
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade

LOG_FILE = "fno_scan_log.csv"
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"
MAX_WORKERS = 3  # Complies with Angel One REST API rate limit (3 req/sec)

smart_api_instance = None
http_session = requests.Session()


# ==========================================
# 2. MARKET HOURS RESTRICTION
# ==========================================
def is_market_open():
    """Ensures scanning runs within live/active trading windows."""
    now = datetime.now().time()
    market_start = dtime(9, 15)
    market_end = dtime(15, 30)
    return market_start <= now <= market_end


# ==========================================
# 3. DYNAMIC F&O UNIVERSE FETCH
# ==========================================
def fetch_dynamic_fno_universe():
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    download_needed = True

    if os.path.exists(MASTER_FILE_LOCAL):
        file_time = datetime.fromtimestamp(
            os.path.getmtime(MASTER_FILE_LOCAL)
        )
        if file_time.date() == datetime.now().date():
            download_needed = False

    if download_needed:
        logger.info(
            ">>> Downloading Angel One Master Instrument File for Dynamic F&O"
            " Universe..."
        )
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
        logger.error("Failed to acquire Scrip Master JSON file.")
        return []

    try:
        df_master = pd.read_json(MASTER_FILE_LOCAL)
        cols = {c.lower(): c for c in df_master.columns}
        exch_col = cols.get("exch_seg", "exch_seg")
        name_col = cols.get("name", "name")
        sym_col = cols.get("symbol", "symbol")
        token_col = cols.get("token", "token")

        nfo_df = df_master[df_master[exch_col] == "NFO"]
        fno_symbols = set(nfo_df[name_col].dropna().unique())

        nse_eq_df = df_master[
            (df_master[exch_col] == "NSE")
            & (df_master[sym_col].astype(str).str.endswith("-EQ"))
            & (df_master[name_col].isin(fno_symbols))
        ]

        fno_list = [
            {"symbol": str(row[sym_col]), "token": str(row[token_col])}
            for _, row in nse_eq_df.iterrows()
        ]

        logger.info(
            f">>> Loaded {len(fno_list)} active F&O underlying stocks dynamically!\n"
        )
        return fno_list

    except Exception as e:
        logger.error(f"Dynamic Universe Fetch Error: {e}")
        return []


# ==========================================
# 4. TELEGRAM NOTIFIER
# ==========================================
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram configuration missing in environment.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        http_session.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Alert Exception: {e}")


# ==========================================
# 5. LOGIN & AUTHENTICATION
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
# 6. DYNAMIC RMS POSITION SIZING
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


# ==========================================
# 7. HISTORICAL DATA FETCH
# ==========================================
def fetch_candle_data_direct(
    auth_token, token, interval="ONE_DAY", days=300, exchange="NSE"
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
        resp = http_session.post(url, headers=headers, json=payload, timeout=8)
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


# ==========================================
# 8. INDIVIDUAL STOCK ANALYSIS
# ==========================================
def process_single_stock(stock, auth_token):
    symbol = stock["symbol"]
    token = stock["token"]

    time.sleep(0.35)  # Enforce REST API Rate Limiting

    df_daily = fetch_candle_data_direct(auth_token, token, "ONE_DAY", days=350)
    if df_daily is None or len(df_daily) < 200:
        return None, None

    # Technical Indicators Calculations
    df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
    df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
    df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
    df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
    df_daily["ema_200"] = ta.trend.ema_indicator(df_daily["close"], window=200)
    df_daily["vol_sma20"] = df_daily["volume"].rolling(window=20).mean()

    curr = df_daily.iloc[-1]
    prev_close = df_daily.iloc[-2]["close"]
    entry_p = round(curr["close"], 2)

    # 1. Volume Surge Check
    vol_ratio = (
        curr["volume"] / curr["vol_sma20"] if curr["vol_sma20"] > 0 else 0
    )
    vol_pass = vol_ratio >= MIN_VOL_SURGE_RATIO

    # 2. 20-Day High Breakout Check
    recent_20_high = df_daily["high"].iloc[-21:-1].max()
    breakout_pass = curr["close"] >= recent_20_high

    # 3. Minimum Intraday Move % (>= 2% Gain)
    day_change_pct = ((curr["close"] - prev_close) / prev_close) * 100
    move_pass = day_change_pct >= MIN_DAY_CHANGE_PCT

    # 4. Strict Indicator Checks (RSI + ROC + 200 EMA)
    rsi_pass = curr["rsi"] >= MIN_RSI
    roc_pass = curr["roc"] >= MIN_ROC

    ema_pass = (
        (curr["close"] > curr["ema_200"])
        and (curr["ema_9"] > curr["ema_21"])
        if ENABLE_EMA_FILTER
        else True
    )

    status_data = {
        "Symbol": symbol,
        "LTP": entry_p,
        "Change%": round(day_change_pct, 2),
        "RSI(14)": round(curr["rsi"], 2),
        "ROC(12)": round(curr["roc"], 2),
        "VolRatio": round(vol_ratio, 2),
        "EMA_200": round(curr["ema_200"], 2),
        "Breakout": "YES" if breakout_pass else "NO",
    }

    match_data = None
    if (
        rsi_pass
        and roc_pass
        and ema_pass
        and vol_pass
        and breakout_pass
        and move_pass
    ):
        qty, capital = calculate_dynamic_position_size(entry_p)
        initial_sl = round(entry_p * (1 - SL_PCT), 2)
        tgt_p = round(entry_p * (1 + TARGET_PCT), 2)

        match_data = status_data.copy()
        match_data["Signal"] = "🚀 HIGH CONVICTION SWING BUY"
        match_data["Alloc_Qty"] = qty
        match_data["Initial_SL"] = initial_sl
        match_data["Target"] = tgt_p

        tg_msg = (
            f"🚀 <b>HIGH CONVICTION SWING BREAKOUT</b>\n\n"
            f"<b>Stock:</b> {symbol}\n"
            f"<b>LTP:</b> ₹{entry_p} (+{round(day_change_pct, 2)}%)\n"
            f"<b>RSI(14):</b> {round(curr['rsi'], 2)}\n"
            f"<b>ROC(12):</b> {round(curr['roc'], 2)}%\n"
            f"<b>Volume Surge:</b> {round(vol_ratio, 2)}x ⚡\n"
            f"<b>Above 200 EMA:</b> YES (₹{round(curr['ema_200'], 2)}) 🎯\n"
            f"<b>20-Day High Breakout:</b> YES\n\n"
            f"<b>Suggested Qty (Cash):</b> {qty}\n"
            f"<b>Stop Loss (4.5%):</b> ₹{initial_sl}\n"
            f"<b>Target (15.75%):</b> ₹{tgt_p}"
        )
        send_telegram_alert(tg_msg)

    return status_data, match_data


# ==========================================
# 9. SCANNER ENGINE (PARALLEL EXECUTION)
# ==========================================
def scan_fno_universe(auth_token, fno_universe):
    logger.info(
        f">>> Scanning {len(fno_universe)} F&O Stocks with 200 EMA + Breakout"
        " Filters..."
    )
    all_scanned = []
    qualified_matches = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(process_single_stock, stock, auth_token)
            for stock in fno_universe
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
# 10. MAIN PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("Session initialization failed. Exiting.")
        return

    fno_universe = fetch_dynamic_fno_universe()
    if not fno_universe:
        logger.error("F&O Universe loading failed. Exiting.")
        return

    send_telegram_alert(
        "🔍 <b>Daily Swing Breakout Screener Started!</b> Scanning"
        f" {len(fno_universe)} stocks..."
    )

    all_scanned, qualified_matches = scan_fno_universe(auth_token, fno_universe)

    print("\n" + "=" * 95)
    print(
        "🔥 FINAL FILTERED CANDIDATES (RSI >= 65, 200 EMA PASSED, VOL >= 1.5x) 🔥"
    )
    print("=" * 95)
    if qualified_matches:
        df_match = pd.DataFrame(qualified_matches)
        print(df_match.to_string(index=False))
        log_scan_results(qualified_matches)
        send_telegram_alert(
            f"✅ <b>Scan Complete!</b> Found {len(qualified_matches)}"
            " high-conviction swing breakout setup(s)."
        )
    else:
        print(
            ">>> ZERO STOCKS MATCHED STRICT BREAKOUT + 200 EMA FILTERS TODAY."
        )
        send_telegram_alert(
            "ℹ️ <b>Scan Complete!</b> No stocks matched strict breakout filters"
            " today."
        )
    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
