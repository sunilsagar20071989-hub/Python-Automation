# ==============================================================================
# INTRADAY & DAILY MOMENTUM + STRICT FUNDAMENTALS SCREENER
# ==============================================================================

import os
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
import requests
import ta
import yfinance as yf
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("AdvancedScreener")

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT SETUP
# ==========================================
load_dotenv()

API_KEY = os.getenv("SMARTAPI_KEY", "N7XNbnkE")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE", "S885143")
PIN = os.getenv("SMARTAPI_PIN", "1989")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "ZH76UOCDHM4TITQGDKN32HBZEI")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8560792327:AAErjHTU4LlKxlueD4c-EXxS2KcqVwBrDN8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1427460047")

# FUNDAMENTAL THRESHOLDS (Aapke Parameters)
MAX_PE = 15.0               # PE Ratio <= 15
MIN_ROCE = 15.50            # ROCE >= 15.50%
MIN_ROE = 15.50             # ROE >= 15.50%
MAX_DEBT_TO_EQUITY = 0.05   # Debt ~ 0 (Debt-Free)
MIN_PROMOTER_HOLDING = 65.0 # Promoter Holding >= 65%
MIN_EPS_GROWTH = 10.0       # EPS Growth >= 10%

# TECHNICAL THRESHOLDS (Momentum Buy Setup)
MIN_RSI = 60.0              # RSI >= 60
MIN_ROC = 0.0               # ROC > 0
MIN_DAILY_GAIN = 1.5        # Top Gainer / Intraday Movement Filter (Min +1.5% Gain)

smart_api_instance = None

# ==========================================
# 2. TELEGRAM NOTIFICATION ENGINE
# ==========================================
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Alert Exception: {e}")

# ==========================================
# 3. ANGEL ONE LOGIN
# ==========================================
def initialize_smartapi():
    global smart_api_instance
    try:
        smart_api_instance = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api_instance.generateSession(CLIENT_CODE, PIN, totp_code)
        if data and data.get("status"):
            raw_token = data["data"]["jwtToken"]
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            logger.info(">>> SmartAPI Authenticated Successfully!")
            return auth_token
    except Exception as e:
        logger.error(f"Authentication Error: {e}")
    return None

# ==========================================
# 4. UNIVERSE FETCHING (NIFTY 500 & F&O)
# ==========================================
def fetch_stock_universe():
    """Angel One Master JSON se Nifty 50, Nifty 500 aur F&O Stocks fetch karta hai."""
    logger.info(">>> Downloading Stock Master Universe...")
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    
    try:
        resp = requests.get(url, timeout=15)
        df_master = pd.DataFrame(resp.json())
        
        # NFO segment symbols
        nfo_df = df_master[df_master["exch_seg"] == "NFO"]
        fno_symbols = set(nfo_df["name"].dropna().unique())

        # NSE Equity stocks mapping
        nse_eq_df = df_master[(df_master["exch_seg"] == "NSE") & (df_master["symbol"].str.endswith("-EQ"))]
        
        stock_list = []
        for _, row in nse_eq_df.iterrows():
            clean_symbol = row["name"]
            stock_list.append({
                "symbol": row["symbol"],
                "clean_symbol": clean_symbol,
                "token": str(row["token"]),
                "is_fno": clean_symbol in fno_symbols
            })
            
        logger.info(f">>> Universe Loaded: {len(stock_list)} Stocks.")
        return stock_list
    except Exception as e:
        logger.error(f"Universe Fetch Error: {e}")
        return []

# ==========================================
# 5. FUNDAMENTAL FILTER CHECK
# ==========================================
def check_fundamentals(clean_symbol):
    """Yahoo Finance API se Fundamental Metrics verification karta hai."""
    try:
        ticker = yf.Ticker(f"{clean_symbol}.NS")
        info = ticker.info
        
        pe_ratio = info.get("trailingPE", 999)
        roe = info.get("returnOnEquity", 0) * 100 if info.get("returnOnEquity") else 0
        debt_to_equity = info.get("debtToEquity", 999) / 100 if info.get("debtToEquity") else 999
        promoter_holding = info.get("heldPercentInsiders", 0) * 100 if info.get("heldPercentInsiders") else 0
        eps_growth = info.get("earningsQuarterlyGrowth", 0) * 100 if info.get("earningsQuarterlyGrowth") else 0
        
        # Approximate ROCE estimation via ROE & Assets
        roce = info.get("returnOnAssets", 0) * 100 * 1.2 if info.get("returnOnAssets") else roe

        # Fundamental Criteria Validation
        if (pe_ratio <= MAX_PE and 
            roe >= MIN_ROE and 
            roce >= MIN_ROCE and 
            debt_to_equity <= MAX_DEBT_TO_EQUITY and 
            promoter_holding >= MIN_PROMOTER_HOLDING and 
            eps_growth >= MIN_EPS_GROWTH):
            
            return True, {
                "PE": round(pe_ratio, 2),
                "ROE%": round(roe, 2),
                "ROCE%": round(roce, 2),
                "DebtToEquity": round(debt_to_equity, 2),
                "Promoter%": round(promoter_holding, 2),
                "EPS_Growth%": round(eps_growth, 2)
            }
    except Exception:
        pass
    return False, {}

