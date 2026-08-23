# HYBRID QUANT SCREENER (SECURE & HYBRID ENV LOADER)
from datetime import datetime as dt
import html
import logging
import os
import pytz
import requests
import ta
import yfinance as yf
import pandas as pd

# Safe & Dynamic Env Loader (Local PC + GitHub Actions Universal Support)
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

# Credentials Fetch
API_KEY = os.getenv("SMARTAPI_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE")
PIN = os.getenv("SMARTAPI_PIN")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FullMarketScreener")

IST = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

MAX_STOCKS = int(os.getenv("FULL_MARKET_MAX_STOCKS", "300"))


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing or invalid.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i : i + 3500] for i in range(0, len(message), 3500)] or [""]
    for chunk in chunks:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        try:
            r = SESSION.post(url, data=payload, timeout=10)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Telegram error: %s", exc)


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
            
        logger.info("Loaded %d clean NSE stocks into scan universe.", len(clean_symbols))
        return clean_symbols
    except Exception as exc:
        logger.error("NSE universe fetch failed: %s", exc)
        return []


def is_market_hours():
    now = dt.now(IST)
    if now.weekday() < 5:
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return start <= now <= end
    return False


def execute_master_scan():
    symbols = fetch_clean_nse_universe()
    if not symbols:
        return

    now_str = dt.now(IST).strftime("%I:%M %p | %d-%b-%Y")
    live_market = is_market_hours()
    
    mode_title = "INTRADAY SCANNER" if live_market else "EOD MARKET REPORT (DAILY CHART)"
    mode_text = "Intraday 15M" if live_market else "EOD Daily"
    
    # Heartbeat Signal
    send_telegram(f"⚡ <b>Engine Active:</b> Scanning {len(symbols)} NSE stocks [{mode_text}] at <code>{html.escape(now_str)}</code>...")

    yf_tickers = [f"{s}.NS" for s in symbols]
    period, interval = ("5d", "15m") if live_market else ("60d", "1d")

    batch_data = yf.download(yf_tickers, period=period, interval=interval, group_by="ticker", progress=False, threads=True)
    matches = []

    for symbol in symbols:
        try:
            yf_symbol = f"{symbol}.NS"
            if yf_symbol in batch_data.columns.levels[0]:
                df = batch_data[yf_symbol].copy().dropna(subset=["Close"])
            else:
                continue
                
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
                "ROC": round(float(c["roc"]), 1),
            })
        except Exception:
            continue

    if matches:
        lines = [f"📊 <b>{mode_title}</b>\n<code>{html.escape(now_str)}</code>\n"]
        for m in matches:
            lines.append(
                f"<b>Stock:</b> {html.escape(m['Symbol'])} | <b>LTP:</b> ₹{m['LTP']}\n"
                f"<b>Signal:</b> {html.escape(m['Setups'])}\n"
                f"<b>RSI (14):</b> {m['RSI']} | <b>ROC (12):</b> {m['ROC']}%\n"
                f"-----------------------------------"
            )
        send_telegram("\n".join(lines))
    else:
        send_telegram(f"📊 <b>{mode_title}:</b> Scanned {len(symbols)} stocks. No stock matched setup parameters.")


if __name__ == "__main__":
    execute_master_scan()
