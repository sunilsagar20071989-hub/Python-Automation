# HYBRID QUANT SCREENER (ZERO RATE-LIMIT ISSUE VIA YFINANCE + SMARTAPI)
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
import yfinance as yf
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FullMarketScreener")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

MAX_STOCKS = int(os.getenv("FULL_MARKET_MAX_STOCKS", "300"))
FUNDAMENTALS_ENABLED = os.getenv("FUNDAMENTALS_ENABLED", "1") == "1"


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
        
        eq = df[
            (df["exch_seg"].eq("NSE"))
            & df["symbol"].astype(str).str.endswith("-EQ")
            & ~df["symbol"].astype(str).str.endswith(("-BE", "-BZ", "-SM", "-ST"))
        ].copy()
        
        eq = eq.drop_duplicates(subset=["symbol"]).sort_values("symbol")
        
        if MAX_STOCKS > 0:
            eq = eq.head(MAX_STOCKS)
            
        result = [str(r.symbol).removesuffix("-EQ") for r in eq.itertuples()]
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

    urls = [
        f"https://www.screener.in/company/{quote(symbol, safe='')}/consolidated/",
        f"https://www.screener.in/company/{quote(symbol, safe='')}/",
    ]
    try:
        time.sleep(0.3)
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


def fetch_historical_candles_yf(symbol):
    yf_symbol = f"{symbol}.NS"
    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="5d", interval="15m")
        if df.empty or len(df) < 30:
            return None
        
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        return df
    except Exception as exc:
        logger.warning("YFinance fetch error for %s: %s", symbol, exc)
        return None


def analyze_stock(symbol):
    df = fetch_historical_candles_yf(symbol)
    if df is None or len(df) < 30:
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
    universe = fetch_nse_stock_universe()
    if not universe:
        return

    matches = []
    total = len(universe)
    
    for i, symbol in enumerate(universe, 1):
        if i % 50 == 0:
            logger.info("Progress: Processed %d/%d stocks...", i, total)
        
        result = analyze_stock(symbol)
        if result:
            matches.append(result)

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
