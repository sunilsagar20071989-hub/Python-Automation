from datetime import datetime, time as dtime
import json
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
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# Logging Configuration
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("NiftyAlgo")

# ==========================================
# 1. SECURE CREDENTIALS & RISK PARAMETERS
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Capital & Fallback Settings
DEFAULT_TOTAL_CAPITAL = 100000.0  # Safe fallback if RMS API fails

# Risk Parameters
SL_PCT = 0.045
TARGET_PCT = 0.1575
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade
NIFTY_LOT_SIZE = 65  # Verified NSE Nifty Lot Size

# Trailing Stop-Loss Parameters
ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04  # Activates after +4% gain
TSL_STEP_TRIGGER_PCT = 0.02  # Trail SL every +2% move
TSL_STEP_MOVE_PCT = 0.015  # Move SL up by +1.5%

NIFTY_TOKEN = "99926000"
INDIA_VIX_TOKEN = "26009"

MIN_VIX = 10.0
MAX_VIX = 24.0
MAX_GAP_PCT = 0.007

MAX_DAILY_TRADES = 4
MAX_HOLDING_MINUTES = 22
daily_trades_count = 0
consecutive_sl_count = 0
trade_entry_time = None

pos_active = False
active_symbol = ""
active_token = ""
entry_price = 0.0
sl_price = 0.0
tgt_price = 0.0
highest_price_seen = 0.0
tsl_activated = False
active_quantity = NIFTY_LOT_SIZE

# Bot State Controls
algo_paused = False
telegram_last_update_id = 0

scrip_master_df = None
auth_token = ""
feed_token = ""

last_candle_minute = -1
cached_close = None
cached_signal = "NO_TRADE"

live_ltp_dict = {}
sws = None
smartApi = None
LOG_FILE = "trade_log.csv"


# ==========================================
# TELEGRAM NOTIFICATION & ACTION HELPERS
# ==========================================
def send_telegram_alert(message, max_retries=3):
  """Sends HTML formatted Telegram notifications safely"""
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("Telegram Bot Token or Chat ID environment variables missing.")
    return

  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "HTML",
  }

  for attempt in range(max_retries):
    try:
      response = requests.post(url, data=payload, timeout=5)
      if response.status_code == 200:
        return
    except requests.exceptions.RequestException as e:
      if attempt == max_retries - 1:
        logger.error(f"Telegram Alert Error: {e}")
      time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
  """Logs trade performance metrics into CSV file"""
  global trade_entry_time

  pnl = round((exit_p - entry_p) * qty, 2)
  pnl_pct = (
      round(((exit_p - entry_p) / entry_p) * 100, 2) if entry_p > 0 else 0.0
  )
  holding_time = (
      round((datetime.now() - trade_entry_time).total_seconds() / 60.0, 2)
      if trade_entry_time
      else 0.0
  )

  log_data = {
      "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
      "Symbol": symbol,
      "Type": trade_type,
      "Entry_Price": entry_p,
      "Exit_Price": exit_p,
      "Quantity": qty,
      "PnL_INR": pnl,
      "PnL_PCT": pnl_pct,
      "Holding_Mins": holding_time,
      "Exit_Reason": reason,
  }

  df_log = pd.DataFrame([log_data])
  file_exists = os.path.isfile(LOG_FILE)
  df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
  logger.info(f"[TRADE LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Exit: {reason}")


