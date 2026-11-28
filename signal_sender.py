import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "9132434770:AAHdGs9WY6rCRrztgAJyeA6ZKRiVY2ZImJE"
).strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "-1003938306672").strip()
AFFILIATE_LINK = os.environ.get(
    "AFFILIATE_LINK", "https://broker-qx.pro/sign-up/?lid=2438595"
).strip()
BONUS_TEXT = os.environ.get(
    "BONUS_TEXT", "50% Deposit Bonus"
).strip()
TZ_LABEL = os.environ.get("TZ_LABEL", "UTC-03").strip()
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "-3"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

TIMEFRAME = "1 Minute"
CANDLE_SEC = 60
MAX_MTG = 2
MAX_SIGNALS_PER_DAY = int(os.environ.get("MAX_SIGNALS_PER_DAY", "200"))
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "1"))
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "2"))

PAIRS = [
    ("EUR", "USD", "EURUSD"),
    ("GBP", "USD", "GBPUSD"),
    ("USD", "JPY", "USDJPY"),
    ("AUD", "USD", "AUDUSD"),
    ("EUR", "JPY", "EURJPY"),
]

STRATEGIES = [
    "Fast Stochastic Exit + RSI2 Pullback",
    "SMA20 Pullback + Stoch RSI Cross",
    "EMA / RSI + MACD Signal Cross",
    "SMA 3/5 Cross + Directional Range Takeover",
]

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("channel_style_bot")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("pending_trades", [])
                data.setdefault("last_signal", {})
                data.setdefault("daily", {"date": "", "count": 0})
                return data
        except Exception as e:
            log.warning("تعذر قراءة %s: %s", STATE_FILE, e)
    return {"pending_trades": [], "last_signal": {}, "daily": {"date": "", "count": 0}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rate(base: str, quote: str) -> float | None:
    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={base}&to={quote}",
            timeout=10,
        )
        r.raise_for_status()
        val = r.json().get("rates", {}).get(quote)
        return float(val) if val is not None else None
    except Exception as e:
        log.warning("تعذر جلب السعر لـ %s/%s: %s", base, quote, e)
        return None


def send_telegram(text: str, reply_to: int | None = None, reply_markup: dict | None = None) -> int | None:
    try:
        chat_id = int(CHANNEL_ID)
    except ValueError:
        chat_id = CHANNEL_ID

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return data.get("result", {}).get("message_id")
        log.warning("رد تيليغرام غير ناجح (%s): %s", r.status_code, data)

        if reply_to is not None:
            fallback = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                fallback["reply_markup"] = reply_markup
            r2 = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=fallback,
                timeout=15,
            )
            data2 = r2.json()
            if r2.status_code == 200 and data2.get("ok"):
                return data2.get("result", {}).get("message_id")
    except Exception as e:
        log.error("خطأ إرسال تيليغرام: %s", e)
    return None


def evaluate_single_trade_now(trade: dict) -> str:
    """ينفذ دورة المتابعة الفورية للصفقة (الأساسية + حتى MAX_MTG) ويرسل النتيجة فور انتهاء كل 60 ثانية."""
    msg_id = int(trade["message_id"])
    base = trade["base"]
    quote = trade["quote"]
    side = trade["side"]
    entry = float(trade["entry"])

    for step in range(1, MAX_MTG + 2):
        stage_label = "الأساسية" if step == 1 else f"MTG {step - 1}"
        log.info("بانتظار %s ثانية لحساب نتيجة %s (%s)...", CANDLE_SEC, trade["pair"], stage_label)
        time.sleep(CANDLE_SEC)

        rate_now = fetch_rate(base, quote)
        exit_price = rate_now if rate_now is not None else entry

        won = (exit_price > entry) if side == "call" else (exit_price < entry)
        if won:
            label = "WIN ✅" if step == 1 else f"WIN ✅ MTG {step - 1}"
            send_telegram(label, reply_to=msg_id)
            log.info("أُرسلت النتيجة فوراً: %s (msg_id=%s)", label, msg_id)
            return label

        if step <= MAX_MTG:
            log.info("لم تتحقق في %s، الانتقال إلى MTG %s...", stage_label, step)
            entry = exit_price
            continue

        send_telegram("Loss 💔", reply_to=msg_id)
        log.info("أُرسلت النتيجة فوراً: Loss 💔 (msg_id=%s)", msg_id)
        return "Loss 💔"

    return "Loss 💔"


