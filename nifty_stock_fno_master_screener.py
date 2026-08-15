import time
from datetime import datetime, timedelta
import pandas as pd
import pyotp
import requests
import ta
import os
from SmartApi import SmartConnect

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================
API_KEY = "N7XNbnkE"
CLIENT_CODE = "S885143"
PIN = "1989"
TOTP_SECRET = "ZH76UOCDHM4TITQGDKN32HBZEI"

# NIFTY 50 INDEX SYMBOL & TOKEN
NIFTY_INDEX = {"symbol": "NIFTY 50", "token": "99926000", "exchange": "NSE"}

# CASH EQUITY STOCKS UNIVERSE
CASH_STOCK_UNIVERSE = [
    {"symbol": "TCS-EQ", "token": "11536"}, {"symbol": "INFY-EQ", "token": "1594"},
    {"symbol": "RELIANCE-EQ", "token": "2885"}, {"symbol": "BAJAJ-AUTO-EQ", "token": "16669"},
    {"symbol": "SUNPHARMA-EQ", "token": "3351"}, {"symbol": "ICICIBANK-EQ", "token": "4963"},
    {"symbol": "SBIN-EQ", "token": "3045"}, {"symbol": "HDFCBANK-EQ", "token": "1333"}
]

# F&O OPTION STOCKS UNIVERSE
FNO_OPTION_UNIVERSE = [
    {"symbol": "SUNPHARMA-EQ", "token": "3351"}, {"symbol": "BAJAJ-AUTO-EQ", "token": "16669"},
    {"symbol": "RELIANCE-EQ", "token": "2885"}, {"symbol": "TCS-EQ", "token": "11536"},
    {"symbol": "INFY-EQ", "token": "1594"}, {"symbol": "ICICIBANK-EQ", "token": "4963"},
    {"symbol": "SBIN-EQ", "token": "3045"}, {"symbol": "HDFCBANK-EQ", "token": "1333"},
    {"symbol": "M&M-EQ", "token": "2031"}, {"symbol": "TATAMOTORS-EQ", "token": "3456"}
]

# ==========================================
# 2. AUTHENTICATION & API HELPERS
# ==========================================
def initialize_smartapi():
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_CODE, PIN, totp_code)
        if data and data.get('status'):
            raw_token = data['data']['jwtToken']
            return raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
    except Exception as e:
        print(">>> Authentication Exception:", e)
    return None

