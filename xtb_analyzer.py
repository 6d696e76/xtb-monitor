#!/usr/bin/env python3
from __future__ import annotations
"""
XTB-Springtea Trading Analyzer v4
===================================
Phân tích tự động theo phương pháp XTB-Springtea + Mochi RSI:
- RSI(14), EMA(9) trên RSI, WMA(45) trên RSI
- VWAP + ±2σ bands, Baseline JMA (Fast/Slow)
- [v3] EMA-WMA gap convergence, RSI delta
- [v4] Trap Detection, Form Pattern, Exit Signal, DCA Safe Zone

Cách dùng:
    python3 xtb_analyzer.py                    # Mặc định: LINKUSDT
    python3 xtb_analyzer.py BTCUSDT            # Chỉ định cặp
    python3 xtb_analyzer.py ETHUSDT --side sell # Kiểm tra SELL
"""

import json
import math
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────────────────────
# 1. DATA API (Binance → OKX fallback)
# ──────────────────────────────────────────────────────────────

# Binance endpoints (ưu tiên khi chạy local)
BINANCE_ENDPOINTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

# OKX interval mapping: Binance format → OKX format
_OKX_INTERVAL_MAP = {
    "1h": "1H", "4h": "4H", "12h": "12H",
    "1d": "1Dutc", "3d": "3Dutc", "1w": "1Wutc",
}

def _http_get(url: str, timeout: int = 15):
    """HTTP GET trả về parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "XTB-Analyzer/4.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def _fetch_klines_binance(symbol: str, interval: str, limit: int) -> list | None:
    """Thử lấy klines từ Binance. Trả None nếu tất cả endpoint thất bại."""
    for base in BINANCE_ENDPOINTS:
        try:
            url = f"{base}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
            return _http_get(url)
        except Exception:
            continue
    return None

def _fetch_klines_okx(symbol: str, interval: str, limit: int) -> list:
    """Lấy klines từ OKX (fallback khi Binance bị chặn)."""
    base_coin = symbol.replace("USDT", "")
    okx_symbol = f"{base_coin}-USDT"
    okx_interval = _OKX_INTERVAL_MAP.get(interval, interval.upper())
    url = f"https://www.okx.com/api/v5/market/candles?instId={okx_symbol}&bar={okx_interval}&limit={limit}"
    data = _http_get(url)
    if data.get("code") != "0":
        raise Exception(f"OKX error: {data.get('msg', 'Unknown')}")
    # OKX trả về newest-first → đảo lại, convert sang Binance format
    rows = data["data"][::-1]  # reverse → oldest-first
    result = []
    for r in rows:
        # OKX: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        ts_ms = int(r[0])
        result.append([
            ts_ms,           # open time ms
            r[1],            # open
            r[2],            # high
            r[3],            # low
            r[4],            # close
            r[5],            # volume
            ts_ms,           # close time ms (approximate)
        ])
    return result

def _fetch_price_okx(symbol: str) -> float:
    """Lấy giá từ OKX."""
    base_coin = symbol.replace("USDT", "")
    okx_symbol = f"{base_coin}-USDT"
    url = f"https://www.okx.com/api/v5/market/ticker?instId={okx_symbol}"
    data = _http_get(url)
    if data.get("code") != "0":
        raise Exception(f"OKX price error: {data.get('msg')}")
    return float(data["data"][0]["last"])

def fetch_klines(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    """Lấy dữ liệu nến. Thử Binance trước, fallback sang OKX."""
    raw = _fetch_klines_binance(symbol, interval, limit)
    if raw is None:
        raw = _fetch_klines_okx(symbol, interval, limit)
    candles = []
    for k in raw:
        candles.append({
            "time": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc),
            "close_time": datetime.fromtimestamp(int(k[6]) / 1000, tz=timezone.utc),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "hl2": (float(k[2]) + float(k[3])) / 2,
        })
    return candles

def fetch_price(symbol: str) -> float:
    """Lấy giá hiện tại. Thử Binance trước, fallback sang OKX."""
    for base in BINANCE_ENDPOINTS:
        try:
            data = _http_get(f"{base}/api/v3/ticker/price?symbol={symbol}", timeout=10)
            return float(data["price"])
        except Exception:
            continue
    return _fetch_price_okx(symbol)

# ──────────────────────────────────────────────────────────────
# 2. CHỈ BÁO KỸ THUẬT
# ──────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Tính RSI theo phương pháp Wilder's Smoothing."""
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_values = [None] * period
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100 - (100 / (1 + rs)))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100 - (100 / (1 + rs)))
    return rsi_values


def calc_ema(data: list[float], period: int) -> list[float]:
    """Tính EMA. Bỏ qua None."""
    ema = [None] * len(data)
    valid = [(i, v) for i, v in enumerate(data) if v is not None]
    if len(valid) < period:
        return ema
    start_idx = valid[period - 1][0]
    sma = sum(v for _, v in valid[:period]) / period
    ema[start_idx] = sma
    multiplier = 2 / (period + 1)
    prev = sma
    for i in range(period, len(valid)):
        idx, val = valid[i]
        current = (val - prev) * multiplier + prev
        ema[idx] = current
        prev = current
    return ema


def calc_wma(data: list[float], period: int) -> list[float]:
    """Tính WMA (Weighted Moving Average). Bỏ qua None."""
    wma = [None] * len(data)
    valid = [(i, v) for i, v in enumerate(data) if v is not None]
    if len(valid) < period:
        return wma
    weight_sum = period * (period + 1) / 2
    for j in range(period - 1, len(valid)):
        window = [v for _, v in valid[j - period + 1: j + 1]]
        weighted = sum(window[k] * (k + 1) for k in range(period))
        idx = valid[j][0]
        wma[idx] = weighted / weight_sum
    return wma


def calc_jma(src: list[float], length: int = 70, power: int = 2, phase: float = 5.0) -> list[float]:
    """
    Tính JMA (Jurik Moving Average) — port từ PineScript.
    Params: length: 70(fast)/150(slow), power: 2, phase: 5.0(fast)/0.0(slow)
    """
    n = len(src)
    if n == 0:
        return []
    if phase < -100:
        phase_ratio = 0.5
    elif phase > 100:
        phase_ratio = 2.5
    else:
        phase_ratio = phase / 100 + 1.5
    beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2)
    alpha = beta ** power
    jma = [None] * n
    e0 = 0.0
    e1 = 0.0
    e2 = 0.0
    jma_prev = 0.0
    for i in range(n):
        if i == 0:
            e0 = src[i]
            e1 = 0.0
            e2 = 0.0
            jma_prev = src[i]
            jma[i] = src[i]
        else:
            e0 = (1 - alpha) * src[i] + alpha * e0
            e1 = (src[i] - e0) * (1 - beta) + beta * e1
            e2 = (e0 + phase_ratio * e1 - jma_prev) * ((1 - alpha) ** 2) + (alpha ** 2) * e2
            jma_prev = e2 + jma_prev
            jma[i] = jma_prev
    return jma


def calc_vwap_session(candles: list[dict]) -> dict:
    """Tính VWAP tích lũy + ±2σ bands."""
    vwap_sum = 0.0
    vol_sum = 0.0
    v2_sum = 0.0
    vwap_values = []
    upper_values = []
    lower_values = []
    for c in candles:
        hl2 = c["hl2"]
        vol = c["volume"]
        vwap_sum += hl2 * vol
        vol_sum += vol
        v2_sum += vol * hl2 * hl2
        if vol_sum > 0:
            vwap = vwap_sum / vol_sum
            dev = math.sqrt(max(v2_sum / vol_sum - vwap * vwap, 0))
            vwap_values.append(vwap)
            upper_values.append(vwap + 2 * dev)
            lower_values.append(vwap - 2 * dev)
        else:
            vwap_values.append(None)
            upper_values.append(None)
            lower_values.append(None)
    return {"vwap": vwap_values, "upper": upper_values, "lower": lower_values}


