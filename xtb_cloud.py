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

# State file để chống duplicate (persist qua GitHub Actions Cache)
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(SCRIPT_DIR, "last_sent.txt"))

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
# ANTI-DUPLICATE: STATE FILE
# ──────────────────────────────────────────────────────────────

def get_current_h4_key() -> str:
    """Trả về H4 close key gần nhất (ISO format UTC), VD: '2026-04-05T00:00Z'."""
    now = datetime.now(timezone.utc)
    h4_hours = [0, 4, 8, 12, 16, 20]

    for h in reversed(h4_hours):
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate <= now:
            return candidate.strftime("%Y-%m-%dT%H:%MZ")

    # Trước 00:00 UTC → H4 close = 20:00 UTC hôm qua
    yesterday_20 = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    return yesterday_20.strftime("%Y-%m-%dT%H:%MZ")


def read_state() -> str:
    """Đọc H4 key đã gửi lần cuối. Trả về '' nếu chưa có."""
    try:
        with open(STATE_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_state(h4_key: str):
    """Ghi H4 key đã gửi thành công."""
    with open(STATE_FILE, "w") as f:
        f.write(h4_key)


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
    # Kiểm tra --force flag
    force = "--force" in sys.argv
    argv_clean = [a for a in sys.argv[1:] if a != "--force"]

    # Anti-duplicate: check state file
    h4_key = get_current_h4_key()
    last_sent = read_state()
    if not force and last_sent == h4_key:
        now_vn = datetime.now(TZ_VN)
        print(f"⏭️  SKIP: H4 close {h4_key} đã gửi rồi. ({now_vn.strftime('%H:%M')} GMT+7)")
        print(f"   Dùng --force để gửi lại.")
        return

    # Parse symbols từ args hoặc env
    symbols = []

    # 1. Từ command-line args
    for arg in argv_clean:
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
    print(f"  �️ H4 close: {h4_key}")
    print(f"  �📊 Cặp: {', '.join(symbols)}")
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

    # Ghi state nếu có ít nhất 1 thành công
    if success_count > 0:
        write_state(h4_key)
        print(f"  📝 State saved: {h4_key}")

    # Summary
    print()
    print("=" * 60)
    print(f"  ✅ Hoàn tất: {success_count} thành công, {fail_count} thất bại")
    print("=" * 60)
    print()

    # Exit code cho CI — chỉ fail nếu KHÔNG gửi được gì
    if fail_count > 0 and success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
