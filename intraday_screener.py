# MULTI-TIMEFRAME DAILY + WEEKLY F&O SCREENER (OPTIMIZED)
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, time as dtime, timedelta
import logging
import os
import threading
import time

from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MultiTimeframeScreener")
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST = pytz.timezone("Asia/Kolkata")
MIN_DAILY_RSI = 60.0
MIN_DAILY_ROC = 1.0
MIN_WEEKLY_RSI = 55.0
MIN_DAILY_VOL_RATIO = 1.0
ENABLE_200_EMA_FILTER = True
MAX_WORKERS = 3         # Optimized worker count for SmartAPI
REQUEST_DELAY = 0.35    # Safe delay (~3 req/sec limit)
MASTER_FILE_LOCAL = "OpenAPIScripMaster.json"
SESSION = requests.Session()

alerted_stocks_today = set()
alert_lock = threading.Lock()
last_alert_reset_date = dt.now(IST).date()


def send_telegram_alert(message, bypass_time_check=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not bypass_time_check and datetime_now().time() > dtime(15, 30):
        return
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=8,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Telegram error: %s", exc)


def datetime_now():
    return dt.now(IST)


def initialize_smartapi():
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
        logger.error("Missing SmartAPI credentials.")
        return None
    try:
        api_inst = SmartConnect(api_key=API_KEY)
        data = api_inst.generateSession(CLIENT_CODE, PIN, pyotp.TOTP(TOTP_SECRET).now())
        if data and data.get("status"):
            return api_inst
    except Exception as exc:
        logger.error("Authentication failed: %s", exc)
    return None


def fetch_dynamic_fno_universe():
    urls = [
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ]
    if os.path.exists(MASTER_FILE_LOCAL):
        try:
            age = datetime_now().date() - dt.fromtimestamp(os.path.getmtime(MASTER_FILE_LOCAL), tz=IST).date()
            if age.days == 0:
                df = pd.read_json(MASTER_FILE_LOCAL)
                return _build_fno_list(df)
        except Exception:
            pass

    for url in urls:
        try:
            r = SESSION.get(url, timeout=30)
            r.raise_for_status()
            with open(MASTER_FILE_LOCAL, "wb") as f:
                f.write(r.content)
            return _build_fno_list(pd.DataFrame(r.json()))
        except Exception:
            continue
    return []


def _build_fno_list(df):
    required = {"exch_seg", "name", "symbol", "token"}
    if not required.issubset(df.columns):
        return []
    nfo_names = set(df.loc[df["exch_seg"].eq("NFO"), "name"].dropna().astype(str))
    eq = df[
        df["exch_seg"].eq("NSE")
        & df["symbol"].astype(str).str.endswith("-EQ")
        & df["name"].astype(str).isin(nfo_names)
    ].drop_duplicates("symbol")
    return [{"symbol": str(r.symbol), "token": str(r.token)} for r in eq.itertuples()]


def fetch_candle_data(api, token, interval, days):
    now = datetime_now()
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
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna().sort_values("timestamp")
    except Exception as exc:
        logger.warning("Candle fetch failed for %s: %s", token, exc)
        return None


def convert_daily_to_weekly(df_daily):
    df = df_daily.set_index("timestamp").sort_index()
    return df.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna().reset_index()


def analyze_stock_multi_tf(api, stock):
    global last_alert_reset_date
    today = datetime_now().date()
    
    with alert_lock:
        if today != last_alert_reset_date:
            alerted_stocks_today.clear()
            last_alert_reset_date = today

    time.sleep(REQUEST_DELAY)
    # Fetching ~365 days (~250 trading candles) is sufficient and fast
    daily = fetch_candle_data(api, stock["token"], "ONE_DAY", 365)
    if daily is None or len(daily) < 200:
        return None

    daily["rsi"] = ta.momentum.rsi(daily["close"], 14)
    daily["roc"] = ta.momentum.roc(daily["close"], 12)
    daily["ema_9"] = ta.trend.ema_indicator(daily["close"], 9)
    daily["ema_21"] = ta.trend.ema_indicator(daily["close"], 21)
    daily["ema_200"] = ta.trend.ema_indicator(daily["close"], 200)
    daily["vol_sma20"] = daily["volume"].rolling(20).mean()
    
    d = daily.iloc[-1]

    daily_pass = (
        d["rsi"] >= MIN_DAILY_RSI
        and d["roc"] >= MIN_DAILY_ROC
        and d["ema_9"] > d["ema_21"]
        and (d["close"] > d["ema_200"] if ENABLE_200_EMA_FILTER else True)
        and d["volume"] >= (d["vol_sma20"] * MIN_DAILY_VOL_RATIO)
    )
    if not daily_pass:
        return None

    weekly = convert_daily_to_weekly(daily)
    if len(weekly) < 23:
        return None
        
    weekly["rsi"] = ta.momentum.rsi(weekly["close"], 14)
    weekly["ema21"] = ta.trend.ema_indicator(weekly["close"], 21)
    
    # Use last completed weekly candle
    w = weekly.iloc[-2]

    if pd.isna(w["rsi"]) or pd.isna(w["ema21"]):
        return None
    if not (w["rsi"] >= MIN_WEEKLY_RSI and w["close"] > w["ema21"]):
        return None

    match = {
        "Symbol": stock["symbol"],
        "LTP": round(float(d["close"]), 2),
        "Daily_RSI": round(float(d["rsi"]), 2),
        "Weekly_RSI": round(float(w["rsi"]), 2),
        "Daily_ROC": round(float(d["roc"]), 2),
        "Signal": "DUAL-TF MOMENTUM BUY",
    }

    with alert_lock:
        if stock["symbol"] in alerted_stocks_today:
            return match
        alerted_stocks_today.add(stock["symbol"])

    send_telegram_alert(
        f"📁 <b>FILE: multi_timeframe_screener.py</b>\n"
        f"⚡ <b>DAILY + WEEKLY MOMENTUM</b>\n\n"
        f"<b>Stock:</b> {match['Symbol']}\n"
        f"<b>LTP:</b> ₹{match['LTP']}\n"
        f"<b>Daily RSI:</b> {match['Daily_RSI']}\n"
        f"<b>Weekly RSI:</b> {match['Weekly_RSI']}\n"
        f"<b>Daily ROC:</b> {match['Daily_ROC']}%\n"
        f"<b>Status:</b> Setup matched"
    )
    return match


def run_live_scanner():
    api = initialize_smartapi()
    if not api:
        return
    universe = fetch_dynamic_fno_universe()
    if not universe:
        logger.error("F&O universe empty.")
        return

    logger.info(f"Scanning {len(universe)} F&O stocks for Daily + Weekly momentum...")
    matches = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze_stock_multi_tf, api, s) for s in universe]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    matches.append(result)
            except Exception as exc:
                logger.warning("Worker failed: %s", exc)

    if matches:
        print(pd.DataFrame(matches).to_string(index=False))
    else:
        logger.info("No dual-timeframe matches.")


if __name__ == "__main__":
    now = datetime_now()
    if now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 15):
        run_live_scanner()
    else:
        logger.info("Outside live market window.")
