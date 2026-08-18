from datetime import datetime, time as dtime
import json
import logging
import os
import sys
import threading
import time

# Safe Import for python-dotenv (Fixes ModuleNotFoundError on GitHub Actions)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # In GitHub Actions, secrets/env variables are provided directly by runner environment

import pandas as pd
import pyotp
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
# 1. SECURE CREDENTIALS & RISK PARAMETERS
# ==========================================
API_KEY = os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_TOTAL_CAPITAL = 100000.0

# Risk Parameters
SL_PCT = 0.045  # 4.5% SL
TARGET_PCT = 0.1575  # 15.75% Target (1:3.5 Risk-Reward)
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade
NIFTY_LOT_SIZE = 65  # NSE Nifty Lot Size

# Trailing Stop-Loss Parameters
ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04  # Activates after +4% gain
TSL_STEP_TRIGGER_PCT = 0.02  # Trail SL every +2% move
TSL_STEP_MOVE_PCT = 0.015  # Move SL up by +1.5%

NIFTY_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926009"  # Default token for India VIX

MAX_DAILY_TRADES = 4
MAX_HOLDING_MINUTES = 22

# Global Position State Tracking
pos_active = False
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
algo_paused = False

scrip_master_df = None
auth_token = ""
feed_token = ""

smartApi = None
LOG_FILE = "trade_log.csv"


# ==========================================
# 2. TELEGRAM NOTIFICATION & LOGGING ENGINE
# ==========================================
def send_telegram_alert(message, max_retries=3):
    """Sends HTML formatted Telegram notifications safely"""
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
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Telegram Alert Error: {e}")
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    """Logs trade performance metrics into CSV file"""
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = (
        round(((exit_p - entry_p) / entry_p) * 100, 2) if entry_p > 0 else 0.0
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
        "Exit_Reason": reason,
    }

    df_log = pd.DataFrame([log_data])
    file_exists = os.path.isfile(LOG_FILE)
    df_log.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False)
    logger.info(f"[TRADE LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Exit: {reason}")


# ==========================================
# 3. ORDER EXECUTION ENGINE
# ==========================================
def place_order(symbol, token, buy_sell_type, quantity, exchange="NFO"):
    """Executes orders on SmartAPI safely with error handling"""
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
        logger.info(
            f"[ORDER ATTEMPT] {buy_sell_type} {quantity} Qty of {symbol} (Token: {token})"
        )

        if "smartApi" in globals() and smartApi is not None:
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
# 4. SMARTAPI INITIALIZATION & LOGIN
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
                    logger.info(f"Scrip Master Loaded! Total Records: {len(scrip_master_df)}")
                    break
            except Exception as e:
                logger.warning(f"Failed loading Scrip Master from {scrip_url}: {e}")

        if not scrip_loaded or scrip_master_df is None or scrip_master_df.empty:
            raise Exception("Scrip Master Download Failed from all mirrors.")

    except Exception as e:
        logger.critical(f"Startup Exception: {e}")
        sys.exit(1)


# ==========================================
# 5. DYNAMIC RISK & POSITION SIZING
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

        if (total_qty * option_price) > net_capital:
            max_affordable_lots = int(
                net_capital // (NIFTY_LOT_SIZE * option_price)
            )
            lots = max(1, max_affordable_lots)
            total_qty = lots * NIFTY_LOT_SIZE

        logger.info(
            f"Dynamic Risk Sizing: Net Cap ₹{net_capital:.2f} | Lots: {lots} ({total_qty} Qty)"
        )
        return total_qty

    except Exception as e:
        logger.error(f"Dynamic Sizing Error: {e}")
        return NIFTY_LOT_SIZE


def get_live_ltp(token, symbol, exchange="NFO"):
    try:
        ltp_data = smartApi.ltpData(exchange, symbol, str(token))
        if ltp_data and ltp_data.get("status") and "data" in ltp_data:
            return float(ltp_data["data"]["ltp"])
    except Exception as e:
        logger.error(f"REST API LTP Error for {symbol}: {e}")
    return None