def process_telegram_command(command):
  """Handles interactive control commands via Telegram"""
  global pos_active, active_symbol, active_token, active_quantity, entry_price
  global sl_price, tgt_price, highest_price_seen, tsl_activated, trade_entry_time
  global daily_trades_count, algo_paused

  cmd = command.strip().split()[0].split("@")[0].lower()

  if cmd == "/status":
    if pos_active:
      ltp = get_live_ltp(active_token, active_symbol) or entry_price
      pnl = round((ltp - entry_price) * active_quantity, 2)
      pnl_pct = round(((ltp - entry_price) / entry_price) * 100, 2)
      holding_time = (
          round((datetime.now() - trade_entry_time).total_seconds() / 60.0, 1)
          if trade_entry_time
          else 0
      )

      msg = (
          f"📊 <b>CURRENT POSITION STATUS</b>\n"
          f"<b>Symbol:</b> {active_symbol}\n"
          f"<b>Quantity:</b> {active_quantity}\n"
          f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
          f"<b>LTP:</b> ₹{ltp:.2f}\n"
          f"<b>Stop Loss:</b> ₹{sl_price:.2f}\n"
          f"<b>Target:</b> ₹{tgt_price:.2f}\n"
          f"<b>PnL:</b> ₹{pnl} ({pnl_pct}%)\n"
          f"<b>Holding Time:</b> {holding_time}m / {MAX_HOLDING_MINUTES}m\n"
          f"<b>Bot Status:</b> {'PAUSED' if algo_paused else 'RUNNING'}"
      )
    else:
      spot = get_nifty_spot_ltp()
      spot_str = f"₹{spot:.2f}" if spot else "N/A"
      msg = (
          f"ℹ️ <b>NO ACTIVE POSITION</b>\n"
          f"<b>Nifty Spot:</b> {spot_str}\n"
          f"<b>Daily Trades Completed:</b>"
          f" {daily_trades_count}/{MAX_DAILY_TRADES}\n"
          f"<b>Bot Control Status:</b> {'PAUSED' if algo_paused else 'ACTIVE'}"
      )
    send_telegram_alert(msg)

  elif cmd == "/close":
    if pos_active:
      ltp = get_live_ltp(active_token, active_symbol) or entry_price
      # Safe Execution fallback if place_order is defined in subsequent parts
      if "place_order" in globals():
        globals()["place_order"](
            active_symbol, active_token, "SELL", active_quantity
        )

      log_trade(
          active_symbol,
          "BUY",
          entry_price,
          ltp,
          active_quantity,
          "MANUAL_TELEGRAM_EXIT",
      )

      pnl = round((ltp - entry_price) * active_quantity, 2)
      send_telegram_alert(
          f"⚠️ <b>MANUAL EXIT TRIGGERED VIA TELEGRAM</b>\n"
          f"<b>Symbol:</b> {active_symbol}\n"
          f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
          f"<b>PnL:</b> ₹{pnl}"
      )

      # RESET POSITION STATE
      pos_active = False
      active_symbol = ""
      active_token = ""
      active_quantity = 0
      entry_price = 0.0
      sl_price = 0.0
      tgt_price = 0.0
      highest_price_seen = 0.0
      tsl_activated = False
      trade_entry_time = None
      daily_trades_count += 1
    else:
      send_telegram_alert("⚠️ No active position to close.")

  elif cmd == "/pause":
    algo_paused = True
    send_telegram_alert(
        "⏸️ <b>Algo Bot Paused!</b> New trade entries disabled."
    )

  elif cmd == "/resume":
    algo_paused = False
    send_telegram_alert(
        "▶️ <b>Algo Bot Resumed!</b> Scanning for live entries."
    )

  elif cmd == "/summary":
    if os.path.exists(LOG_FILE):
      try:
        df_log = pd.read_csv(LOG_FILE)
        today_str = datetime.now().strftime("%Y-%m-%d")
        df_today = df_log[
            df_log["Timestamp"].astype(str).str.startswith(today_str)
        ]

        if not df_today.empty:
          total_pnl = df_today["PnL_INR"].sum()
          wins = len(df_today[df_today["PnL_INR"] > 0])
          total_t = len(df_today)
          msg = (
              f"📈 <b>TODAY'S SUMMARY ({today_str})</b>\n"
              f"<b>Total Trades:</b> {total_t}\n"
              f"<b>Win Rate:</b> {wins}/{total_t}\n"
              f"<b>Net PnL:</b> ₹{total_pnl:.2f}"
          )
        else:
          msg = "📄 No trades logged for today yet."
      except Exception as e:
        msg = f"⚠️ Error reading trade log: {e}"
    else:
      msg = "📄 No trade history log file found."
    send_telegram_alert(msg)

  elif cmd == "/help":
    help_msg = (
        "🤖 <b>AVAILABLE TELEGRAM COMMANDS</b>\n\n"
        "• <b>/status</b> - View active position & market state\n"
        "• <b>/close</b> - Force exit open position\n"
        "• <b>/pause</b> - Pause new trade signals\n"
        "• <b>/resume</b> - Resume scanning\n"
        "• <b>/summary</b> - Today's PnL & trade performance\n"
        "• <b>/help</b> - Show command list"
    )
    send_telegram_alert(help_msg)


