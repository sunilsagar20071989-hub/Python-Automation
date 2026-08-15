import os
import time
from datetime import datetime, timedelta
import pandas as pd
import pyotp
import requests
import ta
from SmartApi import SmartConnect

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
# Fetching credentials securely from GitHub Secrets / Environment Variables
API_KEY = os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

# STRATEGY THRESHOLDS (Strict Momentum Criteria)
MIN_RSI = 60.0       # RSI must be >= 60
MIN_ROC = 0.0        # ROC must be > 0
ENABLE_EMA_FILTER = True # Enforce EMA 9 > EMA 21 & Close > EMA 44

SECTORS_WATCHLIST = [
    {"symbol": "NIFTY IT", "token": "99992002", "cat": "IT"},
    {"symbol": "NIFTY AUTO", "token": "99992004", "cat": "Auto"},
    {"symbol": "NIFTY PHARMA", "token": "99992005", "cat": "Pharma"},
    {"symbol": "NIFTY BANK", "token": "99992001", "cat": "Banking"},
    {"symbol": "NIFTY METAL", "token": "99992008", "cat": "Metal"},
    {"symbol": "NIFTY ENERGY", "token": "99992009", "cat": "Energy"}
]

FNO_UNIVERSE_BY_SECTOR = {
    "IT": [
        {"symbol": "TCS-EQ", "token": "11536"}, {"symbol": "INFY-EQ", "token": "1594"},
        {"symbol": "PERSISTENT-EQ", "token": "18365"}, {"symbol": "HCLTECH-EQ", "token": "7229"},
        {"symbol": "WIPRO-EQ", "token": "3787"}, {"symbol": "COFORGE-EQ", "token": "11543"},
        {"symbol": "TECHM-EQ", "token": "13538"}, {"symbol": "LTIM-EQ", "token": "17818"}
    ],
    "Auto": [
        {"symbol": "BAJAJ-AUTO-EQ", "token": "16669"}, {"symbol": "M&M-EQ", "token": "2031"},
        {"symbol": "MARUTI-EQ", "token": "10999"}, {"symbol": "TATAMOTORS-EQ", "token": "3456"},
        {"symbol": "HEROMOTOCO-EQ", "token": "1348"}, {"symbol": "TVSMOTOR-EQ", "token": "8479"},
        {"symbol": "EICHERMOT-EQ", "token": "910"}, {"symbol": "BHARATFORG-EQ", "token": "422"}
    ],
    "Pharma": [
        {"symbol": "SUNPHARMA-EQ", "token": "3351"}, {"symbol": "CIPLA-EQ", "token": "694"},
        {"symbol": "DRREDDY-EQ", "token": "881"}, {"symbol": "LUPIN-EQ", "token": "10440"},
        {"symbol": "DIVISLAB-EQ", "token": "10940"}, {"symbol": "TORNTPHARM-EQ", "token": "3518"}
    ],
    "Banking": [
        {"symbol": "ICICIBANK-EQ", "token": "4963"}, {"symbol": "SBIN-EQ", "token": "3045"},
        {"symbol": "HDFCBANK-EQ", "token": "1333"}, {"symbol": "AXISBANK-EQ", "token": "5900"},
        {"symbol": "KOTAKBANK-EQ", "token": "1922"}, {"symbol": "INDUSINDBK-EQ", "token": "5258"}
    ],
    "Metal": [
        {"symbol": "TATASTEEL-EQ", "token": "3499"}, {"symbol": "HINDALCO-EQ", "token": "1363"},
        {"symbol": "JINDALSTEL-EQ", "token": "1732"}, {"symbol": "VEDL-EQ", "token": "3063"}
    ],
    "Energy": [
        {"symbol": "RELIANCE-EQ", "token": "2885"}, {"symbol": "NTPC-EQ", "token": "11630"},
        {"symbol": "POWERGRID-EQ", "token": "14977"}, {"symbol": "ONGC-EQ", "token": "2475"}
    ]
}

# ==========================================
# 2. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
    try:
        if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
            print(">>> Error: Environment variables/secrets not set properly.")
            return None

        smart_api = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get('status'):
            raw_token = data['data']['jwtToken']
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            print("\n" + "="*85)
            print(">>> SmartAPI Authenticated! Strict Indicator Verification Active...")
            print("="*85 + "\n")
            return auth_token
    except Exception as e:
        print(">>> Authentication Exception:", e)
    return None

