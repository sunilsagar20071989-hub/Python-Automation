from datetime import datetime, timedelta
import os
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY", "N7XNbnkE")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE", "S885143")
PIN = os.getenv("SMARTAPI_PIN", "1989")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "ZH76UOCDHM4TITQGDKN32HBZEI")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8560792327:AAErjHTU4LlKxlueD4c-EXxS2KcqVwBrDN8"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1427460047")

# STRATEGY THRESHOLDS (Strict Momentum & Breakout Criteria)
MIN_RSI = 65.0  # Raised to 65 for stronger momentum
MIN_ROC = 1.5  # ROC must be >= 1.5%
MIN_VOL_SURGE_RATIO = 1.5  # Volume must be >= 1.5x of 20-day SMA Volume
MIN_DAY_CHANGE_PCT = 2.0  # Minimum 2% intraday price surge
ENABLE_EMA_FILTER = True  # EMA 9 > EMA 21 & Close > EMA 44


# ==========================================
# 2. DYNAMIC F&O UNIVERSE FETCH (ANGEL ONE MASTER SCRIP)
# ==========================================
def fetch_dynamic_fno_universe():
  """Angel One Scrip Master JSON download karke saare F&O Underlying Equity Stocks extract karta hai."""
  print(
      ">>> Downloading Angel One Master Instrument File for Dynamic F&O"
      " Universe..."
  )
  url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

  try:
    resp = requests.get(url, timeout=15)
    data = resp.json()
    df_master = pd.DataFrame(data)

    # NFO segment se saare unique underlying trading symbols extract karein
    nfo_df = df_master[df_master["exch_seg"] == "NFO"]
    fno_symbols = set(nfo_df["name"].dropna().unique())

    # NSE Equity (EQ) segment mein se unhi F&O symbols ke EQ tokens filter karein
    nse_eq_df = df_master[
        (df_master["exch_seg"] == "NSE")
        & (df_master["symbol"].str.endswith("-EQ"))
        & (df_master["name"].isin(fno_symbols))
    ]

    fno_list = []
    for _, row in nse_eq_df.iterrows():
      fno_list.append({"symbol": row["symbol"], "token": str(row["token"])})

    print(f">>> Successfully loaded {len(fno_list)} F&O stocks dynamically!\n")
    return fno_list

  except Exception as e:
    print(">>> Error fetching Scrip Master File:", e)
    return []


# ==========================================
# 3. TELEGRAM NOTIFIER
# ==========================================
def send_telegram_message(message_text):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message_text,
      "parse_mode": "HTML",
  }
  try:
    requests.post(url, json=payload, timeout=5)
  except Exception as e:
    print(">>> Telegram Notification Failed:", e)


# ==========================================
# 4. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
  try:
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
      print(">>> Error: Environment variables/secrets not set properly.")
      return None

    smart_api = SmartConnect(api_key=API_KEY)
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    data = smart_api.generateSession(CLIENT_CODE, PIN, totp_code)

    if data and data.get("status"):
      raw_token = data["data"]["jwtToken"]
      auth_token = (
          raw_token
          if raw_token.startswith("Bearer ")
          else f"Bearer {raw_token}"
      )
      print("\n" + "=" * 85)
      print(
          ">>> SmartAPI Authenticated! Strict Breakout & Momentum Verification"
          " Active..."
      )
      print("=" * 85 + "\n")
      return auth_token
  except Exception as e:
    print(">>> Authentication Exception:", e)
  return None


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
  if now.hour < 9:
    last_trade_date = now - timedelta(days=1)
  else:
    last_trade_date = now

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
      df["high"] = df["high"].astype(float)
      df["low"] = df["low"].astype(float)
      df["volume"] = df["volume"].astype(float)
      return df
  except Exception:
    pass
  return None


