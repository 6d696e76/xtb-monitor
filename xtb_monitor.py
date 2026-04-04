#!/usr/bin/env python3
from __future__ import annotations
"""
XTB-Springtea H4 Candle Monitor v4
====================================
Chạy nền, tự động phân tích mỗi khi nến H4 đóng và gửi thông báo macOS.
Tích hợp v4: Trap, Form, Exit, DCA alerts.

Nến H4 Binance đóng lúc (UTC): 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
Tương đương (GMT+7):            07:00, 11:00, 15:00, 19:00, 23:00, 03:00

Cách dùng:
    python3 xtb_monitor.py                         # Mặc định: LINKUSDT
    python3 xtb_monitor.py BTCUSDT                 # Chỉ định cặp
    python3 xtb_monitor.py LINKUSDT BTCUSDT ETH    # Nhiều cặp
    python3 xtb_monitor.py --test                  # Chạy thử ngay, không chờ

Dừng monitor:
    Ctrl+C hoặc kill process
"""

import json
import math
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────────────────────
# Import từ xtb_analyzer.py
# ──────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from xtb_analyzer import (
    fetch_klines, fetch_price,
    calc_rsi, calc_ema, calc_wma, calc_jma, calc_vwap_session,
    analyze_timeframe, evaluate_consensus, fmt, fmt4, bool_icon,
    signal_label, TIMEFRAMES,
)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

H4_INTERVAL_SECONDS = 4 * 60 * 60  # 4 giờ
BUFFER_SECONDS = 5  # Chờ 5s sau khi nến đóng để data ổn định
LOG_DIR = os.path.join(SCRIPT_DIR, "monitor_logs")
TZ_VN = timezone(timedelta(hours=7))

# Telegram Bot
TELEGRAM_BOT_TOKEN = "8251878627:AAEt7IekJ-_hjdSD7EXKCXU1oCyqnOAIuBk"
TELEGRAM_CHAT_ID = "7032370687"

# ──────────────────────────────────────────────────────────────
# macOS NOTIFICATION
# ──────────────────────────────────────────────────────────────

def send_mac_notification(title: str, message: str, sound: str = "Glass"):
    """Gửi thông báo macOS native qua osascript."""
    # Escape quotes
    title_safe = title.replace('"', '\\"')
    message_safe = message.replace('"', '\\"')
    script = f'display notification "{message_safe}" with title "{title_safe}" sound name "{sound}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception as e:
        log(f"⚠️ Không gửi được notification: {e}")


def send_mac_alert(title: str, message: str):
    """Gửi dialog alert macOS (hiện hộp thoại to)."""
    title_safe = title.replace('"', '\\"')
    message_safe = message.replace('"', '\\"')
    script = f'''
    tell application "System Events"
        display dialog "{message_safe}" with title "{title_safe}" buttons {{"OK"}} default button "OK" giving up after 30
    end tell
    '''
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def send_telegram(message: str):
    """Đẩy thông báo qua Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ Telegram error: {e}")

# ──────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────

def log(msg: str):
    """In log với timestamp."""
    now = datetime.now(TZ_VN).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def save_log(symbol: str, content: str):
    """Lưu kết quả ra file log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now(TZ_VN).strftime("%Y-%m-%d")
    filepath = os.path.join(LOG_DIR, f"{symbol}_{date_str}.log")
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"Thời gian: {datetime.now(TZ_VN).strftime('%Y-%m-%d %H:%M:%S')} (GMT+7)\n")
        f.write(content)
        f.write("\n")

# ──────────────────────────────────────────────────────────────
# TELEGRAM RICH FORMAT
# ──────────────────────────────────────────────────────────────

# Coin icons đã biết
COIN_ICONS = {
    "BTCUSDT": "₿", "ETHUSDT": "Ξ", "BNBUSDT": "🔶",
    "LINKUSDT": "🔗", "SOLUSDT": "◎", "DOTUSDT": "●",
    "ADAUSDT": "🔵", "XRPUSDT": "✕", "DOGEUSDT": "🐕",
    "AVAXUSDT": "🔺", "MATICUSDT": "🟣", "NEARUSDT": "🌐",
}


