import time
import requests

WEBHOOK_URL = "https://alpha-quotex-bot.onrender.com/webhook"
WEBHOOK_SECRET = "alpha_qx_2026"
PAIR_NAME = "EURUSD"
TIMEFRAME = "M1"
CANDLE_SECONDS = 60           # دقيقة واحدة لكل مرحلة
TOTAL_STEPS = 4               # 1 صفقة أساسية + 3 صفقات تعويض (حد أقصى 4 دقائق)

SUPERSCRIPTS = {
    1: "¹",
    2: "²",
    3: "³",
}


def fetch_eurusd_price() -> float | None:
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=USD", timeout=12)
        r.raise_for_status()
        rate = r.json().get("rates", {}).get("USD")
        if rate is not None:
            return float(rate)
    except Exception as e:
        print("تنبيه جلب السعر:", e)
    return None


def post_webhook(payload: dict) -> dict | None:
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("خطأ في إرسال Webhook:", e)
        return None


def is_win(direction: str, entry_price: float, exit_price: float) -> bool:
    if direction == "call":
        return exit_price > entry_price
    return exit_price < entry_price


def format_win_label(step: int) -> str:
    if step == 1:
        return "WINN ✅"
    sup = SUPERSCRIPTS.get(step - 1, str(step - 1))
    return f"WINN{sup} ✅"


def run_cycle():
    p1 = fetch_eurusd_price()
    time.sleep(5)
    p2 = fetch_eurusd_price()
    if p1 is None or p2 is None:
        print("تعذر قراءة سعرين متتاليين.")
        return

    direction = "call" if p2 >= p1 else "put"
    entry_price = p2

    resp = post_webhook(
        {
            "secret": WEBHOOK_SECRET,
            "pair": PAIR_NAME,
            "direction": direction,
            "timeframe": TIMEFRAME,
            "forecast": "85.00",
            "price": f"{entry_price:.5f}",
        }
    )

    if not resp or "message_id" not in resp:
        print("لم يتم استلام message_id من السيرفر.")
        return

    msg_id = resp["message_id"]
    print(f"تم نشر الصفقة ({direction}) بسعر دخول {entry_price:.5f} | message_id={msg_id}")

    for step in range(1, TOTAL_STEPS + 1):
        stage_name = "الصفقة الأساسية" if step == 1 else f"صفقة التعويض {step - 1}"
        print(f"انتظار دقيقة واحدة لنتيجة {stage_name} (الدقيقة {step})...")
        time.sleep(CANDLE_SECONDS)

        exit_price = fetch_eurusd_price()
        if exit_price is None:
            exit_price = entry_price

        if is_win(direction, entry_price, exit_price):
            result_label = format_win_label(step)
            post_webhook(
                {
                    "secret": WEBHOOK_SECRET,
                    "type": "result",
                    "result": result_label,
                    "reply_to_message_id": msg_id,
                }
            )
            print(f"نجحت {stage_name} وتم نشر النتيجة: {result_label}")
            return

        print(f"لم تنجح {stage_name} عند السعر {exit_price:.5f}، الانتقال للمرحلة التالية دون نشر نتيجة...")
        entry_price = exit_price

    post_webhook(
        {
            "secret": WEBHOOK_SECRET,
            "type": "result",
            "result": "💔 Loss",
            "reply_to_message_id": msg_id,
        }
    )
    print("انتهت جميع المراحل (4 دقائق) بخسارة، وتم نشر النتيجة: 💔 Loss")


if __name__ == "__main__":
    print("بدء تشغيل المرسل السحابي عبر Webhook...")
    try:
        requests.get("https://alpha-quotex-bot.onrender.com", timeout=20)
        time.sleep(5)
    except Exception:
        pass
    run_cycle()