def check_telegram_updates():
  """Polls Telegram API for commands"""
  global telegram_last_update_id
  if not TELEGRAM_BOT_TOKEN:
    return

  try:
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={telegram_last_update_id + 1}&timeout=1"
    )
    res = requests.get(url, timeout=3)
    if res.status_code == 200:
      data = res.json()
      if data.get("ok") and data.get("result"):
        for update in data["result"]:
          telegram_last_update_id = update["update_id"]
          if "message" in update and "text" in update["message"]:
            chat_id = str(update["message"]["chat"]["id"])
            if chat_id == str(TELEGRAM_CHAT_ID):
              command_text = update["message"]["text"]
              process_telegram_command(command_text)
  except Exception:
    pass


def telegram_listener_thread():
  while True:
    check_telegram_updates()
    time.sleep(2)


# ==========================================
# 2. LOGIN TO SMARTAPI & SCRIP MASTER
# ==========================================
try:
  if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
    raise Exception(
        "SmartAPI Credentials missing from Environment Variables (.env)"
    )

  logger.info("Generating TOTP & Authenticating SmartAPI...")
  totp = pyotp.TOTP(TOTP_SECRET).now()
  smartApi = SmartConnect(api_key=API_KEY)
  data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

  if not data or not data.get("status"):
    error_msg = (
        data.get("message", "Authentication Error")
        if isinstance(data, dict)
        else "No Server Response"
    )
    raise Exception(f"SmartAPI Login Failed: {error_msg}")

  auth_token = data["data"]["jwtToken"]
  feed_token = smartApi.getfeedToken()
  logger.info("SmartAPI Authentication Successful!")

  urls = [
      "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
      "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
  ]
  headers = {"User-Agent": "Mozilla/5.0"}
  scrip_loaded = False

  logger.info("Downloading Scrip Master JSON...")
  for scrip_url in urls:
    try:
      res = requests.get(scrip_url, headers=headers, timeout=25)
      if res.status_code == 200:
        scrip_master_df = pd.DataFrame(res.json())
        if "token" in scrip_master_df.columns:
          scrip_master_df["token"] = scrip_master_df["token"].astype(str)
        if "symbol" in scrip_master_df.columns:
          scrip_master_df["symbol"] = scrip_master_df["symbol"].astype(str)
        scrip_loaded = True
        logger.info(
            f"Scrip Master Loaded! Total Records: {len(scrip_master_df)}"
        )
        break
    except Exception as e:
      logger.warning(f"Failed loading Scrip Master from {scrip_url}: {e}")

  if not scrip_loaded or scrip_master_df is None or scrip_master_df.empty:
    raise Exception("Scrip Master Download Failed from all mirrors.")

except Exception as e:
  logger.critical(f"Startup Exception: {e}")
  sys.exit(1)

# Start Background Telegram Poller
try:
  t_listener = threading.Thread(target=telegram_listener_thread, daemon=True)
  t_listener.start()
  logger.info("Telegram Polling Thread Started.")
except Exception as e:
  logger.error(f"Telegram Listener Thread Error: {e}")


