import os
import csv
import time
import logging
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests
import telebot
from SmartApi import SmartConnect
import pyotp

# =====================================================================
# 1. CONFIGURATION & STRATEGY PARAMETERS
# =====================================================================
IST = pytz.timezone("Asia/Kolkata")

API_KEY = os.getenv("ANGEL_API_KEY", "YOUR_API_KEY")
CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "YOUR_CLIENT_CODE")
MPIN = os.getenv("ANGEL_MPIN", "YOUR_MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "YOUR_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TG_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TG_CHAT_ID")

PAPER_TRADING = True                  # True = Paper/No-fund order-book log, False = Live orders
LOG_TRADE_EVEN_WITHOUT_FUNDS = True   # Fund na ho tab bhi order book / alert show kare
TRACK_REJECTED_IN_TRADES_BOOK = False # True karne par rejected signals trades_book.csv mein bhi likhega
SCAN_INTERVAL_SEC = 30
HEARTBEAT_MINUTES = 30

ORDERS_BOOK_FILE = "orders_book.csv"
TRADES_BOOK_FILE = "trades_book.csv"

# Nifty & Option Settings (Buyer Only: CE & PE)
NIFTY_SPOT_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926009"
NIFTY_EXCHANGE = "NSE"
OPTION_EXCHANGE = "NFO"
LOT_QTY = 65                          # Current Nifty lot rules ke hisab se set karein
LOTS_PER_TRADE = 1
TARGET_DELTA = 0.60
FALLBACK_ITM_OFFSET = 100

# Timing Rules
ENTRY_START_TIME = (9, 45)            # Trade after 09:45 AM
SQUARE_OFF_TIME = (15, 10)            # Auto square-off at 03:10 PM
MAX_HOLD_MINUTES = 45                 # Theta timeout if no target/SL in 45 mins

# Opening & Spike Rules
FLAT_OPEN_PCT = 0.15                  # Within +/-0.15% of prev_close => Flat open (No trade)
PULLBACK_EMA_TOL_PCT = 0.25           # Distance tolerance near 21 EMA / prev_close
MAX_CANDLE_RANGE_PCT = 0.75           # Avoid sudden wide spike candles
SIGNAL_TIMEFRAME = "FIVE_MINUTE"      # "FIVE_MINUTE" or "FIFTEEN_MINUTE"

# Indicator & Market Filters
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_CE_MIN = 60.0
RSI_PE_MAX = 40.0

VIX_MIN = 10.0
VIX_MAX = 24.0

PCR_CE_MIN = 0.90
PCR_PE_MAX = 1.10

# Per-Trade Risk / Reward (%)
SL_PCT = 0.075                        # 7.5% Stop Loss on premium
TARGET_PCT = 0.215                    # 21.50% Target on premium

# Daily Risk & Circuit Breakers
MAX_DAILY_TRADES = 4
MAX_CONSECUTIVE_LOSSES = 2
MAX_CONSECUTIVE_WINS = 3

# =====================================================================
# 2. LOGGING, ORDER BOOK, TRADE BOOK & TELEGRAM
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("nifty_auto_itm_algo.log", mode="a"),
    ],
)
logger = logging.getLogger("NiftyAutoITMAlgo")

tg_bot = (
    telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    if TELEGRAM_BOT_TOKEN and "YOUR_TG_BOT_TOKEN" not in TELEGRAM_BOT_TOKEN
    else None
)


def send_tg(msg: str):
    logger.info(msg.replace("\n", " | "))
    if tg_bot and TELEGRAM_CHAT_ID and "YOUR_TG_CHAT_ID" not in TELEGRAM_CHAT_ID:
        try:
            tg_bot.send_message(TELEGRAM_CHAT_ID, msg)
        except Exception as exc:
            logger.error(f"Telegram send error: {exc}")


def append_csv_row(filepath: str, headers: list[str], row_dict: dict):
    file_exists = os.path.isfile(filepath)
    with open(filepath, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)


