# ==========================================
# 1. PARAMETERS & CONFIGURATION
# ==========================================
from datetime import datetime
import json
import os
import sys
import threading
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import ta

load_dotenv()

PAPER_TRADING = True  # Set to False for REAL / LIVE Trading Execution

# Secure Credentials Reading (No hardcoded fallback secrets)
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

missing_vars = []
if not API_KEY:
    missing_vars.append("SMARTAPI_KEY/SMARTAPI_API_KEY")
if not CLIENT_CODE:
    missing_vars.append("SMARTAPI_CLIENT_CODE")
if not PIN:
    missing_vars.append("SMARTAPI_PIN")
if not TOTP_SECRET:
    missing_vars.append("SMARTAPI_TOTP_SECRET")

if missing_vars:
    print(
        f">>> [CRITICAL SECURITY ERROR] Missing environment variables: {', '.join(missing_vars)}"
    )
    sys.exit(1)

SL_PCT = 0.08  # 8.00% Stop Loss
TARGET_PCT = 0.16  # 16.00% Target
LOT_SIZE = 65  # Nifty Lot Size
NIFTY_TOKEN = "99926000"
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"

pos_active = False
active_symbol = ""
active_token = ""
entry_price = 0.0
sl_price = 0.0
tgt_price = 0.0
trade_type = ""

virtual_balance = 50000.0
total_virtual_pnl = 0.0

scrip_master_df = None
auth_token = ""
feed_token = ""
smartApi = None

live_ltp_dict = {}
http_session = requests.Session()


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        http_session.post(url, data=payload, timeout=5)
    except Exception as e:
        print(">>> Telegram Alert Error:", e)


# ==========================================
# 2. LOGIN TO SMARTAPI & SCRIP MASTER LOAD
# ==========================================
try:
    totp = pyotp.TOTP(TOTP_SECRET).now()
    smartApi = SmartConnect(api_key=API_KEY)
    data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

    if not data or not data.get("status"):
        raise Exception(
            f"Login Failed: {data.get('message') if data else 'No response'}"
        )

    auth_token = data["data"]["jwtToken"]
    feed_token = smartApi.getfeedToken()

    mode_str = (
        "[PAPER TRADING - SIMULATION]"
        if PAPER_TRADING
        else "[LIVE TRADING - REAL ORDERS]"
    )
    print("\n" + "=" * 70)
    print(">>> SmartAPI Login Successful!")
    print(f">>> MODE: {mode_str}")
    print("=" * 70 + "\n")

    send_telegram_alert(
        f"🤖 <b>Nifty Options Trading Bot Started</b>\n<b>Mode:</b> {mode_str}"
    )

    # Local Caching Logic for Scrip Master
    download_needed = True
    if os.path.exists(MASTER_FILE_LOCAL):
        file_time = datetime.fromtimestamp(os.path.getmtime(MASTER_FILE_LOCAL))
        if file_time.date() == datetime.now().date():
            download_needed = False

    if download_needed:
        urls = [
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
        ]
        headers = {"User-Agent": "Mozilla/5.0"}
        scrip_loaded = False

        print(">>> Downloading SmartAPI Scrip Master...")
        for scrip_url in urls:
            try:
                res = http_session.get(scrip_url, headers=headers, timeout=30)
                if res.status_code == 200:
                    with open(MASTER_FILE_LOCAL, "wb") as f:
                        f.write(res.content)
                    scrip_loaded = True
                    print(">>> Scrip Master Saved Locally!")
                    break
            except Exception:
                continue

    if os.path.exists(MASTER_FILE_LOCAL):
        scrip_master_df = pd.read_json(MASTER_FILE_LOCAL)
        print(">>> Scrip Master Loaded Successfully!")
    else:
        raise Exception("Failed to acquire Scrip Master File.")

except Exception as e:
    err_msg = f">>> Login / Startup Error: {e}"
    print(err_msg)
    send_telegram_alert(f"🚨 <b>BOT STARTUP ERROR</b>\n{err_msg}")
    sys.exit(1)


# ==========================================
# 3. WEBSOCKET TICKER INTEGRATION (V2)
# ==========================================
def on_data(wsapp, message):
    global live_ltp_dict
    try:
        if "token" in message and "last_traded_price" in message:
            token = str(message["token"])
            ltp = float(message["last_traded_price"]) / 100.0  # Paisa to Rupees
            live_ltp_dict[token] = ltp
    except Exception:
        pass


def on_open(wsapp):
    print(">>> WebSocket Live Feed Connected!")


def on_error(wsapp, error):
    print(">>> WebSocket Error:", error)


def on_close(wsapp):
    print(">>> WebSocket Closed!")


sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)
sws.on_open = on_open
sws.on_data = on_data
sws.on_error = on_error
sws.on_close = on_close


def start_websocket():
    sws.connect()


ws_thread = threading.Thread(target=start_websocket, daemon=True)
ws_thread.start()