def _rsi_zone_vi(rsi: float | None) -> str:
    """Mô tả vùng RSI bằng tiếng Việt ngắn gọn."""
    if rsi is None:
        return "N/A"
    if rsi >= 80:
        return "rất mạnh, quá mua"
    elif rsi >= 70:
        return "mạnh, gần quá mua"
    elif rsi >= 50:
        return "vùng trung tính"
    elif rsi >= 35:
        return "vùng yếu"
    elif rsi >= 20:
        return "vùng yếu, gần đổ xăng"
    else:
        return "cực yếu, đang đổ xăng"


def _rsi_trend_vi(delta: float | None) -> str:
    """Mô tả xu hướng RSI delta."""
    if delta is None:
        return ""
    if delta > 1.5:
        return "đang tăng mạnh"
    elif delta > 0.3:
        return "đang tăng nhẹ"
    elif delta < -1.5:
        return "đang giảm mạnh"
    elif delta < -0.3:
        return "đang giảm nhẹ"
    else:
        return "đi ngang"


def _buy_icon(is_favorable: bool | None) -> str:
    """Icon cho BUY: ✅ thuận, ➖ trung tính, ❌ nghịch."""
    if is_favorable is True:
        return "✅"
    elif is_favorable is False:
        return "❌"
    return "➖"


