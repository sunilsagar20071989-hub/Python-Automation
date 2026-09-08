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
NIFTY_LOT_SIZE = 65  # Nifty Lot Size Updated
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
SCAN_INTERVAL_SECONDS = 15

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

# Trend Cache to prevent excessive API Rate Limit hits
cached_15m_trend = "NEUTRAL"
last_15m_fetch_time = None


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


# ==========================================
# 5. TECHNICAL INDICATORS & SCAN ENGINE
# ==========================================
def get_live_ltp(token, symbol, exchange="NFO"):
    try:
        time.sleep(0.3)  # Cooldown between REST calls
        ltp_data = smartApi.ltpData(exchange, symbol, str(token))
        if ltp_data and ltp_data.get("status") and "data" in ltp_data:
            return float(ltp_data["data"]["ltp"])
    except Exception as e:
        logger.error(f"REST API LTP Error for {symbol}: {e}")
    return None


def get_nifty_spot_ltp():
    return get_live_ltp(NIFTY_TOKEN, "NIFTY", exchange="NSE")


def get_india_vix():
    """Dynamically find India VIX token and validate fetched LTP."""
    try:
        if scrip_master_df is not None and not scrip_master_df.empty:
            vix_row = scrip_master_df[
                (scrip_master_df["name"] == "INDIA VIX")
                | (scrip_master_df["symbol"] == "INDIA VIX")
                | (scrip_master_df["symbol"] == "India Vix")
            ]
            if not vix_row.empty:
                vix_token = str(vix_row.iloc[0]["token"])
                vix_symbol = str(vix_row.iloc[0]["symbol"])
                vix_exch = str(vix_row.iloc[0].get("exch_seg", "NSE"))

                vix_ltp = get_live_ltp(vix_token, vix_symbol, exchange=vix_exch)
                if vix_ltp and 5.0 <= vix_ltp <= 100.0:
                    return vix_ltp

        direct_vix = get_live_ltp("26009", "INDIA VIX", exchange="NSE")
        if direct_vix and 5.0 <= direct_vix <= 100.0:
            return direct_vix

    except Exception as e:
        logger.error(f"India VIX Fetch Error: {e}")

    logger.warning("Unable to fetch real-time VIX. Using safe default VIX: 14.5")
    return 14.5


def get_itm_option_scrip(spot_price, option_type="CE"):
    try:
        if scrip_master_df is None or scrip_master_df.empty:
            return None, None
        atm_strike = round(spot_price / 50.0) * 50
        itm_strike = atm_strike - ITM_STRIKE_OFFSET if option_type == "CE" else atm_strike + ITM_STRIKE_OFFSET
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

        valid_df = nifty_df[(nifty_df["strike"] == itm_strike) & (nifty_df["expiry_dt"] >= today)].sort_values(
            by="expiry_dt"
        )

        if not valid_df.empty:
            selected_row = valid_df.iloc[0]
            return selected_row["symbol"], str(selected_row["token"])
    except Exception as e:
        logger.error(f"ITM Option Strike Finder Error: {e}")
    return None, None