# ──────────────────────────────────────────────────────────────
# 2b. [v3] MOCHI RSI — TÍNH NĂNG MỚI
# ──────────────────────────────────────────────────────────────

def calc_rsi_delta(rsi_list: list[float]) -> float | None:
    """
    [v3] RSI Delta: so sánh RSI hiện tại vs nến trước.
    Trả về: dương = RSI đang tăng, âm = RSI đang giảm.
    """
    if len(rsi_list) < 2:
        return None
    now = rsi_list[-1]
    prev = rsi_list[-2]
    if now is None or prev is None:
        return None
    return now - prev


def calc_gap_convergence(ema_list: list[float], wma_list: list[float]) -> dict:
    """
    [v3] EMA-WMA gap convergence trên RSI.
    Lấy cảm hứng từ Mochi RSI:
      buyCondition = ... emaVal < wmaVal AND (wmaVal - emaVal) < (wmaVal[1] - emaVal[1])
    Trả về dict: gap hiện tại, gap trước, converging (bool), direction.
    """
    result = {
        "gap_now": None,
        "gap_prev": None,
        "converging": False,
        "direction": None,  # "bull_converge" | "bear_converge" | "diverging" | None
    }

    if len(ema_list) < 2 or len(wma_list) < 2:
        return result

    ema_now = ema_list[-1]
    ema_prev = ema_list[-2]
    wma_now = wma_list[-1]
    wma_prev = wma_list[-2]

    if any(v is None for v in [ema_now, ema_prev, wma_now, wma_prev]):
        return result

    gap_now = abs(wma_now - ema_now)
    gap_prev = abs(wma_prev - ema_prev)
    result["gap_now"] = gap_now
    result["gap_prev"] = gap_prev
    result["converging"] = gap_now < gap_prev

    if ema_now < wma_now and gap_now < gap_prev:
        # EMA dưới WMA + gap thu hẹp = bull converge (EMA đuổi kịp WMA từ dưới)
        result["direction"] = "bull_converge"
    elif ema_now > wma_now and gap_now < gap_prev:
        # EMA trên WMA + gap thu hẹp = bear converge (EMA đang rơi về WMA)
        result["direction"] = "bear_converge"
    elif gap_now > gap_prev:
        result["direction"] = "diverging"

    return result


def evaluate_mochi_signal(
    rsi_now: float | None,
    ema_now: float | None,
    wma_now: float | None,
    rsi_prev: float | None,
    ema_prev: float | None,
    gap_conv: dict,
    rsi_delta: float | None,
) -> dict:
    """
    [v3] Đánh giá tín hiệu theo logic Mochi RSI.
    Mochi BUY = crossUp AND higherCondUp AND ema < wma AND gap thu hẹp
    """
    result = {
        "mochi_buy": False,
        "mochi_sell": False,
        "mochi_reason": "",
    }

    if any(v is None for v in [rsi_now, ema_now, wma_now, rsi_prev, ema_prev]):
        return result

    cross_up = (rsi_prev <= ema_prev) and (rsi_now > ema_now)
    cross_down = (rsi_prev >= ema_prev) and (rsi_now < ema_now)

    # Mochi BUY: crossUp + ema < wma + gap converging
    if cross_up and ema_now < wma_now and gap_conv.get("direction") == "bull_converge":
        result["mochi_buy"] = True
        result["mochi_reason"] = "RSI cắt lên EMA + gap EMA-WMA thu hẹp (Mochi P2)"

    # Mochi SELL: crossDown + ema > wma + gap converging
    if cross_down and ema_now > wma_now and gap_conv.get("direction") == "bear_converge":
        result["mochi_sell"] = True
        result["mochi_reason"] = "RSI cắt xuống EMA + gap EMA-WMA thu hẹp (Mochi P2)"

    return result


# ──────────────────────────────────────────────────────────────
# 2c. [v4] TRAP, FORM, EXIT, DCA
# ──────────────────────────────────────────────────────────────

def detect_trap(rsi_list: list[float], wma_list: list[float],
                closes: list[float]) -> dict:
    """
    [v4] Phát hiện Trap đỉnh/đáy theo Springtea.
    Trap đỉnh: RSI đạt >=80. Trap đáy: RSI đạt <=20.
    Hỏng trap đỉnh: RSI 80→≤30 mà không cắt lên WMA45.
    Hỏng trap đáy: RSI 20→≥70 mà không cắt xuống WMA45.
    Trả trap: form mua/bán hình thành sau trap, giá vượt đỉnh/đáy trap.
    """
    result = {
        "has_trap_high": False,
        "has_trap_low": False,
        "trap_high_broken": False,   # hỏng trap đỉnh
        "trap_low_broken": False,    # hỏng trap đáy
        "trap_high_paid": False,     # trả trap đỉnh
        "trap_low_paid": False,      # trả trap đáy
        "trap_high_idx": None,
        "trap_low_idx": None,
        "trap_status": "NONE",      # NONE/TRAP_HIGH/TRAP_LOW/TRAP_HIGH_BROKEN/...
    }
    if not rsi_list or not wma_list:
        return result

    valid_pairs = [(i, r, w) for i, (r, w) in enumerate(zip(rsi_list, wma_list))
                   if r is not None and w is not None]
    if len(valid_pairs) < 20:
        return result

    # Scan backward for most recent trap
    last_trap_high_idx = None
    last_trap_low_idx = None
    for i, r, w in reversed(valid_pairs):
        if r >= 80 and last_trap_high_idx is None:
            last_trap_high_idx = i
        if r <= 20 and last_trap_low_idx is None:
            last_trap_low_idx = i
        if last_trap_high_idx and last_trap_low_idx:
            break

    # --- Trap Đỉnh ---
    if last_trap_high_idx is not None:
        result["has_trap_high"] = True
        result["trap_high_idx"] = last_trap_high_idx
        trap_price = closes[last_trap_high_idx] if last_trap_high_idx < len(closes) else None

        # Check trả trap: RSI vượt WMA45 + giá vượt đỉnh trap
        # Check hỏng: RSI rớt ≤30 mà chưa trả trap
        crossed_wma_up = False
        price_exceeded = False
        went_below_30 = False
        for i, r, w in valid_pairs:
            if i <= last_trap_high_idx:
                continue
            if r > w:
                crossed_wma_up = True
            if trap_price and i < len(closes) and closes[i] > trap_price:
                price_exceeded = True
            if r <= 30:
                went_below_30 = True

        if crossed_wma_up and price_exceeded:
            result["trap_high_paid"] = True
            result["trap_status"] = "TRAP_HIGH_PAID"
        elif went_below_30 and not (crossed_wma_up and price_exceeded):
            result["trap_high_broken"] = True
            result["trap_status"] = "TRAP_HIGH_BROKEN"
        else:
            result["trap_status"] = "TRAP_HIGH_ACTIVE"

    # --- Trap Đáy ---
    if last_trap_low_idx is not None:
        result["has_trap_low"] = True
        result["trap_low_idx"] = last_trap_low_idx
        trap_price = closes[last_trap_low_idx] if last_trap_low_idx < len(closes) else None

        # Check trả trap: RSI xuống dưới WMA45 + giá phá đáy trap
        # Check hỏng: RSI vọt ≥70 mà chưa trả trap
        crossed_wma_down = False
        price_broke_low = False
        went_above_70 = False
        for i, r, w in valid_pairs:
            if i <= last_trap_low_idx:
                continue
            if r < w:
                crossed_wma_down = True
            if trap_price and i < len(closes) and closes[i] < trap_price:
                price_broke_low = True
            if r >= 70:
                went_above_70 = True

        if crossed_wma_down and price_broke_low:
            result["trap_low_paid"] = True
            if result["trap_status"] == "NONE":
                result["trap_status"] = "TRAP_LOW_PAID"
        elif went_above_70 and not (crossed_wma_down and price_broke_low):
            result["trap_low_broken"] = True
            if result["trap_status"] == "NONE":
                result["trap_status"] = "TRAP_LOW_BROKEN"
        elif result["trap_status"] == "NONE":
            result["trap_status"] = "TRAP_LOW_ACTIVE"

    # Use the most recent trap if both exist
    if last_trap_high_idx and last_trap_low_idx:
        if last_trap_high_idx > last_trap_low_idx:
            # Trap đỉnh gần hơn
            if result["trap_high_broken"]:
                result["trap_status"] = "TRAP_HIGH_BROKEN"
            elif result["trap_high_paid"]:
                result["trap_status"] = "TRAP_HIGH_PAID"
            else:
                result["trap_status"] = "TRAP_HIGH_ACTIVE"
        else:
            if result["trap_low_broken"]:
                result["trap_status"] = "TRAP_LOW_BROKEN"
            elif result["trap_low_paid"]:
                result["trap_status"] = "TRAP_LOW_PAID"
            else:
                result["trap_status"] = "TRAP_LOW_ACTIVE"

    return result