def get_nifty_spot_ltp():
    try:
        spot_data = smartApi.ltpData("NSE", "NIFTY", str(NIFTY_TOKEN))
        if spot_data and spot_data.get("status") and "data" in spot_data:
            return float(spot_data["data"]["ltp"])
    except Exception as e:
        logger.error(f"Nifty Spot Fetch Error: {e}")
    return None


# ==========================================
# 6. HELPER & DATA INDICATOR FUNCTIONS
# ==========================================
def fetch_option_chain_data(spot_price):
    """Placeholder function for option chain data retrieval"""
    return []


def unsubscribe_websocket(token):
    """Placeholder function for unsubscribing websocket feeds"""
    pass


def get_atm_option_scrip(spot_price, option_type="CE"):
    """Finds nearest ATM weekly option scrip details"""
    try:
        if scrip_master_df is None or scrip_master_df.empty:
            return None, None

        atm_strike = round(spot_price / 50.0) * 50

        nifty_df = scrip_master_df[
            (scrip_master_df["name"] == "NIFTY")
            & (scrip_master_df["instrumenttype"] == "OPTIDX")
            & (scrip_master_df["symbol"].str.endswith(option_type))
        ].copy()

        if nifty_df.empty:
            return None, None

        nifty_df["strike"] = pd.to_numeric(nifty_df["strike"], errors="coerce")
        nifty_df["expiry_dt"] = pd.to_datetime(nifty_df["expiry"], errors="coerce")

        today = datetime.now()
        valid_df = nifty_df[
            (nifty_df["strike"] == atm_strike) & (nifty_df["expiry_dt"] >= today)
        ].sort_values(by="expiry_dt")

        if not valid_df.empty:
            selected_row = valid_df.iloc[0]
            return selected_row["symbol"], str(selected_row["token"])
    except Exception as e:
        logger.error(f"Option Strike Finder Error: {e}")

    return None, None


def fetch_nifty_candles():
    """Fetches 5-minute candles for RSI & Momentum evaluation"""
    try:
        now = datetime.now()
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
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            return df
    except Exception as e:
        logger.error(f"Candle Data Fetch Error: {e}")
    return None


def generate_trade_signal():
    """RSI Momentum + EMA Crossover Strategy"""
    df = fetch_nifty_candles()
    if df is None or len(df) < 25:
        return "NO_TRADE"

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if (
        curr["rsi"] >= 60.0
        and curr["ema_9"] > curr["ema_21"]
        and prev["ema_9"] <= prev["ema_21"]
    ):
        return "CE"

    elif (
        curr["rsi"] <= 40.0
        and curr["ema_9"] < curr["ema_21"]
        and prev["ema_9"] >= prev["ema_21"]
    ):
        return "PE"

    return "NO_TRADE"


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

        if (resistance_strike - current_price) <= buffer_points and current_price < resistance_strike:
            logger.info(
                f"❌ TRADE REJECTED: Price ({current_price:.2f}) near Resistance ({resistance_strike})"
            )
            return False

        logger.info(
            f"✅ OPTION CHAIN PASSED: CE Cleared (PCR: {pcr:.2f}, R: {resistance_strike})"
        )
        return True

    elif signal == "PE":
        if pcr > 1.4:
            logger.info(
                f"❌ TRADE REJECTED: PE Signal ignored due to Bullish PCR ({pcr:.2f})"
            )
            return False

        if (current_price - support_strike) <= buffer_points and current_price > support_strike:
            logger.info(
                f"❌ TRADE REJECTED: Price ({current_price:.2f}) near Support ({support_strike})"
            )
            return False

        logger.info(
            f"✅ OPTION CHAIN PASSED: PE Cleared (PCR: {pcr:.2f}, S: {support_strike})"
        )
        return True

    return False


def get_option_chain_pcr_and_levels(spot_price):
    """Derives PCR, Support, and Resistance from option chain data"""
    chain_data = fetch_option_chain_data(spot_price)
    if not chain_data:
        return 1.0, spot_price + 200, spot_price - 200

    df_chain = pd.DataFrame(chain_data)
    ce_df = df_chain[df_chain["option_type"] == "CE"]
    pe_df = df_chain[df_chain["option_type"] == "PE"]

    total_ce_oi = ce_df["open_interest"].sum()
    total_pe_oi = pe_df["open_interest"].sum()

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

    resistance = (
        ce_df.loc[ce_df["open_interest"].idxmax()]["strike"]
        if not ce_df.empty
        else spot_price + 200
    )
    support = (
        pe_df.loc[pe_df["open_interest"].idxmax()]["strike"]
        if not pe_df.empty
        else spot_price - 200
    )

    return pcr, float(resistance), float(support)


