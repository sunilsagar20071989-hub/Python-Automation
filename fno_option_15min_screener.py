import concurrent.futures
from datetime import datetime, time as dtime, timedelta
import os
import sys
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# 1. SECURE CONFIGURATION & CREDENTIALS
# ==========================================
load_dotenv()

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
    print(f"❌ Error: Missing environment credentials: {', '.join(missing_vars)}")
    sys.exit(1)

# STRICT INTRADAY PARAMETERS
BULL_RSI = 60.0       # Call Option Trigger
BEAR_RSI = 40.0       # Put Option Trigger
MIN_VOL_RATIO = 1.5   # Current volume >= 1.5x of 20 SMA volume
MAX_GAP_PCT = 1.2     # Filter out extreme gap openings
MAX_WORKERS = 10      # Multi-threading concurrency worker limit


# ==========================================
# 2. MARKET HOURS FILTER
# ==========================================
def is_market_open():
    """Validates execution timeframe between 09:15 AM to 03:25 PM IST."""
    IST = pytz.timezone("Asia/Kolkata")
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 25)


# ==========================================
# 3. DYNAMIC F&O UNIVERSE FETCH
# ==========================================
def fetch_dynamic_fno_universe():
    print(">>> Downloading Angel One Master Instrument File...")
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    headers = {"User-Agent": "Mozilla/5.0"}

    for scrip_url in urls:
        try:
            resp = requests.get(scrip_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                df_master = pd.DataFrame(data)

                cols = {c.lower(): c for c in df_master.columns}
                exch_col = cols.get("exch_seg", "exch_seg")
                name_col = cols.get("name", "name")
                sym_col = cols.get("symbol", "symbol")
                token_col = cols.get("token", "token")

                nfo_df = df_master[df_master[exch_col] == "NFO"]
                fno_symbols = set(nfo_df[name_col].dropna().unique())

                nse_eq_df = df_master[
                    (df_master[exch_col] == "NSE")
                    & (df_master[sym_col].astype(str).str.endswith("-EQ"))
                    & (df_master[name_col].isin(fno_symbols))
                ]

                fno_list = []
                for _, row in nse_eq_df.iterrows():
                    fno_list.append({
                        "symbol": str(row[sym_col]),
                        "token": str(row[token_col]),
                    })

                print(f">>> Successfully loaded {len(fno_list)} F&O stocks dynamically!\n")
                return fno_list
        except Exception:
            continue

    print(">>> Dynamic Universe Fetch Failed across all endpoints.")
    return []


# ==========================================
# 4. TELEGRAM NOTIFICATION ENGINE
# ==========================================
def send_telegram_alert(message, bypass_time_check=False, max_retries=3):
    """Sends HTML formatted Telegram alerts with cutoff override capacity."""
    IST = pytz.timezone("Asia/Kolkata")
    current_time = datetime.now(IST).time()

    if not bypass_time_check and current_time > dtime(15, 25):
        print(f"Alert Skipped: Current time ({current_time.strftime('%H:%M')}) past cutoff.")
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
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"Telegram Alert Error: {e}")
            time.sleep(1)


# ==========================================
# 5. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get("status"):
            raw_token = data["data"]["jwtToken"]
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            print("\n" + "=" * 95)
            print(">>> SmartAPI Connected! Dynamic 15-Min 200 EMA High-Conviction Scanner Active...")
            print("=" * 95 + "\n")
            return smart_api, auth_token
        else:
            print("❌ Login Failed:", data.get("message") if data else "No response")
    except Exception as e:
        print(">>> Authentication Exception:", e)
    return None, None