def detect_form_pattern(rsi_list: list[float], ema_list: list[float],
                        wma_list: list[float], lookback: int = 10) -> dict:
    """
    [v4.1] Phát hiện Form theo sơ đồ Springtea (I→II→III→Breakout).

    Form Buy:
      I  : RSI rơi từ trên cả 2 đường xuống
      II : RSI chạm extreme đáy (≤30)
      III: 3 đường hội tụ (cuộn) — RSI hồi lên gần EMA9/WMA45
      Breakout: RSI tách lên, R > E > W

    Form Sell (đối xứng):
      I  : RSI leo từ dưới cả 2 đường lên
      II : RSI chạm extreme đỉnh (≥70)
      III: 3 đường hội tụ (cuộn)
      Breakout: RSI tách xuống, R < E < W
    """
    result = {
        "is_curling": False,        # đang cuộn (Stage III)
        "is_breakout": False,       # đang breakout từ cuộn
        "form_type": "NONE",        # NONE/CURL_BUY/CURL_SELL/BREAKOUT_UP/BREAKOUT_DOWN
        "spread_now": None,
        "spread_min": None,
        "curl_strength": None,      # 0-100, càng cao càng chặt
        # [v4.1] Form stage tracking
        "form_stage": "NONE",       # NONE/STAGE_I/STAGE_II/STAGE_III/BREAKOUT
        "form_after_trap_low": False,   # cuộn sau khi RSI chạm ≤30
        "form_after_trap_high": False,  # cuộn sau khi RSI chạm ≥70
        "extreme_rsi_idx": None,    # index nến RSI extreme gần nhất
    }
    if len(rsi_list) < lookback or len(ema_list) < lookback or len(wma_list) < lookback:
        return result

    # ── 1. Tính spread 3 đường cho lookback gần nhất ──
    spreads = []
    for i in range(len(rsi_list) - lookback, len(rsi_list)):
        r = rsi_list[i]
        e = ema_list[i]
        w = wma_list[i]
        if all(v is not None for v in [r, e, w]):
            spreads.append((i, max(r, e, w) - min(r, e, w)))
    if len(spreads) < 3:
        return result

    spread_now = spreads[-1][1]
    spread_min = min(s for _, s in spreads)
    result["spread_now"] = spread_now
    result["spread_min"] = spread_min

    # ── 2. Phát hiện Cuộn (Stage III) ──
    CURL_THRESHOLD = 5.0
    result["is_curling"] = spread_now < CURL_THRESHOLD
    result["curl_strength"] = max(0, min(100, (1 - spread_now / 15) * 100))

    # ── 3. Phát hiện Breakout ──
    if len(spreads) >= 4:
        recent_min_spread = min(s for _, s in spreads[:-2])
        was_curling = recent_min_spread < CURL_THRESHOLD
        expanding = spread_now > spreads[-2][1] and spread_now > CURL_THRESHOLD
        result["is_breakout"] = was_curling and expanding

    # ── 4. Xác định hướng ──
    r = rsi_list[-1]
    e = ema_list[-1]
    w = wma_list[-1]
    if all(v is not None for v in [r, e, w]):
        if result["is_curling"]:
            if r > w:
                result["form_type"] = "CURL_BUY"
            else:
                result["form_type"] = "CURL_SELL"
        elif result["is_breakout"]:
            if r > e > w:
                result["form_type"] = "BREAKOUT_UP"
            elif r < e < w:
                result["form_type"] = "BREAKOUT_DOWN"

    # ── 5. [v4.1] Scan ngược tìm RSI extreme → xác định Stage ──
    # Scan 50 nến gần nhất tìm điểm RSI chạm ≤30 hoặc ≥70
    scan_range = min(50, len(rsi_list))
    last_extreme_low_idx = None
    last_extreme_high_idx = None
    for i in range(len(rsi_list) - 1, max(len(rsi_list) - scan_range - 1, -1), -1):
        rv = rsi_list[i]
        if rv is None:
            continue
        if rv <= 30 and last_extreme_low_idx is None:
            last_extreme_low_idx = i
        if rv >= 70 and last_extreme_high_idx is None:
            last_extreme_high_idx = i
        if last_extreme_low_idx is not None and last_extreme_high_idx is not None:
            break

    # ── 6. Form Buy stage (sau extreme đáy) ──
    if last_extreme_low_idx is not None:
        result["form_after_trap_low"] = True
        result["extreme_rsi_idx"] = last_extreme_low_idx
        # Stage II đã xảy ra (RSI chạm đáy)
        # Giờ check: RSI đã hồi lên → đang cuộn hoặc breakout?
        if result["is_breakout"] and result["form_type"] == "BREAKOUT_UP":
            result["form_stage"] = "BREAKOUT"
        elif result["is_curling"] and result["form_type"] == "CURL_BUY":
            result["form_stage"] = "STAGE_III"
        elif r is not None and w is not None and e is not None:
            if r < e and r < w:
                # RSI vẫn dưới cả 2 đường, đang hồi → giữa Stage II và III
                result["form_stage"] = "STAGE_II"
            elif r > e and r < w:
                # RSI vượt EMA nhưng chưa tới WMA → đang tiến tới cuộn
                result["form_stage"] = "STAGE_II"

    # ── 7. Form Sell stage (sau extreme đỉnh) — ưu tiên nếu gần hơn ──
    if last_extreme_high_idx is not None:
        # Chỉ ghi đè nếu extreme high gần hơn extreme low
        if last_extreme_low_idx is None or last_extreme_high_idx > last_extreme_low_idx:
            result["form_after_trap_high"] = True
            result["extreme_rsi_idx"] = last_extreme_high_idx
            if result["is_breakout"] and result["form_type"] == "BREAKOUT_DOWN":
                result["form_stage"] = "BREAKOUT"
            elif result["is_curling"] and result["form_type"] == "CURL_SELL":
                result["form_stage"] = "STAGE_III"
            elif r is not None and w is not None and e is not None:
                if r > e and r > w:
                    result["form_stage"] = "STAGE_II"
                elif r < e and r > w:
                    result["form_stage"] = "STAGE_II"

    return result