def subscribe_token(token, exchange_type=2):  # 2 = NFO
    token_list = [{"exchangeType": exchange_type, "tokens": [str(token)]}]
    sws.subscribe("correlation_id_trade", 1, token_list)


def get_live_ltp(token):
    return live_ltp_dict.get(str(token), None)


# ==========================================
# 4. HELPER FUNCTIONS & SIGNALS
# ==========================================
def is_market_open():
    now = datetime.now().time()
    start_time = datetime.strptime("09:15", "%H:%M").time()
    end_time = datetime.strptime("15:15", "%H:%M").time()
    return start_time <= now <= end_time


def is_new_entry_allowed():
    now = datetime.now().time()
    start_time = datetime.strptime("09:30", "%H:%M").time()
    cutoff_time = datetime.strptime("14:45", "%H:%M").time()
    return start_time <= now <= cutoff_time


def is_squareoff_time():
    now = datetime.strptime("15:10", "%H:%M").time()
    return datetime.now().time() >= now


def get_itm_symbol_and_token(spot_price, option_type):
    atm = round(spot_price / 50) * 50
    target_strike = (atm - 100) if option_type == "CE" else (atm + 100)  # 2-ITM

    try:
        cols = {c.lower(): c for c in scrip_master_df.columns}
        name_col, inst_col = (
            cols.get("name", "name"),
            cols.get("instrumenttype", "instrumenttype"),
        )
        strike_col, sym_col = (
            cols.get("strike", "strike"),
            cols.get("symbol", "symbol"),
        )
        token_col, expiry_col = (
            cols.get("token", "token"),
            cols.get("expiry", "expiry"),
        )

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
                filtered[expiry_col], format="%d%b%Y", errors="coerce"
            )
            today = pd.to_datetime(datetime.now().date())
            valid_expiries = filtered[
                filtered["expiry_dt"] >= today
            ].sort_values("expiry_dt")

            if not valid_expiries.empty:
                selected_row = valid_expiries.iloc[0]
                symbol = str(selected_row[sym_col])
                token = str(selected_row[token_col])
                return symbol, token
    except Exception as e:
        print(">>> Dynamic Token Lookup Exception:", e)

    return None, None


def fetch_signals_and_data():
    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    from_date = (datetime.now() - pd.Timedelta(days=3)).strftime(
        "%Y-%m-%d %H:%M"
    )

    param = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = smartApi.getCandleData(param)
        if resp.get("status") and resp.get("data"):
            df = pd.DataFrame(
                resp["data"],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = df["close"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)

            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=12)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            df["ema_44"] = ta.trend.ema_indicator(df["close"], window=44)

            curr = df.iloc[-1]
            prev = df.iloc[-2]

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

            signal = "NO_TRADE"
            if (bullish_cross or bounce_ce) and ce_confirm:
                signal = "CE"
            elif (bearish_cross or reject_pe) and pe_confirm:
                signal = "PE"

            return curr["close"], signal

    except Exception as e:
        print(">>> Indicator Fetch Error:", e)

    return None, "NO_TRADE"


def execute_order(symbol, token, action_label, qty, price=0.0):
    global total_virtual_pnl
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if PAPER_TRADING:
        if action_label == "BUY":
            log_str = (
                f"\n[PAPER TRADE - VIRTUAL BUY] {timestamp}\n"
                f"| Symbol: {symbol} | Qty: {qty} | Entry LTP: ₹{price:.2f}\n"
                f"| Target (+16%): ₹{tgt_price:.2f} | SL (-8%): ₹{sl_price:.2f}\n"
                + "-" * 60
            )
            print(log_str)
            send_telegram_alert(
                f"🟢 <b>PAPER TRADE BUY ENTRY</b>\n"
                f"<b>Symbol:</b> {symbol}\n"
                f"<b>Entry LTP:</b> ₹{price:.2f}\n"
                f"<b>Target (+16%):</b> ₹{tgt_price:.2f}\n"
                f"<b>SL (-8%):</b> ₹{sl_price:.2f}"
            )
        else:
            pnl_per_qty = price - entry_price
            pnl_total = pnl_per_qty * qty
            total_virtual_pnl += pnl_total
            log_str = (
                f"\n[PAPER TRADE - VIRTUAL EXIT: {action_label}] {timestamp}\n"
                f"| Symbol: {symbol} | Exit LTP: ₹{price:.2f} | Entry LTP: ₹{entry_price:.2f}\n"
                f"| Trade PnL: ₹{pnl_total:+.2f} | Overall Paper PnL: ₹{total_virtual_pnl:+.2f}\n"
                + "-" * 60
            )
            print(log_str)
            send_telegram_alert(
                f"🔴 <b>PAPER TRADE EXIT ({action_label})</b>\n"
                f"<b>Symbol:</b> {symbol}\n"
                f"<b>Exit Price:</b> ₹{price:.2f}\n"
                f"<b>Trade PnL:</b> ₹{pnl_total:+.2f}\n"
                f"<b>Total Paper PnL:</b> ₹{total_virtual_pnl:+.2f}"
            )
    else:
        # REAL LIVE ORDER EXECUTION VIA SMARTAPI
        try:
            transaction_type = "BUY" if action_label == "BUY" else "SELL"
            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": str(token),
                "transactiontype": transaction_type,
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "CARRYFORWARD",
                "duration": "DAY",
                "price": "0",
                "quantity": str(qty),
            }
            order_id = smartApi.placeOrder(order_params)
            print(
                f"\n[LIVE ORDER PLACED] ID: {order_id} | Type: {transaction_type} | Symbol: {symbol}"
            )

            if action_label == "BUY":
                send_telegram_alert(
                    f"🚀 <b>LIVE BUY ORDER EXECUTED</b>\n"
                    f"<b>Symbol:</b> {symbol}\n"
                    f"<b>Qty:</b> {qty}\n"
                    f"<b>Order ID:</b> {order_id}"
                )
            else:
                pnl_total = (price - entry_price) * qty
                send_telegram_alert(
                    f"🏁 <b>LIVE EXIT ORDER EXECUTED ({action_label})</b>\n"
                    f"<b>Symbol:</b> {symbol}\n"
                    f"<b>Exit Price:</b> ₹{price:.2f}\n"
                    f"<b>Trade PnL:</b> ₹{pnl_total:+.2f}\n"
                    f"<b>Order ID:</b> {order_id}"
                )
        except Exception as e:
            err_msg = f">>> LIVE ORDER PLACEMENT FAILED: {e}"
            print(err_msg)
            send_telegram_alert(f"🚨 <b>LIVE ORDER ERROR</b>\n{err_msg}")


