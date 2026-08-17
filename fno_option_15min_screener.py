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
# 1. SECURE CONFIGURATION & CREDENTIALS
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

# STRICT INTRADAY PARAMETERS
BULL_RSI = 62.0  # Raised for Call options
BEAR_RSI = 38.0  # Lowered for Put options
MIN_VOL_RATIO = 1.5  # Current 15-min volume >= 1.5x of last 20 candles avg
MAX_GAP_PCT = 1.2  # Avoid high gap-up/gap-down stocks


# ==========================================
# 2. DYNAMIC F&O UNIVERSE FETCH
# ==========================================
def fetch_dynamic_fno_universe():
  print(
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

    print(
        f">>> Successfully loaded {len(fno_list)} F&O stocks dynamically for"
        " 15-Min Intraday Scan!\n"
    )
    return fno_list

  except Exception as e:
    print(">>> Dynamic Universe Fetch Error:", e)
    return []


# ==========================================
# 3. TELEGRAM NOTIFIER FUNCTION
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
    print(">>> Telegram Alert Error:", e)


# ==========================================
# 4. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
  try:
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
      print(">>> Error: API Credentials missing from environment variables.")
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
      print("\n" + "=" * 95)
      print(
          ">>> SmartAPI Engine Connected! Dynamic 15-Min High-Conviction F&O"
          " Scanner Active..."
      )
      print("=" * 95 + "\n")
      return auth_token
  except Exception as e:
    print(">>> Authentication Exception:", e)
  return None


# ==========================================
# 5. DATA FETCHING & EVALUATION (15-MIN STRICT)
# ==========================================
def fetch_live_15min_data(auth_token, token):
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
  from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d 09:15")
  to_date = now.strftime("%Y-%m-%d %H:%M")

  payload = {
      "exchange": "NSE",
      "symboltoken": str(token),
      "interval": "FIFTEEN_MINUTE",
      "fromdate": from_date,
      "todate": to_date,
  }

  try:
    resp = requests.post(url, headers=headers, json=payload, timeout=8)
    resp_json = resp.json()

    if (
        resp_json.get("status")
        and resp_json.get("data")
        and len(resp_json["data"]) > 0
    ):
      df = pd.DataFrame(
          resp_json["data"],
          columns=["timestamp", "open", "high", "low", "close", "volume"],
      )
      df["open"] = df["open"].astype(float)
      df["high"] = df["high"].astype(float)
      df["low"] = df["low"].astype(float)
      df["close"] = df["close"].astype(float)
      df["volume"] = df["volume"].astype(float)
      df["date"] = pd.to_datetime(df["timestamp"]).dt.date
      return df
  except Exception:
    pass
  return None


def evaluate_realtime_setup(auth_token, stock):
  symbol = stock["symbol"]
  token = stock["token"]

  df = fetch_live_15min_data(auth_token, token)
  if df is None or len(df) < 30:
    return None, None

  df["rsi"] = ta.momentum.rsi(df["close"], window=14)
  df["roc"] = ta.momentum.roc(df["close"], window=12)
  df["vol_sma"] = df["volume"].rolling(window=20).mean()

  curr = df.iloc[-1]

  # Gap Calculation
  dates = df["date"].unique()
  if len(dates) >= 2:
    prev_date = dates[-2]
    prev_day_close = df[df["date"] == prev_date].iloc[-1]["close"]
  else:
    prev_day_close = df.iloc[0]["close"]

  gap_percent = ((curr["close"] - prev_day_close) / prev_day_close) * 100

  # Volume Ratio Calculation
  vol_ratio = curr["volume"] / curr["vol_sma"] if curr["vol_sma"] > 0 else 0
  vol_pass = vol_ratio >= MIN_VOL_RATIO

  # 1-Hour High/Low Range Breakout Check (Recent 4 candles)
  recent_4_high = df["high"].iloc[-5:-1].max()
  recent_4_low = df["low"].iloc[-5:-1].min()

  is_bullish_breakout = curr["close"] > recent_4_high
  is_bearish_breakout = curr["close"] < recent_4_low

  rsi_val = curr["rsi"] if not pd.isna(curr["rsi"]) else 50.0
  roc_val = curr["roc"] if not pd.isna(curr["roc"]) else 0.0

  is_bullish = (
      (rsi_val >= BULL_RSI)
      and (roc_val > 0.0)
      and vol_pass
      and is_bullish_breakout
  )
  is_bearish = (
      (rsi_val <= BEAR_RSI)
      and (roc_val < 0.0)
      and vol_pass
      and is_bearish_breakout
  )

  stop_loss = round(curr["low"], 2) if is_bullish else round(curr["high"], 2)
  risk = abs(curr["close"] - stop_loss)
  target = (
      round(curr["close"] + (risk * 2), 2)
      if is_bullish
      else round(curr["close"] - (risk * 2), 2)
  )

  status_data = {
      "Symbol": symbol,
      "Live_LTP": round(curr["close"], 2),
      "Gap_%": round(gap_percent, 2),
      "RSI_15M": round(rsi_val, 2),
      "ROC_15M": round(roc_val, 2),
      "VolRatio": round(vol_ratio, 2),
      "SL_Price": stop_loss,
      "Target_Price": target,
  }

  qualified = None
  if is_bullish and abs(gap_percent) <= MAX_GAP_PCT:
    qualified = status_data.copy()
    qualified["Option_Action"] = "🔥 BUY CALL (CE)"
  elif is_bearish and abs(gap_percent) <= MAX_GAP_PCT:
    qualified = status_data.copy()
    qualified["Option_Action"] = "🔻 BUY PUT (PE)"

  return status_data, qualified


# ==========================================
# 6. MAIN PIPELINE
# ==========================================
def main():
  auth_token = initialize_smartapi()
  if not auth_token:
    return

  fno_universe = fetch_dynamic_fno_universe()
  if not fno_universe:
    print(">>> F&O Universe loading failed. Exiting.")
    return

  print(
      f">>> SCANNING {len(fno_universe)} STOCKS FOR STRICT 15-MIN OPTION"
      " TRADES...\n"
  )
  all_scanned = []
  matches = []

  for stock in fno_universe:
    status, match = evaluate_realtime_setup(auth_token, stock)
    if status:
      all_scanned.append(status)
    if match:
      matches.append(match)
    time.sleep(0.08)

  print("\n" + "=" * 95)
  print("         🎯 LIVE HIGH CONVICTION OPTION TRADES (STRICT FILTERS) 🎯")
  print("=" * 95)
  if matches:
    df_match = pd.DataFrame(matches)
    print(df_match.to_string(index=False))

    match_text = df_match[
        [
            "Symbol",
            "Live_LTP",
            "Option_Action",
            "VolRatio",
            "SL_Price",
            "Target_Price",
        ]
    ].to_string(index=False)
    telegram_msg_match = (
        "<b>🎯 STRICT HIGH CONVICTION OPTION TRADES FOUND</b>\n<pre>"
        f"{match_text}</pre>"
    )
    send_telegram_message(telegram_msg_match)
  else:
    print(">>> ZERO STOCKS MATCHED STRICT REAL-TIME BREAKOUT FILTERS.")
  print("=" * 95 + "\n")


if __name__ == "__main__":
  main()