def format_telegram_rich(symbol: str, results: list[dict],
                         consensus: dict, price: float,
                         side: str = "buy") -> str:
    """
    Sinh message Telegram dạng đẹp, tiếng Việt mô tả chi tiết.
    Tương thích format trong telegram_template.txt.
    """
    pair = symbol.replace("USDT", "/USDT")
    icon = COIN_ICONS.get(symbol, "📊")
    lines = [f"{icon} {pair}", f"Giá: ${price:,.2f}", ""]

    sig_key = "buy_signal" if side == "buy" else "sell_signal"
    mochi_key = "mochi_buy" if side == "buy" else "mochi_sell"

    for r in results:
        rsi = r["rsi"]
        delta = r.get("rsi_delta")
        label = r["label"]
        lines.append(f"--- Khung {label} ---")

        # ── 1. RSI line ──
        zone_vi = _rsi_zone_vi(rsi)
        trend_vi = _rsi_trend_vi(delta)
        # Icon: RSI > 50 thuận BUY, RSI 35-50 trung tính, < 35 nghịch (nếu chưa đổ xăng)
        if side == "buy":
            rsi_fav = rsi > 50 if rsi is not None else None
            if rsi is not None and rsi <= 35:
                rsi_fav = None  # vùng đổ xăng = trung tính (cơ hội bắt đáy)
        else:
            rsi_fav = rsi < 50 if rsi is not None else None
            if rsi is not None and rsi >= 65:
                rsi_fav = None
        rsi_desc = f"RSI {fmt(rsi, 1)}"
        if zone_vi and trend_vi:
            rsi_desc += f" — {zone_vi}, {trend_vi}."
        elif zone_vi:
            rsi_desc += f" — {zone_vi}."
        lines.append(f"{_buy_icon(rsi_fav)} {rsi_desc}")

        # ── 2. RSI position vs EMA/WMA ──
        rsi_ae = r.get("rsi_above_ema")
        rsi_aw = r.get("rsi_above_wma")
        sig_val = r[sig_key]
        if sig_val != "POINT_3":
            if rsi_ae is True and rsi_aw is False:
                pos_icon = _buy_icon(True) if side == "buy" else _buy_icon(False)
                wma_v = fmt(r["wma45"], 1)
                lines.append(f"{pos_icon} RSI đã vượt EMA9 nhưng chưa vượt WMA45, đang trong quá trình chuyển đổi.")
            elif rsi_ae is False and rsi_aw is False:
                lines.append(f"{_buy_icon(False) if side == 'buy' else _buy_icon(True)} RSI nằm dưới cả EMA9 và WMA45, chưa có tín hiệu tăng.")

        # ── 3. Signal line ──
        signal_descs = {
            "POINT_3": ("Điểm 3", "xu hướng đã xác nhận, có thể vào theo đà."),
            "POINT_2": ("Điểm 2", "RSI đang cân bằng giữa EMA và WMA, chờ xác nhận."),
            "POINT_1_ZONE": ("Vùng Điểm 1", "cơ hội bắt đáy nhưng rủi ro cao."),
            "APPROACHING": ("Đang tiếp cận", "RSI vượt EMA9, hướng về WMA45."),
        }
        if sig_val in signal_descs:
            name, desc = signal_descs[sig_val]
            # Icon: P3 = thuận, P2 = trung tính, P1 = nghịch, APPROACHING = trung tính
            if sig_val == "POINT_3":
                sig_icon = _buy_icon(True)
            elif sig_val in ("POINT_2", "APPROACHING"):
                sig_icon = _buy_icon(None)
            else:
                sig_icon = _buy_icon(None)
            lines.append(f"{sig_icon} Tín hiệu: {name} — {desc}")

        # ── 4. Mochi line ──
        if r.get(mochi_key):
            lines.append(f"✅ Mochi: RSI vừa cắt lên EMA9, gap đang thu hẹp — tín hiệu mua mạnh.")

        # ── 5. Form line ──
        ft = r.get("form_type", "NONE")
        spread = r.get("spread_now")
        if ft != "NONE" and spread is not None:
            spread_str = f"spread {spread:.1f}"
            if ft == "CURL_BUY":
                form_icon = _buy_icon(True) if side == "buy" else _buy_icon(False)
                lines.append(f"{form_icon} Form: 3 đường đang cuộn lại ({spread_str}), chuẩn bị tạo form mua.")
            elif ft == "CURL_SELL":
                form_icon = _buy_icon(False) if side == "buy" else _buy_icon(True)
                lines.append(f"{form_icon} Form: 3 đường đang cuộn lại ({spread_str}), chưa rõ hướng.")
            elif ft == "BREAKOUT_UP":
                form_icon = _buy_icon(True) if side == "buy" else _buy_icon(False)
                lines.append(f"{form_icon} Form: BREAKOUT lên từ vùng cuộn ({spread_str}), lực tăng mạnh!")
            elif ft == "BREAKOUT_DOWN":
                form_icon = _buy_icon(False) if side == "buy" else _buy_icon(True)
                lines.append(f"{form_icon} Form: BREAKOUT xuống từ vùng cuộn ({spread_str}), cảnh báo!")

        # ── 6. Trap line ──
        ts = r.get("trap_status", "NONE")
        if ts == "TRAP_HIGH_ACTIVE":
            lines.append(f"➖ Trap: RSI đã chạm 80 trước đó, đang chờ trả trap đỉnh.")
        elif ts == "TRAP_LOW_ACTIVE":
            lines.append(f"➖ Trap: RSI đã chạm 20 trước đó, đang chờ trả trap đáy.")
        elif ts == "TRAP_HIGH_BROKEN":
            lines.append(f"❌ Trap: Trap đỉnh đã HỎNG — xu hướng giảm mạnh!")
        elif ts == "TRAP_LOW_BROKEN":
            lines.append(f"✅ Trap: Trap đáy đã HỎNG — xu hướng tăng mạnh!")
        elif ts == "TRAP_HIGH_PAID":
            lines.append(f"✅ Trap: Trap đỉnh đã TRẢ — đỉnh mới đã lập.")
        elif ts == "TRAP_LOW_PAID":
            lines.append(f"✅ Trap: Trap đáy đã TRẢ — đáy mới đã lập.")

        # ── 7. Exit line ──
        exit_key = r.get("exit_buy") if side == "buy" else r.get("exit_sell")
        if exit_key and exit_key.get("exit_warning"):
            et = exit_key["exit_type"]
            if et == "FULL_EXIT":
                lines.append(f"❌ Exit: {exit_key['exit_reason']}")
            else:
                lines.append(f"➖ Exit: {exit_key['exit_reason']}")

        # ── 8. DCA line ──
        dca_key = r.get("dca_buy") if side == "buy" else r.get("dca_sell")
        if dca_key and dca_key.get("dca_safe"):
            dt = dca_key["dca_type"]
            if dt == "DCA_WMA_CROSS":
                lines.append(f"✅ DCA: RSI cắt lên WMA45, DCA an toàn.")
            elif dt == "DCA_EMA_NARROW":
                lines.append(f"➖ DCA: EMA đang hẹp lại gần WMA, có thể DCA cẩn thận.")
            elif dt == "DCA_FORM_BUY":
                lines.append(f"✅ DCA: RSI trên cả EMA+WMA, có thể DCA.")

        # ── 9. Baseline line ──
        bl_conv = r.get("bl_converging", False)
        bl_dist = r.get("bl_distance_pct")
        if bl_conv and bl_dist is not None:
            lines.append(f"✅ Baseline: 2 đường BL chụm lại ({bl_dist:.1f}%), vùng entry tốt.")

        # ── 10. Momentum line ──
        gap_dir = r.get("gap_direction", "")
        if gap_dir == "bull_converge":
            mom_icon = _buy_icon(True) if side == "buy" else _buy_icon(False)
            lines.append(f"{mom_icon} Momentum: Gap EMA-WMA đang thu hẹp, lực mua đang tích lũy.")
        elif gap_dir == "bear_converge":
            mom_icon = _buy_icon(False) if side == "buy" else _buy_icon(True)
            lines.append(f"{mom_icon} Momentum: Gap EMA-WMA đang thu hẹp, lực bán đang tích lũy.")

        lines.append("")  # blank line between timeframes

    # ── KẾT LUẬN ──
    total = consensus["total_signals"]
    level = consensus["consensus_level"]
    level_short = level.split("✅")[0].strip()  # remove ✅ icons from level
    mochi_cnt = consensus.get("mochi_signals", 0)
    rising = consensus.get("rsi_rising_count", 0)
    falling = consensus.get("rsi_falling_count", 0)

    lines.append("== KẾT LUẬN ==")
    lines.append(f"{level_short} ({total}/5 khung có tín hiệu).")

    # Khung lớn support
    large_sup = consensus.get("large_frame_signal", False)
    if large_sup:
        lines.append("Khung lớn (W, 3D) ủng hộ.")
    else:
        lines.append("Khung lớn (W, 3D) chưa ủng hộ.")

    lines.append(f"Mochi: {mochi_cnt}/5 | RSI tăng: {rising}/5 | RSI giảm: {falling}/5")

    if consensus.get("golden_entry"):
        lines.append("🌟 GOLDEN ENTRY: BL chụm + VWAP gần!")

    lines.append("")
    lines.append(consensus["recommendation"])

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# PHÂN TÍCH & TÓM TẮT
# ──────────────────────────────────────────────────────────────