# ==========================================
# 5. MAIN ENGINE
# ==========================================
print(">>> Trading Bot Active... Watching Live Market Data...")

while True:
    try:
        if is_market_open():
            close, signal = fetch_signals_and_data()

            if close is not None:
                # 1. AUTO SQUARE-OFF AT 15:10
                if is_squareoff_time() and pos_active:
                    ltp = get_live_ltp(active_token) or entry_price
                    execute_order(
                        active_symbol,
                        active_token,
                        "3:10 PM SQUARE-OFF",
                        LOT_SIZE,
                        price=ltp,
                    )
                    pos_active = False

                # 2. EXIT MONITORING
                elif pos_active:
                    ltp = get_live_ltp(active_token)

                    if ltp is not None:
                        curr_pnl = (ltp - entry_price) * LOT_SIZE
                        print(
                            f"[POS WATCH] {active_symbol} | LTP: ₹{ltp:.2f} | Entry:"
                            f" ₹{entry_price:.2f} | PnL: ₹{curr_pnl:+.2f} | SL:"
                            f" ₹{sl_price:.2f} | TGT: ₹{tgt_price:.2f}"
                        )

                        if ltp <= sl_price:
                            execute_order(
                                active_symbol,
                                active_token,
                                "SL HIT (-8%)",
                                LOT_SIZE,
                                price=ltp,
                            )
                            pos_active = False

                        elif ltp >= tgt_price:
                            execute_order(
                                active_symbol,
                                active_token,
                                "TARGET HIT (+16%)",
                                LOT_SIZE,
                                price=ltp,
                            )
                            pos_active = False
                    else:
                        print(
                            f">>> Syncing Live WebSocket LTP for {active_symbol}..."
                        )

                # 3. ENTRY TRIGGER
                elif not pos_active and is_new_entry_allowed():
                    if signal in ["CE", "PE"]:
                        sym, tok = get_itm_symbol_and_token(close, signal)
                        if sym and tok:
                            subscribe_token(tok, exchange_type=2)

                            # Wait for WebSocket ticks
                            ltp = None
                            for _ in range(10):
                                time.sleep(0.5)
                                ltp = get_live_ltp(tok)
                                if ltp is not None:
                                    break

                            if ltp is not None:
                                trade_type = signal
                                entry_price = ltp
                                sl_price = round(
                                    entry_price * (1 - SL_PCT), 2
                                )
                                tgt_price = round(
                                    entry_price * (1 + TARGET_PCT), 2
                                )
                                active_symbol, active_token, pos_active = (
                                    sym,
                                    tok,
                                    True,
                                )

                                execute_order(
                                    sym, tok, "BUY", LOT_SIZE, price=entry_price
                                )
                            else:
                                print(
                                    f">>> Couldn't fetch real-time LTP for {sym}. Skipping"
                                    " entry."
                                )

        else:
            print(
                f"{datetime.now().strftime('%H:%M:%S')} - Outside Market Hours."
                " Waiting for Market Open (09:15 AM)..."
            )

        time.sleep(10)

    except Exception as e:
        print("Loop Exception:", e)
        time.sleep(5)