def detect_exit_signal(rsi_list: list[float], ema_list: list[float],
                       wma_list: list[float], side: str = "buy") -> dict:
    """
    [v4] Phát hiện tín hiệu thoát lệnh.
    BUY exit: RSI cắt xuống EMA + 3 đường bắt đầu chụm (form sell hình thành).
    SELL exit: RSI cắt lên EMA + 3 đường bắt đầu chụm (form buy hình thành).
    """
    result = {
        "exit_warning": False,
        "exit_type": "NONE",  # NONE/PARTIAL_EXIT/FULL_EXIT
        "exit_reason": "",
    }
    if len(rsi_list) < 3 or len(ema_list) < 3 or len(wma_list) < 3:
        return result

    r_now, r_prev = rsi_list[-1], rsi_list[-2]
    e_now, e_prev = ema_list[-1], ema_list[-2]
    w_now = wma_list[-1]

    if any(v is None for v in [r_now, r_prev, e_now, e_prev, w_now]):
        return result

    spread = max(r_now, e_now, w_now) - min(r_now, e_now, w_now)
    was_spread = None
    r3, e3, w3 = rsi_list[-3], ema_list[-3], wma_list[-3]
    if all(v is not None for v in [r3, e3, w3]):
        was_spread = max(r3, e3, w3) - min(r3, e3, w3)

    if side == "buy":
        # Exit BUY: RSI cắt xuống EMA9
        cross_down = (r_prev >= e_prev) and (r_now < e_now)
        converging = was_spread is not None and spread < was_spread
        if cross_down and converging:
            result["exit_warning"] = True
            result["exit_type"] = "PARTIAL_EXIT"
            result["exit_reason"] = "RSI cắt xuống EMA9 + 3 đường chụm lại"
        elif cross_down:
            result["exit_warning"] = True
            result["exit_type"] = "PARTIAL_EXIT"
            result["exit_reason"] = "RSI cắt xuống EMA9"
        # Full exit: RSI cắt xuống WMA45
        w_prev = wma_list[-2]
        if w_prev is not None and (r_prev >= w_prev) and (r_now < w_now):
            result["exit_warning"] = True
            result["exit_type"] = "FULL_EXIT"
            result["exit_reason"] = "RSI cắt xuống WMA45 — chốt toàn bộ"
    else:
        # Exit SELL
        cross_up = (r_prev <= e_prev) and (r_now > e_now)
        converging = was_spread is not None and spread < was_spread
        if cross_up and converging:
            result["exit_warning"] = True
            result["exit_type"] = "PARTIAL_EXIT"
            result["exit_reason"] = "RSI cắt lên EMA9 + 3 đường chụm lại"
        elif cross_up:
            result["exit_warning"] = True
            result["exit_type"] = "PARTIAL_EXIT"
            result["exit_reason"] = "RSI cắt lên EMA9"
        w_prev = wma_list[-2]
        if w_prev is not None and (r_prev <= w_prev) and (r_now > w_now):
            result["exit_warning"] = True
            result["exit_type"] = "FULL_EXIT"
            result["exit_reason"] = "RSI cắt lên WMA45 — chốt toàn bộ"

    return result


def calc_dca_zone(rsi_now: float | None, ema_now: float | None,
                  wma_now: float | None, rsi_prev: float | None,
                  wma_prev: float | None, gap_conv: dict,
                  side: str = "buy") -> dict:
    """
    [v4] Xác định vùng DCA an toàn.
    DCA BUY an toàn: RSI cắt lên WMA45 hoặc EMA9 hẹp lại gần WMA.
    DCA SELL an toàn: RSI cắt xuống WMA45 hoặc EMA9 hẹp lại.
    """
    result = {
        "dca_safe": False,
        "dca_type": "NONE",  # NONE/DCA_WMA_CROSS/DCA_EMA_NARROW/DCA_FORM_BUY
        "dca_reason": "",
    }
    if any(v is None for v in [rsi_now, ema_now, wma_now]):
        return result

    if side == "buy":
        # DCA an toàn 1: RSI vừa cắt lên WMA45
        if rsi_prev is not None and wma_prev is not None:
            if rsi_prev <= wma_prev and rsi_now > wma_now:
                result["dca_safe"] = True
                result["dca_type"] = "DCA_WMA_CROSS"
                result["dca_reason"] = "RSI cắt lên WMA45 — DCA an toàn"
                return result

        # DCA an toàn 2: EMA9 hẹp lại gần WMA45 (gap converging)
        if gap_conv.get("direction") == "bull_converge":
            gap = gap_conv.get("gap_now", 999)
            if gap is not None and gap < 3.0:
                result["dca_safe"] = True
                result["dca_type"] = "DCA_EMA_NARROW"
                result["dca_reason"] = f"EMA9 hẹp lại gần WMA45 (gap {gap:.1f}) — DCA cẩn thận"
                return result

        # DCA an toàn 3: RSI đang ở trên cả 2 đường (form buy đang hoạt động)
        if rsi_now > ema_now and rsi_now > wma_now:
            result["dca_safe"] = True
            result["dca_type"] = "DCA_FORM_BUY"
            result["dca_reason"] = "RSI trên EMA+WMA (form buy) — có thể DCA"
            return result
    else:
        if rsi_prev is not None and wma_prev is not None:
            if rsi_prev >= wma_prev and rsi_now < wma_now:
                result["dca_safe"] = True
                result["dca_type"] = "DCA_WMA_CROSS"
                result["dca_reason"] = "RSI cắt xuống WMA45 — DCA SHORT an toàn"
                return result
        if gap_conv.get("direction") == "bear_converge":
            gap = gap_conv.get("gap_now", 999)
            if gap is not None and gap < 3.0:
                result["dca_safe"] = True
                result["dca_type"] = "DCA_EMA_NARROW"
                result["dca_reason"] = f"EMA9 hẹp lại (gap {gap:.1f}) — DCA SHORT cẩn thận"
                return result
        if rsi_now < ema_now and rsi_now < wma_now:
            result["dca_safe"] = True
            result["dca_type"] = "DCA_FORM_BUY"
            result["dca_reason"] = "RSI dưới EMA+WMA (form sell) — có thể DCA SHORT"
            return result

    return result


# ──────────────────────────────────────────────────────────────
# 3. PHÂN TÍCH THEO SPRINGTEA
# ──────────────────────────────────────────────────────────────

TIMEFRAMES = [
    ("4h",  "H4"),
    ("12h", "H12"),
    ("1d",  "D1"),
    ("3d",  "3D"),
    ("1w",  "W"),
]