def analyze_and_summarize(symbol: str, side: str = "buy") -> tuple[str, str, str, str]:
    """
    Phân tích đầy đủ 1 symbol.
    Trả về (notification_title, notification_body, full_log, telegram_body).
    - notification_body: ngắn gọn cho macOS notification
    - telegram_body: đầy đủ, đẹp cho Telegram
    """
    price = fetch_price(symbol)

    results = []
    for interval, label in TIMEFRAMES:
        try:
            r = analyze_timeframe(symbol, interval, label)
            results.append(r)
        except Exception as e:
            log(f"   ❌ {label}: {e}")

    if not results:
        return f"{symbol} ❌", "Không lấy được dữ liệu", "ERROR: No data", "❌ Không có dữ liệu"

    consensus = evaluate_consensus(results, side)

    # ── Build notification body (ngắn gọn cho macOS) ──
    lines = [f"💰 ${price:,.4f}"]

    for r in results:
        sig_key = "buy_signal" if side == "buy" else "sell_signal"
        rsi_str = fmt(r["rsi"])
        delta = r.get("rsi_delta")
        delta_str = f"Δ{'+' if delta and delta > 0 else ''}{delta:.1f}" if delta is not None else ""
        sig = ""
        if r[sig_key] == "POINT_3":
            sig = "🟢P3"
        elif r[sig_key] == "POINT_2":
            sig = "🟡P2"
        elif r[sig_key] == "POINT_1_ZONE":
            sig = "⚠️P1"
        elif r[sig_key] == "APPROACHING":
            sig = "🔸→"
        else:
            sig = "—"

        # v4 markers
        v4_marks = []
        mochi_key = "mochi_buy" if side == "buy" else "mochi_sell"
        if r.get(mochi_key):
            v4_marks.append("🔥Mochi")
        ft = r.get("form_type", "NONE")
        if ft.startswith("CURL_"):
            v4_marks.append(f"🌀{ft.split('_')[1][:1]}")
        elif ft.startswith("BREAKOUT_"):
            v4_marks.append(f"🚀{'↑' if 'UP' in ft else '↓'}")
        ts = r.get("trap_status", "NONE")
        if "ACTIVE" in ts:
            trap_dir = "đỉnh" if "HIGH" in ts else "đáy"
            v4_marks.append(f"⚠️T{trap_dir[0]}")
        elif "BROKEN" in ts:
            v4_marks.append("🚨Hỏng")
        elif "PAID" in ts:
            v4_marks.append("✅Trả")
        exit_key = r.get("exit_buy") if side == "buy" else r.get("exit_sell")
        if exit_key and exit_key.get("exit_warning"):
            v4_marks.append("🟡Exit" if exit_key["exit_type"] == "PARTIAL_EXIT" else "🔴Exit")
        dca_key = r.get("dca_buy") if side == "buy" else r.get("dca_sell")
        if dca_key and dca_key.get("dca_safe"):
            v4_marks.append("🟢DCA")

        v4_str = " ".join(v4_marks)
        lines.append(f"{r['label']}: RSI {rsi_str} {delta_str} {sig} {v4_str}")

    lines.append(f"Đồng thuận: {consensus['total_signals']}/5")
    lines.append(consensus['recommendation'])

    notif_body = "\n".join(lines)
    notif_title = f"📊 {symbol} | {consensus['recommendation'][:20]}"

    # ── Build rich Telegram body ──
    telegram_body = format_telegram_rich(symbol, results, consensus, price, side)

    # ── Build full log ──
    log_lines = [
        f"Symbol: {symbol} | Giá: ${price:,.4f}",
        f"Kiểm tra: {'BUY' if side == 'buy' else 'SELL'}",
        "",
        f"{'Khung':<6} {'RSI':>8} {'EMA9':>8} {'WMA45':>8} {'VWAP':>10} {'BL Fast':>10} {'BL Slow':>10} {'Tín hiệu':<20}",
        "-" * 90,
    ]
    for r in results:
        sig_key = "buy_signal" if side == "buy" else "sell_signal"
        log_lines.append(
            f"{r['label']:<6} {fmt(r['rsi']):>8} {fmt(r['ema9']):>8} {fmt(r['wma45']):>8} "
            f"{fmt4(r['vwap']):>10} {fmt4(r['bl_fast']):>10} {fmt4(r['bl_slow']):>10} "
            f"{signal_label(r[sig_key]):<20}"
        )
    log_lines.append("")
    log_lines.append(f"Đồng thuận: {consensus['total_signals']}/5 — {consensus['consensus_level']}")
    log_lines.append(f"Hỗ trợ VWAP+BL: {consensus['bl_support_count']} điểm")
    if consensus["golden_entry"]:
        log_lines.append("🌟 GOLDEN ENTRY: BL chụm + VWAP gần!")
    log_lines.append(f"KẾT LUẬN: {consensus['recommendation']}")

    # v4 alerts in log
    for r in results:
        ts = r.get("trap_status", "NONE")
        if ts != "NONE":
            log_lines.append(f"🎯 {r['label']}: Trap={ts}")
        ft = r.get("form_type", "NONE")
        if ft != "NONE":
            log_lines.append(f"🌀 {r['label']}: Form={ft} spread={r.get('spread_now', 0):.1f}")
        exit_key = r.get("exit_buy") if side == "buy" else r.get("exit_sell")
        if exit_key and exit_key.get("exit_warning"):
            log_lines.append(f"⚠️ {r['label']}: EXIT={exit_key['exit_type']} — {exit_key['exit_reason']}")
        dca_key = r.get("dca_buy") if side == "buy" else r.get("dca_sell")
        if dca_key and dca_key.get("dca_safe"):
            log_lines.append(f"🟢 {r['label']}: DCA={dca_key['dca_type']} — {dca_key['dca_reason']}")
        if r.get("bl_fast_cross_up"):
            log_lines.append(f"🔔 {r['label']}: Giá cắt LÊN Baseline Fast!")
        if r.get("ema_crossover_up"):
            log_lines.append(f"🔔 {r['label']}: RSI cắt lên EMA 9!")
        if r.get("wma_crossover_up"):
            log_lines.append(f"🔔 {r['label']}: RSI cắt lên WMA 45!")

    full_log = "\n".join(log_lines)

    return notif_title, notif_body, full_log, telegram_body


