import datetime
import os
import sys
import time
from dotenv import load_dotenv
import pandas as pd
import pyotp
import pytz  # Corrected: Added missing imports
import requests
from SmartApi import SmartConnect

# Local testing ke liye .env load karein
load_dotenv()

# Environment Variables with Multiple Fallbacks
API_KEY = (
    os.getenv("SMARTAPI_KEY")
    or os.getenv("SMARTAPI_API_KEY")
    or os.getenv("API_KEY")
)
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE") or os.getenv("CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN") or os.getenv("PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET") or os.getenv("TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validation Check
if not API_KEY or not CLIENT_CODE or not PIN or not TOTP_SECRET:
    print("ERROR - Missing environment variables: SMARTAPI_KEY/SMARTAPI_API_KEY")
    sys.exit(1)


# ==========================================
# 2. TELEGRAM NOTIFICATION & LOGGING ENGINE
# ==========================================
def send_telegram_alert(message, max_retries=3):
    """Sends HTML formatted Telegram notifications safely with Time-Filter"""

    # --- Market Hours Time Check (3:15 PM IST Cutoff) ---
    IST = pytz.timezone("Asia/Kolkata")
    current_time = datetime.datetime.now(IST).time()
    cutoff_time = datetime.time(15, 15)  # 3:15 PM Cutoff

    # अगर समय शाम 3:15 बजे के बाद का है, तो मैसेज नहीं भेजा जाएगा
    if current_time > cutoff_time:
        print(
            f"Alert Skipped: Current IST time ({current_time.strftime('%H:%M')}) is past market cutoff (15:15)."
        )
        return

    # --- Your Existing Code ---
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=payload, timeout=5)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"Telegram Alert Error: {e}")  # Corrected logger to print
            time.sleep(1)


def main():
    print("Initializing SmartApi Connection...")
    smart_api = SmartConnect(api_key=API_KEY)

    # Generate TOTP
    totp = pyotp.TOTP(TOTP_SECRET).now()

    # Login Session
    data = smart_api.generateSession(CLIENT_CODE, PIN, totp)
    if not data or not data.get("status"):
        print(f"Login failed: {data}")
        sys.exit(1)

    print("Login Successful!")

    # Screener Logic Here
    # (Aapka custom technical analysis ya stock screening logic yahan run hoga)

    msg = "🚀 <b>Stock Screener Execution Completed Successfully!</b>"  # Corrected to HTML format <b>
    print(msg)
    send_telegram_alert(
        msg
    )  # Corrected function call: send_telegram_alert instead of send_telegram_message


if __name__ == "__main__":
    main()