def analyze_timeframe(symbol: str, interval: str, label: str) -> dict:
    """Phân tích một khung thời gian. Trả về dict kết quả."""
    candles = fetch_klines(symbol, interval, limit=200)
    closes = [c["close"] for c in candles]
    last_close = closes[-1]
    last_time = candles[-1]["time"]
    close_time = candles[-1]["close_time"]
    is_open = datetime.now(timezone.utc) < close_time

    # ── RSI + EMA + WMA ──
    rsi_list = calc_rsi(closes, 14)
    ema_list = calc_ema(rsi_list, 9)
    wma_list = calc_wma(rsi_list, 45)

    rsi_now = rsi_list[-1] if rsi_list else None
    ema_now = ema_list[-1] if ema_list else None
    wma_now = wma_list[-1] if wma_list else None
    rsi_prev = rsi_list[-2] if len(rsi_list) >= 2 else None
    ema_prev = ema_list[-2] if len(ema_list) >= 2 else None
    wma_prev = wma_list[-2] if len(wma_list) >= 2 else None

    # ── [v3] RSI Delta ──
    rsi_delta = calc_rsi_delta(rsi_list)

    # ── [v3] Gap Convergence ──
    gap_conv = calc_gap_convergence(ema_list, wma_list)

    # ── [v3] Mochi Signal ──
    mochi = evaluate_mochi_signal(rsi_now, ema_now, wma_now, rsi_prev, ema_prev, gap_conv, rsi_delta)

    # ── [v4] Trap Detection ──
    trap = detect_trap(rsi_list, wma_list, closes)

    # ── [v4] Form Pattern ──
    form = detect_form_pattern(rsi_list, ema_list, wma_list)

    # ── [v4] Exit Signal ──
    exit_sig = detect_exit_signal(rsi_list, ema_list, wma_list, "buy")
    exit_sig_sell = detect_exit_signal(rsi_list, ema_list, wma_list, "sell")

    # ── [v4] DCA Safe Zone ──
    dca = calc_dca_zone(rsi_now, ema_now, wma_now, rsi_prev, wma_prev, gap_conv, "buy")
    dca_sell = calc_dca_zone(rsi_now, ema_now, wma_now, rsi_prev, wma_prev, gap_conv, "sell")

    # ── VWAP ──
    vwap_data = calc_vwap_session(candles)
    vwap_now = vwap_data["vwap"][-1]
    vwap_upper = vwap_data["upper"][-1]
    vwap_lower = vwap_data["lower"][-1]

    # ── Baseline JMA ──
    bl_fast_list = calc_jma(closes, length=70, power=2, phase=5.0)
    bl_slow_list = calc_jma(closes, length=150, power=2, phase=0.0)
    bl_fast = bl_fast_list[-1] if bl_fast_list else None
    bl_slow = bl_slow_list[-1] if bl_slow_list else None

    price_above_bl_fast = (last_close > bl_fast) if bl_fast else None
    price_above_bl_slow = (last_close > bl_slow) if bl_slow else None
    price_above_vwap = (last_close > vwap_now) if vwap_now else None

    prev_close = closes[-2] if len(closes) >= 2 else None
    bl_fast_prev = bl_fast_list[-2] if len(bl_fast_list) >= 2 else None
    bl_slow_prev = bl_slow_list[-2] if len(bl_slow_list) >= 2 else None

    bl_fast_cross_up = False
    bl_fast_cross_down = False
    if prev_close and bl_fast_prev and bl_fast:
        bl_fast_cross_up = (prev_close <= bl_fast_prev) and (last_close > bl_fast)
        bl_fast_cross_down = (prev_close >= bl_fast_prev) and (last_close < bl_fast)

    bl_slow_cross_up = False
    bl_slow_cross_down = False
    if prev_close and bl_slow_prev and bl_slow:
        bl_slow_cross_up = (prev_close <= bl_slow_prev) and (last_close > bl_slow)
        bl_slow_cross_down = (prev_close >= bl_slow_prev) and (last_close < bl_slow)

    bl_distance = None
    bl_converging = False
    if bl_fast and bl_slow and last_close > 0:
        bl_distance = abs(bl_fast - bl_slow) / last_close * 100
        bl_converging = bl_distance < 2.0

    vwap_near_bl = False
    if vwap_now and bl_fast and bl_slow and last_close > 0:
        vwap_near_bl = (abs(vwap_now - (bl_fast + bl_slow) / 2) / last_close * 100) < 3.0

    # ── RSI tín hiệu ──
    rsi_above_ema = (rsi_now > ema_now) if (rsi_now and ema_now) else None
    rsi_above_wma = (rsi_now > wma_now) if (rsi_now and wma_now) else None

    ema_crossover = False
    if all(v is not None for v in [rsi_now, ema_now, rsi_prev, ema_prev]):
        ema_crossover = (rsi_prev <= ema_prev) and (rsi_now > ema_now)

    wma_crossover = False
    if all(v is not None for v in [rsi_now, wma_now, rsi_prev, wma_prev]):
        wma_crossover = (rsi_prev <= wma_prev) and (rsi_now > wma_now)

    ema_crossunder = False
    if all(v is not None for v in [rsi_now, ema_now, rsi_prev, ema_prev]):
        ema_crossunder = (rsi_prev >= ema_prev) and (rsi_now < ema_now)

    wma_crossunder = False
    if all(v is not None for v in [rsi_now, wma_now, rsi_prev, wma_prev]):
        wma_crossunder = (rsi_prev >= wma_prev) and (rsi_now < wma_now)

    # Vùng RSI
    if rsi_now is not None:
        if rsi_now >= 80:
            rsi_zone = "RẤT MẠNH (>80)"
        elif rsi_now >= 70:
            rsi_zone = "MẠNH (70-80)"
        elif rsi_now >= 50:
            rsi_zone = "TRUNG TÍNH (50-70)"
        elif rsi_now >= 30:
            rsi_zone = "YẾU (30-50)"
        elif rsi_now >= 20:
            rsi_zone = "RẤT YẾU (20-30)"
        else:
            rsi_zone = "CỰC YẾU (<20)"
    else:
        rsi_zone = "N/A"

    # Đánh giá tín hiệu BUY (theo sơ đồ Form Buy - Springtea)
    # P3: RSI > EMA > WMA → xu hướng xác nhận
    # P2: RSI > EMA, RSI < WMA → cân bằng, chờ xác nhận
    # P1: RSI < EMA < WMA, RSI đang hồi phục (delta > 0) → bắt đáy
    buy_signal = "NONE"
    if rsi_above_ema and rsi_above_wma:
        buy_signal = "POINT_3"
    elif rsi_above_ema is True and rsi_above_wma is False:
        if rsi_now and wma_now and abs(rsi_now - wma_now) < 5:
            buy_signal = "POINT_2"
        else:
            buy_signal = "APPROACHING"
    elif rsi_above_ema is False and rsi_above_wma is False and rsi_now and rsi_now <= 45:
        # Điểm 1: RSI dưới cả 2 đường + đang hồi phục (delta > 0)
        if rsi_delta is not None and rsi_delta > 0:
            buy_signal = "POINT_1_ZONE"

    # Đánh giá tín hiệu SELL (theo sơ đồ Form Sell - Springtea)
    # P3: RSI < EMA < WMA → xu hướng giảm xác nhận
    # P2: RSI < EMA, RSI > WMA → cắt xuống EMA, chờ
    # P1: RSI > EMA > WMA, RSI đang rơi (delta < 0) → bắt đỉnh
    sell_signal = "NONE"
    rsi_below_ema = (rsi_now < ema_now) if (rsi_now and ema_now) else None
    rsi_below_wma = (rsi_now < wma_now) if (rsi_now and wma_now) else None
    if rsi_below_ema and rsi_below_wma:
        sell_signal = "POINT_3"
    elif rsi_below_ema is True and rsi_below_wma is False:
        sell_signal = "POINT_2"
    elif rsi_below_ema is False and rsi_below_wma is False and rsi_now and rsi_now >= 55:
        # Điểm 1: RSI trên cả 2 đường + đang rơi (delta < 0)
        if rsi_delta is not None and rsi_delta < 0:
            sell_signal = "POINT_1_ZONE"

    return {
        "label": label,
        "interval": interval,
        "last_time": last_time,
        "close_time": close_time,
        "is_open": is_open,
        "last_close": last_close,
        # RSI
        "rsi": rsi_now,
        "ema9": ema_now,
        "wma45": wma_now,
        "rsi_prev": rsi_prev,
        "rsi_zone": rsi_zone,
        "rsi_above_ema": rsi_above_ema,
        "rsi_above_wma": rsi_above_wma,
        "ema_crossover_up": ema_crossover,
        "wma_crossover_up": wma_crossover,
        "ema_crossover_down": ema_crossunder,
        "wma_crossover_down": wma_crossunder,
        "buy_signal": buy_signal,
        "sell_signal": sell_signal,
        # [v3] Mochi features
        "rsi_delta": rsi_delta,
        "gap_now": gap_conv["gap_now"],
        "gap_prev": gap_conv["gap_prev"],
        "gap_converging": gap_conv["converging"],
        "gap_direction": gap_conv["direction"],
        "mochi_buy": mochi["mochi_buy"],
        "mochi_sell": mochi["mochi_sell"],
        "mochi_reason": mochi["mochi_reason"],
        # VWAP
        "vwap": vwap_now,
        "vwap_upper": vwap_upper,
        "vwap_lower": vwap_lower,
        "price_above_vwap": price_above_vwap,
        # Baseline
        "bl_fast": bl_fast,
        "bl_slow": bl_slow,
        "price_above_bl_fast": price_above_bl_fast,
        "price_above_bl_slow": price_above_bl_slow,
        "bl_fast_cross_up": bl_fast_cross_up,
        "bl_fast_cross_down": bl_fast_cross_down,
        "bl_slow_cross_up": bl_slow_cross_up,
        "bl_slow_cross_down": bl_slow_cross_down,
        "bl_distance_pct": bl_distance,
        "bl_converging": bl_converging,
        "vwap_near_bl": vwap_near_bl,
        # [v4] Trap
        "trap_status": trap["trap_status"],
        "has_trap_high": trap["has_trap_high"],
        "has_trap_low": trap["has_trap_low"],
        "trap_high_broken": trap["trap_high_broken"],
        "trap_low_broken": trap["trap_low_broken"],
        "trap_high_paid": trap["trap_high_paid"],
        "trap_low_paid": trap["trap_low_paid"],
        # [v4] Form
        "form_type": form["form_type"],
        "is_curling": form["is_curling"],
        "is_breakout": form["is_breakout"],
        "spread_now": form["spread_now"],
        "curl_strength": form["curl_strength"],
        # [v4.1] Form stage
        "form_stage": form["form_stage"],
        "form_after_trap_low": form["form_after_trap_low"],
        "form_after_trap_high": form["form_after_trap_high"],
        # [v4] Exit
        "exit_buy": exit_sig,
        "exit_sell": exit_sig_sell,
        # [v4] DCA
        "dca_buy": dca,
        "dca_sell": dca_sell,
    }


