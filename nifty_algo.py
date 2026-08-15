from datetime import datetime, time as dtime
import json
import threading
import time
import pandas as pd
import pyotp
import requests
import ta
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# ==========================================
# 1. CREDENTIALS, TELEGRAM & RISK PARAMETERS
# ==========================================
API_KEY = "N7XNbnkE"
CLIENT_CODE = "S885143"
PIN = "1989"
TOTP_SECRET = "ZH76UOCDHM4TITQGDKN32HBZEI"

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # BotFather se mila token daalein
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"      # Apni Telegram Chat ID daalein

SL_PCT = 0.045
TARGET_PCT = 0.125
LOT_SIZE = 65
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

scrip_master_df = None
auth_token = ""
feed_token = ""

last_candle_minute = -1
cached_close = None
cached_signal = "NO_TRADE"

live_ltp_dict = {}
sws = None


# ==========================================
# TELEGRAM NOTIFICATION FUNCTION
# ==========================================
def send_telegram_alert(message):
    """Telegram par immediate alert bhejne ke liye function"""
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        print(">>> Telegram Alert Error:", e)


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


# ==========================================
# 3. WEBSOCKET HANDLERS
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
# 4. OPTION CHAIN & MULTI-TIMEFRAME HELPERS
# ==========================================
def validate_trade_with_option_chain(signal, current_price, pcr, resistance_strike, support_strike):
    """
    Option Chain Filter: Rejects trades near key boundaries or against PCR sentiment.
    """
    buffer_points = 40  # Nifty buffer zone

    if signal == "BUY":
        if pcr < 0.6:
            print(f"❌ TRADE REJECTED: 5-Min BUY Signal ignored because PCR is too Bearish ({pcr:.2f})")
            return False

        if (resistance_strike - current_price) <= buffer_points and current_price < resistance_strike:
            print(f"❌ TRADE REJECTED: Price ({current_price}) is too close to Resistance ({resistance_strike})")
            return False

        print("✅ OPTION CHAIN PASSED: Buy trade cleared for execution.")
        return True

    elif signal == "SELL":
        if pcr > 1.4:
            print(f"❌ TRADE REJECTED: 5-Min SELL Signal ignored because PCR is too Bullish ({pcr:.2f})")
            return False

        if (current_price - support_strike) <= buffer_points and current_price > support_strike:
            print(f"❌ TRADE REJECTED: Price ({current_price}) is too close to Support ({support_strike})")
            return False

        print("✅ OPTION CHAIN PASSED: Sell trade cleared for execution.")
        return True

    return False


