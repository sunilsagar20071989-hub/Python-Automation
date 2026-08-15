from datetime import datetime, time as dtime
import html
import json
import os
import threading
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import requests
import ta
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# ==========================================
# 1. CREDENTIALS, TELEGRAM & RISK PARAMETERS
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

# --- RISK & CAPITAL PARAMETERS ---
SL_PCT = 0.045
TARGET_PCT = 0.1575
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade
NIFTY_LOT_SIZE = 65  # Updated NSE Nifty lot size (Lot size: 65)

# --- TRAILING STOP-LOSS PARAMETERS ---
ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04   # Trailing activates after +4% gain
TSL_STEP_TRIGGER_PCT = 0.02 # Trail SL every +2% price move
TSL_STEP_MOVE_PCT = 0.015   # Move SL up by +1.5% per step

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
active_quantity = 65

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
LOG_FILE = "trade_log.csv"


# ==========================================
# TELEGRAM NOTIFICATION & ACTION HELPERS
# ==========================================
def send_telegram_alert(message, max_retries=3):
    """Sends HTML formatted Telegram notifications safely with retry mechanism."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARNING] Telegram credentials missing.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"[ERROR] Telegram Alert Error after {max_retries} attempts: {e}")
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    """Logs trade performance metrics into CSV file."""
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = round(((exit_p - entry_p) / entry_p) * 100, 2)
    holding_time = (
        round((datetime.now() - trade_entry_time).total_seconds() / 60.0, 2)
        if trade_entry_time
        else 0
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
        "Exit_Reason": reason
    }

    df_log = pd.DataFrame([log_data])
    file_exists = os.path.isfile(LOG_FILE)
    df_log.to_csv(LOG_FILE, mode='a', header=not file_exists, index=False)
    print(f"\n[TRADE LOGGED] PnL: ₹{pnl} ({pnl_pct}%) | Exit: {reason}")


def process_telegram_command(command):
    """Handles incoming commands sent via Telegram."""
    global pos_active, active_symbol, active_token, active_quantity, entry_price, daily_trades_count, algo_paused

    cmd = command.strip().lower()

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
                f"<b>Symbol:</b> {html.escape(active_symbol)}\n"
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
                f"<b>Daily Trades Completed:</b> {daily_trades_count}/{MAX_DAILY_TRADES}\n"
                f"<b>Bot Control Status:</b> {'PAUSED' if algo_paused else 'ACTIVE'}"
            )
        send_telegram_alert(msg)

    elif cmd == "/close":
        if pos_active:
            ltp = get_live_ltp(active_token, active_symbol) or entry_price
            log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, "MANUAL_TELEGRAM_EXIT")
            
            pnl = round((ltp - entry_price) * active_quantity, 2)
            send_telegram_alert(
                f"⚠️ <b>MANUAL EXIT TRIGGERED VIA TELEGRAM</b>\n"
                f"<b>Symbol:</b> {html.escape(active_symbol)}\n"
                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                f"<b>PnL:</b> ₹{pnl}"
            )
            pos_active = False
            daily_trades_count += 1
        else:
            send_telegram_alert("⚠️ No active position to close.")

    elif cmd == "/pause":
        algo_paused = True
        send_telegram_alert("⏸️ <b>Algo Bot Paused!</b> New trade entries are currently disabled.")

    elif cmd == "/resume":
        algo_paused = False
        send_telegram_alert("▶️ <b>Algo Bot Resumed!</b> Active and scanning for entries.")

    elif cmd == "/summary":
        if os.path.exists(LOG_FILE):
            df_log = pd.read_csv(LOG_FILE)
            today_str = datetime.now().strftime("%Y-%m-%d")
            df_today = df_log[df_log["Timestamp"].str.startswith(today_str)]
            
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
        else:
            msg = "📄 No trade history log file found."
        send_telegram_alert(msg)

    elif cmd == "/help":
        help_msg = (
            "🤖 <b>AVAILABLE TELEGRAM COMMANDS</b>\n\n"
            "• <b>/status</b> - View active trade & market info\n"
            "• <b>/close</b> - Force close active open position\n"
            "• <b>/pause</b> - Pause bot from taking new entries\n"
            "• <b>/resume</b> - Resume bot scanning for signals\n"
            "• <b>/summary</b> - View today's total PnL & trade performance\n"
            "• <b>/help</b> - Show this assistance menu"
        )
        send_telegram_alert(help_msg)


def check_telegram_updates():
    """Polls Telegram API for incoming interactive commands."""
    global telegram_last_update_id
    if not TELEGRAM_BOT_TOKEN:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={telegram_last_update_id + 1}&timeout=1"
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
    """Background listener loop for continuous Telegram polling."""
    while True:
        check_telegram_updates()
        time.sleep(2)


# ==========================================
# 2. LOGIN TO SMARTAPI & SCRIP MASTER
# ==========================================
try:
    totp = pyotp.TOTP(TOTP_SECRET).now()
    smartApi = SmartConnect(api_key=API_KEY)
    data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

    auth_token = data["data"]["jwtToken"]
    feed_token = smartApi.getfeedToken()

    print(">>> SmartAPI Login Successful for Live Trading!")

    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]

    headers = {"User-Agent": "Mozilla/5.0"}
    scrip_loaded = False

    print(">>> Fetching SmartAPI Scrip Master...")
    for scrip_url in urls:
        try:
            res = requests.get(scrip_url, headers=headers, timeout=25)
            if res.status_code == 200:
                scrip_data = res.json()
                scrip_master_df = pd.DataFrame(scrip_data)
                scrip_loaded = True
                print(">>> Scrip Master Loaded Successfully!")
                break
        except Exception:
            continue

    if not scrip_loaded:
        raise Exception("Failed to download Scrip Master JSON.")

except Exception as e:
    print(">>> Login / Startup Error:", e)
    exit()

# Start background Telegram Listener Thread
t_listener = threading.Thread(target=telegram_listener_thread, daemon=True)
t_listener.start()


# ==========================================
# 3. DYNAMIC RISK & POSITION SIZING
# ==========================================
def calculate_dynamic_quantity(option_price):
    """Calculates trade quantity based on account net cash and max risk per trade."""
    try:
        rms_data = smartApi.rmsLimit()
        if rms_data and rms_data.get("status") and "data" in rms_data:
            net_capital = float(rms_data["data"].get("net", 0.0))
            if net_capital > 0:
                max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
                risk_per_share = option_price * SL_PCT
                calculated_qty = max_risk_amount / risk_per_share
                
                lots = max(1, int(calculated_qty // NIFTY_LOT_SIZE))
                total_qty = lots * NIFTY_LOT_SIZE
                print(f">>> Dynamic Risk Sizing: Capital ₹{net_capital:.2f} | Risk ₹{max_risk_amount:.2f} | Lots: {lots} ({total_qty} Qty)")
                return total_qty
    except Exception as e:
        print(">>> Dynamic Quantity Calculation Error:", e)
    
    return NIFTY_LOT_SIZE


# ==========================================
# 4. WEBSOCKET HANDLERS
# ==========================================
def on_data(wsapp, message):
    global live_ltp_dict
    try:
        if "token" in message and "last_traded_price" in message:
            token = str(message["token"])
            ltp = float(message["last_traded_price"]) / 100.0
            live_ltp_dict[token] = ltp
    except Exception:
        pass


def on_open(wsapp):
    print(">>> WebSocket Live Feed Connected!")


def on_error(wsapp, error):
    pass


def on_close(wsapp):
    print(">>> WebSocket Live Feed Closed.")


def setup_and_subscribe_websocket(token, exchange_type=2):
    global sws
    try:
        sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)
        sws.on_open = on_open
        sws.on_data = on_data
        sws.on_error = on_error
        sws.on_close = on_close

        def run_ws():
            sws.connect()

        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        time.sleep(2)

        token_list = [{"exchangeType": exchange_type, "tokens": [str(token)]}]
        sws.subscribe("correlation_id_trade", 1, token_list)
        print(f">>> Subscribed WebSocket LTP for Token: {token}")
    except Exception as e:
        print(">>> WebSocket Setup Error:", e)


def get_live_ltp(token, symbol):
    if str(token) in live_ltp_dict:
        return live_ltp_dict[str(token)]
    try:
        ltp_data = smartApi.ltpData("NFO", symbol, token)
        if ltp_data.get("status") and "data" in ltp_data:
            return float(ltp_data["data"]["ltp"])
    except Exception:
        pass
    return None


def get_nifty_spot_ltp():
    try:
        spot_data = smartApi.ltpData("NSE", "NIFTY", NIFTY_TOKEN)
        if spot_data.get("status") and "data" in spot_data:
            return float(spot_data["data"]["ltp"])
    except Exception:
        pass
    return None


# ==========================================
# 5. OPTION CHAIN & MULTI-TIMEFRAME HELPERS
# ==========================================
def validate_trade_with_option_chain(signal, current_price, pcr, resistance_strike, support_strike):
    buffer_points = 40

    if signal == "CE":
        if pcr < 0.6:
            print(f"❌ TRADE REJECTED: 5-Min BUY Signal ignored because PCR is too Bearish ({pcr:.2f})")
            return False

        if (resistance_strike - current_price) <= buffer_points and current_price < resistance_strike:
            print(f"❌ TRADE REJECTED: Price ({current_price}) is too close to Resistance ({resistance_strike})")
            return False

        print("✅ OPTION CHAIN PASSED: CE trade cleared for execution.")
        return True

    elif signal == "PE":
        if pcr > 1.4:
            print(f"❌ TRADE REJECTED: 5-Min SELL Signal ignored because PCR is too Bullish ({pcr:.2f})")
            return False

        if (current_price - support_strike) <= buffer_points and current_price > support_strike:
            print(f"❌ TRADE REJECTED: Price ({current_price}) is too close to Support ({support_strike})")
            return False

        print("✅ OPTION CHAIN PASSED: PE trade cleared for execution.")
        return True

    return False


def fetch_option_chain_data(spot_price):
    try:
        atm = round(spot_price / 50) * 50
        cols = {c.lower(): c for c in scrip_master_df.columns}
        
        filtered = scrip_master_df[
            (scrip_master_df[cols.get("name")].astype(str).str.upper() == "NIFTY") &
            (scrip_master_df[cols.get("instrumenttype")].astype(str).str.upper() == "OPTIDX")
        ].copy()

        filtered["strike_val"] = filtered[cols.get("strike")].astype(float) / 100.0
        filtered["expiry_dt"] = pd.to_datetime(filtered[cols.get("expiry")], format="%d%b%Y", errors="coerce")
        today = pd.to_datetime(datetime.now().date())
        valid = filtered[filtered["expiry_dt"] >= today].sort_values("expiry_dt")
        
        if valid.empty:
            return None

        nearest_expiry = valid.iloc[0]["expiry_dt"]
        chain_df = valid[valid["expiry_dt"] == nearest_expiry]
        strike_range = chain_df[(chain_df["strike_val"] >= atm - 500) & (chain_df["strike_val"] <= atm + 500)].copy()

        option_chain_list = []
        for idx, row in strike_range.iterrows():
            sym = str(row[cols.get("symbol")])
            tok = str(row[cols.get("token")])
            stk = float(row["strike_val"])
            opt_type = "CE" if sym.endswith("CE") else "PE"

            oi = 1000
            try:
                oi_data = smartApi.getMarketData("FULL", {"NSE": [tok]})
                if oi_data and oi_data.get("status") and "fetched" in oi_data["data"]:
                    oi = float(oi_data["data"]["fetched"][0].get("opnInterest", 1000))
            except Exception:
                pass

            option_chain_list.append({
                "strike": stk,
                "option_type": opt_type,
                "open_interest": oi
            })

        return option_chain_list

    except Exception as e:
        print(">>> Option Chain Data Error:", e)
        return None


def get_15m_trend():
    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    from_date = (datetime.now() - pd.Timedelta(days=5)).strftime("%Y-%m-%d %H:%M")

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
            df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["close"] = df["close"].astype(float)
            df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)

            curr = df.iloc[-1]
            return "BULLISH" if curr["close"] > curr["ema_200"] else "BEARISH"
    except Exception as e:
        print(">>> 15m Trend Fetch Error:", e)

    return "NEUTRAL"


def get_india_vix():
    try:
        vix_data = smartApi.ltpData("NSE", "INDIA VIX", INDIA_VIX_TOKEN)
        if vix_data.get("status") and "data" in vix_data:
            val = float(vix_data["data"]["ltp"])
            if val > 100:
                val = val / 100.0
            return val
    except Exception:
        pass
    return 14.5

def fetch_signals_and_data():
    global last_candle_minute, cached_close, cached_signal

    now = datetime.now()
    now_minute = now.minute

    # Run check only once per 5-min candle completion (e.g., min % 5 == 0)
    if cached_close is not None and (now_minute % 5 != 0 or now_minute == last_candle_minute):
        return cached_close, cached_signal

    # Fetch data
    to_date = now.strftime("%Y-%m-%d %H:%M")
    from_date = (now - pd.Timedelta(days=2)).strftime("%Y-%m-%d %H:%M")

    param = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = smartApi.getCandleData(param)
        if resp and resp.get("status") and resp.get("data"):
            df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)

            # Indicator Calculations
            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=12)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            df["ema_44"] = ta.trend.ema_indicator(df["close"], window=44)
            df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)
            df["vol_sma20"] = df["volume"].rolling(window=20).mean()

            # Fix: Use closed candles to avoid repainting
            curr = df.iloc[-2]  
            prev = df.iloc[-3]  

            # Update cache timestamp
            last_candle_minute = now_minute
            
            # ... [Rest of signal validation logic] ...
            
    except Exception as e:
        print(">>> Fetch Signals Error:", e)

    return cached_close, cached_signal