def fetch_nifty_candles(interval="FIVE_MINUTE"):
    """Fetch candle data with rate-limit protection and retries."""
    for attempt in range(2):
        try:
            time.sleep(1.5)  # Enforce 1.5s delay to prevent 'Access Denied' throttling
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
            if candles and isinstance(candles, dict) and candles.get("status") and "data" in candles:
                df = pd.DataFrame(
                    candles["data"],
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["close"] = df["close"].astype(float)
                df["rsi"] = ta.momentum.rsi(df["close"], window=14)
                df["roc"] = ta.momentum.roc(df["close"], window=9)
                df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
                df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
                return df.dropna().reset_index(drop=True)
        except Exception as e:
            logger.error(f"Candle Data Attempt {attempt+1} Failed: {e}")
            time.sleep(2)

    return None


def get_15m_trend():
    """Cached 15m Trend calculation to reduce API calls."""
    global cached_15m_trend, last_15m_fetch_time
    now = get_ist_now()

    if last_15m_fetch_time is not None:
        if (now - last_15m_fetch_time).total_seconds() < 300:
            return cached_15m_trend

    df_15m = fetch_nifty_candles(interval="FIFTEEN_MINUTE")
    if df_15m is None or len(df_15m) < 2:
        return cached_15m_trend

    curr = df_15m.iloc[-1]
    if curr["ema_9"] > curr["ema_21"] or curr["close"] > curr["ema_21"]:
        cached_15m_trend = "BULLISH"
    elif curr["ema_9"] < curr["ema_21"] or curr["close"] < curr["ema_21"]:
        cached_15m_trend = "BEARISH"
    else:
        cached_15m_trend = "NEUTRAL"

    last_15m_fetch_time = now
    return cached_15m_trend


def generate_trade_signal():
    """5-Min Trigger Logic with Flexible Lookback for Crossover and Trend Continuation."""
    df = fetch_nifty_candles(interval="FIVE_MINUTE")
    if df is None or len(df) < 5:
        return "NO_TRADE"

    curr = df.iloc[-1]
    recent_candles = df.iloc[-4:-1]

    recent_bull_cross = any(recent_candles["ema_9"] <= recent_candles["ema_21"]) and (curr["ema_9"] > curr["ema_21"])
    recent_bear_cross = any(recent_candles["ema_9"] >= recent_candles["ema_21"]) and (curr["ema_9"] < curr["ema_21"])

    if (curr["rsi"] >= 58.0 and curr["roc"] > 0.0 and curr["ema_9"] > curr["ema_21"]) and (
        recent_bull_cross or curr["rsi"] > 62
    ):
        return "CE"
    elif (curr["rsi"] <= 42.0 and curr["roc"] < 0.0 and curr["ema_9"] < curr["ema_21"]) and (
        recent_bear_cross or curr["rsi"] < 38
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
# 6. MAIN TRADING EXECUTION ENGINE
# ==========================================
def run_trading_cycle():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price
    global highest_price_seen, tsl_activated, active_quantity, trade_entry_time
    global daily_trades_count, consecutive_sl_count, consecutive_win_count

    if is_squareoff_time() and pos_active:
        ltp = get_live_ltp(active_token, active_symbol) or entry_price
        place_order(active_symbol, active_token, "SELL", active_quantity)
        log_trade(active_symbol, "SELL", entry_price, ltp, active_quantity, "AUTO_SQUARE_OFF")
        send_telegram_alert(f"⏰ AUTO SQUARE-OFF (03:10 PM) | Exit: ₹{ltp:.2f}")
        cleanup_position()
        return

    if pos_active:
        ltp = get_live_ltp(active_token, active_symbol) or entry_price
        holding_time_mins = (get_ist_now() - trade_entry_time).total_seconds() / 60.0

        if ltp > highest_price_seen:
            highest_price_seen = ltp

        if ENABLE_TRAILING_SL and highest_price_seen > entry_price:
            gain_pct = (highest_price_seen - entry_price) / entry_price
            if gain_pct >= TSL_ACTIVATION_PCT:
                steps = int((gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT) + 1
                new_sl = round(entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2)
                if new_sl > sl_price:
                    sl_price = new_sl
                    tsl_activated = True
                    logger.info(f"📈 Trailing SL Updated to ₹{sl_price:.2f}")

        if ltp <= sl_price:
            reason = "TRAILING_SL_HIT" if tsl_activated else "INITIAL_SL_HIT"
            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(active_symbol, "SELL", entry_price, ltp, active_quantity, reason)
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
            log_trade(active_symbol, "SELL", entry_price, ltp, active_quantity, "TARGET_ACHIEVED")
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
            log_trade(active_symbol, "SELL", entry_price, ltp, active_quantity, "THETA_TIMEOUT")
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

                    if not (MIN_VIX <= vix <= MAX_VIX):
                        logger.info(f"Entry Blocked: VIX ({vix}) out of bounds ({MIN_VIX}-{MAX_VIX})")
                    elif signal == "CE" and macro_trend == "BEARISH":
                        logger.info("Entry Blocked: 15m Trend is BEARISH, cannot take CE")
                    elif signal == "PE" and macro_trend == "BULLISH":
                        logger.info("Entry Blocked: 15m Trend is BULLISH, cannot take PE")
                    else:
                        sym, tok = get_itm_option_scrip(spot, option_type=signal)
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
                                    sl_price = round(opt_ltp * (1 - SL_PCT), 2)
                                    tgt_price = round(opt_ltp * (1 + TARGET_PCT), 2)
                                    highest_price_seen = opt_ltp
                                    tsl_activated = False
                                    trade_entry_time = get_ist_now()
                                    send_telegram_alert(
                                        f"🚀 <b>NEW TRADE ENTERED ({signal})</b>\n"
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
        logger.info("Market is Closed. Engine terminating cleanly.")
        send_telegram_alert("ℹ️ <b>Nifty Algo:</b> Market Closed. Workflow finished successfully.")
        sys.exit(0)

    logger.info(
        f"⚡ Engine active ({'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}): Running live Market Scans..."
    )
    send_telegram_alert("🚀 <b>Nifty Option Algo Active!</b>\nEngine listening for signals...")

    while is_market_open():
        try:
            run_trading_cycle()
            time.sleep(2 if pos_active else SCAN_INTERVAL_SECONDS)
        except Exception as main_e:
            logger.error(f"Main Engine Exception: {main_e}")
            time.sleep(3)

    logger.info("Market hours finished. Stopping engine.")
    sys.exit(0)