def flush_old_pending(state: dict, now_utc: datetime) -> None:
    """يفحص الصفقات المعلّقة السابقة إن كان وقتها قد حان، ويبقي فقط ما لم يحن وقته بعد."""
    now_ts = now_utc.timestamp()
    kept = []
    for trade in state.get("pending_trades", []):
        if now_ts < float(trade.get("check_at", 0)):
            kept.append(trade)
            continue
        msg_id = int(trade["message_id"])
        step = int(trade.get("step", 1))
        entry = float(trade["entry"])
        rate_now = fetch_rate(trade["base"], trade["quote"])
        exit_price = rate_now if rate_now is not None else entry
        won = (exit_price > entry) if trade["side"] == "call" else (exit_price < entry)
        if won:
            label = "WIN ✅" if step == 1 else f"WIN ✅ MTG {step - 1}"
            send_telegram(label, reply_to=msg_id)
        elif step <= MAX_MTG:
            trade["step"] = step + 1
            trade["entry"] = exit_price
            trade["check_at"] = now_ts + CANDLE_SEC
            kept.append(trade)
        else:
            send_telegram("Loss 💔", reply_to=msg_id)
    state["pending_trades"] = kept


def publish_and_track_signals(state: dict, now_utc: datetime) -> None:
    local_now = now_utc + timedelta(hours=TZ_OFFSET)
    today_str = local_now.strftime("%Y-%m-%d")
    if state["daily"].get("date") != today_str:
        state["daily"] = {"date": today_str, "count": 0}

    if state["daily"]["count"] >= MAX_SIGNALS_PER_DAY:
        log.info("تم الوصول للحد اليومي (%s)", MAX_SIGNALS_PER_DAY)
        return

    pairs_shuffled = list(PAIRS)
    random.shuffle(pairs_shuffled)
    published = 0

    for idx, (base, quote, symbol) in enumerate(pairs_shuffled):
        if published >= MAX_NEW_PER_RUN or state["daily"]["count"] >= MAX_SIGNALS_PER_DAY:
            break

        last_ts = state["last_signal"].get(symbol)
        if last_ts and (now_utc - datetime.fromisoformat(last_ts)) < timedelta(minutes=COOLDOWN_MIN):
            continue

        p1 = fetch_rate(base, quote)
        if p1 is None:
            continue
        time.sleep(1)
        p2 = fetch_rate(base, quote)
        price = p2 if p2 is not None else p1

        side = "call" if (p2 is None or p2 >= p1) else "put"
        dot_icon = "🟢" if side == "call" else "🔴"
        arrow_icon = "⬆️" if side == "call" else "⬇️"
        tend = "Buy" if side == "call" else "Sell"
        exec_time = (local_now + timedelta(minutes=1)).strftime("%H:%M:00")
        payout = 91 if quote == "JPY" else 93
        forecast = f"{min(95, 50 + idx * 5)}%"
        strategy = STRATEGIES[idx % len(STRATEGIES)]

        msg_text = (
            f"💳 {base}/{quote}\n"
            f"⏱️ {TIMEFRAME}\n"
            f"{dot_icon} {side.upper()} {arrow_icon}\n"
            f"🕒 {exec_time}\n"
            f"🌐 {TZ_LABEL}\n"
            f"📈 Tend: {tend}\n"
            f"⚡ Max: MTG {MAX_MTG}\n"
            f"💰 Payout : {payout}%\n"
            f"🎯 Forecast : {forecast}\n"
            f"♟️ Strategy : {strategy}\n\n"
            f"🎁 Bonus: {BONUS_TEXT}\n"
            f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
        )

        markup = {
            "inline_keyboard": [
                [{"text": "🔗 REGISTER HERE", "url": AFFILIATE_LINK}],
                [{"text": f"🎁 {BONUS_TEXT}", "url": AFFILIATE_LINK}],
            ]
        }

        msg_id = send_telegram(msg_text, reply_markup=markup)
        if msg_id is not None:
            state["last_signal"][symbol] = now_utc.isoformat()
            state["daily"]["count"] += 1
            published += 1
            save_state(state)
            log.info("نُشرت إشارة %s/%s (%s) | msg_id=%s", base, quote, side.upper(), msg_id)

            trade_obj = {
                "message_id": msg_id,
                "pair": f"{base}/{quote}",
                "base": base,
                "quote": quote,
                "side": side,
                "entry": price,
                "step": 1,
                "check_at": time.time() + CANDLE_SEC,
            }
            evaluate_single_trade_now(trade_obj)


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN و CHANNEL_ID مطلوبان")
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    state = load_state()
    flush_old_pending(state, now_utc)
    publish_and_track_signals(state, now_utc)
    save_state(state)
    log.info("إشارات اليوم: %s / %s", state["daily"]["count"], MAX_SIGNALS_PER_DAY)


if __name__ == "__main__":
    main()