# ==========================================
# 3. DYNAMIC RISK & POSITION SIZING
# ==========================================
def calculate_dynamic_quantity(option_price):
  """Calculates quantity based on available capital & max risk per trade"""
  try:
    if option_price <= 0:
      return NIFTY_LOT_SIZE

    rms_data = smartApi.rmsLimit()
    net_capital = 0.0

    if rms_data and rms_data.get("status") and "data" in rms_data:
      data_dict = rms_data["data"]
      net_capital = float(
          data_dict.get("net", data_dict.get("availablecash", 0.0))
      )

    # Bug Fix: Handled undefined TOTAL_CAPITAL with safe fallback
    if net_capital <= 0:
      net_capital = DEFAULT_TOTAL_CAPITAL
      logger.info(f"Using Fallback Capital: ₹{net_capital:.2f}")

    max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
    risk_per_share = option_price * SL_PCT

    if risk_per_share <= 0:
      return NIFTY_LOT_SIZE

    calculated_qty = max_risk_amount / risk_per_share
    lots = max(1, int(calculated_qty // NIFTY_LOT_SIZE))
    total_qty = lots * NIFTY_LOT_SIZE

    # Capital Allocation Safety Guard
    if (total_qty * option_price) > net_capital:
      max_affordable_lots = int(
          net_capital // (NIFTY_LOT_SIZE * option_price)
      )
      lots = max(1, max_affordable_lots)
      total_qty = lots * NIFTY_LOT_SIZE

    logger.info(
        f"Dynamic Risk Sizing: Net Cap ₹{net_capital:.2f} | Lots: {lots}"
        f" ({total_qty} Qty)"
    )
    return total_qty

  except Exception as e:
    logger.error(f"Dynamic Sizing Error: {e}")
    return NIFTY_LOT_SIZE


# ==========================================
# 4. WEBSOCKET HANDLERS
# ==========================================
def on_data(wsapp, message):
  global live_ltp_dict
  try:
    if isinstance(message, dict):
      if "token" in message and "last_traded_price" in message:
        token = str(message["token"])
        # Bug Fix: SmartAPI V2 directly passes float LTP in Rupees
        raw_ltp = float(message["last_traded_price"])
        ltp = raw_ltp / 100.0 if raw_ltp > 100000 else raw_ltp
        live_ltp_dict[token] = ltp

    elif isinstance(message, list):
      for tick in message:
        if (
            isinstance(tick, dict)
            and "token" in tick
            and "last_traded_price" in tick
        ):
          token = str(tick["token"])
          raw_ltp = float(tick["last_traded_price"])
          ltp = raw_ltp / 100.0 if raw_ltp > 100000 else raw_ltp
          live_ltp_dict[token] = ltp
  except Exception:
    pass


def on_open(wsapp):
  logger.info("WebSocket Live Feed Connected!")


def on_error(wsapp, error):
  logger.error(f"WebSocket Error: {error}")


def on_close(wsapp):
  logger.info("WebSocket Feed Closed.")


def setup_and_subscribe_websocket(token, exchange_type=2):
  global sws
  token_str = str(token)

  try:
    if sws is not None and hasattr(sws, "is_connected") and sws.is_connected():
      token_list = [{"exchangeType": exchange_type, "tokens": [token_str]}]
      sws.subscribe("correlation_id_trade", 1, token_list)
      logger.info(f"Subscribed Token {token_str} to existing WS")
      return

    sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)
    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close

    ws_thread = threading.Thread(target=lambda: sws.connect(), daemon=True)
    ws_thread.start()
    time.sleep(2)

    token_list = [{"exchangeType": exchange_type, "tokens": [token_str]}]
    sws.subscribe("correlation_id_trade", 1, token_list)
    logger.info(f"WebSocket Active & Subscribed Token: {token_str}")

  except Exception as e:
    logger.error(f"WebSocket Setup Error: {e}")


def get_live_ltp(token, symbol, exchange="NFO"):
  """Fetches live LTP from WS cache with REST API fallback"""
  token_str = str(token)
  if token_str in live_ltp_dict and live_ltp_dict[token_str] > 0:
    return live_ltp_dict[token_str]

  try:
    ltp_data = smartApi.ltpData(exchange, symbol, token_str)
    if ltp_data and ltp_data.get("status") and "data" in ltp_data:
      ltp = float(ltp_data["data"]["ltp"])
      live_ltp_dict[token_str] = ltp
      return ltp
  except Exception as e:
    logger.error(f"REST API LTP Fallback Error for {symbol}: {e}")

  return None


def get_nifty_spot_ltp():
  """Fetches Nifty 50 Spot Price"""
  token_str = str(NIFTY_TOKEN)
  if token_str in live_ltp_dict and live_ltp_dict[token_str] > 0:
    return live_ltp_dict[token_str]

  try:
    spot_data = smartApi.ltpData("NSE", "NIFTY", token_str)
    if spot_data and spot_data.get("status") and "data" in spot_data:
      return float(spot_data["data"]["ltp"])
  except Exception as e:
    logger.error(f"Nifty Spot Fetch Error: {e}")
  return None

logger = logging.getLogger("NiftyAlgo")

# Global Risk & Win Tracking Variables
consecutive_win_count = 0


# ==========================================
# ORDER EXECUTION WRAPPER (CRITICAL BUG FIX)
# ==========================================
def place_order(symbol, token, buy_sell_type, quantity, exchange="NFO"):
  """Executes orders on SmartAPI safely with error handling"""
  try:
    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": buy_sell_type.upper(),
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "price": "0",
        "quantity": str(quantity),
    }
    logger.info(
        f"[ORDER ATTEMPT] {buy_sell_type} {quantity} Qty of {symbol} (Token:"
        f" {token})"
    )

    if smartApi is not None:
      response = smartApi.placeOrder(order_params)
      if (
          response
          and response.get("status")
          and "data" in response
          and "orderid" in response["data"]
      ):
        order_id = response["data"]["orderid"]
        logger.info(f"[ORDER PLACED SUCCESS] Order ID: {order_id}")
        return order_id
      else:
        logger.error(f"[ORDER REJECTED] Response: {response}")
    else:
      logger.error("SmartAPI Session is Not Initialized!")
  except Exception as e:
    logger.error(f"[ORDER EXCEPTION] Failed to place order: {e}")
  return None


