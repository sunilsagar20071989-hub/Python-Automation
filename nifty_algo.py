from datetime import datetime, time as dtime
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

# --- RISK & CAPITAL PARAMETERS ---
SL_PCT = 0.045
TARGET_PCT = 0.1575
MAX_RISK_PER_TRADE_PCT = 0.015  # Max 1.5% capital risk per trade
NIFTY_LOT_SIZE = 65  # Updated NSE Nifty lot size

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

# Telegram Controls Flags
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
    """Sends HTML formatted Telegram notifications with retry mechanism"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot Token ya Chat ID environment variables mein missing hain.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    for attempt in range(max_retries):
        try:
            # Timeout ko 10s set kiya hai stability ke liye
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Telegram Alert Error after {max_retries} attempts: {e}")
            time.sleep(1)


def log_trade(symbol, trade_type, entry_p, exit_p, qty, reason):
    """Logs trade performance metrics into CSV file"""
    pnl = round((exit_p - entry_p) * qty, 2)
    pnl_pct = round(((exit_p - entry_p) / entry_p) * 100, 2)
    holding_time = round((datetime.now() - trade_entry_time).total_seconds() / 60.0, 2) if trade_entry_time else 0

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
    """Handles incoming commands sent via Telegram"""
    global pos_active, active_symbol, active_token, active_quantity, entry_price, daily_trades_count, algo_paused

    cmd = command.strip().lower()

    if cmd == "/status":
        if pos_active:
            ltp = get_live_ltp(active_token, active_symbol) or entry_price
            pnl = round((ltp - entry_price) * active_quantity, 2)
            pnl_pct = round(((ltp - entry_price) / entry_price) * 100, 2)
            holding_time = round((datetime.now() - trade_entry_time).total_seconds() / 60.0, 1) if trade_entry_time else 0

            msg = (
                f"📊 <b>CURRENT POSITION STATUS</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
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
            place_order(active_symbol, active_token, "SELL", active_quantity)
            log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, "MANUAL_TELEGRAM_EXIT")
            
            pnl = round((ltp - entry_price) * active_quantity, 2)
            send_telegram_alert(
                f"⚠️ <b>MANUAL EXIT TRIGGERED VIA TELEGRAM</b>\n"
                f"<b>Symbol:</b> {active_symbol}\n"
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
    """Polls Telegram API for incoming interactive commands"""
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
    """Background listener loop for continuous Telegram polling"""
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
    """Calculates trade quantity based on account net cash and max risk per trade"""
    try:
        rms_data = smartApi.rmsLimit()
        if rms_data and rms_data.get("status") and "data" in rms_data:
            net_capital = float(rms_data["data"].get("net", 0.0))
            if net_capital > 0:
                max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
                risk_per_share = option_price * SL_PCT
                calculated_qty = max_risk_amount / risk_per_share
                
                # Round to nearest lot size
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


def is_market_open():
    now = datetime.now().time()
    return dtime(9, 15) <= now <= dtime(15, 15)


def is_new_entry_allowed():
    now = datetime.now().time()
    return dtime(9, 20) <= now <= dtime(14, 45)


def is_squareoff_time():
    return datetime.now().time() >= dtime(15, 10)


def get_itm_symbol_and_token(spot_price, option_type):
    atm = round(spot_price / 50) * 50
    target_strike = (atm - 100) if option_type == "CE" else (atm + 100)

    try:
        cols = {c.lower(): c for c in scrip_master_df.columns}
        name_col, inst_col, strike_col = (
            cols.get("name"),
            cols.get("instrumenttype"),
            cols.get("strike"),
        )
        sym_col, token_col, expiry_col = (
            cols.get("symbol"),
            cols.get("token"),
            cols.get("expiry"),
        )

        filtered = scrip_master_df[
            (scrip_master_df[name_col].astype(str).str.upper() == "NIFTY")
            & (scrip_master_df[inst_col].astype(str).str.upper() == "OPTIDX")
            & (scrip_master_df[strike_col].astype(float) == float(target_strike * 100))
            & (scrip_master_df[sym_col].astype(str).str.endswith(option_type))
        ].copy()

        if not filtered.empty:
            filtered["expiry_dt"] = pd.to_datetime(filtered[expiry_col], format="%d%b%Y", errors="coerce")
            today = pd.to_datetime(datetime.now().date())
            valid_expiries = filtered[filtered["expiry_dt"] >= today].sort_values("expiry_dt")

            if not valid_expiries.empty:
                selected_row = valid_expiries.iloc[0]
                return str(selected_row[sym_col]), str(selected_row[token_col])
    except Exception as e:
        print(">>> Dynamic Token Lookup Exception:", e)

    return None, None


def fetch_signals_and_data():
    global last_candle_minute, cached_close, cached_signal

    now_minute = datetime.now().minute

    if cached_close is not None and (now_minute % 5 != 0 or now_minute == last_candle_minute):
        return cached_close, cached_signal

    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    from_date = (datetime.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

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
            last_candle_minute = now_minute
            df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["close"] = df["close"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            df["open"] = df["open"].astype(float)
            df["volume"] = df["volume"].astype(float)

            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=12)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            df["ema_44"] = ta.trend.ema_indicator(df["close"], window=44)
            df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)
            df["vol_sma20"] = df["volume"].rolling(window=20).mean()

            curr = df.iloc[-1]
            prev = df.iloc[-2]

            df["date"] = pd.to_datetime(df["timestamp"]).dt.date
            today_candles = df[df["date"] == datetime.now().date()]
            day_open = today_candles.iloc[0]["open"] if not today_candles.empty else curr["open"]
            gap_pct = (day_open - prev["close"]) / prev["close"]

            bullish_cross = (prev["ema_9"] <= prev["ema_21"]) and (curr["ema_9"] > curr["ema_21"])
            bearish_cross = (prev["ema_9"] >= prev["ema_21"]) and (curr["ema_9"] < curr["ema_21"])

            bounce_ce = (curr["low"] <= curr["ema_44"] * 1.002) and (curr["close"] > curr["ema_44"])
            reject_pe = (curr["high"] >= curr["ema_44"] * 0.998) and (curr["close"] < curr["ema_44"])

            vol_confirm = curr["volume"] >= (curr["vol_sma20"] * 0.9)

            ce_confirm = (
                (curr["rsi"] >= 58)
                and (curr["roc"] > 0.1)
                and (curr["close"] > curr["ema_200"])
                and vol_confirm
            )

            pe_confirm = (
                (curr["rsi"] <= 42)
                and (curr["roc"] < -0.1)
                and (curr["close"] < curr["ema_200"])
                and vol_confirm
            )

            raw_signal = "NO_TRADE"
            if (bullish_cross or bounce_ce) and ce_confirm:
                raw_signal = "CE"
            elif (bearish_cross or reject_pe) and pe_confirm:
                raw_signal = "PE"

            if raw_signal == "NO_TRADE":
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal

            vix = get_india_vix()
            if vix < MIN_VIX or vix > MAX_VIX:
                print(f"\n⚠️ [VIX BLOCK] VIX is {vix:.2f}. Signal Skipped!")
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal

            if raw_signal == "CE" and gap_pct > MAX_GAP_PCT:
                print(f"\n⚠️ [GAP BLOCK] Gap-Up too high ({gap_pct*100:.2f}%). CE Skipped!")
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal
            elif raw_signal == "PE" and gap_pct < -MAX_GAP_PCT:
                print(f"\n⚠️ [GAP BLOCK] Gap-Down too deep ({gap_pct*100:.2f}%). PE Skipped!")
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal

            trend_15m = get_15m_trend()
            if (raw_signal == "CE" and trend_15m != "BULLISH") or (raw_signal == "PE" and trend_15m != "BEARISH"):
                print(f"\n⚠️ SIGNAL IGNORED: 5m Signal ({raw_signal}) does not match 15m Trend ({trend_15m}).")
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal

            oc_data = fetch_option_chain_data(curr["close"])
            if oc_data:
                df_oc = pd.DataFrame(oc_data)
                total_put_oi = df_oc[df_oc['option_type'] == 'PE']['open_interest'].sum()
                total_call_oi = df_oc[df_oc['option_type'] == 'CE']['open_interest'].sum()
                pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

                res_strike = df_oc[df_oc['option_type'] == 'CE'].loc[df_oc[df_oc['option_type'] == 'CE']['open_interest'].idxmax()]['strike']
                sup_strike = df_oc[df_oc['option_type'] == 'PE'].loc[df_oc[df_oc['option_type'] == 'PE']['open_interest'].idxmax()]['strike']

                is_safe = validate_trade_with_option_chain(
                    signal=raw_signal,
                    current_price=curr["close"],
                    pcr=pcr,
                    resistance_strike=res_strike,
                    support_strike=sup_strike
                )

                if not is_safe:
                    cached_close, cached_signal = curr["close"], "NO_TRADE"
                    return cached_close, cached_signal

            cached_close, cached_signal = curr["close"], raw_signal
            return cached_close, cached_signal

    except Exception as e:
        print(">>> Fetch Signals Error:", e)

    spot = get_nifty_spot_ltp()
    if spot is not None:
        cached_close = spot
    return cached_close, cached_signal


def place_order(symbol, token, buy_sell, quantity):
    params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": buy_sell,
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "price": "0",
        "quantity": str(quantity),
    }
    try:
        order_id = smartApi.placeOrder(params)
        print(f"\n[ORDER EXECUTED] {buy_sell} | ID: {order_id} | Symbol: {symbol} | Qty: {quantity}")
        return order_id
    except Exception as e:
        print(f">>> Order Execution Error ({buy_sell}):", e)
        return None


# ==========================================
# 6. MAIN TRADING ENGINE LOOP (OPTIMIZED)
# ==========================================
print(">>> Nifty Algo Bot Active (Telegram Commands + Trailing SL + Risk Manager Enabled)...")
send_telegram_alert("🤖 <b>Nifty Algo Bot Activated!</b>\nDynamic Position Sizing, Trailing SL & Remote Commands Ready.\nSend /help to see commands.")

while True:
    try:
        if is_market_open():
            # Check Max Daily Limit
            if daily_trades_count >= MAX_DAILY_TRADES:
                print(f"\r🛑 Max Daily Limit ({MAX_DAILY_TRADES} Trades) Reached. Bot Paused.", end="")
                time.sleep(15)
                continue

            # Check Consecutive Stop Loss Limit
            if consecutive_sl_count >= 2:
                print("\r🛑 2 Consecutive Stop Loss Hit! Protection Active.", end="")
                time.sleep(15)
                continue

            close, signal = fetch_signals_and_data()

            if close is not None:
                # 1. Mandatory Auto Square-off (3:10 PM)
                if is_squareoff_time() and pos_active:
                    ltp = get_live_ltp(active_token, active_symbol) or entry_price
                    print(f"\n[AUTO SQUARE-OFF 03:10 PM] Closing position for {active_symbol} at ₹{ltp:.2f}")
                    place_order(active_symbol, active_token, "SELL", active_quantity)
                    
                    log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, "AUTO_SQUARE_OFF")
                    send_telegram_alert(
                        f"⏰ <b>AUTO SQUARE-OFF (03:10 PM)</b>\n"
                        f"<b>Symbol:</b> {active_symbol}\n"
                        f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                        f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                        f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                    )
                    pos_active = False
                    daily_trades_count += 1

                # 2. Manage Active Position
                elif pos_active:
                    ltp = get_live_ltp(active_token, active_symbol)
                    holding_time_mins = (datetime.now() - trade_entry_time).total_seconds() / 60.0

                    if ltp is not None:
                        # Dynamic Trailing Stop Loss Engine
                        if ENABLE_TRAILING_SL:
                            if ltp > highest_price_seen:
                                highest_price_seen = ltp
                            
                            gain_pct = (highest_price_seen - entry_price) / entry_price
                            if gain_pct >= TSL_ACTIVATION_PCT:
                                steps = int((gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT)
                                new_sl = round(entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2)
                                if new_sl > sl_price:
                                    sl_price = new_sl
                                    print(f"\n[TRAILING SL UPDATED] Raised SL to ₹{sl_price:.2f} (High: ₹{highest_price_seen:.2f})")

                        print(
                            f"\r[POS ACTIVE] {active_symbol} | LTP: ₹{ltp:.2f} | SL: ₹{sl_price:.2f} | TGT: ₹{tgt_price:.2f} | Time: {holding_time_mins:.1f}m/{MAX_HOLDING_MINUTES}m",
                            end="",
                        )

                        # Stop Loss Exit Check
                        if ltp <= sl_price:
                            reason = "TRAILING_SL_HIT" if sl_price > (entry_price * (1 - SL_PCT)) else "INITIAL_SL_HIT"
                            print(f"\n[EXIT SL] {reason} for {active_symbol} at ₹{ltp:.2f}")
                            place_order(active_symbol, active_token, "SELL", active_quantity)
                            
                            log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, reason)
                            send_telegram_alert(
                                f"🔴 <b>STOP LOSS HIT ({reason})</b>\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                            )
                            pos_active = False
                            daily_trades_count += 1
                            if ltp < entry_price:
                                consecutive_sl_count += 1

                        # Target Exit Check
                        elif ltp >= tgt_price:
                            print(f"\n[EXIT TARGET - 12.5%] Target Hit for {active_symbol} at ₹{ltp:.2f}")
                            place_order(active_symbol, active_token, "SELL", active_quantity)
                            
                            log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, "TARGET_ACHIEVED")
                            send_telegram_alert(
                                f"🟢 <b>TARGET ACHIEVED (+12.5%)</b> 🎉\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                            )
                            pos_active = False
                            daily_trades_count += 1
                            consecutive_sl_count = 0

                        # Time Expiration Exit Check
                        elif holding_time_mins >= MAX_HOLDING_MINUTES:
                            print(f"\n[EXIT THETA TIMEOUT] Auto-Exiting {active_symbol} at ₹{ltp:.2f}")
                            place_order(active_symbol, active_token, "SELL", active_quantity)
                            
                            log_trade(active_symbol, "BUY", entry_price, ltp, active_quantity, "THETA_TIMEOUT")
                            send_telegram_alert(
                                f"⏱️ <b>THETA TIMEOUT EXIT ({MAX_HOLDING_MINUTES}m)</b>\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                f"<b>PnL:</b> ₹{round((ltp - entry_price) * active_quantity, 2)}"
                            )
                            pos_active = False
                            daily_trades_count += 1

                # 3. Fresh Entry Execution Check
                elif not pos_active and signal in ["CE", "PE"] and not algo_paused and is_new_entry_allowed():
                    sym, tok = get_itm_symbol_and_token(close, signal)
                    if sym and tok:
                        opt_ltp = get_live_ltp(tok, sym)
                        if opt_ltp is None:
                            opt_data = smartApi.ltpData("NFO", sym, tok)
                            if opt_data.get("status") and "data" in opt_data:
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
                                    f"🚀 <b>NEW TRADE ENTERED ({signal})</b>\n"
                                    f"<b>Symbol:</b> {sym}\n"
                                    f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                    f"<b>SL:</b> ₹{sl_price:.2f} (-4.5%)\n"
                                    f"<b>Target:</b> ₹{tgt_price:.2f} (+12.5%)\n"
                                    f"<b>Qty:</b> {qty}"
                                )
        else:
            print("\r⏸️ Market Closed. Waiting for market session...", end="")
            time.sleep(10)

        time.sleep(3)
    except Exception as main_e:
        print("\n>>> Main Loop Exception:", main_e)
        time.sleep(5)
        time.sleep(5)
