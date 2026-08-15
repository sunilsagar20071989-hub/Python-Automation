from datetime import datetime, timedelta
import os
import threading
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import requests
import ta
import telebot
from SmartApi import SmartConnect

# ==========================================
# 1. CONFIGURATION & SECURE ENVIRONMENT VARIABLES
# ==========================================
# `.env` file load karein
load_dotenv()

# Fallback ke saath environment variables read karein
API_KEY = os.getenv("SMARTAPI_KEY", "N7XNbnkE")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE", "S885143")
PIN = os.getenv("SMARTAPI_PIN", "1989")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "ZH76UOCDHM4TITQGDKN32HBZEI")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8560792327:AAErjHTU4LlKxlueD4c-EXxS2KcqVwBrDN8"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1427460047")

# Telegram Bot Initialisation
bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None


# ==========================================
# 2. TELEGRAM CHAT ID HANDLER & POLLING THREAD
# ==========================================
if bot:
    @bot.message_handler(func=lambda message: True)
    def handle_telegram_messages(message):
        """Telegram par koi bhi message bhejne par Chat ID reply karega"""
        chat_id = message.chat.id
        user_name = message.from_user.first_name
        print(f"\n[Telegram Activity] Message from {user_name} | Chat ID: {chat_id}")
        bot.reply_to(message, f"Hello {user_name}! Aapki Telegram Chat ID hai: `{chat_id}`", parse_mode="Markdown")

    def start_telegram_polling():
        """Polling in a background thread so scanner isn't blocked"""
        try:
            print(">>> Telegram Bot listener started... Telegram par bot ko message bhej kar Chat ID check kar sakte hain.")
            bot.infinity_polling()
        except Exception as e:
            print(">>> Telegram Polling Error:", e)

    # Start polling in background
    polling_thread = threading.Thread(target=start_telegram_polling, daemon=True)
    polling_thread.start()


# STRATEGY THRESHOLDS (Strict Momentum Criteria)
MIN_RSI = 60.0         # RSI must be >= 60
MIN_ROC = 0.0          # ROC must be > 0
ENABLE_EMA_FILTER = True # Enforce EMA 9 > EMA 21 & Close > EMA 44

# RISK & TRAILING STOP-LOSS PARAMETERS
SL_PCT = 0.045                # 4.5% Initial Stop Loss
TARGET_PCT = 0.125            # 12.5% Target Gain
MAX_RISK_PER_TRADE_PCT = 0.015 # Capping trade risk to 1.5% of total account capital

ENABLE_TRAILING_SL = True
TSL_ACTIVATION_PCT = 0.04    # Trailing activates after +4% profit
TSL_STEP_TRIGGER_PCT = 0.02  # Step trigger every +2% gain
TSL_STEP_MOVE_PCT = 0.015    # Move SL up by +1.5% per step

LOG_FILE = "fno_scan_log.csv"

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

# Global SmartAPI Handle
smart_api_instance = None


# ==========================================
# 3. TELEGRAM ALERT SENDER
# ==========================================
def send_telegram_alert(trade_data):
    """Formats and sends trade signals directly to Telegram Chat"""
    if not bot or not TELEGRAM_CHAT_ID:
        print(">>> Warning: Telegram Bot or Chat ID not configured correctly.")
        return

    msg = (
        f"<b>🚀 MOMENTUM SCANNER SIGNAL DETECTED</b>\n"
        f"-----------------------------------------\n"
        f"<b>Symbol:</b> {trade_data['Symbol']}\n"
        f"<b>Sector:</b> {trade_data['Sector']}\n"
        f"<b>Entry Price:</b> ₹{trade_data['LTP']}\n"
        f"<b>RSI (14):</b> {trade_data['RSI(14)']}\n"
        f"<b>ROC (12):</b> {trade_data['ROC(12)']}\n"
        f"-----------------------------------------\n"
        f"<b>Calculated Quantity:</b> {trade_data['Alloc_Qty']} Shares\n"
        f"<b>Initial Stop Loss:</b> ₹{trade_data['Initial_SL']} (-4.5%)\n"
        f"<b>Target Price:</b> ₹{trade_data['Target']} (+12.5%)\n"
        f"-----------------------------------------\n"
        f"<i>Timestamp: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}</i>"
    )

    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML")
        print(f">>> Telegram alert sent for {trade_data['Symbol']}!")
    except Exception as e:
        print(f">>> Failed to send Telegram alert for {trade_data['Symbol']}:", e)


