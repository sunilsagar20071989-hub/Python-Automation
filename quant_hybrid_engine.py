# HYBRID QUANT SCREENER (LIGHTNING FAST BATCH YFINANCE)
from datetime import datetime as dt
import html
import logging
import os
import re
import time
from urllib.parse import quote

import pandas as pd
import pytz
import requests
import ta
import yfinance as yf
from bs4 import BeautifulSoup

# Suppress noisy logger outputs from yfinance
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
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


def fetch_clean_nse_universe():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        
        # Filter genuine equity stocks and exclude test / non-tradeable symbols
        eq = df[
            (df["exch_seg"].eq("NSE"))
            & df["symbol"].astype(str).str.endswith("-EQ")
            & ~df["symbol"].astype(str).str.contains(r"\$|TEST|NIFTY|BANKNIFTY", regex=True, case=False)
            & ~df["symbol"].astype(str).str.endswith(("-BE", "-BZ", "-SM", "-ST"))
        ].copy()
        
        eq = eq.drop_duplicates(subset=["symbol"]).sort_values("symbol")
        
        clean_symbols = [str(r.symbol).removesuffix("-EQ") for r in eq.itertuples()]
        
        if MAX_STOCKS > 0:
            clean_symbols = clean_symbols[:MAX_STOCKS]
            
        logger.info("Loaded %d clean NSE stocks into scan universe.", len(clean_symbols))
        return clean_symbols
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
        time.sleep(0.2)
        for url in urls:
            r = SESSION.get(url, timeout=5)
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


def execute_master_scan():
    symbols = fetch_clean_nse_universe()
    if not symbols:
        return

    # Map symbols for Yahoo Finance batch download
    yf_tickers = [f"{s}.NS" for s in symbols]
    logger.info("Downloading batch 15m candle data for %d stocks...", len(yf_tickers))
    
    # Download multi-ticker batch data in 1 single network call
    batch_data = yf.download(yf_tickers, period="5d", interval="15m", group_by="ticker", progress=False, threads=True)
    
    matches = []
    
    for symbol in symbols:
        try:
            yf_symbol = f"{symbol}.NS"
            if yf_symbol in batch_data.columns.levels[0]:
                df = batch_data[yf_symbol].copy().dropna(subset=["Close"])
            else:
                continue
                
            if df.empty or len(df) < 30:
                continue

            # Standardize columns
            df.columns = [c.lower() for c in df.columns]

            if df["volume"].tail(20).mean() < 1000:
                continue

            df["rsi"] = ta.momentum.rsi(df["close"], window=14)
            df["roc"] = ta.momentum.roc(df["close"], window=12)
            df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
            df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
            df["vol_sma"] = df["volume"].rolling(20).mean()
            
            df = df.dropna()
            if len(df) < 2:
                continue

            c, p = df.iloc[-1], df.iloc[-2]
            setups = []
            
            vol_sma_val = float(c["vol_sma"]) if pd.notna(c["vol_sma"]) and float(c["vol_sma"]) > 0 else 1.0

            if c["close"] > p["high"] and c["volume"] >= 1.5 * vol_sma_val and c["rsi"] >= 60.0:
                setups.append("15M Vol Breakout")
            if p["ema_9"] <= p["ema_21"] and c["ema_9"] > c["ema_21"]:
                setups.append("EMA Crossover")
                
            if not setups:
                continue

            fund_pass, fund = fetch_screener_fundamentals(symbol)
            if not fund_pass:
                continue

            matches.append({
                "Symbol": symbol,
                "LTP": round(float(c["close"]), 2),
                "Setups": ", ".join(setups),
                "RSI": round(float(c["rsi"]), 1),
                "ROC": round(float(c["roc"]), 1),
                "ROE": fund["ROE"],
                "PE": fund["PE"],
            })
        except Exception:
            continue

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
        send_telegram(f"📊 <b>Market Scan:</b> Scanned {len(symbols)} stocks. No stock matched setup conditions.")


if __name__ == "__main__":
    execute_master_scan()
