#!/usr/bin/env python3
from __future__ import annotations
"""
XTB-Springtea Cloud Monitor
==============================
Chạy trên GitHub Actions (hoặc cloud khác), phân tích H4 và gửi Telegram.
Stateless — chạy 1 lần, gửi kết quả, thoát.

Env vars cần thiết:
    TELEGRAM_BOT_TOKEN  — Bot token từ @BotFather
    TELEGRAM_CHAT_ID    — Chat ID nhận thông báo

Cách dùng:
    python3 xtb_cloud.py                          # Mặc định: BTC, BNB, LINK
    python3 xtb_cloud.py ETHUSDT SOLUSDT           # Chỉ định cặp
    SYMBOLS=BTCUSDT,BNBUSDT python3 xtb_cloud.py   # Từ env var
"""

import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────────────────────
# Import từ xtb_analyzer.py + xtb_monitor.py
# ──────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from xtb_analyzer import (
    fetch_klines, fetch_price,
    calc_rsi, calc_ema, calc_wma, calc_jma, calc_vwap_session,
    analyze_timeframe, evaluate_consensus, fmt, fmt4, bool_icon,
    signal_label, TIMEFRAMES,
)
from xtb_monitor import format_telegram_rich

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEFAULT_SYMBOLS = ["BTCUSDT", "BNBUSDT", "LINKUSDT"]
SIDE = "buy"

TZ_VN = timezone(timedelta(hours=7))

# ──────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Gửi thông báo qua Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID chưa được cấu hình!")
        print("   Thiết lập trong GitHub Secrets hoặc env vars.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=15)
        print(f"   ✅ Telegram sent ({len(message)} chars)")
        return True
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# PHÂN TÍCH
# ──────────────────────────────────────────────────────────────

def analyze_symbol(symbol: str, side: str = "buy") -> str | None:
    """Phân tích 1 symbol, trả về Telegram message hoặc None nếu lỗi."""
    try:
        price = fetch_price(symbol)
        results = []
        for interval, label in TIMEFRAMES:
            try:
                r = analyze_timeframe(symbol, interval, label)
                results.append(r)
            except Exception as e:
                print(f"      ⚠️ {label}: {e}")

        if not results:
            return None

        consensus = evaluate_consensus(results, side)
        telegram_body = format_telegram_rich(symbol, results, consensus, price, side)
        return telegram_body

    except Exception as e:
        print(f"   ❌ {symbol}: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    # Parse symbols từ args hoặc env
    symbols = []

    # 1. Từ command-line args
    for arg in sys.argv[1:]:
        sym = arg.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        symbols.append(sym)

    # 2. Từ env var SYMBOLS (comma-separated)
    if not symbols:
        env_symbols = os.environ.get("SYMBOLS", "")
        if env_symbols:
            for s in env_symbols.split(","):
                sym = s.strip().upper()
                if sym:
                    if not sym.endswith("USDT"):
                        sym += "USDT"
                    symbols.append(sym)

    # 3. Mặc định
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    side = os.environ.get("SIDE", "buy").lower()

    # Header
    now = datetime.now(TZ_VN)
    print()
    print("=" * 60)
    print(f"  🔍 XTB-SPRINGTEA CLOUD MONITOR")
    print(f"  🕐 {now.strftime('%d/%m/%Y %H:%M:%S')} (GMT+7)")
    print(f"  📊 Cặp: {', '.join(symbols)}")
    print(f"  🎯 Chiến lược: {'BUY (LONG)' if side == 'buy' else 'SELL (SHORT)'}")
    print("=" * 60)
    print()

    # Chạy phân tích
    success_count = 0
    fail_count = 0

    for symbol in symbols:
        print(f"  📊 Đang phân tích {symbol}...")
        body = analyze_symbol(symbol, side)
        if body:
            # In ra console (cho GitHub Actions log)
            print()
            for line in body.split("\n"):
                print(f"     {line}")
            print()

            # Gửi Telegram
            if send_telegram(body):
                success_count += 1
            else:
                fail_count += 1
        else:
            print(f"   ❌ Không có kết quả cho {symbol}")
            fail_count += 1

    # Summary
    print()
    print("=" * 60)
    print(f"  ✅ Hoàn tất: {success_count} thành công, {fail_count} thất bại")
    print("=" * 60)
    print()

    # Exit code cho CI
    if fail_count > 0 and success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