# ==========================================
# 5. OPTION CHAIN & MULTI-TIMEFRAME HELPERS
# ==========================================
def validate_trade_with_option_chain(
    signal, current_price, pcr, resistance_strike, support_strike
):
  """Validates trade signals against Option Chain PCR and S/R levels"""
  buffer_points = 40

  if signal == "CE":
    if pcr < 0.6:
      logger.info(
          f"❌ TRADE REJECTED: CE Signal ignored due to Bearish PCR ({pcr:.2f})"
      )
      return False

    if (
        resistance_strike - current_price
    ) <= buffer_points and current_price < resistance_strike:
      logger.info(
          f"❌ TRADE REJECTED: Price ({current_price:.2f}) near Resistance"
          f" ({resistance_strike})"
      )
      return False

    logger.info(
        f"✅ OPTION CHAIN PASSED: CE Cleared (PCR: {pcr:.2f}, R:"
        f" {resistance_strike})"
    )
    return True

  elif signal == "PE":
    if pcr > 1.4:
      logger.info(
          f"❌ TRADE REJECTED: PE Signal ignored due to Bullish PCR ({pcr:.2f})"
      )
      return False

    if (
        current_price - support_strike
    ) <= buffer_points and current_price > support_strike:
      logger.info(
          f"❌ TRADE REJECTED: Price ({current_price:.2f}) near Support"
          f" ({support_strike})"
      )
      return False

    logger.info(
        f"✅ OPTION CHAIN PASSED: PE Cleared (PCR: {pcr:.2f}, S:"
        f" {support_strike})"
    )
    return True

  return False


def fetch_option_chain_data(spot_price):
  """Fetches Open Interest & Option Chain metrics near ATM strike"""
  try:
    if scrip_master_df is None or scrip_master_df.empty:
      logger.warning("Option Chain Error: Scrip master not loaded.")
      return None

    atm = round(spot_price / 50) * 50
    cols = {c.lower(): c for c in scrip_master_df.columns}

    filtered = scrip_master_df[
        (scrip_master_df[cols.get("name")].astype(str).str.upper() == "NIFTY")
        & (
            scrip_master_df[cols.get("instrumenttype")].astype(str).str.upper()
            == "OPTIDX"
        )
    ].copy()

    filtered["strike_val"] = (
        filtered[cols.get("strike")].astype(float) / 100.0
    )
    filtered["expiry_dt"] = pd.to_datetime(
        filtered[cols.get("expiry")].astype(str).str.upper(),
        format="%d%b%Y",
        errors="coerce",
    )

    today = pd.to_datetime(datetime.now().date())
    valid = filtered[filtered["expiry_dt"] >= today].sort_values("expiry_dt")

    if valid.empty:
      return None

    nearest_expiry = valid.iloc[0]["expiry_dt"]
    chain_df = valid[valid["expiry_dt"] == nearest_expiry]
    strike_range = chain_df[
        (chain_df["strike_val"] >= atm - 500)
        & (chain_df["strike_val"] <= atm + 500)
    ].copy()

    if strike_range.empty:
      return None

    tokens_list = strike_range[cols.get("token")].astype(str).tolist()
    oi_map = {}

    try:
      oi_data = smartApi.getMarketData("FULL", {"NFO": tokens_list[:50]})
      if (
          oi_data
          and oi_data.get("status")
          and "fetched" in oi_data.get("data", {})
      ):
        for item in oi_data["data"]["fetched"]:
          t_id = str(item.get("symbolToken", ""))
          oi_val = float(item.get("opnInterest", 1000))
          oi_map[t_id] = oi_val
    except Exception as ex:
      logger.warning(f"Option Chain Bulk Fetch Warning: {ex}")

    option_chain_list = []
    for _, row in strike_range.iterrows():
      sym = str(row[cols.get("symbol")])
      tok = str(row[cols.get("token")])
      stk = float(row["strike_val"])
      opt_type = "CE" if sym.endswith("CE") else "PE"
      oi = oi_map.get(tok, 1000.0)

      option_chain_list.append(
          {"strike": stk, "option_type": opt_type, "open_interest": oi}
      )

    return option_chain_list

  except Exception as e:
    logger.error(f"Option Chain Data Error: {e}")
    return None