def fetch_option_chain_data(spot_price):
    """Fetches Option Chain Open Interest (OI) metrics around ATM strike"""
    try:
        atm = round(spot_price / 50) * 50
        cols = {c.lower(): c for c in scrip_master_df.columns}
        
        filtered = scrip_master_df[
            (scrip_master_df[cols.get("name")].astype(str).str.upper() == "NIFTY") &
            (scrip_master_df[cols.get("instrumenttype")].astype(str).str.upper() == "OPTIDX")
        ].copy()

        filtered["strike_val"] = filtered[cols.get("strike")].astype(float) / 100.0
        
        # Current weekly expiry search
        filtered["expiry_dt"] = pd.to_datetime(filtered[cols.get("expiry")], format="%d%b%Y", errors="coerce")
        today = pd.to_datetime(datetime.now().date())
        valid = filtered[filtered["expiry_dt"] >= today].sort_values("expiry_dt")
        
        if valid.empty:
            return None

        nearest_expiry = valid.iloc[0]["expiry_dt"]
        chain_df = valid[valid["expiry_dt"] == nearest_expiry]
        
        # Dynamic strike range filtering (+/- 500 points)
        strike_range = chain_df[(chain_df["strike_val"] >= atm - 500) & (chain_df["strike_val"] <= atm + 500)].copy()

        option_chain_list = []
        for idx, row in strike_range.iterrows():
            sym = str(row[cols.get("symbol")])
            tok = str(row[cols.get("token")])
            stk = float(row["strike_val"])
            opt_type = "CE" if sym.endswith("CE") else "PE"

            # Fetch Market Depth for Open Interest
            oi = 1000  # Default fallback value
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
    """Fetches 15-Minute Candle Data to verify 200 EMA Trend"""
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
            (
                scrip_master_df[name_col].astype(str).str.upper() == "NIFTY"
            )
            & (
                scrip_master_df[inst_col].astype(str).str.upper()
                == "OPTIDX"
            )
            & (
                scrip_master_df[strike_col].astype(float)
                == float(target_strike * 100)
            )
            & (
                scrip_master_df[sym_col].astype(str).str.endswith(option_type)
            )
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
            df = pd.DataFrame(
                resp["data"],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
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

            # --- VIX & GAP Filters ---
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

            # --- STEP 1: 15-Min Trend Alignment ---
            trend_15m = get_15m_trend()
            if (raw_signal == "CE" and trend_15m != "BULLISH") or (raw_signal == "PE" and trend_15m != "BEARISH"):
                print(f"\n⚠️ SIGNAL IGNORED: 5m Signal ({raw_signal}) does not match 15m Trend ({trend_15m}).")
                cached_close, cached_signal = curr["close"], "NO_TRADE"
                return cached_close, cached_signal

            # --- STEP 2: Option Chain Safety Filter ---
            oc_data = fetch_option_chain_data(curr["close"])
            if oc_data:
                df_oc = pd.DataFrame(oc_data)
                total_put_oi = df_oc[df_oc['option_type'] == 'PE']['open_interest'].sum()
                total_call_oi = df_oc[df_oc['option_type'] == 'CE']['open_interest'].sum()
                pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

                res_strike = df_oc[df_oc['option_type'] == 'CE'].loc[df_oc[df_oc['option_type'] == 'CE']['open_interest'].idxmax()]['strike']
                sup_strike = df_oc[df_oc['option_type'] == 'PE'].loc[df_oc[df_oc['option_type'] == 'PE']['open_interest'].idxmax()]['strike']

                signal_type = "BUY" if raw_signal == "CE" else "SELL"
                is_safe = validate_trade_with_option_chain(
                    signal=signal_type,
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
        print(
            f"\n[ORDER EXECUTED] {buy_sell} | ID: {order_id} | Symbol: {symbol} | Qty: {quantity}"
        )
        return order_id
    except Exception as e:
        print(f">>> Order Execution Error ({buy_sell}):", e)
        return None


# ==========================================
# 5. MAIN LOOP
# ==========================================
print(">>> Nifty Algo Bot Active (Production Ready + Multi-Timeframe + Option Chain Filter)...")
send_telegram_alert("🤖 <b>Nifty Algo Bot Activated!</b>\nMulti-Timeframe Trend & Option Chain Filter Enabled.")

while True:
    try:
        if is_market_open():
            if daily_trades_count >= MAX_DAILY_TRADES:
                print(
                    f"\r🛑 Max Daily Limit ({MAX_DAILY_TRADES} Trades) Reached. Bot Paused.",
                    end="",
                )
                time.sleep(15)
                continue

            if consecutive_sl_count >= 2:
                print("\r🛑 2 Consecutive Stop Loss Hit! Protection Active.", end="")
                time.sleep(15)
                continue

            close, signal = fetch_signals_and_data()

            if close is not None:
                if is_squareoff_time() and pos_active:
                    ltp = get_live_ltp(active_token, active_symbol) or entry_price
                    print(
                        f"\n[AUTO SQUARE-OFF 03:10 PM] Closing position for {active_symbol} at ₹{ltp:.2f}"
                    )
                    place_order(active_symbol, active_token, "SELL", LOT_SIZE)
                    
                    send_telegram_alert(
                        f"⏰ <b>AUTO SQUARE-OFF (03:10 PM)</b>\n"
                        f"<b>Symbol:</b> {active_symbol}\n"
                        f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                        f"<b>Entry Price:</b> ₹{entry_price:.2f}"
                    )
                    pos_active = False
                    daily_trades_count += 1

                elif pos_active:
                    ltp = get_live_ltp(active_token, active_symbol)
                    holding_time_mins = (
                        datetime.now() - trade_entry_time
                    ).total_seconds() / 60.0

                    if ltp is not None:
                        print(
                            f"[POS ACTIVE] {active_symbol} | LTP: ₹{ltp:.2f} | SL: ₹{sl_price:.2f} | TGT: ₹{tgt_price:.2f} | Time: {holding_time_mins:.1f}m/{MAX_HOLDING_MINUTES}m",
                            end="\r",
                        )

                        if ltp <= sl_price:
                            print(
                                f"\n[EXIT SL - 4.5%] SL Hit for {active_symbol} at ₹{ltp:.2f}"
                            )
                            place_order(active_symbol, active_token, "SELL", LOT_SIZE)
                            
                            send_telegram_alert(
                                f"🔴 <b>STOP LOSS HIT (-4.5%)</b>\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}"
                            )
                            pos_active = False
                            daily_trades_count += 1
                            consecutive_sl_count += 1

                        elif ltp >= tgt_price:
                            print(
                                f"\n[EXIT TARGET - 12.5%] Target Hit for {active_symbol} at ₹{ltp:.2f}"
                            )
                            place_order(active_symbol, active_token, "SELL", LOT_SIZE)
                            
                            send_telegram_alert(
                                f"🟢 <b>TARGET ACHIEVED (+12.5%)</b> 🎉\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}"
                            )
                            pos_active = False
                            daily_trades_count += 1
                            consecutive_sl_count = 0

                        elif holding_time_mins >= MAX_HOLDING_MINUTES:
                            print(
                                f"\n[EXIT THETA TIMEOUT] Auto-Exiting {active_symbol} at ₹{ltp:.2f}"
                            )
                            place_order(active_symbol, active_token, "SELL", LOT_SIZE)
                            
                            send_telegram_alert(
                                f"⏳ <b>THETA TIMEOUT EXIT (22 Mins)</b>\n"
                                f"<b>Symbol:</b> {active_symbol}\n"
                                f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                                f"<b>Entry Price:</b> ₹{entry_price:.2f}"
                            )
                            pos_active = False
                            daily_trades_count += 1
                            if ltp < entry_price:
                                consecutive_sl_count += 1
                            else:
                                consecutive_sl_count = 0
                    else:
                        print(
                            f">>> Live Price Monitoring for {active_symbol}...",
                            end="\r",
                        )

                elif not pos_active and is_new_entry_allowed():
                    if signal in ["CE", "PE"]:
                        sym, tok = get_itm_symbol_and_token(close, signal)
                        if sym and tok:
                            order_id = place_order(sym, tok, "BUY", LOT_SIZE)
                            if order_id:
                                setup_and_subscribe_websocket(tok, exchange_type=2)
                                time.sleep(2)
                                entry_price = get_live_ltp(tok, sym) or 150.0
                                sl_price = round(entry_price * (1 - SL_PCT), 2)
                                tgt_price = round(entry_price * (1 + TARGET_PCT), 2)

                                active_symbol, active_token, pos_active = (
                                    sym,
                                    tok,
                                    True,
                                )
                                trade_entry_time = datetime.now()
                                print(
                                    f"\n[ENTRY {signal} TRIGGERED] {sym} @ ₹{entry_price:.2f} | SL: ₹{sl_price:.2f} | TGT: ₹{tgt_price:.2f}"
                                )
                                
                                send_telegram_alert(
                                    f"🚀 <b>NEW ENTRY TRIGGERED ({signal})</b>\n"
                                    f"<b>Symbol:</b> {sym}\n"
                                    f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                    f"<b>Stop Loss:</b> ₹{sl_price:.2f} (-4.5%)\n"
                                    f"<b>Target:</b> ₹{tgt_price:.2f} (+12.5%)\n"
                                    f"<b>Qty:</b> {LOT_SIZE}"
                                )
                    else:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring Nifty Spot: ₹{close:.2f} | Signal: {signal}",
                            end="\r",
                        )

        else:
            print(
                f"{datetime.now().strftime('%H:%M:%S')} - Outside Market Hours... Waiting.",
                end="\r",
            )

        time.sleep(10)

    except Exception as e:
        time.sleep(10)