# ==========================================
# 3. HISTORICAL DATA FETCH
# ==========================================
def fetch_candle_data_direct(auth_token, token, interval, days, exchange="NSE"):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": API_KEY,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "106.193.147.98",
        "X-MACAddress": "00:2b:67:30:5e:99",
        "Authorization": auth_token
    }

    now = datetime.now()
    if now.hour < 9:
        last_trade_date = now - timedelta(days=1)
    else:
        last_trade_date = now

    to_date = last_trade_date.strftime("%Y-%m-%d 15:30")
    from_date = (last_trade_date - timedelta(days=days)).strftime("%Y-%m-%d 09:15")

    payload = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        resp_json = resp.json()

        if resp_json.get('status') and resp_json.get('data'):
            df = pd.DataFrame(resp_json['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            return df
    except Exception:
        pass
    return None

# ==========================================
# 4. STRICT F&O STOCK SCANNER
# ==========================================
def scan_fno_universe(auth_token):
    print(f">>> SCANNING F&O STOCKS WITH STRICT FILTERS (RSI >= {MIN_RSI}, ROC > {MIN_ROC}, EMA Alignment)...\n")
    all_scanned = []
    qualified_matches = []

    for sector_name, stock_list in FNO_UNIVERSE_BY_SECTOR.items():
        print(f" Scanning [{sector_name} Sector] ({len(stock_list)} F&O stocks)...")

        for stock in stock_list:
            symbol = stock['symbol']
            token = stock['token']

            df_daily = fetch_candle_data_direct(auth_token, token, "ONE_DAY", days=100)
            if df_daily is None or len(df_daily) < 45:
                continue

            # Compute Technical Indicators
            df_daily['rsi'] = ta.momentum.rsi(df_daily['close'], window=14)
            df_daily['roc'] = ta.momentum.roc(df_daily['close'], window=12)
            df_daily['ema_9'] = ta.trend.ema_indicator(df_daily['close'], window=9)
            df_daily['ema_21'] = ta.trend.ema_indicator(df_daily['close'], window=21)
            df_daily['ema_44'] = ta.trend.ema_indicator(df_daily['close'], window=44)
            df_daily['vol_sma20'] = df_daily['volume'].rolling(window=20).mean()

            curr = df_daily.iloc[-1]

            # Individual Conditions Check
            rsi_pass = curr['rsi'] >= MIN_RSI
            roc_pass = curr['roc'] > MIN_ROC
            ema_pass = (curr['ema_9'] > curr['ema_21']) and (curr['close'] > curr['ema_44']) if ENABLE_EMA_FILTER else True
            vol_pass = curr['volume'] > curr['vol_sma20']

            status_data = {
                "Sector": sector_name,
                "Symbol": symbol,
                "LTP": round(curr['close'], 2),
                "RSI(14)": round(curr['rsi'], 2),
                "ROC(12)": round(curr['roc'], 2),
                "RSI_>=_60": "PASS" if rsi_pass else "FAIL",
                "ROC_>_0": "PASS" if roc_pass else "FAIL",
                "EMA_Trend": "PASS" if ema_pass else "FAIL",
                "VolSurge": "YES" if vol_pass else "NO"
            }
            all_scanned.append(status_data)

            # STRICT ENFORCEMENT: ONLY QUALIFY IF ALL THREE PASS
            if rsi_pass and roc_pass and ema_pass:
                match_data = status_data.copy()
                match_data["Signal"] = "🚀 STRICT MOMENTUM BUY"
                qualified_matches.append(match_data)

            time.sleep(0.08)

    return all_scanned, qualified_matches

# ==========================================
# 5. MAIN PIPELINE
# ==========================================
def main():
    auth_token = initialize_smartapi()
    if not auth_token:
        print(">>> Session initialization failed. Exiting.")
        return

    all_scanned, qualified_matches = scan_fno_universe(auth_token)

    print("\n" + "="*95)
    print("                    RAW METRICS LOG (ALL SCANNED F&O STOCKS)")
    print("="*95)
    if all_scanned:
        df_all = pd.DataFrame(all_scanned)
        print(df_all.to_string(index=False))
    else:
        print(">>> No stock data retrieved.")
    print("="*95 + "\n")

    print("="*95)
    print("         🔥 FINAL FILTERED CANDIDATES (RSI >= 60 & ROC > 0 & EMA ALIGNED) 🔥")
    print("="*95)
    if qualified_matches:
        df_match = pd.DataFrame(qualified_matches)
        print(df_match.to_string(index=False))
    else:
        print(">>> ZERO STOCKS MATCHED STRICT RSI + ROC + EMA FILTERS TODAY.")
    print("="*95 + "\n")

if __name__ == "__main__":
    main()