# ──────────────────────────────────────────────────────────────
# TIMING
# ──────────────────────────────────────────────────────────────

def get_next_h4_close() -> datetime:
    """Tính thời điểm nến H4 tiếp theo đóng (UTC)."""
    now = datetime.now(timezone.utc)
    # H4 close times: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC
    current_hour = now.hour
    # Tìm H4 tiếp theo
    h4_hours = [0, 4, 8, 12, 16, 20]
    next_h4 = None
    for h in h4_hours:
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            next_h4 = candidate
            break
    if next_h4 is None:
        # Sang ngày hôm sau
        next_day = now + timedelta(days=1)
        next_h4 = next_day.replace(hour=0, minute=0, second=0, microsecond=0)
    return next_h4


def format_countdown(seconds: float) -> str:
    """Format số giây thành h:m:s."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"

# ──────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────

def run_analysis_cycle(symbols: list[str], side: str = "buy"):
    """Chạy 1 chu kỳ phân tích cho tất cả symbols."""
    log(f"🔄 Bắt đầu phân tích {len(symbols)} cặp...")

    for symbol in symbols:
        log(f"   📊 Đang phân tích {symbol}...")
        try:
            title, body, full_log, telegram_body = analyze_and_summarize(symbol, side)

            # Gửi notification macOS (compact)
            has_signal = "✅" in body
            if has_signal:
                send_mac_alert(title, body)
                send_mac_notification(title, body, sound="Hero")
            else:
                send_mac_notification(title, body, sound="Glass")

            # Gửi Telegram (rich format)
            send_telegram(telegram_body)

            # Lưu log
            save_log(symbol, full_log)

            # In rich format ra terminal
            print()
            print(f"  ┌── {symbol} {'─' * (50 - len(symbol))}┐")
            for line in telegram_body.split("\n"):
                print(f"  │  {line}")
            print(f"  └{'─' * 54}┘")
            print()

        except Exception as e:
            log(f"   ❌ {symbol}: {e}")
            send_mac_notification(f"❌ {symbol} Error", str(e), sound="Basso")

    log("✅ Phân tích xong!")


def main():
    # Parse arguments
    symbols = []
    test_mode = False
    side = "buy"

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--test":
            test_mode = True
        elif args[i] == "--side" and i + 1 < len(args):
            side = args[i + 1].lower()
            i += 1
        elif not args[i].startswith("--"):
            sym = args[i].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            symbols.append(sym)
        i += 1

    if not symbols:
        symbols = ["LINKUSDT"]

    # Header
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║        🔍 XTB-SPRINGTEA H4 CANDLE MONITOR               ║")
    print("╠════════════════════════════════════════════════════════════╣")
    print(f"║  Cặp theo dõi: {', '.join(symbols):<42} ║")
    print(f"║  Chiến lược:   {'BUY (LONG)' if side == 'buy' else 'SELL (SHORT)':<42} ║")
    print(f"║  Nến H4 đóng (GMT+7): 03:00 07:00 11:00 15:00 19:00 23:00║")
    print("║  Nhấn Ctrl+C để dừng                                    ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()

    # Test mode: chạy ngay, không chờ
    if test_mode:
        log("🧪 CHẾ ĐỘ TEST — chạy phân tích ngay...")
        run_analysis_cycle(symbols, side)
        log("🧪 Test xong!")
        return

    # Gửi notification khởi động
    send_mac_notification(
        "🔍 XTB Monitor Đã Khởi Động",
        f"Theo dõi: {', '.join(symbols)}\nThông báo mỗi nến H4 đóng",
        sound="Pop"
    )

    # Main loop
    try:
        while True:
            next_close = get_next_h4_close()
            target_time = next_close + timedelta(seconds=BUFFER_SECONDS)
            now = datetime.now(timezone.utc)
            wait_seconds = (target_time - now).total_seconds()

            next_close_vn = next_close.astimezone(TZ_VN)
            log(f"⏳ Nến H4 tiếp theo: {next_close_vn.strftime('%H:%M')} GMT+7 — chờ {format_countdown(wait_seconds)}")

            # Sleep cho đến khi nến đóng
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            # Đến giờ — chạy phân tích ngay
            log(f"🕐 NẮN H4 ĐÃ ĐÓNG! ({next_close_vn.strftime('%H:%M')} GMT+7)")
            run_analysis_cycle(symbols, side)

    except KeyboardInterrupt:
        print()
        log("🛑 Monitor đã dừng.")
        send_mac_notification("🛑 XTB Monitor", "Monitor đã dừng.", sound="Purr")


if __name__ == "__main__":
    main()
