# ==============================================================================
# MULTI-TIMEFRAME (DAILY + WEEKLY) LIVE INTRADAY SCREENER & TELEGRAM BOT
# ==============================================================================

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
import logging
import os
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

# SECURE CONFIGURATION READ
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# MULTI-TIMEFRAME MOMENTUM PARAMETERS
MIN_DAILY_RSI = 60.0
MIN_DAILY_ROC = 1.0
REQUIRE_VOL_SURGE = True
MIN_WEEKLY_RSI = 55.0
ENABLE_200_EMA_FILTER = True  # Strict institutional trend filter

# LIVE MONITORING CONFIG
SCAN_INTERVAL_MINUTES = 15
MAX_WORKERS = 3  # Reduced workers to comply with Angel One API Rate Limits (3 req/sec)
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"

LOG_FILE = "fno_scan_log.csv"
smart_api_instance = None
http_session = requests.Session()
alerted_stocks_today = set()
last_alert_reset_date = datetime.now().date()


def send_telegram_alert(message):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "HTML",
  }
  try:
    http_session.post(url, data=payload, timeout=5)
  except Exception as e:
    logger.error(f"Telegram Alert Exception: {e}")


def initialize_smartapi():
  global smart_api_instance
  try:
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
      logger.error("Missing SmartAPI environment variables.")
      return None
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
  except Exception as e:
    logger.error(f"Auth Exception: {e}")
  return None


def fetch_dynamic_fno_universe():
  url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
  download_needed = True

  if os.path.exists(MASTER_FILE_LOCAL):
    file_time = datetime.fromtimestamp(os.path.getmtime(MASTER_FILE_LOCAL))
    if file_time.date() == datetime.now().date():
      download_needed = False

  if download_needed:
    try:
      resp = http_session.get(url, timeout=30)
      with open(MASTER_FILE_LOCAL, "wb") as f:
        f.write(resp.content)
    except Exception:
      if not os.path.exists(MASTER_FILE_LOCAL):
        return []

  try:
    df_master = pd.read_json(MASTER_FILE_LOCAL)
    nfo_df = df_master[df_master["exch_seg"] == "NFO"]
    fno_symbols = set(nfo_df["name"].dropna().unique())

    nse_eq_df = df_master[
        (df_master["exch_seg"] == "NSE")
        & (df_master["symbol"].str.endswith("-EQ"))
        & (df_master["name"].isin(fno_symbols))
    ]

    return [
        {"symbol": row["symbol"], "token": str(row["token"])}
        for _, row in nse_eq_df.iterrows()
    ]
  except Exception:
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
      "X-MACAddress": "MAC_ADDRESS",
      "Authorization": auth_token,
  }

  now = datetime.now()
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


def analyze_stock_multi_tf(stock, auth_token):
  global alerted_stocks_today, last_alert_reset_date

  # Reset daily tracking set at midnight
  if datetime.now().date() != last_alert_reset_date:
    alerted_stocks_today.clear()
    last_alert_reset_date = datetime.now().date()

  symbol = stock["symbol"]
  token = stock["token"]

  # Rate Limiter Sleep (Ensures compliance with Angel API rate limit)
  time.sleep(0.35)

  # 1. Fetch Daily Data (Minimum 300 days for 200 EMA accuracy)
  df_daily = fetch_candle_data_direct(
      auth_token, token, "ONE_DAY", days=350
  )
  if df_daily is None or len(df_daily) < 200:
    return None, None

  df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
  df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
  df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
  df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
  df_daily["ema_200"] = ta.trend.ema_indicator(df_daily["close"], window=200)
  df_daily["vol_sma20"] = df_daily["volume"].rolling(window=20).mean()

  curr_d = df_daily.iloc[-1]
  entry_p = round(curr_d["close"], 2)

  daily_rsi_pass = curr_d["rsi"] >= MIN_DAILY_RSI
  daily_roc_pass = curr_d["roc"] >= MIN_DAILY_ROC
  daily_ema_pass = curr_d["ema_9"] > curr_d["ema_21"]
  ema_200_pass = (
      curr_d["close"] > curr_d["ema_200"] if ENABLE_200_EMA_FILTER else True
  )
  vol_pass = (
      curr_d["volume"] >= curr_d["vol_sma20"] if REQUIRE_VOL_SURGE else True
  )

  # Initial fast filter check
  if not (
      daily_rsi_pass
      and daily_roc_pass
      and daily_ema_pass
      and ema_200_pass
      and vol_pass
  ):
    return None, None

  # 2. Fetch Native Weekly Setup (Avoids manual resampling bias)
  df_weekly = fetch_candle_data_direct(
      auth_token, token, "ONE_WEEK", days=300
  )
  if df_weekly is None or len(df_weekly) < 20:
    return None, None

  df_weekly["w_rsi"] = ta.momentum.rsi(df_weekly["close"], window=14)
  df_weekly["w_ema21"] = ta.trend.ema_indicator(
      df_weekly["close"], window=21
  )

  curr_w = df_weekly.iloc[-1]
  weekly_pass = (curr_w["w_rsi"] >= MIN_WEEKLY_RSI) and (
      curr_w["close"] > curr_w["w_ema21"]
  )

  if not weekly_pass:
    return None, None

  # Both Daily & Weekly Aligned
  match_data = {
      "Symbol": symbol,
      "LTP": entry_p,
      "Daily_RSI": round(curr_d["rsi"], 2),
      "Weekly_RSI": round(curr_w["w_rsi"], 2),
      "Daily_ROC": round(curr_d["roc"], 2),
      "Signal": "🚀 DUAL-TF MOMENTUM BUY",
  }

  if symbol not in alerted_stocks_today:
    alerted_stocks_today.add(symbol)
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
    return

  logger.info(
      f"[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(fno_universe)}"
      " F&O stocks..."
  )

  qualified_matches = []
  with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [
        executor.submit(analyze_stock_multi_tf, stock, auth_token)
        for stock in fno_universe
    ]
    for future in as_completed(futures):
      _, match_data = future.result()
      if match_data:
        qualified_matches.append(match_data)

  if qualified_matches:
    df_match = pd.DataFrame(qualified_matches)
    print("\n" + "=" * 80)
    print("                🔥 ACTIVE DUAL-TIMEFRAME MATCHES 🔥")
    print("=" * 80)
    print(df_match.to_string(index=False))
    print("=" * 80 + "\n")


# MAIN AUTO-SCHEDULER LOOP
if __name__ == "__main__":
  send_telegram_alert(
      "🤖 <b>Dual-Timeframe Screener Bot Online!</b> Listening for live"
      " breakouts..."
  )

  while True:
    now = datetime.now()
    if now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 15)
        or (10 <= now.hour < 15)
        or (now.hour == 15 and now.minute <= 30)
    ):
      run_live_scanner()
      logger.info(
          f"Sleeping for {SCAN_INTERVAL_MINUTES} minutes until next cycle..."
      )
      time.sleep(SCAN_INTERVAL_MINUTES * 60)
    else:
      logger.info("Market is Closed. Waiting for market hours...")
      time.sleep(300)
