import pandas as pd
import pyotp
from SmartApi import SmartConnect

# Credentials
API_KEY = "N7XNbnkE"
CLIENT_CODE = "S885143"
PIN = "1989"
TOTP_SECRET = "ZH76UOCDHM4TITQGDKN32HBZEI"


def fetch_today_full_history():
    try:
        totp = pyotp.TOTP(TOTP_SECRET).now()
        smartApi = SmartConnect(api_key=API_KEY)
        data = smartApi.generateSession(CLIENT_CODE, PIN, totp)

        print(">>> Fetching Today's Order & Trade Book History...\n")

        # 1. Fetch Order Book (Covers Pending, Executed, Rejected)
        order_book = smartApi.orderBook()
        has_orders = False

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

        # 2. Fetch Trade Book (Executed trades only)
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
                print("✅ Saved to 'today_trade_history.csv'")
                has_orders = True

        if not has_orders:
            print(
                "⚠️ API Memory Reset: Raat ko SmartAPI active session clear kar deta hai."
            )
            print(
                "📌 Plz Angel One App -> 'Orders' -> 'Order History' mein check karein!"
            )

    except Exception as e:
        print("❌ Error fetching history:", e)


if __name__ == "__main__":
    fetch_today_full_history()
    run: |
