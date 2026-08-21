# ==============================================================================
# DYNAMIC FULL-MARKET QUANT SCREENER (NSE ALL STOCKS + FUNDAMENTALS + DEPTH)
# ==============================================================================

from datetime import datetime as dt, timedelta
import logging
import os
import sys
import time
from bs4 import BeautifulSoup
import pandas as pd
import pyotp
import pytz
import requests
import ta
from SmartApi import SmartConnect

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FullMarketScreener")

# CONFIGURATION VIA GITHUB ENVIRONMENT SECRETS
API_KEY = os.getenv("SMARTAPI_API_KEY") or os.getenv("SMARTAPI_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST = pytz.timezone("Asia/Kolkata")
http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

# ------------------------------------------------------------------------------
# 1. TELEGRAM NOTIFIER
# ------------------------------------------------------------------------------
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            resp = http_session.post(url, data=payload, timeout=8)
            if resp.status_code == 200:
                logger.info("Telegram notification sent.")
                break
        except Exception as e:
            logger.error(f"Telegram retry {attempt + 1} failed: {e}")
            time.sleep(2)

# ------------------------------------------------------------------------------
# 2. DYNAMIC NSE MARKET UNIVERSE FETCH
# ------------------------------------------------------------------------------
def fetch_nse_stock_universe(max_stocks=150):
    """
    Angel One Master Instrument JSON se saare active NSE Equity stocks download karta hai.
    """
    logger.info("Fetching complete NSE stock universe from Angel One master list...")
    url = "https://margincalculator.angelbroking.com/OpenAPI_Standard_Metadata/OpenAPIScripMaster.json"
    try:
        resp = http_session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            df = pd.DataFrame(data)
            
            # Filter NSE Equity Cash Stocks (-EQ)
            eq_df = df[(df["exch_seg"] == "NSE") & (df["symbol"].str.endswith("-EQ"))].copy()
            
            # Junk / SME / BE series stocks filter kar rahe hain
            eq_df = eq_df[~eq_df["symbol"].str.contains("-BE|-BZ|-SM|-ST")]
            
            # Active universe limit (Rate-limiting aur GitHub action execution limit ke liye)
            stock_list = []
            for _, row in eq_df.head(max_stocks).iterrows():
                stock_list.append({
                    "symbol": row["symbol"],
                    "token": row["token"]
                })
            
            logger.info(f"Successfully loaded {len(stock_list)} dynamic NSE stocks.")
            return stock_list
    except Exception as e:
        logger.error(f"Failed to fetch NSE master scrip list: {e}")
    
    # Fallback list agar download fail ho jaaye
    return [
        {"symbol": "RELIANCE-EQ", "token": "2885"},
        {"symbol": "TATASTEEL-EQ", "token": "3499"},
        {"symbol": "INFY-EQ", "token": "1594"}
    ]

# ------------------------------------------------------------------------------
# 3. SCREENER.IN WEB SCRAPER
# ------------------------------------------------------------------------------
def fetch_screener_fundamentals(symbol):
    clean_symbol = symbol.replace("-EQ", "").replace("&", "%26")
    url = f"https://www.screener.in/company/{clean_symbol}/consolidated/"
    
    try:
        resp = http_session.get(url, timeout=5)
        if resp.status_code != 200:
            url = f"https://www.screener.in/company/{clean_symbol}/"
            resp = http_session.get(url, timeout=5)
            
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            top_ratios = soup.find_all("li", class_="flex flex-space-between")
            
            data = {}
            for item in top_ratios:
                name = item.find("span", class_="name")
                value = item.find("span", class_="number")
                if name and value:
                    key = name.text.strip().lower()
                    val_str = value.text.strip().replace(",", "")
                    try:
                        data[key] = float(val_str)
                    except ValueError:
                        data[key] = val_str

            roe = data.get("return on equity", 0.0)
            roce = data.get("return on capital employed", 0.0)
            pe = data.get("stock p/e", 0.0)
            
            # Pass filters
            pass_filter = (roe >= 10.0) and (roce >= 10.0) and (pe <= 75.0)
            return pass_filter, {"ROE": roe, "ROCE": roce, "PE": pe}
            
    except Exception:
        pass
        
    return True, {"ROE": "N/A", "ROCE": "N/A", "PE": "N/A"}

# ------------------------------------------------------------------------------
# 4. SMARTAPI CONNECT & DEPTH
# ------------------------------------------------------------------------------
def initialize_smartapi():
    try:
        obj = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = obj.generateSession(CLIENT_CODE, PIN, totp)
        if data and data.get("status"):
            return obj, data["data"]["jwtToken"]
    except Exception as e:
        logger.error(f"SmartAPI Auth Failed: {e}")
    return None, None

def analyze_order_book_depth(smart_obj, exchange, symbol, token):
    try:
        res = smart_obj.getMarketDepth(exchange=exchange, symboltoken=token)
        if res and res.get("status") and "data" in res:
            depth_data = res["data"]
            total_buy_qty = depth_data.get("totBuyQuan", 0)
            total_sell_qty = depth_data.get("totSellQuan", 0)
            total_vol = total_buy_qty + total_sell_qty
            if total_vol > 0:
                return round(total_buy_qty / total_vol, 2)
    except Exception:
        pass
    return 0.50

# ------------------------------------------------------------------------------
# 5. TECHNICAL ANALYSIS ENGINE
# ------------------------------------------------------------------------------
def fetch_historical_candles(smart_obj, token, interval="FIFTEEN_MINUTE", days=12):
    now = dt.now(IST)
    to_date = now.strftime("%Y-%m-%d %H:%M")
    from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    payload = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    }
    try:
        res = smart_obj.getCandleData(payload)
        if res and res.get("status") and res.get("data"):
            df = pd.DataFrame(res["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
            return df
    except Exception:
        pass
    return None

def analyze_stock(symbol, token, smart_obj):
    # Historical Candles
    df = fetch_historical_candles(smart_obj, token, interval="FIFTEEN_MINUTE", days=12)
    if df is None or len(df) < 40:
        return None

    # Technical Indicators
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["roc"] = ta.momentum.roc(df["close"], window=12)
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["vol_sma"] = df["volume"].rolling(20).mean()

    c = df.iloc[-1]
    p = df.iloc[-2]

    setups = []
    # 1. Momentum & Breakout (RSI 60+ & Volume Surge)
    if c["close"] > p["high"] and c["volume"] >= (1.5 * c["vol_sma"]) and c["rsi"] >= 60:
        setups.append("15M Vol Breakout")

    # 2. EMA Crossover
    if (p["ema_9"] <= p["ema_21"]) and (c["ema_9"] > c["ema_21"]):
        setups.append("EMA Crossover")

    # High Momentum Condition Filter
    if not setups:
        return None

    # Fundamentals Check (Only for technically qualified stocks to save API calls)
    fund_pass, fund_metrics = fetch_screener_fundamentals(symbol)
    if not fund_pass:
        return None

    # Order Book Depth Check
    buyer_ratio = analyze_order_book_depth(smart_obj, "NSE", symbol, token)

    return {
        "Symbol": symbol,
        "LTP": round(c["close"], 2),
        "Setups": ", ".join(setups),
        "RSI": round(c["rsi"], 1),
        "ROC": round(c["roc"], 1),
        "BuyerRatio": f"{int(buyer_ratio * 100)}%",
        "ROE": fund_metrics.get("ROE", "N/A"),
        "PE": fund_metrics.get("PE", "N/A"),
    }

# ------------------------------------------------------------------------------
# 6. MASTER EXECUTION
# ------------------------------------------------------------------------------
def execute_master_scan():
    smart_obj, _ = initialize_smartapi()
    if not smart_obj:
        return

    # DYNAMIC UNIVERSE: Automatically fetches live market stocks
    universe = fetch_nse_stock_universe(max_stocks=200)

    matches = []
    for stock in universe:
        res = analyze_stock(stock["symbol"], stock["token"], smart_obj)
        if res:
            matches.append(res)
        time.sleep(0.3)  # Rate limiting safety

    now_str = dt.now(IST).strftime("%I:%M %p")

    if matches:
        msg = f"🔍 <b>FULL MARKET QUANT SCANNER ALERT ({now_str})</b> 🔍\n\n"
        for m in matches:
            msg += (
                f"<b>Stock:</b> {m['Symbol']} | <b>LTP:</b> ₹{m['LTP']}\n"
                f"<b>Signal:</b> {m['Setups']}\n"
                f"<b>Metrics:</b> RSI: {m['RSI']} | ROC: {m['ROC']}% | Buyers: {m['BuyerRatio']}\n"
                f"<b>Fundamentals:</b> ROE: {m['ROE']}% | PE: {m['PE']}\n"
                f"-----------------------------------\n"
            )
        send_telegram(msg)
    else:
        send_telegram(f"📊 <b>Market Scan ({now_str}):</b> Scanned {len(universe)} stocks. No stock met the strict criteria.")

if __name__ == "__main__":
    execute_master_scan()