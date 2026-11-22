import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime
import pandas as pd
import pyotp
import pytz
import requests
import ta

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass
from SmartApi import SmartConnect

# ==========================================
# LOGGING CONFIGURATION
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NiftyAlgo")

# ==========================================
# 1. HYBRID CONFIG & PARAMETERS
# ==========================================
PAPER_TRADING = True
API_KEY = os.getenv("SMARTAPI_API_KEY") or os.getenv("SMARTAPI_KEY") or os.getenv("API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE") or os.getenv("CLIENT_CODE") or os.getenv("CLIENT_ID")
PIN = os.getenv("SMARTAPI_PIN") or os.getenv("PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET") or os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_TOTAL_CAPITAL = 100000.0
SL_PCT = 0.045  # 4.5% Stop Loss
TARGET_PCT = 0.25  # 25% Target Profit
MAX_RISK_PER_TRADE_PCT = 0.015
NIFTY_LOT_SIZE = 65  # Nifty Lot Size
ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04
TSL_STEP_TRIGGER_PCT = 0.02
TSL_STEP_MOVE_PCT = 0.015

MIN_VIX = 9.0
MAX_VIX = 26.0
ITM_STRIKE_OFFSET = 50
NIFTY_TOKEN = "99926000"
MAX_DAILY_TRADES = 4
MAX_HOLDING_MINUTES = 22
SCAN_INTERVAL_SECONDS = 30  # Increased scan gap for zero rate-limiting

# Global State Tracking
pos_active = False
algo_paused = False
active_symbol = ""
active_token = ""
entry_price = 0.0
sl_price = 0.0
tgt_price = 0.0
highest_price_seen = 0.0
tsl_activated = False
active_quantity = NIFTY_LOT_SIZE
trade_entry_time = None
daily_trades_count = 0
consecutive_sl_count = 0
consecutive_win_count = 0
scrip_master_df = None
auth_token = ""
feed_token = ""
smartApi = None
LOG_FILE = "trade_log.csv"

# Global rate limit tracker
last_api_call_timestamp = 0.0


# ==========================================
# 2. TIMEZONE & RATE LIMIT ENGINE
# ==========================================
def get_ist_now():
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def is_market_open():
    now_time = get_ist_now().time()
    return dtime(9, 15) <= now_time <= dtime(15, 15)


def is_new_entry_allowed():
    now_time = get_ist_now().time()
    return dtime(9, 20) <= now_time <= dtime(14, 45)


def is_squareoff_time():
    return get_ist_now().time() >= dtime(15, 10)


def rate_limit_cooldown(seconds=4.0):
    """Enforces strict minimum gap between consecutive API calls."""
    global last_api_call_timestamp
    elapsed = time.time() - last_api_call_timestamp
    if elapsed < seconds:
        time.sleep(seconds - elapsed)
    last_api_call_timestamp = time.time()


# ==========================================
# 3. TELEGRAM NOTIFICATION & LOGGING ENGINE
# ==========================================
def send_telegram_alert(message, max_retries=3, allow_after_hours=False):
    current_time = get_ist_now().time()
    if not allow_after_hours and current_time > dtime(15, 15):
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    prefix = "📄 [PAPER TRADE] " if PAPER_TRADING else "⚡ [REAL TRADE] "
    full_message = f"{prefix}{message}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": full_message,
        "parse_mode": "HTML",
    }
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=payload, timeout=5)
            if response.status_code == 200:
                return
        except Exception:
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = round(((exit_p - entry_p) / entry_p) * 100, 2) if entry_p > 0 else 0.0
    log_data = {
        "Timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        "Mode": "PAPER" if PAPER_TRADING else "LIVE",
        "Symbol": symbol,
        "Type": trade_type,
        "Entry_Price": entry_p,
        "Exit_Price": exit_p,
        "Quantity": qty,
        "PnL_INR": pnl,
        "PnL_PCT": pnl_pct,
        "Exit_Reason": reason,
    }
    df_log = pd.DataFrame([log_data])
    file_exists = os.path.isfile(LOG_FILE)
    df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
    logger.info(f"[{'PAPER' if PAPER_TRADING else 'LIVE'} LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Exit: {reason}")