# ==========================================
# 6. DATA FETCHING & EVALUATION
# ==========================================
def fetch_live_15min_data(smart_api, auth_token, token):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": API_KEY,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "FE:80:00:00:00:00",
        "Authorization": auth_token,
    }

    now = datetime.now()
    from_date = (now - timedelta(days=20)).strftime("%Y-%m-%d 09:15")
    to_date = now.strftime("%Y-%m-%d %H:%M")

    payload = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        resp_json = resp.json()

        if resp_json.get("status") and resp_json.get("data") and len(resp_json["data"]) > 0:
            candle_data = resp_json["data"]
        else:
            sdk_resp = smart_api.getCandleData(payload)
            candle_data = sdk_resp.get("data", []) if sdk_resp and sdk_resp.get("status") else []

        if candle_data:
            df = pd.DataFrame(
                candle_data,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["open"] = df["open"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)
            df["date"] = pd.to_datetime(df["timestamp"]).dt.date
            return df
    except Exception:
        pass
    return None


def evaluate_realtime_setup(smart_api, auth_token, stock):
    symbol = stock["symbol"]
    token = stock["token"]

    df = fetch_live_15min_data(smart_api, auth_token, token)
    if df is None or len(df) < 50:
        return None, None

    # Dynamic EMA Windowing
    ema_window = 200 if len(df) >= 200 else len(df) - 1

    # TECHNICAL INDICATORS CALCULATION
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["roc"] = ta.momentum.roc(df["close"], window=12)
    df["ema_200"] = ta.trend.ema_indicator(df["close"], window=ema_window)
    df["vol_sma"] = df["volume"].rolling(window=20).mean()

    curr = df.iloc[-1]

    # Gap Calculation
    dates = df["date"].unique()
    if len(dates) >= 2:
        prev_date = dates[-2]
        prev_day_close = df[df["date"] == prev_date].iloc[-1]["close"]
    else:
        prev_day_close = df.iloc[0]["close"]

    gap_percent = ((curr["close"] - prev_day_close) / prev_day_close) * 100

    # Volume Ratio Guard
    vol_sma_val = curr["vol_sma"] if pd.notna(curr["vol_sma"]) and curr["vol_sma"] > 0 else 1.0
    vol_ratio = curr["volume"] / vol_sma_val
    vol_pass = vol_ratio >= MIN_VOL_RATIO

    # Range Breakout Check
    recent_4_high = df["high"].iloc[-5:-1].max() if len(df) >= 5 else curr["high"]
    recent_4_low = df["low"].iloc[-5:-1].min() if len(df) >= 5 else curr["low"]

    is_bullish_breakout = curr["close"] > recent_4_high
    is_bearish_breakout = curr["close"] < recent_4_low

    rsi_val = curr["rsi"] if pd.notna(curr["rsi"]) else 50.0
    roc_val = curr["roc"] if pd.notna(curr["roc"]) else 0.0
    ema_200_val = curr["ema_200"] if pd.notna(curr["ema_200"]) else curr["close"]

    # STRICT STRATEGY EVALUATION
    is_bullish = (
        (curr["close"] > ema_200_val)
        and (rsi_val >= BULL_RSI)
        and (roc_val > 0.0)
        and vol_pass
        and is_bullish_breakout
    )

    is_bearish = (
        (curr["close"] < ema_200_val)
        and (rsi_val <= BEAR_RSI)
        and (roc_val < 0.0)
        and vol_pass
        and is_bearish_breakout
    )

    stop_loss = round(curr["low"], 2) if is_bullish else round(curr["high"], 2)
    risk = abs(curr["close"] - stop_loss)
    target = (
        round(curr["close"] + (risk * 2.0), 2)
        if is_bullish
        else round(curr["close"] - (risk * 2.0), 2)
    )

    status_data = {
        "Symbol": symbol,
        "Live_LTP": round(curr["close"], 2),
        "Gap_%": round(gap_percent, 2),
        "RSI_15M": round(rsi_val, 2),
        "ROC_15M": round(roc_val, 2),
        "VolRatio": round(vol_ratio, 2),
        "SL_Price": stop_loss,
        "Target_Price": target,
    }

    qualified = None
    if is_bullish and abs(gap_percent) <= MAX_GAP_PCT:
        qualified = status_data.copy()
        qualified["Option_Action"] = "🔥 BUY CALL (CE)"
    elif is_bearish and abs(gap_percent) <= MAX_GAP_PCT:
        qualified = status_data.copy()
        qualified["Option_Action"] = "🔻 BUY PUT (PE)"

    return status_data, qualified


# ==========================================
# 7. MAIN PIPELINE
# ==========================================
def main():
    if not is_market_open():
        print(">>> Market Closed! Execution blocked to prevent late post-market signals.")
        return

    smart_api, auth_token = initialize_smartapi()
    if not auth_token:
        return

    fno_universe = fetch_dynamic_fno_universe()
    if not fno_universe:
        print(">>> F&O Universe loading failed. Exiting.")
        return

    print(f">>> SCANNING {len(fno_universe)} STOCKS WITH MULTI-THREADING (10 WORKERS)...")
    matches = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(evaluate_realtime_setup, smart_api, auth_token, stock)
            for stock in fno_universe
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                _, match = future.result()
                if match:
                    matches.append(match)
            except Exception:
                continue

    print("\n" + "=" * 95)
    print("  🎯 LIVE HIGH CONVICTION OPTION TRADES (200 EMA FILTERED) 🎯")
    print("=" * 95)
    if matches:
        df_match = pd.DataFrame(matches)
        print(df_match.to_string(index=False))

        for item in matches:
            tg_msg = (
                f"🎯 <b>STRICT HIGH CONVICTION OPTION TRADE</b>\n\n"
                f"<b>Stock:</b> {item['Symbol']}\n"
                f"<b>Action:</b> {item['Option_Action']}\n"
                f"<b>LTP:</b> ₹{item['Live_LTP']}\n"
                f"<b>Vol Ratio:</b> {item['VolRatio']}x\n"
                f"<b>SL:</b> ₹{item['SL_Price']} | <b>Target (1:2):</b> ₹{item['Target_Price']}"
            )
            send_telegram_alert(tg_msg)
    else:
        print(">>> ZERO STOCKS MATCHED STRICT REAL-TIME BREAKOUT & 200 EMA FILTERS.")
        send_telegram_alert("📊 <b>Scanner Summary:</b> Execution completed. No stocks matched current strict options setup filters.", bypass_time_check=True)
    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
