from datetime import datetime
import os
import numpy as np
from dotenv import load_dotenv
import pandas as pd
import pyotp
from SmartApi import SmartConnect
import ta

# ==========================================
# 1. CREDENTIALS & PARAMETERS
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

NIFTY_TOKEN = "99926000"
SL_PCT = 0.08  # 8.00% Stop Loss
TARGET_PCT = 0.16  # 16.00% Target (1:2 Risk to Reward)
DELTA = 0.55
MAX_DAILY_TRADES = 2  # Maximum 2 trades per day filter

# ==========================================
# 2. LOGIN TO SMARTAPI
# ==========================================
try:
  totp = pyotp.TOTP(TOTP_SECRET).now()
  smartApi = SmartConnect(api_key=API_KEY)
  data = smartApi.generateSession(CLIENT_CODE, PIN, totp)
  print(
      ">>> Login Successful! Fetching Data (5-Min Timeframe + Daily Limit"
      " Filter)..."
  )
except Exception as e:
  print(">>> Login Error:", e)
  exit()

# ==========================================
# 3. FETCH HISTORICAL SPOT DATA
# ==========================================
to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
from_date = (datetime.now() - pd.Timedelta(days=15)).strftime("%Y-%m-%d %H:%M")

param = {
    "exchange": "NSE",
    "symboltoken": NIFTY_TOKEN,
    "interval": "FIVE_MINUTE",
    "fromdate": from_date,
    "todate": to_date,
}

resp = smartApi.getCandleData(param)

if not resp.get("status") or not resp.get("data"):
  print(">>> Failed to fetch historical data!")
  exit()

df = pd.DataFrame(
    resp["data"],
    columns=["timestamp", "open", "high", "low", "close", "volume"],
)
df["timestamp"] = pd.to_datetime(df["timestamp"])
df["close"] = df["close"].astype(float)
df["high"] = df["high"].astype(float)
df["low"] = df["low"].astype(float)

# Indicators
df["rsi"] = ta.momentum.rsi(df["close"], window=14)
df["roc"] = ta.momentum.roc(df["close"], window=12)
df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
df["ema_44"] = ta.trend.ema_indicator(df["close"], window=44)

# ==========================================
# 4. BACKTESTING ENGINE WITH DAILY TRADE LIMIT
# ==========================================
trades = []
pos_active = False
entry_spot = 0.0
entry_prem = 0.0
trade_type = ""
sl_prem = 0.0
tgt_prem = 0.0
entry_time = None

target_count = 0
sl_count = 0
squareoff_count = 0

current_day = None
daily_trades_count = 0

print("\n" + "=" * 90)
print(
    f"{'ENTRY TIME':<18} | {'TYPE':<4} | {'SPOT ENTRY':<10} | {'PREM ENTRY':<10}"
    f" | {'PREM EXIT':<10} | {'RESULT'}"
)
print("=" * 90)