# ==========================================
# 4. SMARTAPI AUTHENTICATION & EXECUTION ENGINE
# ==========================================
def initialize_smartapi():
    global smartApi, auth_token, feed_token, scrip_master_df
    try:
        if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
            raise Exception("SmartAPI Credentials missing from Environment Variables.")
        logger.info("Generating TOTP & Authenticating SmartAPI...")
        totp = pyotp.TOTP(TOTP_SECRET).now()
        smartApi = SmartConnect(api_key=API_KEY)
        data = smartApi.generateSession(CLIENT_CODE, PIN, totp)
        if not data or not data.get("status"):
            raise Exception("SmartAPI Login Failed.")
        auth_token = data["data"]["jwtToken"]
        feed_token = smartApi.getfeedToken()
        logger.info("SmartAPI Authentication Successful!")

        urls = [
            "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        ]
        headers = {"User-Agent": "Mozilla/5.0"}
        for scrip_url in urls:
            try:
                res = requests.get(scrip_url, headers=headers, timeout=25)
                if res.status_code == 200:
                    scrip_master_df = pd.DataFrame(res.json())
                    if "token" in scrip_master_df.columns:
                        scrip_master_df["token"] = scrip_master_df["token"].astype(str)
                    if "symbol" in scrip_master_df.columns:
                        scrip_master_df["symbol"] = scrip_master_df["symbol"].astype(str)
                    logger.info(f"Scrip Master Loaded! Total Records: {len(scrip_master_df)}")
                    break
            except Exception:
                continue
    except Exception as e:
        logger.critical(f"Startup Exception: {e}")
        sys.exit(1)


def place_order(symbol, token, buy_sell_type, quantity, exchange="NFO"):
    try:
        if PAPER_TRADING:
            logger.info(f"[VIRTUAL ORDER] Executed {buy_sell_type} for {symbol} | Qty: {quantity}")
            return "VIRTUAL_ORDER_123"

        rate_limit_cooldown(2.0)
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "transactiontype": str(buy_sell_type).upper(),
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": "CARRYFORWARD",
            "duration": "DAY",
            "price": "0",
            "quantity": str(quantity),
        }
        order_id = "VIRTUAL_ORDER"
        if smartApi is not None:
            response = smartApi.placeOrder(order_params)
            if response and response.get("status") and "data" in response:
                order_id = response["data"]["orderid"]
            else:
                logger.warning("Angel One Order Attempted but rejected/failed. Continuing virtual tracking.")
                order_id = "REJECTED_ORDER_BOOK"
        send_telegram_alert(
            f"<b>ORDER SENT TO BROKER</b>\n"
            f"<b>Symbol:</b> {symbol}\n"
            f"<b>Type:</b> {buy_sell_type}\n"
            f"<b>Qty:</b> {quantity}\n"
            f"<b>Order ID/Status:</b> {order_id}"
        )
        return order_id
    except Exception as e:
        logger.error(f"[ORDER EXCEPTION]: {e}")
        if PAPER_TRADING:
            return "VIRTUAL_ORDER_EXCEPTION"
    return None


def calculate_dynamic_quantity(option_price):
    try:
        if option_price <= 0:
            return NIFTY_LOT_SIZE
        rate_limit_cooldown(2.0)
        rms_data = smartApi.rmsLimit()
        net_capital = DEFAULT_TOTAL_CAPITAL
        if rms_data and rms_data.get("status") and "data" in rms_data:
            data_dict = rms_data["data"]
            net_capital = float(data_dict.get("net", data_dict.get("availablecash", DEFAULT_TOTAL_CAPITAL)))
        if net_capital <= 0:
            net_capital = DEFAULT_TOTAL_CAPITAL
        max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
        risk_per_share = option_price * SL_PCT
        if risk_per_share <= 0:
            return NIFTY_LOT_SIZE

        calculated_qty = max_risk_amount / risk_per_share
        lots = max(1, int(calculated_qty // NIFTY_LOT_SIZE))
        total_qty = lots * NIFTY_LOT_SIZE
        if (total_qty * option_price) > net_capital:
            max_affordable_lots = int(net_capital // (NIFTY_LOT_SIZE * option_price))
            lots = max(1, max_affordable_lots)
            total_qty = lots * NIFTY_LOT_SIZE
        return total_qty
    except Exception as e:
        logger.error(f"Dynamic Sizing Error: {e}")
        return NIFTY_LOT_SIZE
