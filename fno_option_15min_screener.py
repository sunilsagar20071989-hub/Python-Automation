from datetime import datetime, timedelta
import os
import time
import pandas as pd
from dotenv import load_dotenv
import pyotp
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# 1. SECURE CONFIGURATION & CREDENTIALS
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

FNO_OPTION_UNIVERSE = [
    # BANKING & FINANCIALS
    {"symbol": "HDFCBANK-EQ", "token": "1333"},
    {"symbol": "ICICIBANK-EQ", "token": "4963"},
    {"symbol": "SBIN-EQ", "token": "3045"},
    {"symbol": "AXISBANK-EQ", "token": "5900"},
    {"symbol": "KOTAKBANK-EQ", "token": "1922"},
    {"symbol": "INDUSINDBK-EQ", "token": "5258"},
    {"symbol": "BANKBARODA-EQ", "token": "4668"},
    {"symbol": "PNB-EQ", "token": "10666"},
    {"symbol": "AUBANK-EQ", "token": "21238"},
    {"symbol": "CANBK-EQ", "token": "10794"},
    {"symbol": "IDFCFIRSTB-EQ", "token": "11184"},
    {"symbol": "FEDERALBNK-EQ", "token": "1023"},
    # IT & TECH
    {"symbol": "TCS-EQ", "token": "11536"},
    {"symbol": "INFY-EQ", "token": "1594"},
    {"symbol": "PERSISTENT-EQ", "token": "18365"},
    {"symbol": "HCLTECH-EQ", "token": "7229"},
    {"symbol": "WIPRO-EQ", "token": "3787"},
    {"symbol": "COFORGE-EQ", "token": "11543"},
    {"symbol": "TECHM-EQ", "token": "13538"},
    {"symbol": "LTIM-EQ", "token": "17818"},
    # AUTO
    {"symbol": "BAJAJ-AUTO-EQ", "token": "16669"},
    {"symbol": "M&M-EQ", "token": "2031"},
    {"symbol": "MARUTI-EQ", "token": "10999"},
    {"symbol": "TATAMOTORS-EQ", "token": "3456"},
    {"symbol": "HEROMOTOCO-EQ", "token": "1348"},
    {"symbol": "TVSMOTOR-EQ", "token": "8479"},
    {"symbol": "EICHERMOT-EQ", "token": "910"},
    {"symbol": "BHARATFORG-EQ", "token": "422"},
    # PHARMA
    {"symbol": "SUNPHARMA-EQ", "token": "3351"},
    {"symbol": "CIPLA-EQ", "token": "694"},
    {"symbol": "DRREDDY-EQ", "token": "881"},
    {"symbol": "LUPIN-EQ", "token": "10440"},
    {"symbol": "DIVISLAB-EQ", "token": "10940"},
    {"symbol": "TORNTPHARM-EQ", "token": "3518"},
    # METALS & ENERGY
    {"symbol": "RELIANCE-EQ", "token": "2885"},
    {"symbol": "TATASTEEL-EQ", "token": "3499"},
    {"symbol": "HINDALCO-EQ", "token": "1363"},
    {"symbol": "JINDALSTEL-EQ", "token": "1732"},
    {"symbol": "VEDL-EQ", "token": "3063"},
    {"symbol": "NTPC-EQ", "token": "11630"},
    {"symbol": "POWERGRID-EQ", "token": "14977"},
    {"symbol": "ONGC-EQ", "token": "2475"},
]


# ==========================================
# 2. TELEGRAM NOTIFIER FUNCTION
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
# 3. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
  try:
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
      print(
          ">>> Error: API Credentials missing from environment variables."
      )
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
          ">>> SmartAPI Engine Connected! Real-Time Multi-Day Intraday"
          " Active..."
      )
      print("=" * 95 + "\n")
      return auth_token
  except Exception as e:
    print(">>> Authentication Exception:", e)
  return None


# ==========================================
# 4. DATA FETCHING & EVALUATION
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

      # Extract Date for previous day close mapping
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

  curr = df.iloc[-1]

  # Correct Previous Day Close Logic
  dates = df["date"].unique()
  if len(dates) >= 2:
    prev_date = dates[-2]
    prev_day_close = df[df["date"] == prev_date].iloc[-1]["close"]
  else:
    prev_day_close = df.iloc[0]["close"]

  gap_percent = ((curr["close"] - prev_day_close) / prev_day_close) * 100

  is_open_equal_low = abs(curr["open"] - curr["low"]) <= (curr["close"] * 0.0008)
  is_open_equal_high = abs(curr["open"] - curr["high"]) <= (
      curr["close"] * 0.0008
  )

  ohlc_type = (
      "OPEN=LOW (CE)"
      if is_open_equal_low
      else ("OPEN=HIGH (PE)" if is_open_equal_high else "NORMAL")
  )

  rsi_val = curr["rsi"] if not pd.isna(curr["rsi"]) else 50.0
  roc_val = curr["roc"] if not pd.isna(curr["roc"]) else 0.0

  is_bullish = (rsi_val >= 60.0) and (roc_val > 0.0)
  is_bearish = (rsi_val <= 40.0) and (roc_val < 0.0)

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
      "OHLC_Pattern": ohlc_type,
      "RSI_15M": round(rsi_val, 2),
      "ROC_15M": round(roc_val, 2),
      "SL_Price": stop_loss,
      "Target_Price": target,
  }

  qualified = None
  if is_bullish and abs(gap_percent) <= 2.0:
    qualified = status_data.copy()
    qualified["Option_Action"] = "🔥 BUY CALL (CE)"
  elif is_bearish and abs(gap_percent) <= 2.0:
    qualified = status_data.copy()
    qualified["Option_Action"] = "🔻 BUY PUT (PE)"

  return status_data, qualified


# ==========================================
# 5. MAIN PIPELINE
# ==========================================
def main():
  auth_token = initialize_smartapi()
  if not auth_token:
    return

  print(">>> SCANNING REAL-TIME 15-MIN F&O OPTION TRADES...\n")
  all_scanned = []
  matches = []

  for stock in FNO_OPTION_UNIVERSE:
    status, match = evaluate_realtime_setup(auth_token, stock)
    if status:
      all_scanned.append(status)
    if match:
      matches.append(match)
    time.sleep(0.06)

  # 1. Terminal Output Print
  print("\n" + "=" * 95)
  print("                 LIVE MARKET F&O STOCKS MONITORING TABLE")
  print("=" * 95)
  if all_scanned:
    df_all = pd.DataFrame(all_scanned)
    print(df_all.to_string(index=False))
  else:
    print(">>> Data fetching issue. Verify API Key/Session.")
  print("=" * 95 + "\n")

  print("=" * 95)
  print("       🎯 LIVE REAL-TIME HIGH CONVICTION OPTION TRADES 🎯")
  print("=" * 95)
  if matches:
    df_match = pd.DataFrame(matches)
    print(df_match.to_string(index=False))
  else:
    print(">>> No low-risk setups matched live conditions right now.")
  print("=" * 95 + "\n")

  # 2. Telegram Broadcast (Filtered High Conviction Matches Only)
  if matches:
    df_match = pd.DataFrame(matches)
    match_text = df_match[
        ["Symbol", "Live_LTP", "Option_Action", "SL_Price", "Target_Price"]
    ].to_string(index=False)
    telegram_msg_match = (
        f"<b>🎯 HIGH CONVICTION OPTION TRADES FOUND</b>\n<pre>{match_text}</pre>"
    )
    send_telegram_message(telegram_msg_match)


if __name__ == "__main__":
     main()
  main()
