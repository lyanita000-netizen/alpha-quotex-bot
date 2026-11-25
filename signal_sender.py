import time
import requests

WEBHOOK_URL = "https://alpha-quotex-bot.onrender.com/webhook"
WEBHOOK_SECRET = "alpha_qx_2026"
PAIR_NAME = "EURUSD"
TIMEFRAME = "M5"
CANDLE_SECONDS = 300          # 5 دقائق لكل مرحلة (M5)
MAX_MARTINGALE_STEPS = 3      # 1 صفقة أساسية + 2 تعويض (winn1, winn2, winn3 أو Loss)


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
    print(f"تم إرسال الصفقة ({direction}) بسعر {entry_price:.5f} | message_id={msg_id}")

    for step in range(1, MAX_MARTINGALE_STEPS + 1):
        time.sleep(CANDLE_SECONDS)
        exit_price = fetch_eurusd_price()
        if exit_price is None:
            exit_price = entry_price

        won = (direction == "call" and exit_price > entry_price) or (
            direction == "put" and exit_price < entry_price
        )

        if won:
            result_label = f"winn{step} ✅"
            post_webhook(
                {
                    "secret": WEBHOOK_SECRET,
                    "type": "result",
                    "result": result_label,
                    "reply_to_message_id": msg_id,
                }
            )
            print(f"تم إرسال النتيجة: {result_label}")
            return

        entry_price = exit_price

    post_webhook(
        {
            "secret": WEBHOOK_SECRET,
            "type": "result",
            "result": "💔 Loss",
            "reply_to_message_id": msg_id,
        }
    )
    print("تم إرسال النتيجة النهائية: 💔 Loss")


if __name__ == "__main__":
    print("بدء تشغيل المرسل السحابي عبر Webhook...")
    try:
        requests.get("https://alpha-quotex-bot.onrender.com", timeout=20)
        time.sleep(5)
    except Exception:
        pass
    run_cycle()