def record_order_book(
    now_ist: datetime,
    symbol: str,
    side: str,
    action: str,
    price: float,
    qty: int,
    status: str,
    notes: str,
):
    headers = [
        "timestamp", "symbol", "side", "action", "price", "qty", "status", "notes"
    ]
    append_csv_row(
        ORDERS_BOOK_FILE,
        headers,
        {
            "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "action": action,
            "price": round(price, 2),
            "qty": qty,
            "status": status,
            "notes": notes,
        },
    )


def record_trade_book(
    entry_time: datetime,
    exit_time: datetime,
    symbol: str,
    side: str,
    qty: int,
    entry_price: float,
    exit_price: float,
    pnl: float,
    exit_reason: str,
    mode_tag: str,
):
    headers = [
        "entry_time", "exit_time", "symbol", "side", "qty",
        "entry_price", "exit_price", "pnl", "exit_reason", "mode_tag"
    ]
    append_csv_row(
        TRADES_BOOK_FILE,
        headers,
        {
            "entry_time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "pnl": round(pnl, 2),
            "exit_reason": exit_reason,
            "mode_tag": mode_tag,
        },
    )


# =====================================================================
# 3. BROKER & OPTION CHAIN HELPERS
# =====================================================================
def connect_smartapi() -> SmartConnect:
    client = SmartConnect(api_key=API_KEY)
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    session = client.generateSession(CLIENT_CODE, MPIN, totp_code)
    if not session or not session.get("status"):
        raise RuntimeError(f"SmartAPI login failed: {session}")
    logger.info("SmartAPI session active.")
    return client


def get_available_margin(client: SmartConnect) -> float:
    """
    Returns available cash/margin if fetchable; returns -1.0 if endpoint unavailable.
    """
    try:
        rms = client.rmsLimit()
        data = rms.get("data") if isinstance(rms, dict) else None
        if isinstance(data, dict):
            for key in ("availablecash", "net"):
                if key in data and data[key] is not None:
                    return float(data[key])
    except Exception:
        pass
    return -1.0


def load_nifty_options_master() -> pd.DataFrame:
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    nifty_df = df[(df["name"] == "NIFTY") & (df["instrumenttype"] == "OPTIDX")].copy()
    nifty_df["strike"] = pd.to_numeric(nifty_df["strike"], errors="coerce")
    nifty_df["expiry_dt"] = pd.to_datetime(nifty_df["expiry"], format="%d%b%Y", errors="coerce")
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    nifty_df = nifty_df[nifty_df["expiry_dt"] >= today_start].sort_values("expiry_dt")
    return nifty_df


def fetch_candles(client: SmartConnect, token: str, interval: str, days: int = 5) -> pd.DataFrame:
    now_ist = datetime.now(IST)
    from_dt = (now_ist - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    to_dt = now_ist.strftime("%Y-%m-%d %H:%M")

    res = client.getCandleData({
        "exchange": NIFTY_EXCHANGE,
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_dt,
        "todate": to_dt,
    })
    rows = res.get("data") if isinstance(res, dict) else None
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)


def get_india_vix(client: SmartConnect) -> float:
    try:
        q = client.ltpData(NIFTY_EXCHANGE, "INDIA VIX", INDIA_VIX_TOKEN)
        val = q.get("data", {}).get("ltp")
        if val and float(val) > 0:
            return float(val)
    except Exception:
        pass
    return 15.0


def check_oi_and_pcr(client: SmartConnect, side: str) -> tuple[bool, str]:
    try:
        if hasattr(client, "getOptionChain"):
            oc = client.getOptionChain("NIFTY", "")
            data = oc.get("data") if isinstance(oc, dict) else None
            if isinstance(data, list) and len(data) > 0:
                ce_oi = sum(float(x.get("ce", {}).get("oi", 0) or 0) for x in data)
                pe_oi = sum(float(x.get("pe", {}).get("oi", 0) or 0) for x in data)
                if ce_oi > 0:
                    pcr = pe_oi / ce_oi
                    if side == "CE" and pcr < PCR_CE_MIN:
                        return False, f"PCR {pcr:.2f} < {PCR_CE_MIN} blocks CE"
                    if side == "PE" and pcr > PCR_PE_MAX:
                        return False, f"PCR {pcr:.2f} > {PCR_PE_MAX} blocks PE"
                    return True, f"PCR:{pcr:.2f} (CE_OI:{int(ce_oi)}, PE_OI:{int(pe_oi)})"
    except Exception as exc:
        logger.debug(f"Option chain OI check fallback: {exc}")
    return True, "PCR/OI:Fallback_OK"


