import os
import sys
import pyotp
import requests
import pandas as pd
from dotenv import load_dotenv
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

def send_telegram_message(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")

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
    
    msg = "🚀 *Stock Screener Execution Completed Successfully!*"
    print(msg)
    send_telegram_message(msg)

if __name__ == "__main__":
    main()
