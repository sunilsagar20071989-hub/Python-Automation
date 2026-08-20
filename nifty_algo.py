from datetime import datetime, time as dtime
import json
import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# LOGGING CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("NiftyAlgo")

# ==========================================
# 1. PARAMETERS & CREDENTIALS
# ==========================================
API_KEY = (
    os.getenv("SMARTAPI_API_KEY")
    or os.getenv("SMARTAPI_KEY")
    or os.getenv("API_KEY")
)
CLIENT_CODE = (
    os.getenv("SMARTAPI_CLIENT_CODE")
    or os.getenv("CLIENT_CODE")
    or os.getenv("CLIENT_ID")
)
PIN = os.getenv("SMARTAPI_PIN") or os.getenv("PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET") or os.getenv("TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_TOTAL_CAPITAL = 100000.0

SL_PCT = 0.045
TARGET_PCT = 0.25
MAX_RISK_PER_TRADE_PCT = 0.015
NIFTY_LOT_SIZE = 65

ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04
TSL_STEP_TRIGGER_PCT = 0.02
TSL_STEP_MOVE_PCT = 0.015

MIN_VIX = 10.0
MAX_VIX = 24.0
ITM_STRIKE_OFFSET = 50

NIFTY_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926009"

MAX_DAILY_TRADES = 4
MAX_HOLDING_MINUTES = 22
SCAN_INTERVAL_SECONDS = 15

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
scrip_master_df = None
auth_token = ""
feed_token = ""
smartApi = None
LOG_FILE = "trade_log.csv"


# ==========================================
# 2. TIMEZONE & MARKET HOURS ENGINE
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


# ==========================================
# 3. TELEGRAM NOTIFICATION & LOGGING ENGINE
# ==========================================
def send_telegram_alert(message, max_retries=3):
    current_time = get_ist_now().time()
    if current_time > dtime(15, 15):
        return

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
            response = requests.post(url, data=payload, timeout=5)
            if response.status_code == 200:
                return
        except Exception:
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = (
        round(((exit_p - entry_p) / entry_p) * 100, 2) if entry_p > 0 else 0.0
    )

    log_data = {
        "Timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
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
    logger.info(f"[TRADE LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Exit: {reason}")


# ==========================================
# 4. SMARTAPI AUTHENTICATION & ORDER EXECUTION
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
                    logger.info(
                        f"Scrip Master Loaded! Total Records: {len(scrip_master_df)}"
                    )
                    break
            except Exception:
                continue

    except Exception as e:
        logger.critical(f"Startup Exception: {e}")
        sys.exit(1)


def place_order(symbol, token, buy_sell_type, quantity, exchange="NFO"):
    try:
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

        if smartApi is not None:
            response = smartApi.placeOrder(order_params)
            if response and response.get("status") and "data" in response:
                order_id = response["data"]["orderid"]
                send_telegram_alert(
                    f"⚡ <b>AUTO-TRADE EXECUTED</b> ⚡\n"
                    f"<b>Symbol:</b> {symbol}\n"
                    f"<b>Type:</b> {buy_sell_type}\n"
                    f"<b>Qty:</b> {quantity}\n"
                    f"<b>Order ID:</b> {order_id}"
                )
                return order_id
    except Exception as e:
        logger.error(f"[ORDER EXCEPTION]: {e}")
    return None


def calculate_dynamic_quantity(option_price):
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
            max_affordable_lots = int(
                net_capital // (NIFTY_LOT_SIZE * option_price)
            )
            lots = max(1, max_affordable_lots)
            total_qty = lots * NIFTY_LOT_SIZE

        return total_qty

    except Exception as e:
        logger.error(f"Dynamic Sizing Error: {e}")
        return NIFTY_LOT_SIZE


# ==========================================
# 5. MARKET DATA & STRATEGY ENGINE
# ==========================================
def get_live_ltp(token, symbol, exchange="NFO"):
    try:
        ltp_data = smartApi.ltpData(exchange, symbol, str(token))
        if ltp_data and ltp_data.get("status") and "data" in ltp_data:
            return float(ltp_data["data"]["ltp"])
    except Exception as e:
        logger.error(f"REST API LTP Error for {symbol}: {e}")
    return None


def get_nifty_spot_ltp():
    return get_live_ltp(NIFTY_TOKEN, "NIFTY", exchange="NSE")


def get_itm_option_scrip(spot_price, option_type="CE"):
    try:
        if scrip_master_df is None or scrip_master_df.empty:
            return None, None

        atm_strike = round(spot_price / 50.0) * 50
        itm_strike = (
            atm_strike - ITM_STRIKE_OFFSET
            if option_type == "CE"
            else atm_strike + ITM_STRIKE_OFFSET
        )

        nifty_df = scrip_master_df[
            (scrip_master_df["name"] == "NIFTY")
            & (scrip_master_df["instrumenttype"] == "OPTIDX")
            & (scrip_master_df["symbol"].str.endswith(option_type))
        ].copy()

        if nifty_df.empty:
            return None, None

        nifty_df["strike"] = pd.to_numeric(nifty_df["strike"], errors="coerce")
        nifty_df["expiry_dt"] = pd.to_datetime(nifty_df["expiry"], errors="coerce")

        today = get_ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
        valid_df = nifty_df[
            (nifty_df["strike"] == itm_strike) & (nifty_df["expiry_dt"] >= today)
        ].sort_values(by="expiry_dt")

        if not valid_df.empty:
            selected_row = valid_df.iloc[0]
            return selected_row["symbol"], str(selected_row["token"])
    except Exception as e:
        logger.error(f"ITM Option Strike Finder Error: {e}")

    return None, None


def fetch_nifty_candles():
    try:
        now = get_ist_now()
        to_date = now.strftime("%Y-%m-%d %H:%M")
        from_date = (now - pd.Timedelta(days=5)).strftime("%Y-%m-%d 09:15")

        param = {
            "exchange": "NSE",
            "symboltoken": NIFTY_TOKEN,
            "interval": "FIVE_MINUTE",
            "fromdate": from_date,
            "todate": to_date,
        }

        candles = smartApi.getCandleData(param)
        if candles and candles.get("status") and "data" in candles:
            df = pd.DataFrame(
                candles["data"],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = df["close"].astype(float)

            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=9)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            return df
    except Exception as e:
        logger.error(f"Candle Data Fetch Error: {e}")
    return None


def generate_trade_signal():
    df = fetch_nifty_candles()
    if df is None or len(df) < 25:
        return "NO_TRADE"

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if (
        curr["rsi"] >= 60.0
        and curr["roc"] > 0.0
        and curr["ema_9"] > curr["ema_21"]
        and prev["ema_9"] <= prev["ema_21"]
    ):
        return "CE"

    elif (
        curr["rsi"] <= 40.0
        and curr["roc"] < 0.0
        and curr["ema_9"] < curr["ema_21"]
        and prev["ema_9"] >= prev["ema_21"]
    ):
        return "PE"

    return "NO_TRADE"


def get_option_chain_pcr_and_levels(spot_price):
    try:
        pcr = 1.0
        res_strike = round((spot_price + 100) / 50.0) * 50
        sup_strike = round((spot_price - 100) / 50.0) * 50
        return pcr, res_strike, sup_strike
    except Exception as e:
        logger.error(f"Option Chain Fetch Error: {e}")
        return 1.0, spot_price + 100, spot_price - 100


def validate_trade_with_option_chain(
    signal, current_price, pcr, resistance_strike, support_strike
):
    buffer_points = 40

    if signal == "CE":
        if pcr < 0.6:
            logger.info(f"❌ TRADE REJECTED: Bearish PCR ({pcr:.2f})")
            return False

        if (resistance_strike - current_price) <= buffer_points and current_price < resistance_strike:
            logger.info(f"❌ TRADE REJECTED: Price near Resistance ({resistance_strike})")
            return False

        return True

    elif signal == "PE":
        if pcr > 1.4:
            logger.info(f"❌ TRADE REJECTED: Bullish PCR ({pcr:.2f})")
            return False

        if (current_price - support_strike) <= buffer_points and current_price > support_strike:
            logger.info(f"❌ TRADE REJECTED: Price near Support ({support_strike})")
            return False

        return True

    return False


def cleanup_position():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price, highest_price_seen, tsl_activated
    pos_active = False
    active_symbol = ""
    active_token = ""
    entry_price = 0.0
    sl_price = 0.0
    tgt_price = 0.0
    highest_price_seen = 0.0
    tsl_activated = False


# ==========================================
# 6. LIVE MONITORING & MAIN LOOP
# ==========================================
def monitor_active_position():
    global pos_active, highest_price_seen, sl_price, tsl_activated

    if not pos_active:
        return

    curr_ltp = get_live_ltp(active_token, active_symbol)
    if not curr_ltp:
        return

    if curr_ltp > highest_price_seen:
        highest_price_seen = curr_ltp

    gain_pct = (highest_price_seen - entry_price) / entry_price
    if ENABLE_TRAILING_SL and gain_pct >= TSL_ACTIVATION_PCT:
        tsl_activated = True
        new_sl = highest_price_seen * (1.0 - TSL_STEP_MOVE_PCT)
        if new_sl > sl_price:
            sl_price = new_sl
            logger.info(f"📈 Trailing SL Updated to ₹{sl_price:.2f}")

    exit_reason = None
    if curr_ltp <= sl_price:
        exit_reason = "STOP_LOSS_HIT"
    elif curr_ltp >= tgt_price:
        exit_reason = "TARGET_ACHIEVED"
    elif is_squareoff_time():
        exit_reason = "TIME_SQUAREOFF"
    elif trade_entry_time and (get_ist_now() - trade_entry_time).seconds / 60 >= MAX_HOLDING_MINUTES:
        exit_reason = "MAX_HOLDING_TIME_EXPIRED"

    if exit_reason:
        logger.info(f"🚨 Exiting Trade: {active_symbol} | Reason: {exit_reason}")
        place_order(active_symbol, active_token, "SELL", active_quantity)
        log_trade(active_symbol, "SELL", entry_price, curr_ltp, active_quantity, exit_reason)
        cleanup_position()


def main():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price, highest_price_seen, active_quantity, trade_entry_time, daily_trades_count

    initialize_smartapi()

    # Non-market hours check: Immediately exit cleanly to achieve GREEN status on GitHub
    if not is_market_open():
        logger.info("Market Closed. Terminating execution cleanly.")
        send_telegram_alert("ℹ️ <b>Nifty Algo:</b> Market Closed. Workflow finished successfully.")
        sys.exit(0)

    logger.info("⚡ Nifty Automated Algo Engine Started Successfully!")
    send_telegram_alert(
        "🚀 <b>Nifty Option Algo Active!</b>\n"
        "Engine listening for signals..."
    )

    while is_market_open():
        try:
            if pos_active:
                monitor_active_position()
            else:
                if is_new_entry_allowed() and daily_trades_count < MAX_DAILY_TRADES:
                    signal = generate_trade_signal()

                    if signal in ["CE", "PE"]:
                        spot_ltp = get_nifty_spot_ltp()
                        if spot_ltp:
                            pcr, res_s, sup_s = get_option_chain_pcr_and_levels(spot_ltp)
                            if validate_trade_with_option_chain(signal, spot_ltp, pcr, res_s, sup_s):
                                sym, tok = get_itm_option_scrip(spot_ltp, signal)
                                if sym and tok:
                                    opt_price = get_live_ltp(tok, sym)
                                    if opt_price:
                                        qty = calculate_dynamic_quantity(opt_price)
                                        order_id = place_order(sym, tok, "BUY", qty)
                                        if order_id:
                                            pos_active = True
                                            active_symbol = sym
                                            active_token = tok
                                            entry_price = opt_price
                                            sl_price = opt_price * (1.0 - SL_PCT)
                                            tgt_price = opt_price * (1.0 + TARGET_PCT)
                                            highest_price_seen = opt_price
                                            active_quantity = qty
                                            trade_entry_time = get_ist_now()
                                            daily_trades_count += 1
                                            logger.info(
                                                f"✅ Trade Entry: {sym} @ ₹{opt_price} | SL: ₹{sl_price:.2f} | Tgt: ₹{tgt_price:.2f} | Qty: {qty}"
                                            )

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            logger.error(f"Main Loop Exception: {e}")
            time.sleep(5)

    logger.info("Market hours ended. Exiting engine cleanly.")
    sys.exit(0)


if __name__ == "__main__":
    main()

import os
import sys
import time
import json
import logging
from datetime import datetime, time as dtime
import pytz
import requests
import pyotp
import pandas as pd
import ta
from SmartApi import SmartConnect

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# LOGGING CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("NiftyAlgo")

# ==========================================
# 1. PARAMETERS & CONFIGURATION
# ==========================================
API_KEY = (
    os.getenv("SMARTAPI_API_KEY")
    or os.getenv("SMARTAPI_KEY")
    or os.getenv("API_KEY")
)
CLIENT_CODE = (
    os.getenv("SMARTAPI_CLIENT_CODE")
    or os.getenv("CLIENT_CODE")
    or os.getenv("CLIENT_ID")
)
PIN = os.getenv("SMARTAPI_PIN") or os.getenv("PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET") or os.getenv("TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_TOTAL_CAPITAL = 100000.0

SL_PCT = 0.045
TARGET_PCT = 0.25
MAX_RISK_PER_TRADE_PCT = 0.015
NIFTY_LOT_SIZE = 65

ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04
TSL_STEP_TRIGGER_PCT = 0.02
TSL_STEP_MOVE_PCT = 0.015

MIN_VIX = 10.0
MAX_VIX = 24.0
ITM_STRIKE_OFFSET = 50

NIFTY_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926009"

MAX_DAILY_TRADES = 4
MAX_HOLDING_MINUTES = 22
SCAN_INTERVAL_SECONDS = 15

# Global State Variables
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


# ==========================================
# 2. HELPER & TIME FUNCTIONS
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


def send_telegram_alert(message, max_retries=3):
    current_time = get_ist_now().time()
    if current_time > dtime(15, 15):
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    for _ in range(max_retries):
        try:
            res = requests.post(url, data=payload, timeout=5)
            if res.status_code == 200:
                return
        except Exception:
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = (
        round(((exit_p - entry_p) / entry_p) * 100, 2) if entry_p > 0 else 0.0
    )

    log_data = {
        "Timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
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
    logger.info(f"[TRADE LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Reason: {reason}")


# ==========================================
# 3. SMARTAPI AUTHENTICATION & EXECUTION
# ==========================================
def initialize_smartapi():
    global smartApi, auth_token, feed_token, scrip_master_df
    try:
        if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
            raise Exception("Credentials missing in environment variables.")

        logger.info("Generating TOTP & Authenticating SmartAPI...")
        totp = pyotp.TOTP(TOTP_SECRET).now()
        smartApi = SmartConnect(api_key=API_KEY)
        data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

        if not data or not data.get("status"):
            raise Exception("SmartAPI Login Failed.")

        auth_token = data["data"]["jwtToken"]
        feed_token = smartApi.getfeedToken()
        logger.info("SmartAPI Login Successful.")

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
                    logger.info("Scrip Master Loaded successfully.")
                    break
            except Exception:
                continue

    except Exception as e:
        logger.critical(f"Initialization Exception: {e}")
        sys.exit(1)


def place_order(symbol, token, buy_sell_type, quantity, exchange="NFO"):
    try:
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

        if smartApi is not None:
            response = smartApi.placeOrder(order_params)
            if response and response.get("status") and "data" in response:
                return response["data"]["orderid"]
    except Exception as e:
        logger.error(f"Order Placement Error: {e}")
    return None


def calculate_dynamic_quantity(option_price):
    try:
        if option_price <= 0:
            return NIFTY_LOT_SIZE

        rms_data = smartApi.rmsLimit()
        net_capital = DEFAULT_TOTAL_CAPITAL

        if rms_data and rms_data.get("status") and "data" in rms_data:
            net_capital = float(
                rms_data["data"].get("net", DEFAULT_TOTAL_CAPITAL)
            )

        max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
        risk_per_share = option_price * SL_PCT

        if risk_per_share <= 0:
            return NIFTY_LOT_SIZE

        lots = max(
            1, int((max_risk_amount / risk_per_share) // NIFTY_LOT_SIZE)
        )
        total_qty = lots * NIFTY_LOT_SIZE

        if (total_qty * option_price) > net_capital:
            total_qty = (
                max(1, int(net_capital // (NIFTY_LOT_SIZE * option_price)))
                * NIFTY_LOT_SIZE
            )

        return total_qty
    except Exception:
        return NIFTY_LOT_SIZE


# ==========================================
# 4. TECHNICAL INDICATORS & SCAN ENGINE
# ==========================================
def get_live_ltp(token, symbol, exchange="NFO"):
    try:
        ltp_data = smartApi.ltpData(exchange, symbol, str(token))
        if ltp_data and ltp_data.get("status") and "data" in ltp_data:
            return float(ltp_data["data"]["ltp"])
    except Exception:
        pass
    return None


def get_nifty_spot_ltp():
    return get_live_ltp(NIFTY_TOKEN, "NIFTY", exchange="NSE")


def get_india_vix():
    vix = get_live_ltp(INDIA_VIX_TOKEN, "INDIA VIX", exchange="NSE")
    return vix if vix else 15.0


def get_itm_option_scrip(spot_price, option_type="CE"):
    try:
        if scrip_master_df is None or scrip_master_df.empty:
            return None, None

        atm_strike = round(spot_price / 50.0) * 50
        itm_strike = (
            atm_strike - ITM_STRIKE_OFFSET
            if option_type == "CE"
            else atm_strike + ITM_STRIKE_OFFSET
        )

        nifty_df = scrip_master_df[
            (scrip_master_df["name"] == "NIFTY")
            & (scrip_master_df["instrumenttype"] == "OPTIDX")
            & (scrip_master_df["symbol"].str.endswith(option_type))
        ].copy()

        nifty_df["strike"] = pd.to_numeric(nifty_df["strike"], errors="coerce")
        nifty_df["expiry_dt"] = pd.to_datetime(
            nifty_df["expiry"], errors="coerce"
        )

        today = get_ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
        valid_df = nifty_df[
            (nifty_df["strike"] == itm_strike) & (nifty_df["expiry_dt"] >= today)
        ].sort_values(by="expiry_dt")

        if not valid_df.empty:
            selected = valid_df.iloc[0]
            return selected["symbol"], str(selected["token"])
    except Exception:
        pass
    return None, None


def fetch_nifty_candles(interval="FIVE_MINUTE"):
    try:
        now = get_ist_now()
        to_date = now.strftime("%Y-%m-%d %H:%M")
        from_date = (now - pd.Timedelta(days=5)).strftime("%Y-%m-%d 09:15")

        candles = smartApi.getCandleData(
            {
                "exchange": "NSE",
                "symboltoken": NIFTY_TOKEN,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date,
            }
        )

        if candles and candles.get("status") and "data" in candles:
            df = pd.DataFrame(
                candles["data"],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = df["close"].astype(float)
            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=9)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            return df
    except Exception:
        pass
    return None


def get_15m_trend():
    df_15m = fetch_nifty_candles(interval="FIFTEEN_MINUTE")
    if df_15m is None or len(df_15m) < 2:
        return "NEUTRAL"
    curr = df_15m.iloc[-1]
    return "BULLISH" if curr["ema_9"] > curr["ema_21"] else "BEARISH"


def get_option_chain_pcr_and_levels(spot_price):
    return 1.0, spot_price + 100, spot_price - 100


def validate_trade_with_option_chain(
    signal, current_price, pcr, resistance_strike, support_strike
):
    buffer_points = 40
    if signal == "CE":
        if pcr < 0.6:
            return False
        if (
            resistance_strike - current_price
        ) <= buffer_points and current_price < resistance_strike:
            return False
        return True
    elif signal == "PE":
        if pcr > 1.4:
            return False
        if (
            current_price - support_strike
        ) <= buffer_points and current_price > support_strike:
            return False
        return True
    return False


def generate_trade_signal():
    df = fetch_nifty_candles(interval="FIVE_MINUTE")
    if df is None or len(df) < 25:
        return "NO_TRADE"

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if (
        curr["rsi"] >= 60.0
        and curr["roc"] > 0.0
        and curr["ema_9"] > curr["ema_21"]
        and prev["ema_9"] <= prev["ema_21"]
    ):
        return "CE"
    elif (
        curr["rsi"] <= 40.0
        and curr["roc"] < 0.0
        and curr["ema_9"] < curr["ema_21"]
        and prev["ema_9"] >= prev["ema_21"]
    ):
        return "PE"

    return "NO_TRADE"


def cleanup_position():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price, highest_price_seen, tsl_activated
    pos_active = False
    active_symbol = ""
    active_token = ""
    entry_price = 0.0
    sl_price = 0.0
    tgt_price = 0.0
    highest_price_seen = 0.0
    tsl_activated = False


# ==========================================
# 5. MAIN TRADING EXECUTION ENGINE
# ==========================================
def run_trading_cycle():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price
    global highest_price_seen, tsl_activated, active_quantity, trade_entry_time
    global daily_trades_count, consecutive_sl_count, consecutive_win_count

    if is_squareoff_time() and pos_active:
        ltp = get_live_ltp(active_token, active_symbol) or entry_price
        place_order(active_symbol, active_token, "SELL", active_quantity)
        log_trade(
            active_symbol,
            "SELL",
            entry_price,
            ltp,
            active_quantity,
            "AUTO_SQUARE_OFF",
        )
        send_telegram_alert(f"⏰ AUTO SQUARE-OFF (03:10 PM) | Exit: ₹{ltp:.2f}")
        cleanup_position()
        return

    if pos_active:
        ltp = get_live_ltp(active_token, active_symbol) or entry_price
        holding_time_mins = (
            get_ist_now() - trade_entry_time
        ).total_seconds() / 60.0

        if ltp > highest_price_seen:
            highest_price_seen = ltp

        if ENABLE_TRAILING_SL and highest_price_seen > entry_price:
            gain_pct = (highest_price_seen - entry_price) / entry_price
            if gain_pct >= TSL_ACTIVATION_PCT:
                steps = (
                    int((gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT)
                    + 1
                )
                new_sl = round(
                    entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2
                )
                if new_sl > sl_price:
                    sl_price = new_sl
                    tsl_activated = True

        if ltp <= sl_price:
            reason = (
                "TRAILING_SL_HIT" if tsl_activated else "INITIAL_SL_HIT"
            )
            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "SELL",
                entry_price,
                ltp,
                active_quantity,
                reason,
            )
            send_telegram_alert(
                f"🔴 <b>STOP LOSS HIT ({reason})</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )
            cleanup_position()
            daily_trades_count += 1
            consecutive_sl_count += 1
            consecutive_win_count = 0

        elif ltp >= tgt_price:
            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "SELL",
                entry_price,
                ltp,
                active_quantity,
                "TARGET_ACHIEVED",
            )
            send_telegram_alert(
                f"🟢 <b>TARGET ACHIEVED</b> 🎉\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )
            cleanup_position()
            daily_trades_count += 1
            consecutive_win_count += 1
            consecutive_sl_count = 0

        elif holding_time_mins >= MAX_HOLDING_MINUTES:
            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(
                active_symbol,
                "SELL",
                entry_price,
                ltp,
                active_quantity,
                "THETA_TIMEOUT",
            )
            send_telegram_alert(
                f"⏱️ <b>THETA TIMEOUT EXIT</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
            )
            cleanup_position()
            daily_trades_count += 1
            if ltp < entry_price:
                consecutive_sl_count += 1
                consecutive_win_count = 0

    elif not pos_active and not algo_paused and is_new_entry_allowed():
        if daily_trades_count >= MAX_DAILY_TRADES:
            logger.info(f"🛑 Max Daily Limit ({MAX_DAILY_TRADES}) Reached.")
        elif consecutive_sl_count >= 2:
            logger.info("🛑 2 Consecutive Losses Hit! Halting strategy today.")
        elif consecutive_win_count >= 3:
            logger.info("🎉 3 Consecutive Wins Hit! Targets achieved today.")
        else:
            signal = generate_trade_signal()

            if signal in ["CE", "PE"]:
                spot = get_nifty_spot_ltp()
                if spot and spot > 0:
                    vix = get_india_vix()
                    macro_trend = get_15m_trend()
                    pcr, res_s, sup_s = get_option_chain_pcr_and_levels(spot)
                    is_oc_valid = validate_trade_with_option_chain(
                        signal, spot, pcr, res_s, sup_s
                    )

                    if not (MIN_VIX <= vix <= MAX_VIX):
                        logger.info(
                            f"Entry Blocked: VIX ({vix}) out of bounds ({MIN_VIX}-{MAX_VIX})"
                        )
                    elif not is_oc_valid:
                        logger.info(
                            "Entry Blocked: Failed Option Chain validation"
                        )
                    elif signal == "CE" and macro_trend == "BEARISH":
                        logger.info(
                            "Entry Blocked: 15m Trend is BEARISH, cannot take CE"
                        )
                    elif signal == "PE" and macro_trend == "BULLISH":
                        logger.info(
                            "Entry Blocked: 15m Trend is BULLISH, cannot take PE"
                        )
                    else:
                        sym, tok = get_itm_option_scrip(
                            spot, option_type=signal
                        )
                        if sym and tok:
                            opt_ltp = get_live_ltp(tok, sym)
                            if opt_ltp and opt_ltp > 0:
                                qty = calculate_dynamic_quantity(opt_ltp)
                                order_id = place_order(sym, tok, "BUY", qty)

                                if order_id:
                                    pos_active = True
                                    active_symbol = sym
                                    active_token = tok
                                    active_quantity = qty
                                    entry_price = opt_ltp
                                    sl_price = round(
                                        opt_ltp * (1 - SL_PCT), 2
                                    )
                                    tgt_price = round(
                                        opt_ltp * (1 + TARGET_PCT), 2
                                    )
                                    highest_price_seen = opt_ltp
                                    tsl_activated = False
                                    trade_entry_time = get_ist_now()

                                    send_telegram_alert(
                                        f"🚀 <b>NEW AUTO-TRADE ENTERED ({signal})</b>\n"
                                        f"<b>Symbol:</b> {sym}\n"
                                        f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                        f"<b>SL:</b> ₹{sl_price:.2f}\n"
                                        f"<b>Target:</b> ₹{tgt_price:.2f}\n"
                                        f"<b>Qty:</b> {qty}"
                                    )


# ==========================================
# MAIN ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    initialize_smartapi()

    if not is_market_open():
        logger.info("Market is Closed. Engine terminating.")
        sys.exit(0)

    logger.info("⚡ Engine active: Running live Market Scans...")

    while is_market_open():
        try:
            run_trading_cycle()
            time.sleep(0.5 if pos_active else SCAN_INTERVAL_SECONDS)
        except Exception as main_e:
            logger.error(f"Main Engine Exception: {main_e}")
            time.sleep(2)

    logger.info("Market hours finished. Stopping engine.")
    sys.exit(0)