def fetch_candle_data(auth_token, token, interval, days_back, exchange="NSE"):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-PrivateKey": API_KEY, "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "106.193.147.98", "X-MACAddress": "00:2b:67:30:5e:99",
        "Authorization": auth_token
    }
    now = datetime.now()
    payload = {
        "exchange": exchange, "symboltoken": str(token), "interval": interval,
        "fromdate": (now - timedelta(days=days_back)).strftime("%Y-%m-%d 09:15"),
        "todate": now.strftime("%Y-%m-%d %H:%M")
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8).json()
        if resp.get('status') and resp.get('data'):
            df = pd.DataFrame(resp['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            return df
    except Exception:
        pass
    return None

# ==========================================
# 3. MODULE 1: NIFTY INDEX LIVE ANALYZER
# ==========================================
def run_nifty_analyzer(auth_token):
    print("\n" + "="*95)
    print(">>> RUNNING MODULE 1: NIFTY 50 INDEX LIVE ANALYZER...")
    print("="*95)
    
    df = fetch_candle_data(auth_token, NIFTY_INDEX['token'], "FIFTEEN_MINUTE", 5, exchange="NSE")
    if df is None or len(df) < 15:
        print("❌ Unable to fetch Nifty Index candles.")
        return

    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['roc'] = ta.momentum.roc(df['close'], window=12)
    curr = df.iloc[-1]

    rsi_val = round(curr['rsi'], 2) if not pd.isna(curr['rsi']) else 50.0
    roc_val = round(curr['roc'], 2) if not pd.isna(curr['roc']) else 0.0
    ltp = round(curr['close'], 2)

    if rsi_val >= 60.0 and roc_val > 0:
        bias = "🔥 BULLISH MOMENTUM (CE Bias)"
    elif rsi_val <= 40.0 and roc_val < 0:
        bias = "🔻 BEARISH MOMENTUM (PE Bias)"
    else:
        bias = "⚠️ SIDEWAYS / CONSOLIDATION"

    nifty_log = [{
        "Time": datetime.now().strftime("%H:%M:%S"),
        "Symbol": "NIFTY 50", "Token": NIFTY_INDEX['token'],
        "Live_LTP": ltp, "RSI_15M": rsi_val, "ROC_15M": roc_val, "Market_Bias": bias
    }]

    df_nifty = pd.DataFrame(nifty_log)
    df_nifty.to_csv("nifty_index_logs.csv", index=False)
    print(">>> [LOG SAVED] Nifty status saved to 'nifty_index_logs.csv'")
    print(df_nifty.to_string(index=False))

# ==========================================
# 4. MODULE 2: DAILY CASH STOCK SCREENER
# ==========================================
def run_daily_stock_screener(auth_token):
    print("\n" + "="*95)
    print(">>> RUNNING MODULE 2: DAILY STOCK SCREENER...")
    print("="*95)
    
    matches = []
    for st in CASH_STOCK_UNIVERSE:
        df = fetch_candle_data(auth_token, st['token'], "ONE_DAY", 100)
        if df is None or len(df) < 30: continue
        
        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
        df['roc'] = ta.momentum.roc(df['close'], window=12)
        curr = df.iloc[-1]
        
        if curr['rsi'] >= 60.0 and curr['roc'] > 0.0:
            matches.append({
                "Date": datetime.now().strftime("%Y-%m-%d"),
                "Symbol": st['symbol'], "Token": st['token'],
                "Entry_Price": round(curr['close'], 2),
                "Stop_Loss": round(curr['close'] * 0.985, 2),
                "Target": round(curr['close'] * 1.03, 2),
                "Reason": f"Daily RSI ({round(curr['rsi'],1)}) >= 60 & ROC > 0"
            })
        time.sleep(0.05)

    if matches:
        df_match = pd.DataFrame(matches)
        df_match.to_csv("stock_screener_logs.csv", index=False)
        print(">>> [LOG SAVED] Candidates saved to 'stock_screener_logs.csv'")
        print(df_match.to_string(index=False))
    else:
        print(">>> No stocks matched today's Daily criteria.")

# ==========================================
# 5. MODULE 3: 15-MIN F&O OPTION SCREENER
# ==========================================
def run_15min_fno_screener(auth_token):
    print("\n" + "="*95)
    print(">>> RUNNING MODULE 3: 15-MIN F&O LIVE OPTION SCREENER...")
    print("="*95)
    
    matches = []
    for st in FNO_OPTION_UNIVERSE:
        df = fetch_candle_data(auth_token, st['token'], "FIFTEEN_MINUTE", 5)
        if df is None or len(df) < 15: continue
        
        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
        df['roc'] = ta.momentum.roc(df['close'], window=12)
        curr = df.iloc[-1]
        
        is_open_equal_low = abs(curr['open'] - curr['low']) <= (curr['close'] * 0.0008)
        is_open_equal_high = abs(curr['open'] - curr['high']) <= (curr['close'] * 0.0008)

        is_bullish = (curr['rsi'] >= 60.0) and (curr['roc'] > 0.0)
        is_bearish = (curr['rsi'] <= 40.0) and (curr['roc'] < 0.0)
        
        if is_bullish:
            sl = round(curr['low'], 2)
            risk = curr['close'] - sl
            matches.append({
                "Time": datetime.now().strftime("%H:%M:%S"),
                "Symbol": st['symbol'], "Token": st['token'],
                "Action": "BUY CALL (CE)", "Entry_Price": round(curr['close'], 2),
                "Stop_Loss": sl, "Target": round(curr['close'] + (risk * 2), 2),
                "OHLC_Match": "OPEN=LOW" if is_open_equal_low else "MOMENTUM_BREAKOUT",
                "Reason": f"15M RSI ({round(curr['rsi'],1)}) >= 60 & ROC > 0"
            })
        elif is_bearish:
            sl = round(curr['high'], 2)
            risk = sl - curr['close']
            matches.append({
                "Time": datetime.now().strftime("%H:%M:%S"),
                "Symbol": st['symbol'], "Token": st['token'],
                "Action": "BUY PUT (PE)", "Entry_Price": round(curr['close'], 2),
                "Stop_Loss": sl, "Target": round(curr['close'] - (risk * 2), 2),
                "OHLC_Match": "OPEN=HIGH" if is_open_equal_high else "BEARISH_BREAKOUT",
                "Reason": f"15M RSI ({round(curr['rsi'],1)}) <= 40 & ROC < 0"
            })
        time.sleep(0.05)

    if matches:
        df_match = pd.DataFrame(matches)
        df_match.to_csv("fno_15min_option_logs.csv", index=False)
        print(">>> [LOG SAVED] Candidates saved to 'fno_15min_option_logs.csv'")
        print(df_match.to_string(index=False))
    else:
        print(">>> No 15-min option setups matched right now.")

# ==========================================
# 6. MODULE 4: SAFE PERFORMANCE & MOVEMENT TRACKER
# ==========================================
def run_performance_tracker(auth_token):
    print("\n" + "="*95)
    print(">>> RUNNING MODULE 4: COMBINED PERFORMANCE & MOVEMENT TRACKER...")
    print("="*95)

    def evaluate_file(csv_file, title):
        if not os.path.exists(csv_file):
            print(f"⚠️ Log file '{csv_file}' not found.")
            return

        df = pd.read_csv(csv_file)
        print(f"\n--- 📊 PERFORMANCE REPORT: {title} ---")

        for idx, row in df.iterrows():
            token = row.get('Token')
            symbol = row.get('Symbol', row.get('Index', 'NIFTY'))
            
            # Safe Check for Nifty Bias File
            if 'Entry_Price' not in row:
                print(f"📌 NIFTY BIAS LOGGED: {row.get('Market_Bias')} | LTP: {row.get('Live_LTP')} | RSI: {row.get('RSI_15M')}")
                continue

            entry = float(row['Entry_Price'])
            sl = float(row['Stop_Loss'])
            target = float(row['Target'])
            action = row.get('Action', 'BUY CALL (CE)')

            df_day = fetch_candle_data(auth_token, token, "ONE_DAY", 1)
            if df_day is None or len(df_day) == 0:
                print(f"❌ Could not fetch live data for {symbol}")
                continue

            last = df_day.iloc[-1]
            high, low, close = last['high'], last['low'], last['close']
            
            if "CALL" in str(action) or "BUY" in str(action):
                max_up_move = round(((high - entry) / entry) * 100, 2)
                if high >= target:
                    status = "🎯 TARGET HIT"
                    behind_reason = f"High touched {high} (+{max_up_move}% move). Strong buyer momentum."
                elif low <= sl:
                    status = "❌ STOP LOSS HIT"
                    behind_reason = f"Low touched {low}. Reversal due to selling pressure."
                else:
                    status = "🟢 PROFIT / IN PROGRESS" if close > entry else "🔴 LOSS / IN PROGRESS"
                    behind_reason = f"Closed at {close}. Max up-move reached: +{max_up_move}%."
            else:
                max_down_move = round(((entry - low) / entry) * 100, 2)
                if low <= target:
                    status = "🎯 TARGET HIT"
                    behind_reason = f"Low touched {low} (+{max_down_move}% PE move). Heavy downside expansion."
                elif high >= sl:
                    status = "❌ STOP LOSS HIT"
                    behind_reason = f"High touched {high}. Short squeeze occurred."
                else:
                    status = "🟢 PROFIT / IN PROGRESS" if close < entry else "🔴 LOSS / IN PROGRESS"
                    behind_reason = f"Closed at {close}. Max PE gain move: +{max_down_move}%."

            print(f"\n📌 ASSET: {symbol} | Setup: {action} | Entry: {entry} | SL: {sl} | Target: {target}")
            print(f"   • Outcome Status: {status}")
            print(f"   • Movement Analysis: {behind_reason}")
            time.sleep(0.08)

    evaluate_file("nifty_index_logs.csv", "NIFTY INDEX BIAS")
    evaluate_file("stock_screener_logs.csv", "DAILY STOCK SCREENER")
    evaluate_file("fno_15min_option_logs.csv", "15-MIN F&O OPTION SCREENER")

# ==========================================
# 7. MAIN INTERACTIVE MENU
# ==========================================
def main():
    print("Connecting to SmartAPI Engine...")
    auth_token = initialize_smartapi()
    if not auth_token:
        print(">>> Authentication failed! Check API Credentials.")
        return

    while True:
        print("\n" + "="*65)
        print("    🚀 MASTER TRADING ENGINE: NIFTY + STOCKS + F&O")
        print("="*65)
        print("1. Run Nifty 50 Index Live Analyzer")
        print("2. Run Daily Stock Screener")
        print("3. Run 15-Min Live F&O Option Screener")
        print("4. Run Today's Performance & Movement Tracker (All Assets)")
        print("5. ALL-IN-ONE (Run Everything Together)")
        print("6. Exit")
        print("="*65)
        
        choice = input("Enter option number (1-6): ").strip()

        if choice == "1":
            run_nifty_analyzer(auth_token)
        elif choice == "2":
            run_daily_stock_screener(auth_token)
        elif choice == "3":
            run_15min_fno_screener(auth_token)
        elif choice == "4":
            run_performance_tracker(auth_token)
        elif choice == "5":
            run_nifty_analyzer(auth_token)
            run_daily_stock_screener(auth_token)
            run_15min_fno_screener(auth_token)
            run_performance_tracker(auth_token)
        elif choice == "6":
            print("\n>>> Exiting Master Engine. Happy Trading!")
            break
        else:
            print("❌ Invalid choice! Select between 1 and 6.")

if __name__ == "__main__":
    main()