def evaluate_consensus(results: list[dict], side: str = "buy") -> dict:
    """Đánh giá đồng thuận đa khung thời gian."""
    signal_key = "buy_signal" if side == "buy" else "sell_signal"

    large_frames = [r for r in results if r["label"] in ("W", "3D")]
    mid_frames = [r for r in results if r["label"] in ("D1", "H12")]
    small_frames = [r for r in results if r["label"] in ("H4",)]

    def has_signal(frames):
        return any(r[signal_key] != "NONE" for r in frames)

    total_with_signal = sum(1 for r in results if r[signal_key] != "NONE")

    violations = []
    for r in results:
        if side == "buy" and r["rsi"] is not None and r["rsi"] < 20:
            violations.append(f"⛔ {r['label']}: RSI={r['rsi']:.2f} < 20 → CẤM LONG!")
        if side == "sell" and r["rsi"] is not None and r["rsi"] > 80:
            violations.append(f"⛔ {r['label']}: RSI={r['rsi']:.2f} > 80 → CẤM SHORT!")

    consensus_level = "KHÔNG ĐỒNG THUẬN"
    if total_with_signal >= 4:
        consensus_level = "ĐỒNG THUẬN RẤT MẠNH ✅✅✅"
    elif total_with_signal >= 3:
        consensus_level = "ĐỒNG THUẬN MẠNH ✅✅"
    elif total_with_signal >= 2:
        consensus_level = "ĐỒNG THUẬN CƠ BẢN ✅"
    elif total_with_signal == 1:
        consensus_level = "CHỈ 1 KHUNG → CHƯA ĐỦ ⚠️"

    # VWAP + Baseline support
    bl_support_count = 0
    for r in results:
        if side == "buy":
            if r.get("price_above_bl_fast"):
                bl_support_count += 1
            if r.get("price_above_vwap"):
                bl_support_count += 1
        else:
            if r.get("price_above_bl_fast") is False:
                bl_support_count += 1
            if r.get("price_above_vwap") is False:
                bl_support_count += 1

    golden_entry = any(r.get("bl_converging") and r.get("vwap_near_bl") for r in results)

    # [v3] Mochi signal count
    mochi_key = "mochi_buy" if side == "buy" else "mochi_sell"
    mochi_signals = sum(1 for r in results if r.get(mochi_key))

    # [v3] RSI delta — count how many frames have RSI rising (buy) or falling (sell)
    rsi_rising_count = 0
    rsi_falling_count = 0
    for r in results:
        delta = r.get("rsi_delta")
        if delta is not None:
            if delta > 0:
                rsi_rising_count += 1
            elif delta < 0:
                rsi_falling_count += 1

    recommendation = "❌ KHÔNG VÀO LỆNH"
    if total_with_signal >= 2 and has_signal(large_frames) and len(violations) == 0:
        recommendation = f"✅ CÓ THỂ VÀO LỆNH {'BUY' if side == 'buy' else 'SELL'}"
    elif total_with_signal >= 2 and len(violations) == 0:
        recommendation = "⚠️ CÂN NHẮC — khung lớn chưa ủng hộ"

    return {
        "total_signals": total_with_signal,
        "large_frame_signal": has_signal(large_frames),
        "mid_frame_signal": has_signal(mid_frames),
        "small_frame_signal": has_signal(small_frames),
        "consensus_level": consensus_level,
        "recommendation": recommendation,
        "violations": violations,
        "bl_support_count": bl_support_count,
        "golden_entry": golden_entry,
        # v3
        "mochi_signals": mochi_signals,
        "rsi_rising_count": rsi_rising_count,
        "rsi_falling_count": rsi_falling_count,
    }


# ──────────────────────────────────────────────────────────────
# 4. HIỂN THỊ KẾT QUẢ
# ──────────────────────────────────────────────────────────────

def fmt(val, decimals=2):
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}"

def fmt4(val):
    return fmt(val, 4)

def bool_icon(val):
    if val is True:
        return "✅"
    elif val is False:
        return "❌"
    return "—"

def signal_label(sig):
    labels = {
        "NONE": "—",
        "POINT_1_ZONE": "⚠️ Vùng Điểm 1 (Bắt đáy)",
        "POINT_2": "🟡 Điểm 2 (Cân bằng)",
        "POINT_3": "🟢 Điểm 3 (Theo đà)",
        "APPROACHING": "🔸 Đang tiếp cận",
    }
    return labels.get(sig, sig)


