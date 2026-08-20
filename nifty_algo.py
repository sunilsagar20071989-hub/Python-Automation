# ==========================================
# 7. SCANNER EXECUTION CYCLE & ENGINE LOOP
# ==========================================


def execute_nifty_algo_cycle():
    """15-Min Interval Algo Scan Execution Log / Cron Alert"""
    spot_ltp = get_nifty_spot_ltp()
    if not spot_ltp:
        logger.error("Unable to fetch Nifty Spot LTP. Exiting cycle.")
        return

    logger.info(f"⚡ [NIFTY SCANNER RUN] Nifty Spot LTP: ₹{spot_ltp:.2f}")

    send_telegram_alert(
        f"🎯 <b>Nifty 15-Min Scan Complete</b>\n"
        f"<b>Spot Level:</b> ₹{spot_ltp:.2f}\n"
        f"<b>Status:</b> Market Scanned Successfully"
    )


def main_loop():
    global pos_active, active_symbol, active_token, entry_price, sl_price, tgt_price
    global highest_price_seen, tsl_activated, active_quantity, trade_entry_time
    global daily_trades_count, consecutive_sl_count, consecutive_win_count, algo_paused

    logger.info("🚀 Nifty Option Algo Bot Started Engine Loop...")
    send_telegram_alert(
        "🚀 <b>Nifty Option Trading Algo Active!</b>\n"
        "Rule 1: 2 Losses -> Stop Trading.\n"
        "Rule 2: 3 Profits -> Stop Trading."
    )

    while True:
        try:
            if not is_market_open():
                logger.info("⏸️ Market Closed. Exiting execution cleanly.")
                sys.exit(0)

            # ----------------------------------------------------
            # 1. AUTO SQUARE-OFF AT 03:10 PM (STRICT CONCURRENT EXIT)
            # ----------------------------------------------------
            if is_squareoff_time():
                if pos_active:
                    ltp = get_live_ltp(active_token, active_symbol) or entry_price
                    logger.info(
                        f"[AUTO SQUARE-OFF 03:10 PM] Exiting {active_symbol} at ₹{ltp:.2f}"
                    )

                    exit_status = place_order(
                        active_symbol, active_token, "SELL", active_quantity
                    )

                    if exit_status:
                        pnl = round((ltp - entry_price) * active_quantity, 2)
                        log_trade(
                            active_symbol,
                            "BUY",
                            entry_price,
                            ltp,
                            active_quantity,
                            "AUTO_SQUARE_OFF",
                        )
                        send_telegram_alert(
                            f"⏰ <b>AUTO SQUARE-OFF (03:10 PM)</b>\n"
                            f"<b>Symbol:</b> {active_symbol}\n"
                            f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                            f"<b>PnL:</b> ₹{pnl}"
                        )
                        cleanup_position()
                        daily_trades_count += 1
                    else:
                        logger.error(f"⚠️ Auto Square-Off Order Failed for {active_symbol}!")
                
                # Market closing window reached - pause scans and avoid new trades
                time.sleep(5.0)
                continue

            # ----------------------------------------------------
            # 2. ACTIVE POSITION MONITOR & TRAILING SL
            # ----------------------------------------------------
            elif pos_active:
                ltp = get_live_ltp(active_token, active_symbol) or entry_price
                now = datetime.now()
                holding_time_mins = (now - trade_entry_time).total_seconds() / 60.0

                if ltp > highest_price_seen:
                    highest_price_seen = ltp

                # Dynamic Trailing SL Logic
                if ENABLE_TRAILING_SL and highest_price_seen > entry_price:
                    gain_pct = (highest_price_seen - entry_price) / entry_price
                    if gain_pct >= TSL_ACTIVATION_PCT:
                        steps = int(
                            (gain_pct - TSL_ACTIVATION_PCT) / TSL_STEP_TRIGGER_PCT
                        ) + 1
                        new_sl = round(
                            entry_price * (1 + (steps * TSL_STEP_MOVE_PCT)), 2
                        )
                        if new_sl > sl_price:
                            sl_price = new_sl
                            tsl_activated = True
                            logger.info(
                                f"[TRAILING SL UPDATED] Raised SL to ₹{sl_price:.2f} (High: ₹{highest_price_seen:.2f})"
                            )

                # A. Stop Loss Check
                if ltp <= sl_price:
                    reason = "TRAILING_SL_HIT" if tsl_activated else "INITIAL_SL_HIT"
                    exit_status = place_order(
                        active_symbol, active_token, "SELL", active_quantity
                    )
                    if exit_status:
                        pnl = round((ltp - entry_price) * active_quantity, 2)
                        log_trade(
                            active_symbol,
                            "BUY",
                            entry_price,
                            ltp,
                            active_quantity,
                            reason,
                        )
                        send_telegram_alert(
                            f"🔴 <b>STOP LOSS HIT ({reason})</b>\n"
                            f"<b>Symbol:</b> {active_symbol}\n"
                            f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                            f"<b>PnL:</b> ₹{pnl}"
                        )
                        cleanup_position()
                        daily_trades_count += 1
                        consecutive_sl_count += 1
                        consecutive_win_count = 0

                # B. Target Check
                elif ltp >= tgt_price:
                    exit_status = place_order(
                        active_symbol, active_token, "SELL", active_quantity
                    )
                    if exit_status:
                        pnl = round((ltp - entry_price) * active_quantity, 2)
                        log_trade(
                            active_symbol,
                            "BUY",
                            entry_price,
                            ltp,
                            active_quantity,
                            "TARGET_ACHIEVED",
                        )
                        send_telegram_alert(
                            f"🟢 <b>TARGET ACHIEVED</b> 🎉\n"
                            f"<b>Symbol:</b> {active_symbol}\n"
                            f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                            f"<b>PnL:</b> ₹{pnl}"
                        )
                        cleanup_position()
                        daily_trades_count += 1
                        consecutive_win_count += 1
                        consecutive_sl_count = 0

                # C. Time Exit Check (Theta Decay Guard)
                elif holding_time_mins >= MAX_HOLDING_MINUTES:
                    exit_status = place_order(
                        active_symbol, active_token, "SELL", active_quantity
                    )
                    if exit_status:
                        pnl = round((ltp - entry_price) * active_quantity, 2)
                        log_trade(
                            active_symbol,
                            "BUY",
                            entry_price,
                            ltp,
                            active_quantity,
                            "THETA_TIMEOUT",
                        )
                        send_telegram_alert(
                            f"⏱️ <b>THETA TIMEOUT EXIT</b>\n"
                            f"<b>Symbol:</b> {active_symbol}\n"
                            f"<b>Exit Price:</b> ₹{ltp:.2f}\n"
                            f"<b>PnL:</b> ₹{pnl}"
                        )
                        cleanup_position()
                        daily_trades_count += 1
                        if ltp < entry_price:
                            consecutive_sl_count += 1
                            consecutive_win_count = 0

            # ----------------------------------------------------
            # 3. NEW ENTRY SCANNING
            # ----------------------------------------------------
            elif not pos_active and not algo_paused and is_new_entry_allowed():
                if daily_trades_count >= MAX_DAILY_TRADES:
                    logger.info(
                        f"🛑 Max Daily Limit ({MAX_DAILY_TRADES}) Reached."
                    )
                elif consecutive_sl_count >= 2:
                    logger.info(
                        "🛑 2 Consecutive Losses Hit! Halting strategy for today."
                    )
                elif consecutive_win_count >= 3:
                    logger.info(
                        "🎉 3 Consecutive Wins Hit! Targets achieved for today."
                    )
                else:
                    signal = generate_trade_signal()

                    if signal in ["CE", "PE"]:
                        spot = get_nifty_spot_ltp()
                        if spot and spot > 0:
                            vix = get_india_vix()
                            macro_trend = get_15m_trend()
                            pcr, res_strike, sup_strike = (
                                get_option_chain_pcr_and_levels(spot)
                            )
                            is_oc_valid = validate_trade_with_option_chain(
                                signal, spot, pcr, res_strike, sup_strike
                            )

                            if not (MIN_VIX <= vix <= MAX_VIX):
                                logger.info(
                                    f"Entry Blocked: VIX ({vix}) out of bounds ({MIN_VIX}-{MAX_VIX})"
                                )
                            elif not is_oc_valid:
                                logger.info(
                                    "Entry Blocked: Failed Option Chain validation"
                                )
                            elif signal == "CE" and macro_trend == "BEARISH":
                                logger.info(
                                    "Entry Blocked: 15m Trend is BEARISH, cannot take CE"
                                )
                            elif signal == "PE" and macro_trend == "BULLISH":
                                logger.info(
                                    "Entry Blocked: 15m Trend is BULLISH, cannot take PE"
                                )
                            else:
                                sym, tok = get_itm_option_scrip(
                                    spot, option_type=signal
                                )

                                if sym and tok:
                                    opt_ltp = get_live_ltp(tok, sym)
                                    if opt_ltp and opt_ltp > 0:
                                        qty = calculate_dynamic_quantity(
                                            opt_ltp
                                        )
                                        order_id = place_order(
                                            sym, tok, "BUY", qty
                                        )

                                        if order_id:
                                            pos_active = True
                                            active_symbol = sym
                                            active_token = tok
                                            active_quantity = qty
                                            entry_price = opt_ltp
                                            sl_price = round(
                                                opt_ltp * (1 - SL_PCT), 2
                                            )
                                            tgt_price = round(
                                                opt_ltp * (1 + TARGET_PCT), 2
                                            )
                                            highest_price_seen = opt_ltp
                                            tsl_activated = False
                                            trade_entry_time = datetime.now()

                                            setup_and_subscribe_websocket(
                                                tok, exchange_type=2
                                            )
                                            send_telegram_alert(
                                                f"🚀 <b>NEW AUTO-TRADE ENTERED ({signal})</b>\n"
                                                f"<b>Symbol:</b> {sym}\n"
                                                f"<b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                                f"<b>SL:</b> ₹{sl_price:.2f}\n"
                                                f"<b>Target:</b> ₹{tgt_price:.2f}\n"
                                                f"<b>Qty:</b> {qty}"
                                            )

            time.sleep(0.5 if pos_active else 2.0)

        except Exception as main_e:
            logger.error(f"Main Engine Exception: {main_e}")
            time.sleep(2)


# ==========================================
# MAIN ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    initialize_smartapi()

    if is_market_open():
        main_loop()
    else:
        logger.info("Market is Closed. Engine will not start.")
