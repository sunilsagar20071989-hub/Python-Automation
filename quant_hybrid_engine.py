name: Hybrid Quant Screener Engine

on:
  schedule:
    - cron: '35 3 * * 1-5'  # 9:05 AM IST
    - cron: '0 4 * * 1-5'   # 9:30 AM IST
    - cron: '0 10 * * 1-5'  # 3:30 PM IST
  workflow_dispatch:

jobs:
  run-screener:
    runs-on: ubuntu-latest

    env:
      FULL_MARKET_MAX_STOCKS: "0"
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      SMARTAPI_KEY: ${{ secrets.SMARTAPI_KEY }}
      SMARTAPI_CLIENT_CODE: ${{ secrets.SMARTAPI_CLIENT_CODE }}
      SMARTAPI_PIN: ${{ secrets.SMARTAPI_PIN }}
      SMARTAPI_TOTP_SECRET: ${{ secrets.SMARTAPI_TOTP_SECRET }}
      FMP_API_KEY: ${{ secrets.FMP_API_KEY }}

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pandas yfinance ta requests pytz python-dotenv openpyxl

      - name: Execute Master Screener Engine
        run: python quant_hybrid_engine.py