def print_report(symbol: str, results: list[dict], side: str, price: float):
    """In báo cáo phân tích đầy đủ."""
    tz_vn = timezone(timedelta(hours=7))
    now = datetime.now(tz_vn)
    side_vn = "MUA (LONG)" if side == "buy" else "BÁN (SHORT)"

    print()
    print("=" * 80)
    print(f"  📊 XTB-SPRINGTEA ANALYZER v4 — {symbol}")
    print(f"  💰 Giá hiện tại: ${price:,.4f}")
    print(f"  🕐 Thời gian: {now.strftime('%d/%m/%Y %H:%M:%S')} (GMT+7)")
    print(f"  🎯 Kiểm tra: {side_vn}")
    print("=" * 80)

    # ── BẢNG 1: RSI + v3 ──
    print()
    print("  ╔══════════════════════════════════════════════════════════════════════════╗")
    print("  ║                    📈 RSI + MOMENTUM (v3)                               ║")
    print("  ╚══════════════════════════════════════════════════════════════════════════╝")
    print()
    print("  ┌───────┬────────┬────────┬────────┬────────┬────────┬──────┬─────────────┐")
    print("  │ Khung │RSI(14) │ EMA(9) │WMA(45) │Δ RSI   │Gap E-W │Conv? │ Vùng        │")
    print("  ├───────┼────────┼────────┼────────┼────────┼────────┼──────┼─────────────┤")
    for r in results:
        label = r["label"].center(5)
        rsi = fmt(r["rsi"]).rjust(6)
        ema = fmt(r["ema9"]).rjust(6)
        wma = fmt(r["wma45"]).rjust(6)
        # Delta RSI
        delta = r.get("rsi_delta")
        if delta is not None:
            delta_str = f"{'+' if delta > 0 else ''}{delta:.2f}"
            delta_icon = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
        else:
            delta_str = " N/A"
            delta_icon = ""
        delta_str = delta_str.rjust(6)
        # Gap
        gap = r.get("gap_now")
        gap_str = fmt(gap).rjust(6) if gap is not None else "   N/A"
        # Convergence
        conv = r.get("gap_converging", False)
        gap_dir = r.get("gap_direction", "")
        if gap_dir == "bull_converge":
            conv_str = "🐂 ↗"
        elif gap_dir == "bear_converge":
            conv_str = "🐻 ↘"
        elif gap_dir == "diverging":
            conv_str = "  ↔"
        else:
            conv_str = "  —"
        conv_str = conv_str.ljust(4)
        # Zone (shortened)
        zone = r["rsi_zone"].split("(")[0].strip().ljust(11)
        candle = "🕯" if r["is_open"] else "  "
        print(f"  │ {label} │{rsi} │{ema} │{wma} │{delta_str} │{gap_str} │{conv_str} │ {zone} │{candle}")
    print("  └───────┴────────┴────────┴────────┴────────┴────────┴──────┴─────────────┘")
    print("    Δ RSI = thay đổi so với nến trước | Gap E-W = |EMA - WMA| trên RSI")
    print("    🐂 ↗ = bull converge (EMA đuổi WMA) | 🐻 ↘ = bear converge")

    # ── BẢNG 2: TÍN HIỆU ──
    print()
    print("  ┌───────┬─────────┬─────────┬────────────────────────────┬───────────────┐")
    print("  │ Khung │ >EMA9?  │ >WMA45? │ Springtea                  │ Mochi         │")
    print("  ├───────┼─────────┼─────────┼────────────────────────────┼───────────────┤")
    for r in results:
        label = r["label"].center(5)
        sig_key = "buy_signal" if side == "buy" else "sell_signal"
        mochi_key = "mochi_buy" if side == "buy" else "mochi_sell"
        if side == "buy":
            ae = bool_icon(r["rsi_above_ema"]).center(7)
            aw = bool_icon(r["rsi_above_wma"]).center(7)
        else:
            ae = bool_icon(not r["rsi_above_ema"] if r["rsi_above_ema"] is not None else None).center(7)
            aw = bool_icon(not r["rsi_above_wma"] if r["rsi_above_wma"] is not None else None).center(7)
        sig = signal_label(r[sig_key]).ljust(26)
        mochi = "🔥 BUY" if r.get("mochi_buy") and side == "buy" else \
                "🔥 SELL" if r.get("mochi_sell") and side == "sell" else "   —"
        mochi = mochi.ljust(13)
        print(f"  │ {label} │ {ae} │ {aw} │ {sig} │ {mochi} │")
    print("  └───────┴─────────┴─────────┴────────────────────────────┴───────────────┘")
    print("    Mochi 🔥 = RSI cắt EMA + gap thu hẹp (tín hiệu Điểm 2 chặt)")

    # ── BẢNG 3: VWAP + BASELINE ──
    print()
    print("  ╔══════════════════════════════════════════════════════════════════════════╗")
    print("  ║                    📍 VWAP + BASELINE (JMA)                             ║")
    print("  ╚══════════════════════════════════════════════════════════════════════════╝")
    print()
    print("  ┌───────┬──────────┬──────────┬──────────┬──────────┬───────┐")
    print("  │ Khung │   VWAP   │ BL Fast  │ BL Slow  │   Giá    │BL Chụm│")
    print("  ├───────┼──────────┼──────────┼──────────┼──────────┼───────┤")
    for r in results:
        label = r["label"].center(5)
        vwap = fmt4(r["vwap"]).rjust(8)
        blf = fmt4(r["bl_fast"]).rjust(8)
        bls = fmt4(r["bl_slow"]).rjust(8)
        close = fmt4(r["last_close"]).rjust(8)
        bl_d = fmt(r["bl_distance_pct"]).rjust(5) if r["bl_distance_pct"] is not None else "  N/A"
        conv = "✨" if r["bl_converging"] else "  "
        print(f"  │ {label} │ {vwap} │ {blf} │ {bls} │ {close} │{bl_d}{conv}│")
    print("  └───────┴──────────┴──────────┴──────────┴──────────┴───────┘")

    # ── BẢNG 4: VỊ TRÍ GIÁ ──
    print()
    print("  ┌───────┬─────────┬─────────┬─────────┬─────────┬──────────┐")
    print("  │ Khung │ >VWAP?  │ >BL Fst?│ >BL Slw?│ VWAP≈BL?│ Vùng SL  │")
    print("  ├───────┼─────────┼─────────┼─────────┼─────────┼──────────┤")
    for r in results:
        label = r["label"].center(5)
        pv = bool_icon(r["price_above_vwap"]).center(7)
        pbf = bool_icon(r["price_above_bl_fast"]).center(7)
        pbs = bool_icon(r["price_above_bl_slow"]).center(7)
        vnb = ("✨Có" if r["vwap_near_bl"] else "  —").center(7)
        sl_candidates = [v for v in [r["bl_fast"], r["bl_slow"], r["vwap"]] if v is not None]
        sl_zone = fmt4(min(sl_candidates)).rjust(8) if sl_candidates else "     N/A"
        print(f"  │ {label} │ {pv} │ {pbf} │ {pbs} │ {vnb} │ {sl_zone} │")
    print("  └───────┴─────────┴─────────┴─────────┴─────────┴──────────┘")

    # ── CROSSOVER ALERTS ──
    alerts = []
    for r in results:
        if r.get("mochi_buy") and side == "buy":
            alerts.append(f"  🔥 {r['label']}: MOCHI BUY — {r['mochi_reason']}")
        if r.get("mochi_sell") and side == "sell":
            alerts.append(f"  🔥 {r['label']}: MOCHI SELL — {r['mochi_reason']}")
        # [v4] Trap alerts
        ts = r.get("trap_status", "NONE")
        if ts == "TRAP_HIGH_ACTIVE":
            alerts.append(f"  ⚠️ {r['label']}: TRAP ĐỈNH đang hoạt động — chờ trả trap!")
        elif ts == "TRAP_LOW_ACTIVE":
            alerts.append(f"  ⚠️ {r['label']}: TRAP ĐÁY đang hoạt động — chờ trả trap!")
        elif ts == "TRAP_HIGH_BROKEN":
            alerts.append(f"  🚨 {r['label']}: Trap đỉnh đã HỎMG — xu hướng giảm mạnh!")
        elif ts == "TRAP_LOW_BROKEN":
            alerts.append(f"  🚨 {r['label']}: Trap đáy đã HỌNG — xu hướng tăng mạnh!")
        elif ts == "TRAP_HIGH_PAID":
            alerts.append(f"  ✅ {r['label']}: Trap đỉnh đã TRẢ — đỉnh mới đã lập.")
        elif ts == "TRAP_LOW_PAID":
            alerts.append(f"  ✅ {r['label']}: Trap đáy đã TRẢ — đáy mới đã lập.")
        # [v4] Form alerts
        ft = r.get("form_type", "NONE")
        if ft == "CURL_BUY":
            alerts.append(f"  🌀 {r['label']}: Đang CUỘN (form buy) — spread {r.get('spread_now', 0):.1f}")
        elif ft == "CURL_SELL":
            alerts.append(f"  🌀 {r['label']}: Đang CUỘN (form sell) — spread {r.get('spread_now', 0):.1f}")
        elif ft == "BREAKOUT_UP":
            alerts.append(f"  🚀 {r['label']}: BREAKOUT LÊN từ cuộn!")
        elif ft == "BREAKOUT_DOWN":
            alerts.append(f"  📉 {r['label']}: BREAKOUT XUỐNG từ cuộn!")
        # [v4] Exit alerts
        exit_key = r.get("exit_buy") if side == "buy" else r.get("exit_sell")
        if exit_key and exit_key.get("exit_warning"):
            icon = "🔴" if exit_key["exit_type"] == "FULL_EXIT" else "🟡"
            alerts.append(f"  {icon} {r['label']}: {exit_key['exit_reason']}")
        # [v4] DCA alerts
        dca_key = r.get("dca_buy") if side == "buy" else r.get("dca_sell")
        if dca_key and dca_key.get("dca_safe"):
            alerts.append(f"  🟢 {r['label']}: DCA — {dca_key['dca_reason']}")
        # Baseline cross
        if r["bl_fast_cross_up"]:
            alerts.append(f"  🔔 {r['label']}: Giá cắt LÊN Baseline Fast!")
        if r["bl_fast_cross_down"]:
            alerts.append(f"  🔻 {r['label']}: Giá cắt XUỐNG Baseline Fast!")
        if r["ema_crossover_up"] and side == "buy":
            alerts.append(f"  🔔 {r['label']}: RSI cắt lên EMA 9!")
        if r["wma_crossover_up"] and side == "buy":
            alerts.append(f"  🔔 {r['label']}: RSI cắt lên WMA 45!")

    if alerts:
        print()
        print("  ┌──────────────────────────────────────────────────────────────────────┐")
        print("  │                    ⚡ CẢNH BÁO / TÍN HIỆU                           │")
        print("  ├──────────────────────────────────────────────────────────────────────┤")
        for a in alerts:
            print(f"  │ {a:<68} │")
        print("  └──────────────────────────────────────────────────────────────────────┘")

    # ── ĐÁNH GIÁ ĐỒNG THUẬN ──
    consensus = evaluate_consensus(results, side)
    print()
    print("─" * 80)
    print(f"  📋 ĐÁNH GIÁ ĐỒNG THUẬN ({side_vn})")
    print("─" * 80)
    print(f"  • Tín hiệu Springtea:      {consensus['total_signals']}/5 khung")
    print(f"  • Tín hiệu Mochi:          {consensus['mochi_signals']}/5 khung")
    print(f"  • RSI đang tăng:           {consensus['rsi_rising_count']}/5 khung")
    print(f"  • RSI đang giảm:           {consensus['rsi_falling_count']}/5 khung")
    print(f"  • Khung lớn (W, 3D):       {'Có ✅' if consensus['large_frame_signal'] else 'Không ❌'}")
    print(f"  • Khung trung (D1, H12):   {'Có ✅' if consensus['mid_frame_signal'] else 'Không ❌'}")
    print(f"  • Khung nhỏ (H4):          {'Có ✅' if consensus['small_frame_signal'] else 'Không ❌'}")
    print(f"  • Mức đồng thuận:          {consensus['consensus_level']}")
    print(f"  • Hỗ trợ VWAP+Baseline:    {consensus['bl_support_count']} điểm")
    if consensus["golden_entry"]:
        print("  • 🌟 GOLDEN ENTRY: BL chụm + VWAP gần!")
    print()

    if consensus["violations"]:
        print("  ⚠️  CẢNH BÁO VI PHẠM QUY TẮC:")
        for v in consensus["violations"]:
            print(f"      {v}")
        print()

    print("═" * 80)
    print(f"  🏁 KẾT LUẬN: {consensus['recommendation']}")
    print("═" * 80)

    # ── GỢI Ý ──
    print()
    print("  💡 GỢI Ý:")
    for r in results:
        if r["buy_signal"] == "POINT_1_ZONE" and side == "buy":
            print(f"     → {r['label']}: RSI ~{r['rsi']:.0f}, đang tiếp cận vùng 'đổ xăng'.")
        if r["buy_signal"] == "APPROACHING" and side == "buy":
            print(f"     → {r['label']}: RSI vượt EMA 9, hướng về WMA 45 ({fmt(r['wma45'])}).")
        if r.get("gap_direction") == "bull_converge":
            print(f"     🐂 {r['label']}: Gap EMA-WMA thu hẹp (momentum tăng dần).")
        if r["bl_converging"]:
            print(f"     ✨ {r['label']}: 2 Baseline chụm ({r['bl_distance_pct']:.1f}%).")
        if r["is_open"]:
            print(f"     🕯 {r['label']}: Nến CHƯA đóng — chờ xác nhận.")
    print()
    print("  ⚖️  'Đứng ngoài cũng là một vị thế.'")
    print("  📝  Công cụ phân tích, KHÔNG phải lời khuyên đầu tư.")
    print()


# ──────────────────────────────────────────────────────────────
# 5. MAIN
# ──────────────────────────────────────────────────────────────

def main():
    symbol = "LINKUSDT"
    side = "buy"
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--side" and i + 1 < len(args):
            side = args[i + 1].lower()
        elif not arg.startswith("--"):
            symbol = arg.upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"

    print(f"\n⏳ Đang lấy dữ liệu {symbol} từ Binance (v4: +Trap/Form/Exit/DCA)...")
    price = fetch_price(symbol)
    results = []
    for interval, label in TIMEFRAMES:
        print(f"   📡 {label} ({interval})...", end=" ", flush=True)
        try:
            r = analyze_timeframe(symbol, interval, label)
            results.append(r)
            delta_str = f"Δ{r['rsi_delta']:+.1f}" if r.get("rsi_delta") is not None else ""
            mochi_str = "🔥Mochi" if r.get("mochi_buy") or r.get("mochi_sell") else ""
            print(f"RSI={fmt(r['rsi'])} {delta_str} {mochi_str}")
        except Exception as e:
            print(f"❌ Lỗi: {e}")

    print_report(symbol, results, side, price)


if __name__ == "__main__":
    main()
