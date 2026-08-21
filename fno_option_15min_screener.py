# F&O OPTION-DIRECTION 15-MINUTE SCREENER (OPTIMIZED)
import concurrent.futures
from datetime import datetime, time as dtime, timedelta
import os
import time

from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

load_dotenv()
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BULL_RSI = 60.0
BEAR_RSI = 40.0
MIN_VOL_RATIO = 1.5
MAX_GAP_PCT = 1.2
MAX_WORKERS = 3         # Optimized worker count
REQUEST_DELAY = 0.35    # Safe tuned delay (~3 req/sec limit)
IST = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()


def is_market_open():
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 25)


def fetch_dynamic_fno_universe():
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            df = pd.DataFrame(r.json())
            needed = {"exch_seg", "name", "symbol", "token"}
            if not needed.issubset(df.columns):
                continue
            nfo_names = set(df.loc[df["exch_seg"].eq("NFO"), "name"].dropna().astype(str))
            eq = df[
                df["exch_seg"].eq("NSE")
                & df["symbol"].astype(str).str.endswith("-EQ")
                & df["name"].astype(str).isin(nfo_names)
            ].drop_duplicates("symbol")
            return [{"symbol": str(r.symbol), "token": str(r.token)} for r in eq.itertuples()]
        except Exception:
            continue
    return []


def send_telegram_alert(message, bypass_time_check=False, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not bypass_time_check and datetime.now(IST).time() > dtime(15, 25):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(max_retries):
        try:
            r = SESSION.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
                timeout=8,
            )
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1 + attempt)


def initialize_smartapi():
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
        print("Missing SmartAPI credentials.")
        return None
    try:
        api = SmartConnect(api_key=API_KEY)
        data = api.generateSession(CLIENT_CODE, PIN, pyotp.TOTP(TOTP_SECRET).now())
        if data and data.get("status"):
            return api
    except Exception as exc:
        print("Authentication failed:", exc)
    return None


def fetch_15min_data(api, token, days=10):
    """Reduced days to 10 (~250 candles) for fast and reliable 200 EMA calculation without API timeout."""
    now = datetime.now(IST)
    payload = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        res = api.getCandleData(payload)
        if not res or not res.get("status") or not res.get("data"):
            return None
        df = pd.DataFrame(res["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna().sort_values("timestamp")
    except Exception:
        return None


def evaluate_realtime_setup(api, stock):
    time.sleep(REQUEST_DELAY)
    df = fetch_15min_data(api, stock["token"])
    if df is None or len(df) < 205:
        return None

    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    df["roc"] = ta.momentum.roc(df["close"], 12)
    df["ema_200"] = ta.trend.ema_indicator(df["close"], 200)
    df["vol_sma"] = df["volume"].rolling(20).mean()
    df["session_date"] = df["timestamp"].dt.date
    df = df.dropna(subset=["rsi", "roc", "ema_200", "vol_sma"])
    
    if len(df) < 6:
        return None

    curr = df.iloc[-1]
    today_rows = df[df["session_date"] == curr["session_date"]]
    previous_day_rows = df[df["session_date"] < curr["session_date"]]

    if today_rows.empty or previous_day_rows.empty:
        return None

    # Opening gap calculation
    today_open = float(today_rows.iloc[0]["open"])
    previous_close = float(previous_day_rows.iloc[-1]["close"])
    gap_percent = (today_open - previous_close) / previous_close * 100

    vol_sma_val = float(curr["vol_sma"]) if pd.notna(curr["vol_sma"]) and float(curr["vol_sma"]) > 0 else 1.0
    vol_ratio = float(curr["volume"]) / vol_sma_val
    
    recent_high = float(df["high"].iloc[-5:-1].max())
    recent_low = float(df["low"].iloc[-5:-1].min())

    bullish = (
        curr["close"] > curr["ema_200"]
        and curr["rsi"] >= BULL_RSI
        and curr["roc"] > 0
        and vol_ratio >= MIN_VOL_RATIO
        and curr["close"] > recent_high
    )
    bearish = (
        curr["close"] < curr["ema_200"]
        and curr["rsi"] <= BEAR_RSI
        and curr["roc"] < 0
        and vol_ratio >= MIN_VOL_RATIO
        and curr["close"] < recent_low
    )

    if abs(gap_percent) > MAX_GAP_PCT or not (bullish or bearish):
        return None

    entry = float(curr["close"])
    stop = float(curr["low"] if bullish else curr["high"])
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    target = entry + risk * 2 if bullish else entry - risk * 2
    return {
        "Symbol": stock["symbol"],
        "Live_LTP": round(entry, 2),
        "Gap_%": round(gap_percent, 2),
        "RSI_15M": round(float(curr["rsi"]), 2),
        "ROC_15M": round(float(curr["roc"]), 2),
        "VolRatio": round(vol_ratio, 2),
        "SL_Price": round(stop, 2),
        "Target_Price": round(target, 2),
        "Option_Action": "BUY CALL (CE)" if bullish else "BUY PUT (PE)",
    }


def main():
    if not is_market_open():
        print("Market closed.")
        return
    api = initialize_smartapi()
    if not api:
        return
    universe = fetch_dynamic_fno_universe()
    if not universe:
        return

    print(f"Scanning {len(universe)} F&O stocks for 15M Option Direction setups...")
    matches = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(evaluate_realtime_setup, api, s) for s in universe]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    matches.append(result)
            except Exception:
                pass

    if matches:
        print(pd.DataFrame(matches).to_string(index=False))
        for item in matches:
            send_telegram_alert(
                f"📁 <b>FILE: fno_option_15min_screener.py</b>\n"
                f"🎯 <b>OPTION DIRECTION SIGNAL</b>\n\n"
                f"<b>Stock:</b> {item['Symbol']}\n"
                f"<b>Action:</b> {item['Option_Action']}\n"
                f"<b>LTP:</b> ₹{item['Live_LTP']}\n"
                f"<b>Gap:</b> {item['Gap_%']}%\n"
                f"<b>Volume:</b> {item['VolRatio']}x\n"
                f"<b>SL:</b> ₹{item['SL_Price']} | <b>Target 1:2:</b> ₹{item['Target_Price']}"
            )
    else:
        send_telegram_alert("📊 Option-direction scanner completed. No strict setup matched.", bypass_time_check=True)


if __name__ == "__main__":
    main()