def get_15m_trend():
    """Calculates 15-Minute Macro Trend using EMA 200"""
    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    from_date = (datetime.now() - pd.Timedelta(days=15)).strftime("%Y-%m-%d %H:%M")

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
    return 14.5


def is_market_open():
    now = datetime.now().time()
    return dtime(9, 15) <= now <= dtime(15, 15)


def is_new_entry_allowed():
    now = datetime.now().time()
    return dtime(9, 20) <= now <= dtime(14, 45)


def is_squareoff_time():
    return datetime.now().time() >= dtime(15, 10)


def cleanup_position():
    """Resets global active trading flags safely"""
    global pos_active, active_symbol, active_token, highest_price_seen
    if active_token:
        try:
            unsubscribe_websocket(active_token)
        except Exception as e:
            logger.warning(f"Failed to unsubscribe token {active_token}: {e}")

    pos_active = False
    active_symbol = ""
    active_token = ""
    highest_price_seen = 0.0


# ==========================================
# 7. SCANNER EXECUTION CYCLE
# ==========================================
def execute_nifty_algo_cycle():
    """15-Min Interval Algo Scan Execution"""
    spot_ltp = get_nifty_spot_ltp()
    if not spot_ltp:
        logger.error("Unable to fetch Nifty Spot LTP. Exiting cycle.")
        return

    logger.info(f"⚡ [NIFTY SCANNER RUN] Nifty Spot LTP: ₹{spot_ltp:.2f}")

    # Alert on Scan Completion
    send_telegram_alert(
        f"🎯 <b>Nifty 15-Min Scan Complete</b>\n"
        f"<b>Spot Level:</b> ₹{spot_ltp:.2f}\n"
        f"<b>Status:</b> Market Scanned Successfully"
    )


# ==========================================
# MAIN ENTRYPOINT FOR WORKFLOW & CI/CD
# ==========================================
if __name__ == "__main__":
    initialize_smartapi()

    now = datetime.now()
    # Check Market Hours (Monday-Friday 9:15 AM - 3:30 PM IST)
    if now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 15)
        or (10 <= now.hour < 15)
        or (now.hour == 15 and now.minute <= 30)
    ):
        execute_nifty_algo_cycle()
    else:
        logger.info("Market is Closed. Skipping Nifty Scan.")

