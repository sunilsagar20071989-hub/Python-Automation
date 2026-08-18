# ==============================================================================
# DYNAMIC F&O STOCKS MASTER SCREENER & TELEGRAM ALERT BOT (200 EMA & SWING ENHANCED)
# ==============================================================================

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

# ==========================================
# 1. SECURE CONFIGURATION (STRICT .ENV READ)
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
smart_api_instance = None


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
  logger.info(
      ">>> Downloading Angel One Master Instrument File for Dynamic F&O"
      " Universe..."
  )
  url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

  try:
    resp = requests.get(url, timeout=15)
    data = resp.json()
    df_master = pd.DataFrame(data)

    nfo_df = df_master[df_master["exch_seg"] == "NFO"]
    fno_symbols = set(nfo_df["name"].dropna().unique())

    nse_eq_df = df_master[
        (df_master["exch_seg"] == "NSE")
        & (df_master["symbol"].str.endswith("-EQ"))
        & (df_master["name"].isin(fno_symbols))
    ]

    fno_list = []
    for _, row in nse_eq_df.iterrows():
      fno_list.append({"symbol": row["symbol"], "token": str(row["token"])})

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
    requests.post(url, data=payload, timeout=10)
  except Exception as e:
    logger.error(f"Telegram Alert Exception: {e}")


# ==========================================
# 5. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
  global smart_api_instance
  try:
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
      logger.error("SmartAPI Credentials missing from .env file!")
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
      logger.info(">>> SmartAPI Authenticated Successfully!")
      return auth_token
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
# 7. HISTORICAL DATA FETCH (200 BARS DATA ENFORCED)
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
      "X-MACAddress": "MAC_ADDRESS",
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


# ==========================================
# 8. STOCK SCANNER ENGINE (WITH 200 EMA)
# ==========================================
def scan_fno_universe(auth_token, fno_universe):
  logger.info(
      f">>> Scanning {len(fno_universe)} F&O Stocks with 200 EMA + Breakout"
      " Filters..."
  )
  all_scanned = []
  qualified_matches = []

  for stock in fno_universe:
    symbol = stock["symbol"]
    token = stock["token"]

    df_daily = fetch_candle_data_direct(
        auth_token, token, "ONE_DAY", days=300
    )  # 300 days ensures accurate 200 EMA
    if df_daily is None or len(df_daily) < 200:
      continue

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

    # ENFORCED STRICT 200 EMA FILTER
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
    all_scanned.append(status_data)

    # ALL STRICT FILTERS MUST PASS TOGETHER
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
      qualified_matches.append(match_data)

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

    time.sleep(0.08)

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
        f"✅ <b>Scan Complete!</b> Found {len(qualified_matches)} high-conviction"
        " swing breakout setup(s)."
    )
  else:
    print(">>> ZERO STOCKS MATCHED STRICT BREAKOUT + 200 EMA FILTERS TODAY.")
    send_telegram_alert(
        "ℹ️ <b>Scan Complete!</b> No stocks matched strict breakout filters"
        " today."
    )
  print("=" * 95 + "\n")


if __name__ == "__main__":
  main()
