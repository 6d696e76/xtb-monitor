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
    Sinh message Telegram theo phong cách bài phân tích mẫu XTB-Springtea.
    Văn xuôi tự nhiên, đọc từ khung lớn → nhỏ, kèm đồng thuận và plan.
    """
    pair = symbol.replace("USDT", "/USDT")
    icon = COIN_ICONS.get(symbol, "📊")
    side_vn = "BUY" if side == "buy" else "SELL"
    lines = [f"{icon} {pair}", f"Giá: ${price:,.2f}", ""]

    sig_key = "buy_signal" if side == "buy" else "sell_signal"
    mochi_key = "mochi_buy" if side == "buy" else "mochi_sell"

    # ── Đảo thứ tự: khung lớn trước (W → H4) ──
    ordered = list(reversed(results))

    TF_NAMES = {"W": "W (tuần)", "3D": "3D", "D1": "D1",
                "H12": "H12", "H4": "H4"}

    # ── Tìm khung có RSI mạnh nhất ──
    best_rsi_label = None
    best_rsi_val = -1
    for r in results:
        if r["rsi"] is not None and r["rsi"] > best_rsi_val:
            best_rsi_val = r["rsi"]
            best_rsi_label = r["label"]

    # ── Phân tích từng khung — văn xuôi ──
    for r in ordered:
        rsi = r["rsi"]
        delta = r.get("rsi_delta")
        label = r["label"]
        tf_name = TF_NAMES.get(label, label)
        sig_val = r[sig_key]
        rsi_ae = r.get("rsi_above_ema")
        rsi_aw = r.get("rsi_above_wma")
        ts = r.get("trap_status", "NONE")
        ft = r.get("form_type", "NONE")
        spread = r.get("spread_now")
        gap_dir = r.get("gap_direction", "")
        bl_conv = r.get("bl_converging", False)
        bl_dist = r.get("bl_distance_pct")

        # ── Build 1 đoạn văn xuôi cho khung này ──
        para = f"📊 Khung {tf_name}: RSI ở {fmt(rsi, 1)}"

        # Vị trí RSI vs EMA/WMA
        if rsi_ae is True and rsi_aw is True:
            para += f", đã vượt cả EMA9 lẫn WMA45"
        elif rsi_ae is True and rsi_aw is False:
            para += f", đã vượt EMA9 nhưng chưa vượt WMA45"
        elif rsi_ae is False and rsi_aw is False:
            para += f", nằm dưới cả EMA9 và WMA45"

        # Signal point
        if sig_val == "POINT_3":
            para += " → Điểm 3."
        elif sig_val == "POINT_2":
            para += ". Đây là Điểm 2 — RSI đang nằm giữa 2 đường"
            if gap_dir == "bull_converge":
                para += ", gap EMA-WMA đang thu hẹp (🐂 bull converge)"
            elif gap_dir == "bear_converge":
                para += ", gap EMA-WMA đang thu hẹp (🐻 bear converge)"
            para += "."
        elif sig_val == "POINT_1_ZONE":
            para += ". Đang ở vùng Điểm 1 — cơ hội bắt đáy, rủi ro cao."
        elif sig_val == "APPROACHING":
            para += ". RSI vượt EMA9, đang hướng về WMA45."
        elif rsi_ae is False and rsi_aw is False:
            para += " — chưa có tín hiệu tăng."
        else:
            para += "."

        # Form
        if ft == "BREAKOUT_UP":
            para += f" Đặc biệt: đang breakout lên từ cuộn!"
        elif ft == "BREAKOUT_DOWN":
            para += f" Đang breakout xuống từ cuộn."
        elif ft == "CURL_BUY":
            para += f" 3 đường đang cuộn vào form mua (spread {spread:.1f})."
        elif ft == "CURL_SELL":
            para += f" 3 đường đang cuộn vào form bán (spread {spread:.1f})."

        # Trap
        if ts == "TRAP_HIGH_ACTIVE":
            para += " Có trap đỉnh đang hoạt động → giá vẫn còn lực để leo lên trả trap."
        elif ts == "TRAP_LOW_ACTIVE":
            para += f" Có trap đáy đang hoạt động (RSI trước đó chạm ≤20) → kỳ vọng giá sẽ vòng xuống trả trap (test lại đáy cũ)."
        elif ts == "TRAP_HIGH_BROKEN":
            para += " Trap đỉnh đã HỎNG — xu hướng giảm mạnh!"
        elif ts == "TRAP_LOW_BROKEN":
            para += " Trap đáy đã HỎNG — xu hướng tăng mạnh!"
        elif ts == "TRAP_HIGH_PAID":
            para += " Trap đỉnh đã trả xong — đỉnh mới đã lập."
        elif ts == "TRAP_LOW_PAID":
            para += " Trap đáy đã trả xong — đáy mới đã lập."

        # Baseline
        if bl_conv and bl_dist is not None:
            para += f" Baseline chụm ({bl_dist:.1f}%) → vùng entry tốt."

        # Momentum (nếu chưa nhắc ở Điểm 2)
        if gap_dir == "bull_converge" and sig_val != "POINT_2":
            para += " Gap EMA-WMA thu hẹp (🐂 bull converge)."
        elif gap_dir == "bear_converge" and sig_val != "POINT_2":
            para += " Gap EMA-WMA thu hẹp (🐻 bear converge)."

        # Giá cắt Baseline
        if r.get("bl_fast_cross_up"):
            para += " Giá vừa cắt lên Baseline Fast → tín hiệu tích cực."
        elif r.get("bl_fast_cross_down"):
            para += " Giá cắt xuống Baseline Fast → cảnh báo."

        # Mochi
        if r.get(mochi_key):
            para += " 🔥 Mochi: RSI cắt lên EMA9 + gap thu hẹp → tín hiệu mua mạnh."

        # RSI mạnh nhất
        if label == best_rsi_label and len(results) > 1:
            para += f" RSI mạnh nhất trong 5 khung."

        lines.append(para)

        # ── Nhận định đặc biệt ──
        if ts == "TRAP_LOW_ACTIVE" and sig_val in ("POINT_3", "POINT_2"):
            lines.append(f'💡 Nhận định: {label} đang trong quá trình trả trap đáy. "RSI sẽ điều chỉnh, cuộn dần tạo form sell, cắt xuống 45, giá bằng hoặc thấp hơn đáy cũ → hoàn thành trả trap."')
        elif ts == "TRAP_HIGH_ACTIVE" and sig_val in ("POINT_2", "APPROACHING"):
            lines.append(f'💡 Nhận định: {label} có trap đỉnh nhưng RSI đang hướng lên → giá kỳ vọng leo lên trả trap đỉnh.')
        elif ft == "CURL_BUY" and ts == "TRAP_LOW_ACTIVE":
            lines.append(f'⚠️ Lưu ý: {label} đang cuộn form buy nhưng có trap đáy chưa trả — đây có thể chỉ là nhịp hồi kỹ thuật trước khi giá quay xuống test đáy cũ.')

        # ── Exit warning ──
        exit_key = r.get("exit_buy") if side == "buy" else r.get("exit_sell")
        if exit_key and exit_key.get("exit_warning"):
            et = exit_key["exit_type"]
            icon_e = "🔴" if et == "FULL_EXIT" else "🟡"
            lines.append(f"{icon_e} Cảnh báo: {exit_key['exit_reason']}")

        lines.append("")  # blank line between timeframes

    # ── 📋 ĐỒNG THUẬN ──
    total = consensus["total_signals"]
    level = consensus["consensus_level"]
    level_icons = ""
    if total >= 4: level_icons = " ✅✅✅"
    elif total >= 3: level_icons = " ✅✅"
    elif total >= 2: level_icons = " ✅"
    large_sup = consensus.get("large_frame_signal", False)

    lines.append("📋 Đồng thuận")
    for r in ordered:
        sig = r.get(sig_key, "NONE")
        ts = r.get("trap_status", "NONE")
        ft = r.get("form_type", "NONE")
        gap_dir = r.get("gap_direction", "")
        parts = []
        if sig == "POINT_3": parts.append("Điểm 3")
        elif sig == "POINT_2":
            parts.append("Điểm 2")
            if gap_dir == "bull_converge": parts[-1] += " (hướng lên WMA45)"
        elif sig == "POINT_1_ZONE": parts.append("Vùng Điểm 1")
        elif sig == "APPROACHING": parts.append("Tiếp cận WMA45")
        if ft == "BREAKOUT_UP": parts.append("Breakout lên")
        elif ft == "BREAKOUT_DOWN": parts.append("Breakout xuống")
        elif ft.startswith("CURL_"): parts.append("Cuộn")
        if ts == "TRAP_LOW_ACTIVE": parts.append("Trap đáy đang trả")
        elif ts == "TRAP_HIGH_ACTIVE": parts.append("Trap đỉnh")
        elif ts == "TRAP_HIGH_PAID": parts.append("Trap đỉnh ✅ trả")
        elif ts == "TRAP_LOW_PAID": parts.append("Trap đáy ✅ trả")
        if gap_dir == "bull_converge" and sig != "POINT_2": parts.append("Gap thu hẹp")

        sig_icon = "✅" if sig != "NONE" else "❌"
        desc = " + ".join(parts) if parts else "Chưa có tín hiệu"
        lines.append(f"{r['label']:>3} → {desc:40s} {sig_icon}")

    lines.append(f"Đồng thuận: {total}/5 khung{level_icons}")

    # ── 🎯 PLAN GIAO DỊCH ──
    lines.append("")
    lines.append(f"🎯 Plan giao dịch")

    # Tìm traps, forms, breakouts
    active_traps = []
    breakouts = []
    curling = []
    for r in results:
        ts = r.get("trap_status", "NONE")
        if ts == "TRAP_LOW_ACTIVE": active_traps.append((r["label"], "đáy"))
        elif ts == "TRAP_HIGH_ACTIVE": active_traps.append((r["label"], "đỉnh"))
        ft = r.get("form_type", "NONE")
        if ft == "BREAKOUT_UP": breakouts.append(r["label"])
        elif ft.startswith("CURL_"): curling.append(r["label"])

    # Tổng quan
    overview_parts = []
    if total >= 4:
        overview_parts.append(f"Tất cả các khung từ H4 → W đều đồng thuận hướng {'lên' if side == 'buy' else 'xuống'}")
    elif total >= 2:
        overview_parts.append(f"{total}/5 khung đồng thuận {side_vn}")

    trap_low = [t for t in active_traps if t[1] == "đáy"]
    trap_high = [t for t in active_traps if t[1] == "đỉnh"]
    if trap_low:
        labels = " và ".join(t[0] for t in trap_low)
        overview_parts.append(f"{labels} đang trả trap đáy → xác suất tiếp tục giảm cao (~70%)")
    if breakouts:
        labels = " và ".join(breakouts)
        overview_parts.append(f"{labels} breakout lên → đà tăng đang được xác nhận")

    if overview_parts:
        lines.append(". ".join(overview_parts) + ".")

    # Entry
    if total >= 2:
        lines.append(f"")
        lines.append(f"Nếu muốn vào lệnh {side_vn}:")
        cond = "ĐẠT" if total >= 3 else "CƠ BẢN"
        lines.append(f"✅ Đồng thuận: {cond} ({total}/5 khung)")
        lines.append(f"Vào tại vùng giá ~${price:,.2f}")

        # SL — lấy BL Slow của H4 hoặc khung nhỏ nhất
        h4 = next((r for r in results if r["label"] == "H4"), None)
        if h4:
            sl_candidates = [v for v in [h4.get("bl_slow"), h4.get("bl_fast")] if v]
            if sl_candidates:
                sl_ref = min(sl_candidates) if side == "buy" else max(sl_candidates)
                lines.append(f"SL tham khảo: {'dưới' if side == 'buy' else 'trên'} BL Slow H4 ~${sl_ref:,.2f}")

        # Nến chưa đóng
        open_frames = [r["label"] for r in results if r.get("is_open")]
        if open_frames:
            lines.append(f"Chờ {open_frames[0]} đóng nến xác nhận (nến đang mở)")

    # Rủi ro
    risks = []
    if trap_high:
        labels = ", ".join(t[0] for t in trap_high)
        risks.append(f"{labels} có trap đỉnh → khi trả xong sẽ có đợt điều chỉnh")

    # VWAP position
    for r in results:
        if r["label"] in ("H12", "D1") and r.get("price_above_vwap") is False:
            vwap_v = r.get("vwap")
            if vwap_v:
                risks.append(f"Giá dưới VWAP {r['label']} (${vwap_v:,.2f}) → cần vượt qua")
            break

    if risks:
        lines.append("")
        lines.append("Rủi ro cần lưu ý:")
        for risk in risks:
            lines.append(f"⚠️ {risk}")

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