for i in range(50, len(df)):
  curr = df.iloc[i]
  prev = df.iloc[i - 1]

  t_date = curr["timestamp"].date()
  t_time = curr["timestamp"].strftime("%Y-%m-%d %H:%M")
  curr_time = curr["timestamp"].time()

  # Reset Daily Trade Counter on new day
  if current_day != t_date:
    current_day = t_date
    daily_trades_count = 0

  # 1. MONITOR ACTIVE POSITION
  if pos_active:
    if trade_type == "CE":
      max_prem_gain = (curr["high"] - entry_spot) * DELTA
      max_prem_loss = (entry_spot - curr["low"]) * DELTA
      high_prem = entry_prem + max_prem_gain
      low_prem = entry_prem - max_prem_loss

      # Evaluation logic
      if low_prem <= sl_prem:
        sl_count += 1
        trades.append(("SL", -8.0))
        print(
            f"{entry_time:<18} | {trade_type:<4} | ₹{entry_spot:<9.2f} |"
            f" ₹{entry_prem:<9.2f} | ₹{sl_prem:<9.2f} | SL HIT (-8.00%)"
        )
        pos_active = False
      elif high_prem >= tgt_prem:
        target_count += 1
        trades.append(("TGT", 16.0))
        print(
            f"{entry_time:<18} | {trade_type:<4} | ₹{entry_spot:<9.2f} |"
            f" ₹{entry_prem:<9.2f} | ₹{tgt_prem:<9.2f} | TARGET HIT (+16.00%)"
        )
        pos_active = False

    elif trade_type == "PE":
      max_prem_gain = (entry_spot - curr["low"]) * DELTA
      max_prem_loss = (curr["high"] - entry_spot) * DELTA
      high_prem = entry_prem + max_prem_gain
      low_prem = entry_prem - max_prem_loss

      if low_prem <= sl_prem:
        sl_count += 1
        trades.append(("SL", -8.0))
        print(
            f"{entry_time:<18} | {trade_type:<4} | ₹{entry_spot:<9.2f} |"
            f" ₹{entry_prem:<9.2f} | ₹{sl_prem:<9.2f} | SL HIT (-8.00%)"
        )
        pos_active = False
      elif high_prem >= tgt_prem:
        target_count += 1
        trades.append(("TGT", 16.0))
        print(
            f"{entry_time:<18} | {trade_type:<4} | ₹{entry_spot:<9.2f} |"
            f" ₹{entry_prem:<9.2f} | ₹{tgt_prem:<9.2f} | TARGET HIT (+16.00%)"
        )
        pos_active = False

    # EOD Square-off 15:10 (If position active)
    if pos_active and curr_time >= datetime.strptime("15:10", "%H:%M").time():
      squareoff_count += 1
      trades.append(("SQUARE_OFF", 0))
      print(
          f"{entry_time:<18} | {trade_type:<4} | ₹{entry_spot:<9.2f} |"
          f" ₹{entry_prem:<9.2f} | ₹{entry_prem:<9.2f} | 03:10 PM SQUARE-OFF"
      )
      pos_active = False

  # 2. ENTRY SIGNALS WITH MAX TRADES PER DAY LIMIT
  elif (
      not pos_active
      and (daily_trades_count < MAX_DAILY_TRADES)
      and (
          datetime.strptime("09:30", "%H:%M").time()
          <= curr_time
          <= datetime.strptime("14:45", "%H:%M").time()
      )
  ):

    bullish_cross = (prev["ema_9"] <= prev["ema_21"]) and (
        curr["ema_9"] > curr["ema_21"]
    )
    bearish_cross = (prev["ema_9"] >= prev["ema_21"]) and (
        curr["ema_9"] < curr["ema_21"]
    )

    bounce_ce = (curr["low"] <= curr["ema_44"] * 1.002) and (
        curr["close"] > curr["ema_44"]
    )
    reject_pe = (curr["high"] >= curr["ema_44"] * 0.998) and (
        curr["close"] < curr["ema_44"]
    )

    ce_confirm = (curr["rsi"] > 60) and (curr["roc"] > 0)
    pe_confirm = (curr["rsi"] < 40) and (curr["roc"] < 0)

    if (bullish_cross or bounce_ce) and ce_confirm:
      pos_active = True
      daily_trades_count += 1
      trade_type = "CE"
      entry_spot = curr["close"]
      entry_prem = round(curr["close"] * 0.0075, 2)
      sl_prem = round(entry_prem * (1 - SL_PCT), 2)
      tgt_prem = round(entry_prem * (1 + TARGET_PCT), 2)
      entry_time = t_time

    elif (bearish_cross or reject_pe) and pe_confirm:
      pos_active = True
      daily_trades_count += 1
      trade_type = "PE"
      entry_spot = curr["close"]
      entry_prem = round(curr["close"] * 0.0075, 2)
      sl_prem = round(entry_prem * (1 - SL_PCT), 2)
      tgt_prem = round(entry_prem * (1 + TARGET_PCT), 2)
      entry_time = t_time

print("=" * 90)
total_t = target_count + sl_count + squareoff_count
win_rate = (target_count / total_t * 100) if total_t > 0 else 0
print(
    f"Total Trades: {total_t} | Target (+16%): {target_count} | SL (-8%):"
    f" {sl_count} | Sq-Off: {squareoff_count} | Win Rate: {win_rate:.2f}%"
)
print("=" * 90)
