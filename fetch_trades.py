from datetime import datetime
import os
from dotenv import load_dotenv
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ==========================================
# 1. CREDENTIALS & SETUP
# ==========================================
load_dotenv()

# Secure Environment Variables (Fallback for naming conventions)
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def fetch_today_full_history():
    # Verification of Credentials
    missing_credentials = []
    if not API_KEY:
        missing_credentials.append("SMARTAPI_KEY/SMARTAPI_API_KEY")
    if not CLIENT_CODE:
        missing_credentials.append("SMARTAPI_CLIENT_CODE")
    if not PIN:
        missing_credentials.append("SMARTAPI_PIN")
    if not TOTP_SECRET:
        missing_credentials.append("SMARTAPI_TOTP_SECRET")

    if missing_credentials:
        print(
            f"❌ Error: Missing environment credentials:"
            f" {', '.join(missing_credentials)}"
        )
        return

    try:
        # Generate fresh TOTP & Login Session
        totp = pyotp.TOTP(TOTP_SECRET).now()
        smartApi = SmartConnect(api_key=API_KEY)
        data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

        if not data or not data.get("status"):
            print("❌ Login Failed:", data.get("message") if data else "No response")
            return

        print(">>> Fetching Today's Order & Trade Book History...\n")
        has_orders = False

        # ------------------------------------------
        # 1. Fetch Order Book (Pending, Executed, Cancelled, Rejected)
        # ------------------------------------------
        order_book = smartApi.orderBook()
        if order_book and order_book.get("status") and order_book.get("data"):
            orders = order_book["data"]
            if len(orders) > 0:
                df_orders = pd.DataFrame(orders)
                cols = [
                    "orderid",
                    "updatetime",
                    "tradingsymbol",
                    "transactiontype",
                    "orderstatus",
                    "price",
                    "quantity",
                    "text",
                ]
                avail_cols = [c for c in cols if c in df_orders.columns]
                df_orders_clean = df_orders[avail_cols]

                df_orders_clean.to_csv("today_order_book.csv", index=False)
                print("=" * 80)
                print("📋 TODAY'S ORDER BOOK (All Statuses)")
                print("=" * 80)
                print(df_orders_clean.to_string(index=False))
                print("=" * 80)
                print("✅ Saved to 'today_order_book.csv'\n")
                has_orders = True

        # ------------------------------------------
        # 2. Fetch Trade Book (Executed Trades Only)
        # ------------------------------------------
        trade_book = smartApi.tradeBook()
        if trade_book and trade_book.get("status") and trade_book.get("data"):
            trades = trade_book["data"]
            if len(trades) > 0:
                df_trades = pd.DataFrame(trades)
                cols_t = [
                    "filltime",
                    "tradingsymbol",
                    "transactiontype",
                    "fillprice",
                    "fillshares",
                    "orderid",
                ]
                avail_t = [c for c in cols_t if c in df_trades.columns]
                df_trades_clean = df_trades[avail_t]

                df_trades_clean.to_csv("today_trade_history.csv", index=False)
                print("=" * 80)
                print("📊 TODAY'S EXECUTED TRADE BOOK")
                print("=" * 80)
                print(df_trades_clean.to_string(index=False))
                print("=" * 80)
                print("✅ Saved to 'today_trade_history.csv'\n")
                has_orders = True

        # ------------------------------------------
        # 3. Fallback Notice
        # ------------------------------------------
        if not has_orders:
            print(
                "⚠️ No Orders/Trades Found for Today (or API Session Memory Cleared"
                " by Broker)."
            )
            print(
                "📌 Kindly check Angel One App -> 'Orders' -> 'Order History' for"
                " past records."
            )

    except Exception as e:
        print("❌ Error fetching history:", e)


if __name__ == "__main__":
    fetch_today_full_history()