def select_itm_060_contract(nifty_df: pd.DataFrame, spot: float, side: str):
    atm = round(spot / 50) * 50
    target_strike = (atm - FALLBACK_ITM_OFFSET) if side == "CE" else (atm + FALLBACK_ITM_OFFSET)
    suffix = "CE" if side == "CE" else "PE"

    nearest_exp = nifty_df["expiry_dt"].iloc[0]
    exp_df = nifty_df[nifty_df["expiry_dt"] == nearest_exp].copy()

    candidate_strikes = (
        [target_strike, target_strike - 50, target_strike + 50]
        if side == "CE"
        else [target_strike, target_strike + 50, target_strike - 50]
    )

    for st in candidate_strikes:
        if side == "CE" and st >= spot:
            continue
        if side == "PE" and st <= spot:
            continue

        matched = exp_df[
            (exp_df["strike"] == st * 100) & (exp_df["symbol"].str.endswith(suffix))
        ]
        if matched.empty:
            matched = exp_df[
                (exp_df["strike"] == st) & (exp_df["symbol"].str.endswith(suffix))
            ]
        if not matched.empty:
            row = matched.iloc[0].to_dict()
            row["chosen_strike"] = st
            row["target_delta"] = TARGET_DELTA
            return row

    return None


# =====================================================================
# 4. OPENING, 15M MACRO & ENTRY SIGNAL ENGINE
# =====================================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_9"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema_21"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)
    return out


def classify_opening_and_prev_close(df_15m: pd.DataFrame, now_ist: datetime):
    today_str = now_ist.strftime("%Y-%m-%d")
    today_df = df_15m[df_15m["timestamp"].dt.strftime("%Y-%m-%d") == today_str]
    prev_df = df_15m[df_15m["timestamp"].dt.strftime("%Y-%m-%d") < today_str]

    if today_df.empty or prev_df.empty:
        return "UNKNOWN", None, None

    prev_close = float(prev_df.iloc[-1]["close"])
    day_open = float(today_df.iloc[0]["open"])
    gap_pct = ((day_open - prev_close) / prev_close) * 100.0

    if abs(gap_pct) <= FLAT_OPEN_PCT:
        open_type = "FLAT"
    elif gap_pct > FLAT_OPEN_PCT:
        open_type = "GAP_UP"
    else:
        open_type = "GAP_DOWN"

    return open_type, prev_close, day_open


