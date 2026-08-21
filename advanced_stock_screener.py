# ADVANCED F&O 5-MINUTE MOMENTUM SCREENER (OPTIMIZED)
import concurrent.futures
from datetime import datetime, time as dtime, timedelta
import os
import sys
import time

import pandas as pd
import pyotp
import pytz
import requests
import ta
from dotenv import load_dotenv
from SmartApi import SmartConnect

load_dotenv()
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_RSI = 60.0
MIN_ROC = 0.0
MIN_VOL_SURGE_RATIO = 1.2
TIMEFRAME = "FIVE_MINUTE"
MAX_WORKERS = 3         # Optimized for SmartAPI limits
REQUEST_DELAY = 0.35    # Strictly tuned to avoid HTTP 429 while maxing speed
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
            required = {"exch_seg", "name", "symbol", "token"}
            if not required.issubset(df.columns):
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
    now = datetime.now(IST).time()
    if not bypass_time_check and now > dtime(15, 30):
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        print("Missing SmartAPI environment variables.")
        return None
    try:
        api = SmartConnect(api_key=API_KEY)
        data = api.generateSession(CLIENT_CODE, PIN, pyotp.TOTP(TOTP_SECRET).now())
        return api if data and data.get("status") else None
    except Exception as exc:
        print("Authentication failed:", exc)
        return None


def fetch_candle_data(api, token, interval=TIMEFRAME, days=6):
    """Days limit set to 6 days (~450 candles) for reliable 200 EMA calculation without API timeout."""
    now = datetime.now(IST)
    payload = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        res = api.getCandleData(payload)
        if not res or not res.get("status") or not res.get("data"):
            return None
        df = pd.DataFrame(res["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna()
    except Exception:
        return None


def evaluate_stock_setup(api, stock):
    time.sleep(REQUEST_DELAY)
    df = fetch_candle_data(api, stock["token"])
    if df is None or len(df) < 205:
        return None

    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["roc"] = ta.momentum.roc(df["close"], window=9)
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["ema_200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["vol_sma21"] = df["volume"].rolling(21).mean()
    df = df.dropna()
    
    if len(df) < 2:
        return None

    curr, prev = df.iloc[-1], df.iloc[-2]
    
    vol_sma_val = curr["vol_sma21"] if pd.notna(curr["vol_sma21"]) and curr["vol_sma21"] > 0 else 1.0
    vol_ratio = curr["volume"] / vol_sma_val
    candle_change = ((curr["close"] - prev["close"]) / prev["close"]) * 100

    passed = (
        curr["close"] > curr["ema_200"]
        and curr["rsi"] >= MIN_RSI
        and curr["roc"] > MIN_ROC
        and vol_ratio >= MIN_VOL_SURGE_RATIO
        and curr["ema_9"] > curr["ema_21"]
    )
    if not passed:
        return None

    return {
        "Symbol": stock["symbol"],
        "LTP": round(float(curr["close"]), 2),
        "Change%": round(float(candle_change), 2),
        "RSI(14)": round(float(curr["rsi"]), 2),
        "VolRatio": round(float(vol_ratio), 2),
        "Signal": "BUY / LONG MOMENTUM",
    }


def main():
    if not is_market_open():
        print("Market closed. Scanner execution stopped.")
        return
        
    api = initialize_smartapi()
    if not api:
        print("SmartAPI login failed.")
        return
        
    stocks = fetch_dynamic_fno_universe()
    if not stocks:
        print("Failed to fetch F&O stock universe.")
        return

    print(f"Scanning {len(stocks)} F&O stocks for 5M Momentum setups...")
    matches = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(evaluate_stock_setup, api, s) for s in stocks]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    matches.append(result)
            except Exception:
                pass

    if matches:
        for item in matches:
            send_telegram_alert(
                f"📁 <b>FILE: advanced_stock_screener.py</b>\n"
                f"🚀 <b>5M MOMENTUM ALERT</b>\n\n"
                f"<b>Symbol:</b> {item['Symbol']}\n"
                f"<b>LTP:</b> ₹{item['LTP']}\n"
                f"<b>RSI:</b> {item['RSI(14)']}\n"
                f"<b>Volume:</b> {item['VolRatio']}x\n"
                f"<b>Signal:</b> {item['Signal']}"
            )
    else:
        send_telegram_alert(
            f"📊 Scanned {len(stocks)} F&O stocks. No setup matched.",
            bypass_time_check=True
        )


if __name__ == "__main__":
    main()
