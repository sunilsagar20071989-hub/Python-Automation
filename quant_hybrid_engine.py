# DYNAMIC FULL-MARKET QUANT SCREENER (OPTIMIZED)
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, timedelta
import html
import logging
import os
import re
import time
from urllib.parse import quote

import pandas as pd
import pyotp
import pytz
import requests
import ta
from bs4 import BeautifulSoup
from SmartApi import SmartConnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FullMarketScreener")

API_KEY = os.getenv("SMARTAPI_API_KEY") or os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

MAX_STOCKS = int(os.getenv("FULL_MARKET_MAX_STOCKS", "0"))  # 0 = All NSE-EQ
FUNDAMENTALS_ENABLED = os.getenv("FUNDAMENTALS_ENABLED", "1") == "1"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))  # Safe concurrent worker pool


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i : i + 3500] for i in range(0, len(message), 3500)] or [""]
    for chunk in chunks:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        try:
            r = SESSION.post(url, data=payload, timeout=10)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Telegram error: %s", exc)


def fetch_nse_stock_universe():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        
        required = {"exch_seg", "symbol", "token"}
        if not required.issubset(df.columns):
            raise ValueError(f"Master file missing columns: {required - set(df.columns)}")
            
        eq = df[
            (df["exch_seg"].eq("NSE"))
            & df["symbol"].astype(str).str.endswith("-EQ")
            & ~df["symbol"].astype(str).str.contains(r"-(BE|BZ|SM|ST)$", regex=True)
        ].copy()
        
        eq = eq.drop_duplicates(subset=["symbol"]).sort_values("symbol")
        
        if MAX_STOCKS > 0:
            eq = eq.head(MAX_STOCKS)
            
        result = [{"symbol": str(r.symbol), "token": str(r.token)} for r in eq.itertuples()]
        logger.info("Loaded %d NSE-EQ stocks into scan universe.", len(result))
        return result
    except Exception as exc:
        logger.error("NSE universe fetch failed: %s", exc)
        return []


def _number(text):
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def fetch_screener_fundamentals(symbol):
    if not FUNDAMENTALS_ENABLED:
        return True, {"ROE": "DISABLED", "ROCE": "DISABLED", "PE": "DISABLED"}

    clean = symbol.removesuffix("-EQ")
    urls = [
        f"https://www.screener.in/company/{quote(clean, safe='')}/consolidated/",
        f"https://www.screener.in/company/{quote(clean, safe='')}/",
    ]
    try:
        time.sleep(0.4)
        for url in urls:
            r = SESSION.get(url, timeout=6)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            data = {}
            for li in soup.find_all("li"):
                name = li.find("span", class_="name")
                value = li.find("span", class_="number")
                if name and value:
                    data[name.get_text(" ", strip=True).lower()] = _number(value.get_text(" ", strip=True))

            roe = data.get("return on equity")
            roce = data.get("return on capital employed")
            pe = data.get("stock p/e")
            
            if roe is None or roce is None or pe is None:
                continue
                
            passed = roe >= 10.0 and roce >= 10.0 and 0 < pe <= 75.0
            return passed, {"ROE": roe, "ROCE": roce, "PE": pe}
            
    except Exception as exc:
        logger.warning("Fundamental lookup failed for %s: %s", symbol, exc)

    return False, {"ROE": "N/A", "ROCE": "N/A", "PE": "N/A"}


def initialize_smartapi():
    if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
        logger.error("Missing SmartAPI environment credentials.")
        return None
    try:
        api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = api.generateSession(CLIENT_CODE, PIN, totp)
        if data and data.get("status"):
            logger.info("SmartAPI session successfully active.")
            return api
        logger.error("SmartAPI login failed: %s", data)
    except Exception as exc:
        logger.error("SmartAPI authentication error: %s", exc)
    return None


def fetch_historical_candles(api, token, interval="FIFTEEN_MINUTE", days=10):
    now = dt.now(IST)
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
            
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        return df
    except Exception as exc:
        logger.warning("Candle fetch error for token %s: %s", token, exc)
        return None


def analyze_stock(stock_info, api):
    symbol = stock_info["symbol"]
    token = stock_info["token"]
    
    df = fetch_historical_candles(api, token)
    if df is None or len(df) < 50:
        return None

    if df["volume"].tail(20).mean() < 1000:
        return None

    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["roc"] = ta.momentum.roc(df["close"], window=12)
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    df = df.dropna()
    if len(df) < 2:
        return None

    c, p = df.iloc[-1], df.iloc[-2]
    setups = []
    
    vol_sma_val = float(c["vol_sma"]) if pd.notna(c["vol_sma"]) and float(c["vol_sma"]) > 0 else 1.0

    if c["close"] > p["high"] and c["volume"] >= 1.5 * vol_sma_val and c["rsi"] >= 60.0:
        setups.append("15M Vol Breakout")
    if p["ema_9"] <= p["ema_21"] and c["ema_9"] > c["ema_21"]:
        setups.append("EMA Crossover")
        
    if not setups:
        return None

    fund_pass, fund = fetch_screener_fundamentals(symbol)
    if not fund_pass:
        return None

    return {
        "Symbol": symbol,
        "LTP": round(float(c["close"]), 2),
        "Setups": ", ".join(setups),
        "RSI": round(float(c["rsi"]), 1),
        "ROC": round(float(c["roc"]), 1),
        "ROE": fund["ROE"],
        "PE": fund["PE"],
    }


def execute_master_scan():
    api = initialize_smartapi()
    if not api:
        return
        
    universe = fetch_nse_stock_universe()
    if not universe:
        return

    matches = []
    total = len(universe)
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_stock = {executor.submit(analyze_stock, stock, api): stock for stock in universe}
        for future in as_completed(future_to_stock):
            try:
                result = future.result()
                if result:
                    matches.append(result)
            except Exception as exc:
                logger.error("Scan error: %s", exc)

    now_str = dt.now(IST).strftime("%I:%M %p")
    if matches:
        lines = [f"🔍 <b>FULL MARKET QUANT SCANNER ({html.escape(now_str)})</b>", ""]
        for m in matches:
            lines.append(
                f"<b>Stock:</b> {html.escape(m['Symbol'])} | <b>LTP:</b> ₹{m['LTP']}\n"
                f"<b>Signal:</b> {html.escape(m['Setups'])}\n"
                f"<b>RSI:</b> {m['RSI']} | <b>ROC:</b> {m['ROC']}%\n"
                f"<b>ROE:</b> {m['ROE']}% | <b>PE:</b> {m['PE']}\n"
                f"-----------------------------------"
            )
        send_telegram("\n".join(lines))
    else:
        send_telegram(f"📊 <b>Market Scan:</b> Scanned {total} stocks. No stock matched setup conditions.")


if __name__ == "__main__":
    execute_master_scan()