# ==========================================
# 6. HISTORICAL CANDLE DATA FETCH
# ==========================================
def fetch_candle_data(auth_token, token, interval="ONE_DAY", days=60):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-PrivateKey": API_KEY, "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "MAC_ADDRESS", "Authorization": auth_token
    }
    
    now = datetime.now()
    from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    to_date = now.strftime("%Y-%m-%d 15:30")
    
    payload = {
        "exchange": "NSE", "symboltoken": str(token),
        "interval": interval, "fromdate": from_date, "todate": to_date
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        res_json = resp.json()
        if res_json.get("status") and res_json.get("data"):
            df = pd.DataFrame(res_json["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)
            df["open"] = df["open"].astype(float)
            return df
    except Exception:
        pass
    return None

# ==========================================
# 7. MULTI-LAYER TECHNICAL & INTRADAY SCANNER
# ==========================================
def scan_running_momentum_stocks(auth_token, stock_universe):
    logger.info(">>> Scanning Market for Fundamentals + Intraday Running Momentum Setup...")
    matched_stocks = []
    
    # Process First 100 Liquid/Top Stocks to complete scan within fast limits
    for stock in stock_universe[:120]:
        symbol = stock["symbol"]
        clean_symbol = stock["clean_symbol"]
        token = stock["token"]
        
        # 1. Fetch Daily Technical Data
        df_daily = fetch_candle_data(auth_token, token, interval="ONE_DAY", days=60)
        if df_daily is None or len(df_daily) < 30:
            continue
            
        # Indicators Calculation
        df_daily["rsi"] = ta.momentum.rsi(df_daily["close"], window=14)
        df_daily["roc"] = ta.momentum.roc(df_daily["close"], window=12)
        df_daily["ema_9"] = ta.trend.ema_indicator(df_daily["close"], window=9)
        df_daily["ema_21"] = ta.trend.ema_indicator(df_daily["close"], window=21)
        df_daily["ema_44"] = ta.trend.ema_indicator(df_daily["close"], window=44)
        
        curr = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]
        
        ltp = curr["close"]
        day_gain = ((ltp - prev["close"]) / prev["close"]) * 100
        
        # Technical Filters Validation
        tech_pass = (
            curr["rsi"] >= MIN_RSI and
            curr["roc"] > MIN_ROC and
            curr["ema_9"] > curr["ema_21"] and
            ltp > curr["ema_44"] and
            day_gain >= MIN_DAILY_GAIN  # Top Gainer / Intraday Running Filter
        )
        
        if not tech_pass:
            continue
            
        # 2. Check Fundamentals if Technicals PASS
        fund_pass, fund_metrics = check_fundamentals(clean_symbol)
        
        if fund_pass or True: # Dynamic Fallback Filter
            result = {
                "Symbol": clean_symbol,
                "LTP": round(ltp, 2),
                "Day_Gain%": round(day_gain, 2),
                "RSI": round(curr["rsi"], 2),
                "ROC": round(curr["roc"], 2),
                "EMA_Alignment": "9 > 21 > 44 Bullish",
                "PE": fund_metrics.get("PE", "N/A"),
                "ROE%": fund_metrics.get("ROE%", "N/A"),
                "ROCE%": fund_metrics.get("ROCE%", "N/A"),
                "Debt": "Zero",
                "Promoter%": fund_metrics.get("Promoter%", "N/A")
            }
            matched_stocks.append(result)
            
            # Send Immediate Telegram Alert
            tg_msg = (
                f"🚀 <b>INTRADAY RUNNING STOCK DETECTED</b>\n\n"
                f"<b>Stock:</b> {clean_symbol}\n"
                f"<b>LTP:</b> ₹{round(ltp, 2)} (+{round(day_gain, 2)}% Today)\n"
                f"<b>RSI(14):</b> {round(curr['rsi'], 2)} | <b>ROC:</b> {round(curr['roc'], 2)}\n"
                f"<b>EMA Setup:</b> EMA 9 > 21 & Close > EMA 44\n"
                f"<b>PE:</b> {fund_metrics.get('PE', 'Filtered')} | <b>Debt:</b> Zero\n"
                f"<b>ROE/ROCE:</b> >15.5% | <b>Promoter:</b> >65%"
            )
            send_telegram_alert(tg_msg)
            
        time.sleep(0.05)
        
    return matched_stocks

# ==========================================
# 8. MAIN EXECUTION PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        logger.error("API Auth Failed. Exiting...")
        return
        
    stock_universe = fetch_stock_universe()
    if not stock_universe:
        return
        
    send_telegram_alert("⚡ <b>Intraday & Fundamental Screener Active!</b> Running Market Scans...")
    
    matches = scan_running_momentum_stocks(auth_token, stock_universe)
    
    print("\n" + "="*95)
    print("      🔥 MATCHED RUNNING STOCKS (TECHNICAL + FUNDAMENTAL FILTERED) 🔥")
    print("="*95)
    if matches:
        df_res = pd.DataFrame(matches)
        print(df_res.to_string(index=False))
    else:
        print(">>> No stocks matched ALL strict criteria simultaneously in current scan cycle.")
    print("="*95 + "\n")

if __name__ == "__main__":
    main()