def evaluate_entry_conditions(
    df_15m: pd.DataFrame,
    df_sig: pd.DataFrame,
    vix: float,
    now_ist: datetime,
):
    if df_15m.empty or len(df_15m) < 25 or df_sig.empty or len(df_sig) < 25:
        return None, "Insufficient candle history"

    if not (VIX_MIN <= vix <= VIX_MAX):
        return None, f"VIX {vix:.2f} outside [{VIX_MIN}, {VIX_MAX}]"

    ind_15m = add_indicators(df_15m)
    last_15m = ind_15m.iloc[-1]
    ema9_15m = float(last_15m["ema_9"])
    ema21_15m = float(last_15m["ema_21"])

    if ema9_15m > ema21_15m:
        macro_15m = "BULLISH"
    elif ema9_15m < ema21_15m:
        macro_15m = "BEARISH"
    else:
        macro_15m = "NEUTRAL"

    open_type, prev_close, day_open = classify_opening_and_prev_close(df_15m, now_ist)
    if open_type == "FLAT":
        return None, f"FLAT OPEN ({day_open:.1f} vs prev close {prev_close:.1f}) -> NO TRADE"
    if open_type == "UNKNOWN" or prev_close is None:
        return None, "Prev close / opening data unavailable"

    ind_sig = add_indicators(df_sig)
    curr = ind_sig.iloc[-1]
    prev = ind_sig.iloc[-2]

    spot = float(curr["close"])
    high_c = float(curr["high"])
    low_c = float(curr["low"])
    ema9 = float(curr["ema_9"])
    ema21 = float(curr["ema_21"])
    prev_ema9 = float(prev["ema_9"])
    prev_ema21 = float(prev["ema_21"])
    rsi_val = float(curr["rsi"])

    candle_range_pct = ((high_c - low_c) / low_c) * 100.0 if low_c > 0 else 0.0
    if candle_range_pct > MAX_CANDLE_RANGE_PCT:
        return None, f"Spike avoided (candle range {candle_range_pct:.2f}% > {MAX_CANDLE_RANGE_PCT}%)"

    near_prev_close = abs((spot - prev_close) / prev_close) * 100.0 <= PULLBACK_EMA_TOL_PCT
    near_21_ema = abs((spot - ema21) / ema21) * 100.0 <= PULLBACK_EMA_TOL_PCT

    loc_ok = near_prev_close or near_21_ema
    loc_desc = f"{open_type} near PrevClose/21EMA"

    if not loc_ok:
        return None, f"{open_type} waiting near PrevClose ({prev_close:.1f}) or 21EMA ({ema21:.1f}) | Spot:{spot:.1f}"

    bull_cross = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
    bear_cross = (prev_ema9 >= prev_ema21) and (ema9 < ema21)

    if macro_15m == "BULLISH" and bull_cross and rsi_val >= RSI_CE_MIN:
        return "CE", f"CE Signal | {loc_desc} | 15m:BULLISH | 9x21 CrossUp | RSI:{rsi_val:.1f} | VIX:{vix:.2f}"

    if macro_15m == "BEARISH" and bear_cross and rsi_val <= RSI_PE_MAX:
        return "PE", f"PE Signal | {loc_desc} | 15m:BEARISH | 9x21 CrossDn | RSI:{rsi_val:.1f} | VIX:{vix:.2f}"

    return None, (
        f"Scan | Open:{open_type} Spot:{spot:.1f} 21EMA:{ema21:.1f} | "
        f"15m:{macro_15m} | RSI:{rsi_val:.1f} | VIX:{vix:.2f}"
    )


# =====================================================================
# 5. ORDER SUBMISSION, POSITION & DAILY CIRCUIT BREAKERS
# =====================================================================
class ActiveOptionTrade:
    def __init__(self, symbol: str, token: str, side: str, entry_price: float, qty: int, mode_tag: str):
        self.symbol = symbol
        self.token = token
        self.side = side
        self.entry_price = entry_price
        self.qty = qty
        self.mode_tag = mode_tag
        self.entry_time = datetime.now(IST)
        self.sl_price = round(entry_price * (1.0 - SL_PCT), 2)
        self.target_price = round(entry_price * (1.0 + TARGET_PCT), 2)


def get_ltp(client: SmartConnect, exchange: str, symbol: str, token: str, fallback: float) -> float:
    try:
        q = client.ltpData(exchange, symbol, token)
        ltp = q.get("data", {}).get("ltp")
        if ltp and float(ltp) > 0:
            return float(ltp)
    except Exception:
        pass
    return fallback


