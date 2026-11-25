import os
import time
import logging
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests
import telebot
from SmartApi import SmartConnect
import pyotp

# ==========================================
# CONFIGURATION (SOFT / BALANCED MODE)
# ==========================================
IST = pytz.timezone("Asia/Kolkata")

# Credentials (set via environment variables or replace locally)
API_KEY = os.getenv("ANGEL_API_KEY", "YOUR_API_KEY")
CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "YOUR_CLIENT_CODE")
MPIN = os.getenv("ANGEL_MPIN", "YOUR_MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "YOUR_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TG_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TG_CHAT_ID")

PAPER_TRADING = True
SCAN_INTERVAL_SEC = 30
HEARTBEAT_MINUTES = 30

# Nifty Spot & ITM Option Settings
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_EXCHANGE = "NSE"
OPTION_EXCHANGE = "NFO"
ITM_OFFSET = 100          # ~100-200 pts ITM strike
DEFAULT_QTY = 65          # Update per current Nifty lot rules if needed

# Soft / Balanced Technical Thresholds
RSI_PERIOD = 14
ROC_PERIOD = 9
RSI_BULL_MIN = 56.0
RSI_BEAR_MAX = 44.0
ROC_BULL_MIN = 0.01
ROC_BEAR_MAX = -0.01

VIX_MIN = 8.0
VIX_MAX = 30.0

SL_PCT = 0.07             # 7% Stop Loss on option premium
TARGET_PCT = 0.16         # 16% Target on option premium

# ==========================================
# LOGGING & TELEGRAM SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("nifty_soft_algo.log", mode="a")]
)
logger = logging.getLogger("NiftySoftAlgo")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if "YOUR_TG_BOT_TOKEN" not in TELEGRAM_BOT_TOKEN else None


def send_tg(msg: str):
    logger.info(f"TG_MSG: {msg}")
    if bot and "YOUR_TG_CHAT_ID" not in TELEGRAM_CHAT_ID:
        try:
            bot.send_message(TELEGRAM_CHAT_ID, msg)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")


# ==========================================
# BROKER & SCRIP MASTER HELPERS
# ==========================================
def login_smartapi() -> SmartConnect:
    smart = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = smart.generateSession(CLIENT_CODE, MPIN, totp)
    if not data or not data.get("status"):
        raise RuntimeError(f"SmartAPI login failed: {data}")
    logger.info("SmartAPI login successful.")
    return smart


def load_nifty_options_master() -> pd.DataFrame:
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    nifty_df = df[(df["name"] == "NIFTY") & (df["instrumenttype"] == "OPTIDX")].copy()
    nifty_df["strike"] = pd.to_numeric(nifty_df["strike"], errors="coerce")
    nifty_df["expiry_dt"] = pd.to_datetime(nifty_df["expiry"], format="%d%b%Y", errors="coerce")
    today_midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    nifty_df = nifty_df[nifty_df["expiry_dt"] >= today_midnight].sort_values("expiry_dt")
    return nifty_df


def get_itm_option_contract(nifty_df: pd.DataFrame, spot_price: float, side: str):
    """
    side: 'CE' for bullish, 'PE' for bearish
    Angel One scrip master stores strike multiplied by 100.
    """
    base_strike = round(spot_price / 50) * 50
    if side == "CE":
        target_strike = base_strike - ITM_OFFSET
        option_type_suffix = "CE"
    else:
        target_strike = base_strike + ITM_OFFSET
        option_type_suffix = "PE"

    nearest_expiry = nifty_df["expiry_dt"].iloc[0]
    exp_df = nifty_df[nifty_df["expiry_dt"] == nearest_expiry].copy()

    # Match strike * 100 first, with fallback if needed
    matched = exp_df[
        (exp_df["strike"] == target_strike * 100) &
        (exp_df["symbol"].str.endswith(option_type_suffix))
    ]
    if matched.empty:
        matched = exp_df[
            (exp_df["strike"] == target_strike) &
            (exp_df["symbol"].str.endswith(option_type_suffix))
        ]
    if matched.empty:
        return None
    return matched.iloc[0].to_dict()