# ==========================================
# 3. MAIN RUNTIME LOOP & ENGINE CONTROLLER (REFINED)
# ==========================================
def main_loop():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price
    global highest_price_seen, tsl_activated, active_quantity, trade_entry_time
    global daily_trades_count, consecutive_sl_count, consecutive_win_count, algo_paused

    logger.info("🚀 Nifty Option Algo Bot Started Engine Loop...")
    send_telegram_alert(
        "🚀 <b>Nifty Option Trading Algo Active!</b>\n"
        "Rule 1: 2 Losses -> Stop Trading.\n"
        "Rule 2: 3 Profits -> Stop Trading."
    )

    while True:
        try:
            if not is_market_open():
                print(
                    "\r⏸️ Market Closed. Waiting for market session...",
                    end="",
                    flush=True,
                )
                time.sleep(10)
                continue

            # 1. AUTO SQUARE-OFF AT 03:10 PM
            if is_squareoff_time() and pos_active:
                ltp = (
                    get_live_ltp(active_token, active_symbol) or entry_price
                )
                logger.info(
                    f"[AUTO SQUARE-OFF 03:10 PM] Exiting {active_symbol} at ₹{ltp:.2f}"
                )

                # Order response check
                exit_status = place_order(
                    active_symbol,
                    active_token,
                    "SELL",
                    active_quantity,
                )
                
                if exit_status:
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
                    cleanup_position()
                    daily_trades_count += 1

            # 2. ACTIVE POSITION MONITOR & TRAILING SL
            elif pos_active:
                ltp = get_live_ltp(active_token, active_symbol) or entry_price
                now = datetime.now()
                holding_time_mins = (
                    now - trade_entry_time
                ).total_seconds() / 60.0

                if ltp > highest_price_seen:
                    highest_price_seen = ltp

                if ENABLE_TRAILING_SL:
                    gain_pct = (
                        highest_price_seen - entry_price
                    ) / entry_price
                    if gain_pct >= TSL_ACTIVATION_PCT:
                        steps = int(
                            (gain_pct - TSL_ACTIVATION_PCT)
                            / TSL_STEP_TRIGGER_PCT
                        )
                        new_sl = round(
                            entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)),
                            2,
                        )
                        if new_sl > sl_price:
                            sl_price = new_sl
                            tsl_activated = True
                            logger.info(
                                f"[TRAILING SL UPDATED] Raised SL to ₹{sl_price:.2f}"
                            )

                # --- STOP LOSS CHECK ---
                if ltp <= sl_price:
                    reason = (
                        "TRAILING_SL_HIT" if tsl_activated else "INITIAL_SL_HIT"
                    )
                    exit_status = place_order(
                        active_symbol,
                        active_token,
                        "SELL",
                        active_quantity,
                    )
                    if exit_status:
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
                            f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                        )

                        cleanup_position()
                        daily_trades_count += 1
                        consecutive_sl_count += 1
                        consecutive_win_count = 0

                # --- TARGET CHECK ---
                elif ltp >= tgt_price:
                    exit_status = place_order(
                        active_symbol,
                        active_token,
                        "SELL",
                        active_quantity,
                    )
                    if exit_status:
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
                            f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                        )

                        cleanup_position()
                        daily_trades_count += 1
                        consecutive_win_count += 1
                        consecutive_sl_count = 0

                # --- TIME EXIT (THETA TIMEOUT) ---
                elif holding_time_mins >= MAX_HOLDING_MINUTES:
                    exit_status = place_order(
                        active_symbol,
                        active_token,
                        "SELL",
                        active_quantity,
                    )
                    if exit_status:
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
                            f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                        )

                        cleanup_position()
                        daily_trades_count += 1
                        if ltp < entry_price:
                            consecutive_sl_count += 1
                            consecutive_win_count = 0

            # 3. NEW ENTRY SCANNING
            elif (
                not pos_active
                and not algo_paused
                and is_new_entry_allowed()
            ):
                if daily_trades_count >= MAX_DAILY_TRADES:
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
                    signal = generate_trade_signal()

                    if signal in ["CE", "PE"]:
                        spot = get_nifty_spot_ltp()
                        if spot and spot > 0:
                            vix = get_india_vix()
                            macro_trend = get_15m_trend()
                            pcr, res_strike, sup_strike = (
                                get_option_chain_pcr_and_levels(spot)
                            )

                            is_oc_valid = validate_trade_with_option_chain(
                                signal, spot, pcr, res_strike, sup_strike
                            )

                            if not (MIN_VIX <= vix <= MAX_VIX):
                                logger.info(
                                    f"Entry Blocked: VIX ({vix}) out of bounds"
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
                                sym, tok = get_atm_option_scrip(
                                    spot, option_type=signal
                                )

                                if sym and tok:
                                    opt_ltp = get_live_ltp(tok, sym)
                                    if opt_ltp and opt_ltp > 0:
                                        qty = calculate_dynamic_quantity(
                                            opt_ltp
                                        )
                                        order_id = place_order(
                                            sym, tok, "BUY", qty
                                        )

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
                                            trade_entry_time = datetime.now()

                                            setup_and_subscribe_websocket(
                                                tok, exchange_type=2
                                            )
                                            send_telegram_alert(
                                                f"🚀 <b>NEW AUTO-TRADE ENTERED ({signal})</b>\n"
                                                f"<b>Symbol:</b> {sym}\n"
                                                f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                                f"<b>SL:</b> ₹{sl_price:.2f}\n"
                                                f"<b>Target:</b> ₹{tgt_price:.2f}\n"
                                                f"<b>Qty:</b> {qty}"
                                            )

            time.sleep(1)

        except Exception as main_e:
            logger.error(f"Main Engine Exception: {main_e}")
            time.sleep(2)


if __name__ == "__main__":
    main_loop()