def submit_order_with_book_tracking(
    client: SmartConnect,
    now_ist: datetime,
    symbol: str,
    token: str,
    side: str,
    entry_prem: float,
    qty: int,
    reason: str,
    oi_info: str,
) -> tuple[bool, str]:
    """
    Attempts order or simulates check; ensures orders rejected due to insufficient funds
    are explicitly recorded in ORDERS_BOOK_FILE as REJECTED_INSUFFICIENT_FUNDS.
    """
    req_margin = entry_prem * qty
    avail_margin = get_available_margin(client)
    has_sufficient_funds = True if avail_margin < 0 else (avail_margin >= req_margin)

    if not PAPER_TRADING:
        order_payload = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": "BUY",
            "exchange": OPTION_EXCHANGE,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }
        try:
            resp = client.placeOrder(order_payload)
            logger.info(f"Broker placeOrder response: {resp}")
            resp_str = str(resp).lower()
            is_rejected = (
                isinstance(resp, dict) and not resp.get("status", True)
            ) or any(k in resp_str for k in ("reject", "insufficient", "margin", "fund"))

            if is_rejected or not has_sufficient_funds:
                status_tag = "REJECTED_INSUFFICIENT_FUNDS"
                record_order_book(
                    now_ist, symbol, side, "BUY", entry_prem, qty,
                    status=status_tag,
                    notes=f"Rejected due to funds | Req:{req_margin:.0f}, Avail:{avail_margin:.0f} | Resp:{resp} | {reason}"
                )
                if TRACK_REJECTED_IN_TRADES_BOOK:
                    record_trade_book(
                        now_ist, now_ist, symbol, side, qty, entry_prem, entry_prem,
                        0.0, "REJECTED_INSUFFICIENT_FUNDS", "NO_FUND_REJECTED"
                    )
                send_tg(
                    f"ORDER REJECTED (NO FUNDS) | {side} {symbol}\n"
                    f"Req: Rs {req_margin:.0f} | Avail: Rs {avail_margin:.0f}\n"
                    f"Order Book: {status_tag}\n"
                    f"Resp: {resp}"
                )
                return False, status_tag

            status_tag = "EXECUTED_LIVE"
            record_order_book(
                now_ist, symbol, side, "BUY", entry_prem, qty,
                status=status_tag,
                notes=f"{reason} | {oi_info} | Resp:{resp}"
            )
            return True, status_tag

        except Exception as exc:
            status_tag = "REJECTED_INSUFFICIENT_FUNDS"
            record_order_book(
                now_ist, symbol, side, "BUY", entry_prem, qty,
                status=status_tag,
                notes=f"Rejected/error (check funds): {exc} | {reason}"
            )
            if TRACK_REJECTED_IN_TRADES_BOOK:
                record_trade_book(
                    now_ist, now_ist, symbol, side, qty, entry_prem, entry_prem,
                    0.0, "REJECTED_INSUFFICIENT_FUNDS", "NO_FUND_REJECTED"
                )
            send_tg(
                f"ORDER REJECTED (NO FUNDS/ERR) | {side} {symbol}\n"
                f"Error: {exc}\n"
                f"Order Book: {status_tag}"
            )
            return False, status_tag

    # Paper mode: show rejected-due-to-funds if margin is short, else paper executed
    if not has_sufficient_funds:
        status_tag = "REJECTED_INSUFFICIENT_FUNDS"
        record_order_book(
            now_ist, symbol, side, "BUY", entry_prem, qty,
            status=status_tag,
            notes=f"Paper no-fund check | Req:{req_margin:.0f}, Avail:{avail_margin:.0f} | {reason}"
        )
        if TRACK_REJECTED_IN_TRADES_BOOK:
            record_trade_book(
                now_ist, now_ist, symbol, side, qty, entry_prem, entry_prem,
                0.0, "REJECTED_INSUFFICIENT_FUNDS", "NO_FUND_REJECTED"
            )
        send_tg(
            f"ORDER REJECTED (NO FUNDS - PAPER) | {side} {symbol}\n"
            f"Req: Rs {req_margin:.0f} | Avail: Rs {avail_margin:.0f}\n"
            f"Order Book: {status_tag}"
        )
        return False, status_tag

    status_tag = "PAPER_EXECUTED"
    record_order_book(
        now_ist, symbol, side, "BUY", entry_prem, qty,
        status=status_tag,
        notes=f"{reason} | {oi_info}"
    )
    return True, status_tag