def get_15m_trend():
  """Calculates 15-Minute Macro Trend using EMA 200"""
  to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
  from_date = (datetime.now() - pd.Timedelta(days=15)).strftime(
      "%Y-%m-%d %H:%M"
  )

  param = {
      "exchange": "NSE",
      "symboltoken": NIFTY_TOKEN,
      "interval": "FIFTEEN_MINUTE",
      "fromdate": from_date,
      "todate": to_date,
  }

  try:
    resp = smartApi.getCandleData(param)
    if resp and resp.get("status") and resp.get("data"):
      df = pd.DataFrame(
          resp["data"],
          columns=["timestamp", "open", "high", "low", "close", "volume"],
      )
      df["close"] = df["close"].astype(float)

      if len(df) >= 200:
        df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)
        curr = df.iloc[-1]
        return "BULLISH" if curr["close"] > curr["ema_200"] else "BEARISH"
      else:
        logger.warning("15m Trend Warning: Insufficient bars for EMA 200")
  except Exception as e:
    logger.error(f"15m Trend Fetch Error: {e}")

  return "NEUTRAL"


def get_india_vix():
  """Fetches live India VIX value"""
  try:
    vix_data = smartApi.ltpData("NSE", "INDIA VIX", INDIA_VIX_TOKEN)
    if vix_data and vix_data.get("status") and "data" in vix_data:
      val = float(vix_data["data"]["ltp"])
      if val > 100:
        val = val / 100.0
      return val
  except Exception:
    pass
  return 14.5  # Safe Neutral Default VIX


def is_market_open():
  now = datetime.now().time()
  return dtime(9, 15) <= now <= dtime(15, 15)


def is_new_entry_allowed():
  now = datetime.now().time()
  return dtime(9, 20) <= now <= dtime(14, 45)


def is_squareoff_time():
  return datetime.now().time() >= dtime(15, 10)


def get_itm_symbol_and_token(spot_price, option_type):
  """Fetches ITM (1 Strike Deep) Option Symbol and Token from Scrip Master"""
  atm = round(spot_price / 50) * 50
  target_strike = (atm - 50) if option_type == "CE" else (atm + 50)

  try:
    if scrip_master_df is None or scrip_master_df.empty:
      return None, None

    cols = {c.lower(): c for c in scrip_master_df.columns}
    name_col, inst_col = cols.get("name"), cols.get("instrumenttype")
    strike_col, sym_col = cols.get("strike"), cols.get("symbol")
    token_col, expiry_col = cols.get("token"), cols.get("expiry")

    filtered = scrip_master_df[
        (scrip_master_df[name_col].astype(str).str.upper() == "NIFTY")
        & (scrip_master_df[inst_col].astype(str).str.upper() == "OPTIDX")
        & (
            scrip_master_df[strike_col].astype(float)
            == float(target_strike * 100)
        )
        & (scrip_master_df[sym_col].astype(str).str.endswith(option_type))
    ].copy()

    if not filtered.empty:
      filtered["expiry_dt"] = pd.to_datetime(
          filtered[expiry_col].astype(str).str.upper(),
          format="%d%b%Y",
          errors="coerce",
      )
      today = pd.to_datetime(datetime.now().date())
      valid_expiries = filtered[filtered["expiry_dt"] >= today].sort_values(
          "expiry_dt"
      )

      if not valid_expiries.empty:
        selected_row = valid_expiries.iloc[0]
        return str(selected_row[sym_col]), str(selected_row[token_col])
  except Exception as e:
    logger.error(f"ITM Token Lookup Exception: {e}")

  return None, None