# ==========================================
# 4. LOGIN & AUTHENTICATION
# ==========================================
def initialize_smartapi():
    global smart_api_instance
    try:
        if not all([API_KEY, CLIENT_CODE, PIN, TOTP_SECRET]):
            print(">>> Error: Environment variables/secrets not set properly.")
            return None

        smart_api_instance = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api_instance.generateSession(CLIENT_CODE, PIN, totp_code)

        if data and data.get('status'):
            raw_token = data['data']['jwtToken']
            auth_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
            print("\n" + "="*85)
            print(">>> SmartAPI Authenticated! Momentum Scan + Dynamic Risk Control Active...")
            print("="*85 + "\n")
            return auth_token
    except Exception as e:
        print(">>> Authentication Exception:", e)
    return None


# ==========================================
# 5. DYNAMIC POSITION SIZING & TRAILING SL CALCULATOR
# ==========================================
def calculate_dynamic_position_size(entry_price):
    """Calculates maximum allowed quantity based on account risk rules"""
    try:
        if smart_api_instance:
            rms_data = smart_api_instance.rmsLimit()
            if rms_data and rms_data.get("status") and "data" in rms_data:
                net_capital = float(rms_data["data"].get("net", 0.0))
                if net_capital > 0:
                    max_risk_amount = net_capital * MAX_RISK_PER_TRADE_PCT
                    risk_per_share = entry_price * SL_PCT
                    quantity = int(max_risk_amount // risk_per_share)
                    return max(1, quantity), net_capital
    except Exception as e:
        print(">>> Error Fetching RMS Data:", e)
    
    return 10, 0.0


def calculate_trailing_sl(entry_price, highest_price_seen):
    """Calculates active Trailing Stop-Loss level based on peak price reach"""
    base_sl = round(entry_price * (1 - SL_PCT), 2)
    if not ENABLE_TRAILING_SL or highest_price_seen <= entry_price:
        return base_sl

    gain_pct = (highest_price_seen - entry_price) / entry_price
    if gain_pct >= TSL_ACTIVATION_PCT:
        steps = int((gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT)
        trailed_sl = round(entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2)
        return max(base_sl, trailed_sl)

    return base_sl


# ==========================================
# 6. HISTORICAL DATA FETCH
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
# 7. STRICT F&O STOCK SCANNER WITH RISK ENGINE & ALERTS
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
            entry_p = round(curr['close'], 2)

            # Individual Conditions Check
            rsi_pass = curr['rsi'] >= MIN_RSI
            roc_pass = curr['roc'] > MIN_ROC
            ema_pass = (curr['ema_9'] > curr['ema_21']) and (curr['close'] > curr['ema_44']) if ENABLE_EMA_FILTER else True
            vol_pass = curr['volume'] > curr['vol_sma20']

            status_data = {
                "Sector": sector_name,
                "Symbol": symbol,
                "LTP": entry_p,
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
                qty, capital = calculate_dynamic_position_size(entry_p)
                initial_sl = round(entry_p * (1 - SL_PCT), 2)
                tgt_p = round(entry_p * (1 + TARGET_PCT), 2)

                match_data = status_data.copy()
                match_data["Signal"] = "🚀 STRICT MOMENTUM BUY"
                match_data["Alloc_Qty"] = qty
                match_data["Initial_SL"] = initial_sl
                match_data["Target"] = tgt_p
                qualified_matches.append(match_data)
                
                # TRIGGER TELEGRAM ALERT FOR MATCHED STOCK
                send_telegram_alert(match_data)

            time.sleep(0.08)

    return all_scanned, qualified_matches


def log_scan_results(qualified_matches):
    """Exports scanned results into a persistence log CSV"""
    if qualified_matches:
        df_log = pd.DataFrame(qualified_matches)
        df_log['Scan_Timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        df_log.to_csv(LOG_FILE, mode='a', header=not file_exists, index=False)
        print(f"\n>>> [LOG EXPORTED] Saved {len(qualified_matches)} qualified setup(s) to {LOG_FILE}")


# ==========================================
# 8. MAIN PIPELINE
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
        log_scan_results(qualified_matches)
    else:
        print(">>> ZERO STOCKS MATCHED STRICT RSI + ROC + EMA FILTERS TODAY.")
    print("="*95 + "\n")

if __name__ == "__main__":
    main()