def run_algo():
    qty = LOT_QTY * LOTS_PER_TRADE
    send_tg(
        f"Nifty Auto ITM Algo Started\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'} | NoFundShow: {LOG_TRADE_EVEN_WITHOUT_FUNDS}\n"
        f"Start: {ENTRY_START_TIME[0]:02d}:{ENTRY_START_TIME[1]:02d} | SqOff: {SQUARE_OFF_TIME[0]:02d}:{SQUARE_OFF_TIME[1]:02d}\n"
        f"SL: {SL_PCT*100:.1f}% | Target: {TARGET_PCT*100:.2f}% | Timeout: {MAX_HOLD_MINUTES}m"
    )

    client = connect_smartapi()
    nifty_df = load_nifty_options_master()

    trade: ActiveOptionTrade | None = None
    trades_today = 0
    consec_losses = 0
    consec_wins = 0
    day_pnl = 0.0
    halted_reason: str | None = None
    last_hb = datetime.now(IST) - timedelta(minutes=HEARTBEAT_MINUTES)
    last_status = "Initializing..."

    while True:
        now = datetime.now(IST)
        m_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        m_close = now.replace(hour=15, minute=25, second=0, microsecond=0)
        entry_after = now.replace(hour=ENTRY_START_TIME[0], minute=ENTRY_START_TIME[1], second=0, microsecond=0)
        sq_off_dt = now.replace(hour=SQUARE_OFF_TIME[0], minute=SQUARE_OFF_TIME[1], second=0, microsecond=0)

        if now.weekday() >= 5 or not (m_open <= now <= m_close):
            logger.info(f"Market closed ({now.strftime('%Y-%m-%d %H:%M:%S')}).")
            time.sleep(60)
            continue

        try:
            df_15m = fetch_candles(client, NIFTY_SPOT_TOKEN, "FIFTEEN_MINUTE", days=7)
            df_sig = fetch_candles(client, NIFTY_SPOT_TOKEN, SIGNAL_TIMEFRAME, days=5)
            vix = get_india_vix(client)

            # 1. Manage Open Position
            if trade is not None:
                ltp = get_ltp(client, OPTION_EXCHANGE, trade.symbol, trade.token, trade.entry_price)
                elapsed_mins = (now - trade.entry_time).total_seconds() / 60.0

                exit_reason = None
                outcome = "FLAT"

                if ltp <= trade.sl_price:
                    exit_reason = f"SL Hit ({ltp:.2f} <= {trade.sl_price:.2f})"
                    outcome = "LOSS"
                elif ltp >= trade.target_price:
                    exit_reason = f"Target Hit ({ltp:.2f} >= {trade.target_price:.2f})"
                    outcome = "WIN"
                elif elapsed_mins >= MAX_HOLD_MINUTES:
                    exit_reason = f"45-Min Timeout ({elapsed_mins:.1f}m)"
                    outcome = "WIN" if ltp > trade.entry_price else ("LOSS" if ltp < trade.entry_price else "FLAT")
                elif now >= sq_off_dt:
                    exit_reason = "03:10 PM Square-off"
                    outcome = "WIN" if ltp > trade.entry_price else ("LOSS" if ltp < trade.entry_price else "FLAT")

                if exit_reason:
                    pnl = (ltp - trade.entry_price) * trade.qty
                    day_pnl += pnl

                    if outcome == "LOSS":
                        consec_losses += 1
                        consec_wins = 0
                    elif outcome == "WIN":
                        consec_wins += 1
                        consec_losses = 0

                    if consec_losses >= MAX_CONSECUTIVE_LOSSES:
                        halted_reason = f"Circuit Breaker: {consec_losses} consecutive SL hits"
                    elif consec_wins >= MAX_CONSECUTIVE_WINS:
                        halted_reason = f"Circuit Breaker: {consec_wins} consecutive wins"
                    elif trades_today >= MAX_DAILY_TRADES:
                        halted_reason = f"Max {MAX_DAILY_TRADES} daily trades reached"

                    record_order_book(
                        now, trade.symbol, trade.side, "SELL_EXIT", ltp, trade.qty,
                        status=f"RECORDED_{trade.mode_tag}", notes=exit_reason
                    )
                    record_trade_book(
                        trade.entry_time, now, trade.symbol, trade.side, trade.qty,
                        trade.entry_price, ltp, pnl, exit_reason, trade.mode_tag
                    )

                    send_tg(
                        f"EXIT ({trade.side}) {trade.symbol} [{trade.mode_tag}]\n"
                        f"Reason: {exit_reason}\n"
                        f"Entry: {trade.entry_price:.2f} -> Exit: {ltp:.2f}\n"
                        f"PnL: Rs {pnl:.2f} | Day PnL: Rs {day_pnl:.2f}\n"
                        f"Streak: {consec_wins}W / {consec_losses}L | Halted: {halted_reason or 'No'}"
                    )
                    trade = None

            # 2. Entry Check When Flat & After 09:45 AM
            else:
                if halted_reason:
                    last_status = f"HALTED ({halted_reason})"
                elif now < entry_after:
                    last_status = f"Waiting till {ENTRY_START_TIME[0]:02d}:{ENTRY_START_TIME[1]:02d} AM"
                elif now >= sq_off_dt:
                    last_status = "Past 03:10 PM square-off cutoff"
                elif trades_today >= MAX_DAILY_TRADES:
                    halted_reason = f"Max {MAX_DAILY_TRADES} daily trades reached"
                    last_status = halted_reason
                else:
                    side, reason = evaluate_entry_conditions(df_15m, df_sig, vix, now)
                    last_status = reason

                    if side in ("CE", "PE"):
                        oi_ok, oi_info = check_oi_and_pcr(client, side)
                        if not oi_ok:
                            last_status = f"{side} rejected by {oi_info}"
                        else:
                            spot = float(df_sig.iloc[-1]["close"])
                            contract = select_itm_060_contract(nifty_df, spot, side)
                            if not contract:
                                send_tg(f"Signal {side} at {spot:.1f}, par ITM contract nahi mila.")
                            else:
                                sym = contract["symbol"]
                                tok = str(contract["token"])
                                fallback_prem = 165.0 if side == "CE" else 160.0
                                entry_prem = get_ltp(client, OPTION_EXCHANGE, sym, tok, fallback_prem)

                                accepted, status_tag = submit_order_with_book_tracking(
                                    client, now, sym, tok, side, entry_prem, qty, reason, oi_info
                                )
                                if accepted:
                                    trade = ActiveOptionTrade(sym, tok, side, entry_prem, qty, status_tag)
                                    trades_today += 1
                                    send_tg(
                                        f"ENTRY ({side}) {sym} [{status_tag}]\n"
                                        f"Strike: {contract['chosen_strike']} (~{TARGET_DELTA:.2f} Delta) | Prem: {entry_prem:.2f} | Qty: {qty}\n"
                                        f"SL: {trade.sl_price:.2f} (-{SL_PCT*100:.1f}%) | Target: {trade.target_price:.2f} (+{TARGET_PCT*100:.2f}%)\n"
                                        f"{reason} | {oi_info}"
                                    )
                                elif LOG_TRADE_EVEN_WITHOUT_FUNDS:
                                    last_status = f"{side} {sym} -> {status_tag} (logged in {ORDERS_BOOK_FILE})"

            logger.info(last_status)

            # 3. Periodic Telegram Heartbeat
            if (now - last_hb).total_seconds() >= HEARTBEAT_MINUTES * 60:
                pos_str = f"{trade.symbol} @{trade.entry_price:.2f} [{trade.mode_tag}]" if trade else "FLAT"
                send_tg(
                    f"Heartbeat ({now.strftime('%H:%M')}) | {pos_str} | "
                    f"Trades: {trades_today}/{MAX_DAILY_TRADES} | PnL: Rs {day_pnl:.2f}\n"
                    f"{last_status}"
                )
                last_hb = now

        except Exception as exc:
            err_msg = f"Algo error: {exc}"
            logger.exception(err_msg)
            send_tg(err_msg)

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    run_algo()