# ==========================================
# 6. DYNAMIC F&O STOCK SCANNER (STRICT HIGH-CONVICTION FILTERS)
# ==========================================
def scan_fno_universe(auth_token, fno_stock_list):
  print(
      f">>> SCANNING {len(fno_stock_list)} F&O STOCKS WITH STRICT BREAKOUT"
      f" FILTERS (RSI >= {MIN_RSI}, Vol >= {MIN_VOL_SURGE_RATIO}x, 20-Day High"
      " Breakout)...\n"
  )
  all_scanned = []
  qualified_matches = []

  for stock in fno_stock_list:
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
    prev_close = df_daily.iloc[-2]["close"]

    # 1. Volume Surge Check (Volume >= 1.5x 20-day Volume Average)
    vol_ratio = (
        curr["volume"] / curr["vol_sma20"] if curr["vol_sma20"] > 0 else 0
    )
    vol_pass = vol_ratio >= MIN_VOL_SURGE_RATIO

    # 2. Price Breakout Check (Closing higher than last 20-day highest candle)
    recent_20_high = df_daily["high"].iloc[-21:-1].max()
    breakout_pass = curr["close"] >= recent_20_high

    # 3. Minimum Intraday Move % (At least 2% gain today)
    day_change_pct = ((curr["close"] - prev_close) / prev_close) * 100
    move_pass = day_change_pct >= MIN_DAY_CHANGE_PCT

    # 4. Strict Indicators Filter
    rsi_pass = curr["rsi"] >= MIN_RSI
    roc_pass = curr["roc"] >= MIN_ROC
    ema_pass = (
        (curr["ema_9"] > curr["ema_21"]) and (curr["close"] > curr["ema_44"])
        if ENABLE_EMA_FILTER
        else True
    )

    status_data = {
        "Symbol": symbol,
        "LTP": round(curr["close"], 2),
        "Change%": round(day_change_pct, 2),
        "RSI(14)": round(curr["rsi"], 2),
        "ROC(12)": round(curr["roc"], 2),
        "VolRatio": round(vol_ratio, 2),
        "RSI_Pass": "PASS" if rsi_pass else "FAIL",
        "Vol_Pass": "PASS" if vol_pass else "FAIL",
        "Breakout": "YES" if breakout_pass else "NO",
    }
    all_scanned.append(status_data)

    # STRICT ENFORCEMENT: ALL FILTERS MUST PASS
    if (
        rsi_pass
        and roc_pass
        and ema_pass
        and vol_pass
        and breakout_pass
        and move_pass
    ):
      match_data = status_data.copy()
      match_data["Signal"] = "🚀 HIGH CONVICTION BREAKOUT"
      qualified_matches.append(match_data)

    time.sleep(0.12)  # API Rate limit safety

  return all_scanned, qualified_matches


# ==========================================
# 7. MAIN PIPELINE
# ==========================================
def main():
  auth_token = initialize_smartapi()
  if not auth_token:
    print(">>> Session initialization failed. Exiting.")
    return

  # Step 1: Fetch all F&O stocks dynamically from Angel One Scrip Master
  fno_stock_list = fetch_dynamic_fno_universe()

  if not fno_stock_list:
    print(">>> F&O Stock List fetch failed. Exiting.")
    return

  # Step 2: Run Scanner on dynamic universe
  all_scanned, qualified_matches = scan_fno_universe(
      auth_token, fno_stock_list
  )

  print("\n" + "=" * 95)
  print(
      "                    RAW METRICS LOG (ALL DYNAMICALLY SCANNED F&O STOCKS)"
  )
  print("=" * 95)
  if all_scanned:
    df_all = pd.DataFrame(all_scanned)
    print(df_all.to_string(index=False))
  else:
    print(">>> No stock data retrieved.")
  print("=" * 95 + "\n")

  print("=" * 95)
  print(
      "  🔥 FINAL FILTERED CANDIDATES (RSI >= 65, Vol >= 1.5x, 20-Day High"
      " Breakout) 🔥"
  )
  print("=" * 95)
  if qualified_matches:
    df_match = pd.DataFrame(qualified_matches)
    print(df_match.to_string(index=False))

    # Send Filtered Alert to Telegram
    formatted_str = df_match[
        ["Symbol", "LTP", "Change%", "RSI(14)", "VolRatio"]
    ].to_string(index=False)
    msg = (
        "<b>🚀 HIGH CONVICTION BREAKOUT CANDIDATES</b>\n<pre>"
        f"{formatted_str}</pre>"
    )
    send_telegram_message(msg)
  else:
    print(
        ">>> ZERO STOCKS MATCHED STRICT RSI + VOL SURGE + BREAKOUT FILTERS"
        " TODAY."
    )
  print("=" * 95 + "\n")


if __name__ == "__main__":
  main()
