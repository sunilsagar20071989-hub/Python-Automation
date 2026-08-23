from datetime import datetime as dt
import html
import logging
import os
import pytz
import requests
import ta
import yfinance as yf
import pandas as pd

# Load Environment / Secrets
try:
    from dotenv import load_dotenv
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    for env_file in ["secret.env", "secret.env.txt", ".env"]:
        env_path = os.path.join(BASE_DIR, env_file)
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
except ImportError:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    for fname in ["secret.env", "secret.env.txt", ".env"]:
        fpath = os.path.join(BASE_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.startswith("#") and "=" in line:
                        k, v = line.strip().split("=", 1)
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")
            break

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MomentumQuantScreener")

IST = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

MAX_STOCKS = int(os.getenv("FULL_MARKET_MAX_STOCKS", "0"))


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i : i + 3500] for i in range(0, len(message), 3500)] or [""]
    for chunk in chunks:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        try:
            r = SESSION.post(url, data=payload, timeout=10)
            r.raise_for_status()
        except requests.RequestException:
            pass


def send_telegram_document(file_path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as doc:
            files = {"document": doc}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            r = SESSION.post(url, data=data, files=files, timeout=30)
            r.raise_for_status()
    except Exception:
        pass


def fetch_clean_nse_universe():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        
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
            
        return clean_symbols
    except Exception:
        return []


def execute_master_scan():
    symbols = fetch_clean_nse_universe()
    if not symbols:
        return

    now_dt = dt.now(IST)
    now_str = now_dt.strftime("%I:%M %p | %d-%b-%Y")
    
    send_telegram(f"⚡ <b>Engine Active:</b> Scanning {len(symbols)} NSE stocks at <code>{html.escape(now_str)}</code>...")

    yf_tickers = [f"{s}.NS" for s in symbols]
    batch_data = yf.download(yf_tickers, period="60d", interval="1d", group_by="ticker", progress=False, threads=True)
    matches = []

    for symbol in symbols:
        try:
            yf_symbol = f"{symbol}.NS"
            
            if isinstance(batch_data.columns, pd.MultiIndex):
                if yf_symbol in batch_data.columns.levels[0]:
                    df = batch_data[yf_symbol].copy()
                else:
                    continue
            else:
                df = batch_data.copy()

            df = df.dropna(subset=["Close"])
                
            if df.empty or len(df) < 20:
                continue

            df.columns = [c.lower() for c in df.columns]

            if df["volume"].tail(20).mean() < 3000:
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

            if c["close"] > p["high"] and c["volume"] >= 1.2 * vol_sma_val and c["rsi"] >= 60.0:
                setups.append("🔥 High Conviction Breakout")
            elif c["close"] > p["high"] and c["rsi"] >= 58.0:
                setups.append("📈 Early Momentum Buildup")

            if p["ema_9"] <= p["ema_21"] and c["ema_9"] > c["ema_21"] and c["rsi"] >= 55.0:
                setups.append("⚡ Bullish EMA Crossover")
                
            if not setups:
                continue

            matches.append({
                "Symbol": symbol,
                "LTP": round(float(c["close"]), 2),
                "Setups": ", ".join(setups),
                "RSI": round(float(c["rsi"]), 1),
                "ROC_%": round(float(c["roc"]), 1),
                "Volume": int(c["volume"]),
            })
        except Exception:
            continue

    if matches:
        lines = [f"📊 <b>MOMENTUM SCAN REPORT</b>\n<code>{html.escape(now_str)}</code>\n"]
        for m in matches:
            lines.append(
                f"<b>Stock:</b> {html.escape(m['Symbol'])} | <b>LTP:</b> ₹{m['LTP']}\n"
                f"<b>Signal:</b> {html.escape(m['Setups'])}\n"
                f"<b>RSI:</b> {m['RSI']} | <b>ROC:</b> {m['ROC_%']}% | <b>Vol:</b> {m['Volume']:,}\n"
                f"-----------------------------------"
            )
        send_telegram("\n".join(lines))
        
        excel_filename = f"Quant_Screener_{now_dt.strftime('%Y%m%d_%H%M%S')}.xlsx"
        report_df = pd.DataFrame(matches)
        report_df = report_df[["Symbol", "LTP", "Setups", "RSI", "ROC_%", "Volume"]]
        report_df.to_excel(excel_filename, index=False)
        
        send_telegram_document(excel_filename, caption=f"📁 <b>Excel Report:</b> ({now_str})")
        
        if os.path.exists(excel_filename):
            os.remove(excel_filename)


if __name__ == "__main__":
    execute_master_scan()