# ==========================================
# CANDLES & INDICATORS
# ==========================================
def fetch_candles(smart: SmartConnect, token: str, interval: str, days: int = 5) -> pd.DataFrame:
    now_ist = datetime.now(IST)
    from_dt = (now_ist - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    to_dt = now_ist.strftime("%Y-%m-%d %H:%M")

    payload = {
        "exchange": NIFTY_EXCHANGE,
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_dt,
        "todate": to_dt,
    }
    res = smart.getCandleData(payload)
    rows = res.get("data") if isinstance(res, dict) else None
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_9"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema_21"] = out["close"].ewm(span=21, adjust=False).mean()
    out["ema_50"] = out["close"].ewm(span=50, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

    out["roc"] = out["close"].pct_change(ROC_PERIOD) * 100.0
    return out


def get_15m_trend(df_15m: pd.DataFrame) -> str:
    if df_15m.empty or len(df_15m) < 25:
        return "NEUTRAL"
    ind = compute_indicators(df_15m)
    last = ind.iloc[-1]
    if last["close"] > last["ema_21"] and last["ema_9"] > last["ema_21"]:
        return "BULLISH"
    if last["close"] < last["ema_21"] and last["ema_9"] < last["ema_21"]:
        return "BEARISH"
    return "NEUTRAL"


# ==========================================
# SOFT SIGNAL GENERATOR (5M ENTRY + 15M BIAS)
# ==========================================
def evaluate_soft_signal(df_5m: pd.DataFrame, trend_15m: str, vix_val: float = 15.0):
    if df_5m.empty or len(df_5m) < 55:
        return None, "Insufficient 5m candles"

    ind = compute_indicators(df_5m)
    curr = ind.iloc[-1]
    prev = ind.iloc[-2]

    close_val = float(curr["close"])
    ema9, ema21, ema50 = float(curr["ema_9"]), float(curr["ema_21"]), float(curr["ema_50"])
    rsi_val = float(curr["rsi"])
    roc_val = float(curr["roc"]) if pd.notna(curr["roc"]) else 0.0

    # Soft VIX check
    if not (VIX_MIN <= vix_val <= VIX_MAX):
        return None, f"VIX {vix_val:.2f} outside soft range [{VIX_MIN}, {VIX_MAX}]"

    # Bullish (CE Buyer) - Soft conditions
    bull_price_ok = (close_val > ema50) and (ema9 > ema21)
    bull_momentum_ok = (rsi_val >= RSI_BULL_MIN) and (roc_val > ROC_BULL_MIN)
    bull_15m_ok = trend_15m != "BEARISH"

    if bull_price_ok and bull_momentum_ok:
        if not bull_15m_ok:
            return None, f"CE blocked by strong 15m BEARISH trend (RSI {rsi_val:.1f}, ROC {roc_val:.2f})"
        return "CE", f"5m CE | Close {close_val:.1f} > EMA50 {ema50:.1f} | EMA9>EMA21 | RSI {rsi_val:.1f} | ROC {roc_val:.2f} | 15m:{trend_15m}"

    # Bearish (PE Buyer) - Soft conditions
    bear_price_ok = (close_val < ema50) and (ema9 < ema21)
    bear_momentum_ok = (rsi_val <= RSI_BEAR_MAX) and (roc_val < ROC_BEAR_MAX)
    bear_15m_ok = trend_15m != "BULLISH"

    if bear_price_ok and bear_momentum_ok:
        if not bear_15m_ok:
            return None, f"PE blocked by strong 15m BULLISH trend (RSI {rsi_val:.1f}, ROC {roc_val:.2f})"
        return "PE", f"5m PE | Close {close_val:.1f} < EMA50 {ema50:.1f} | EMA9<EMA21 | RSI {rsi_val:.1f} | ROC {roc_val:.2f} | 15m:{trend_15m}"

    status_note = (
        f"No Signal | Spot:{close_val:.1f} | E9:{ema9:.1f} E21:{ema21:.1f} E50:{ema50:.1f} | "
        f"RSI:{rsi_val:.1f} | ROC:{roc_val:.2f} | 15m:{trend_15m}"
    )
    return None, status_note


# ==========================================
# PAPER / LIVE POSITION MANAGER
# ==========================================
class PaperPosition:
    def __init__(self, symbol: str, token: str, side: str, entry_price: float, qty: int):
        self.symbol = symbol
        self.token = token
        self.side = side
        self.entry_price = entry_price
        self.qty = qty
        self.sl_price = round(entry_price * (1.0 - SL_PCT), 2)
        self.target_price = round(entry_price * (1.0 + TARGET_PCT), 2)
        self.entry_time = datetime.now(IST)


def get_ltp(smart: SmartConnect, exchange: str, symbol: str, token: str, fallback_price: float) -> float:
    try:
        q = smart.ltpData(exchange, symbol, token)
        ltp = q.get("data", {}).get("ltp")
        if ltp and float(ltp) > 0:
            return float(ltp)
    except Exception as e:
        logger.warning(f"LTP fetch warning for {symbol}: {e}")
    return fallback_price


def run_bot():
    send_tg(
        f"Nifty Soft Algo Started | Mode: {'PAPER' if PAPER_TRADING else 'LIVE'} | "
        f"RSI: >={RSI_BULL_MIN}/<={RSI_BEAR_MAX} | ROC: >{ROC_BULL_MIN}/<{ROC_BEAR_MAX} | SL:{int(SL_PCT*100)}% Target:{int(TARGET_PCT*100)}%"
    )

    smart = login_smartapi()
    options_df = load_nifty_options_master()
    active_pos: PaperPosition | None = None
    last_heartbeat = datetime.now(IST) - timedelta(minutes=HEARTBEAT_MINUTES)
    last_status_line = "Starting up..."
    realized_pnl = 0.0
    trades_today = 0

    while True:
        now = datetime.now(IST)
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=25, second=0, microsecond=0)
        entry_cutoff = now.replace(hour=14, minute=45, second=0, microsecond=0)

        if now.weekday() >= 5 or not (market_open <= now <= market_close):
            logger.info(f"Market closed or weekend ({now.strftime('%Y-%m-%d %H:%M:%S')}). Sleeping 60s...")
            time.sleep(60)
            continue

        try:
            df_5m = fetch_candles(smart, NIFTY_SPOT_TOKEN, "FIVE_MINUTE", days=5)
            df_15m = fetch_candles(smart, NIFTY_SPOT_TOKEN, "FIFTEEN_MINUTE", days=7)
            trend_15m = get_15m_trend(df_15m)

            side, reason = evaluate_soft_signal(df_5m, trend_15m, vix_val=15.0)
            last_status_line = reason
            logger.info(reason)

            # Manage existing position first
            if active_pos is not None:
                est_ltp = active_pos.entry_price
                ltp = get_ltp(smart, OPTION_EXCHANGE, active_pos.symbol, active_pos.token, est_ltp)

                exit_reason = None
                if ltp <= active_pos.sl_price:
                    exit_reason = f"SL Hit ({ltp:.2f} <= {active_pos.sl_price:.2f})"
                elif ltp >= active_pos.target_price:
                    exit_reason = f"Target Hit ({ltp:.2f} >= {active_pos.target_price:.2f})"
                elif now >= market_close - timedelta(minutes=3):
                    exit_reason = f"Square-off ({ltp:.2f})"

                if exit_reason:
                    pnl = (ltp - active_pos.entry_price) * active_pos.qty
                    realized_pnl += pnl
                    send_tg(
                        f"EXIT ({active_pos.side}) {active_pos.symbol}\n"
                        f"Reason: {exit_reason}\n"
                        f"Entry: {active_pos.entry_price:.2f} -> Exit: {ltp:.2f}\n"
                        f"Trade PnL: Rs {pnl:.2f} | Day Realized: Rs {realized_pnl:.2f}"
                    )
                    active_pos = None

            # Look for new entry if flat and before cutoff
            elif side in ("CE", "PE") and now <= entry_cutoff:
                spot_now = float(df_5m.iloc[-1]["close"])
                contract = get_itm_option_contract(options_df, spot_now, side)
                if not contract:
                    send_tg(f"Signal {side} found at Spot {spot_now:.1f}, but ITM contract not matched in scrip master.")
                else:
                    sym = contract["symbol"]
                    tok = str(contract["token"])
                    # Approximate premium fallback if live option LTP isn't fetched
                    approx_prem = 160.0 if side == "CE" else 155.0
                    entry_prem = get_ltp(smart, OPTION_EXCHANGE, sym, tok, approx_prem)

                    if PAPER_TRADING:
                        active_pos = PaperPosition(sym, tok, side, entry_prem, DEFAULT_QTY)
                        trades_today += 1
                        send_tg(
                            f"PAPER ENTRY ({side}) {sym}\n"
                            f"Spot: {spot_now:.1f} | Entry Prem: {entry_prem:.2f} | Qty: {DEFAULT_QTY}\n"
                            f"SL: {active_pos.sl_price:.2f} (-{int(SL_PCT*100)}%) | Target: {active_pos.target_price:.2f} (+{int(TARGET_PCT*100)}%)\n"
                            f"Info: {reason}"
                        )
                    else:
                        order_params = {
                            "variety": "NORMAL",
                            "tradingsymbol": sym,
                            "symboltoken": tok,
                            "transactiontype": "BUY",
                            "exchange": OPTION_EXCHANGE,
                            "ordertype": "MARKET",
                            "producttype": "INTRADAY",
                            "duration": "DAY",
                            "price": "0",
                            "squareoff": "0",
                            "stoploss": "0",
                            "quantity": str(DEFAULT_QTY),
                        }
                        resp = smart.placeOrder(order_params)
                        logger.info(f"Live order response: {resp}")
                        send_tg(f"LIVE BUY ORDER SENT ({side}) {sym} Qty:{DEFAULT_QTY} | Resp: {resp}")

            # Periodic Telegram Heartbeat so you always know the bot is active
            if (now - last_heartbeat).total_seconds() >= HEARTBEAT_MINUTES * 60:
                pos_text = f"{active_pos.symbol} @{active_pos.entry_price:.2f}" if active_pos else "FLAT"
                send_tg(
                    f"Heartbeat ({now.strftime('%H:%M')}) | Pos: {pos_text} | Trades: {trades_today} | PnL: Rs {realized_pnl:.2f}\n"
                    f"{last_status_line}"
                )
                last_heartbeat = now

        except Exception as e:
            err_msg = f"Loop error: {e}"
            logger.exception(err_msg)
            send_tg(err_msg)

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    run_bot()