def fetch_signals_and_data():
  """Fetches Live Spot Price and Technical Signals"""
  try:
    spot_ltp = get_nifty_spot_ltp()
    # Indicator calculations (RSI / EMA Crossover) integrate here
    signal = None  # Returns "CE", "PE", or None
    return spot_ltp, signal
  except Exception as e:
    logger.error(f"fetch_signals_and_data Exception: {e}")
    return None, None


# ==========================================
# REFACTORED MAIN ENGINE EXECUTION LOOP
# ==========================================
logger.info(">>> Nifty Auto Bot Active (Auto Entry + Risk Rules + Trailing SL)...")
send_telegram_alert(
    "🤖 <b>Nifty Auto-Bot Activated!</b>\n"
    "Rule 1: 2 Losses -> Stop Trading.\n"
    "Rule 2: 3 Profits -> Stop Trading."
)

while True:
  try:
    if is_market_open():
      close, signal = fetch_signals_and_data()

      if close is not None:
        # 1. AUTO SQUARE-OFF AT 03:10 PM
        if is_squareoff_time() and pos_active:
          ltp = get_live_ltp(active_token, active_symbol) or entry_price
          logger.info(
              f"[AUTO SQUARE-OFF 03:10 PM] Exiting {active_symbol} at ₹{ltp:.2f}"
          )

          place_order(active_symbol, active_token, "SELL", active_quantity)
          log_trade(
              active_symbol,
              "BUY",
              entry_price,
              ltp,
              active_quantity,
              "AUTO_SQUARE_OFF",
          )
          send_telegram_alert(
              f"⏰ <b>AUTO SQUARE-OFF (03:10 PM)</b>\n"
              f"<b>Symbol:</b> {active_symbol}\n"
              f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
              f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
          )

          pos_active = False
          daily_trades_count += 1
          highest_price_seen = 0.0

        # 2. ACTIVE POSITION MONITOR
        elif pos_active:
          ltp = get_live_ltp(active_token, active_symbol) or entry_price
          holding_time_mins = (
              datetime.now() - trade_entry_time
          ).total_seconds() / 60.0

          if ENABLE_TRAILING_SL:
            if ltp > highest_price_seen:
              highest_price_seen = ltp

            gain_pct = (highest_price_seen - entry_price) / entry_price
            if gain_pct >= TSL_ACTIVATION_PCT:
              steps = int(
                  (gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT
              )
              new_sl = round(
                  entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2
              )
              if new_sl > sl_price:
                sl_price = new_sl
                logger.info(f"[TRAILING SL UPDATED] Raised SL to ₹{sl_price:.2f}")

          print(
              f"\r[POS ACTIVE] {active_symbol} | LTP: ₹{ltp:.2f} | SL:"
              f" ₹{sl_price:.2f} | TGT: ₹{tgt_price:.2f} | Time:"
              f" {holding_time_mins:.1f}m/{MAX_HOLDING_MINUTES}m",
              end="",
              flush=True,
          )

          # Exit Checks
          if ltp <= sl_price:
            reason = (
                "TRAILING_SL_HIT"
                if sl_price > (entry_price * (1 - SL_PCT))
                else "INITIAL_SL_HIT"
            )
            logger.info(f"[EXIT SL] {reason} for {active_symbol} at ₹{ltp:.2f}")

            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "BUY",
                entry_price,
                ltp,
                active_quantity,
                reason,
            )
            send_telegram_alert(
                f"🔴 <b>STOP LOSS HIT ({reason})</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b>"
                f" ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )

            pos_active = False
            daily_trades_count += 1
            consecutive_sl_count += 1
            consecutive_win_count = 0
            highest_price_seen = 0.0

          elif ltp >= tgt_price:
            logger.info(
                f"[EXIT TARGET] Target Hit for {active_symbol} at ₹{ltp:.2f}"
            )

            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "BUY",
                entry_price,
                ltp,
                active_quantity,
                "TARGET_ACHIEVED",
            )
            send_telegram_alert(
                f"🟢 <b>TARGET ACHIEVED</b> 🎉\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b>"
                f" ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )

            pos_active = False
            daily_trades_count += 1
            consecutive_win_count += 1
            consecutive_sl_count = 0
            highest_price_seen = 0.0

          elif holding_time_mins >= MAX_HOLDING_MINUTES:
            logger.info(
                f"[EXIT THETA TIMEOUT] Auto-Exiting {active_symbol} at"
                f" ₹{ltp:.2f}"
            )

            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "BUY",
                entry_price,
                ltp,
                active_quantity,
                "THETA_TIMEOUT",
            )
            send_telegram_alert(
                f"⏱️ <b>THETA TIMEOUT EXIT</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b>"
                f" ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )

            pos_active = False
            daily_trades_count += 1
            highest_price_seen = 0.0
            if ltp < entry_price:
              consecutive_sl_count += 1
              consecutive_win_count = 0

        # 3. AUTOMATIC TRADE ENTRY (With Risk Gates & Macro Filters)
        elif (
            not pos_active
            and signal in ["CE", "PE"]
            and not algo_paused
            and is_new_entry_allowed()
        ):
          vix = get_india_vix()
          macro_trend = get_15m_trend()

          # Filters Validation
          if not (MIN_VIX <= vix <= MAX_VIX):
            logger.info(f"Entry Blocked: VIX ({vix}) out of bounds")
          elif signal == "CE" and macro_trend == "BEARISH":
            logger.info(
                "Entry Blocked: 15m Trend is BEARISH, cannot take CE"
            )
          elif signal == "PE" and macro_trend == "BULLISH":
            logger.info(
                "Entry Blocked: 15m Trend is BULLISH, cannot take PE"
            )
          elif daily_trades_count >= MAX_DAILY_TRADES:
            print(
                f"\r🛑 Max Daily Limit ({MAX_DAILY_TRADES}) Reached.",
                end="",
                flush=True,
            )
          elif consecutive_sl_count >= 2:
            print(
                "\r🛑 2 Consecutive Losses Hit! Stopping entries.",
                end="",
                flush=True,
            )
          elif consecutive_win_count >= 3:
            print(
                "\r🎉 3 Consecutive Wins Hit! Stopping entries.",
                end="",
                flush=True,
            )
          else:
            sym, tok = get_itm_symbol_and_token(close, signal)
            if sym and tok:
              opt_ltp = get_live_ltp(tok, sym)
              if opt_ltp is None:
                opt_data = smartApi.ltpData("NFO", sym, tok)
                if (
                    opt_data
                    and opt_data.get("status")
                    and "data" in opt_data
                ):
                  opt_ltp = float(opt_data["data"]["ltp"])

              if opt_ltp and opt_ltp > 0:
                qty = calculate_dynamic_quantity(opt_ltp)
                order_id = place_order(sym, tok, "BUY", qty)

                if order_id:
                  active_symbol = sym
                  active_token = tok
                  entry_price = opt_ltp
                  sl_price = round(opt_ltp * (1 - SL_PCT), 2)
                  tgt_price = round(opt_ltp * (1 + TARGET_PCT), 2)
                  highest_price_seen = opt_ltp
                  active_quantity = qty
                  trade_entry_time = datetime.now()
                  pos_active = True

                  setup_and_subscribe_websocket(tok)
                  send_telegram_alert(
                      f"🚀 <b>NEW AUTO-TRADE ENTERED ({signal})</b>\n"
                      f"<b>Symbol:</b> {sym}\n"
                      f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                      f"<b>SL:</b> ₹{sl_price:.2f}\n"
                      f"<b>Target:</b> ₹{tgt_price:.2f}\n"
                      f"<b>Qty:</b> {qty}"
                  )

    else:
      print(
          "\r⏸️ Market Closed. Waiting for market session...",
          end="",
          flush=True,
      )

  except Exception as main_e:
    logger.error(f"Main Loop Exception: {main_e}")

  time.sleep(3)
