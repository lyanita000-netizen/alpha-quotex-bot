import time
import requests

WEBHOOK_URL = "https://alpha-quotex-bot.onrender.com/webhook"
WEBHOOK_SECRET = "alpha_qx_2026"
TIMEFRAME = "M1"
CANDLE_SECONDS = 60           # دقيقة واحدة لكل مرحلة
TOTAL_STEPS = 4               # 1 صفقة أساسية + 3 صفقات تعويض
SESSION_DURATION = 25 * 60    # مراقبة مستمرة لمدة 25 دقيقة في كل جلسة سحابية
SCAN_INTERVAL = 8             # فحص الأزواج كل 8 ثوانٍ عند البحث عن فرصة
MIN_MOVE_THRESHOLD = 0.00012  # حد الحركة المطلوب لاعتبار الفرصة مربحة

PAIRS = [
    ("EUR", "USD", "EURUSD"),
    ("GBP", "USD", "GBPUSD"),
    ("USD", "JPY", "USDJPY"),
    ("AUD", "USD", "AUDUSD"),
    ("EUR", "JPY", "EURJPY"),
]

SUPERSCRIPTS = {
    1: "¹",
    2: "²",
    3: "³",
}


def fetch_pair_price(base: str, quote: str) -> float | None:
    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={base}&to={quote}",
            timeout=12,
        )
        r.raise_for_status()
        rate = r.json().get("rates", {}).get(quote)
        if rate is not None:
            return float(rate)
    except Exception as e:
        print(f"تنبيه جلب السعر {base}{quote}:", e)
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
        return "WIN ✅"
    sup = SUPERSCRIPTS.get(step - 1, str(step - 1))
    return f"WIN{sup} ✅"


def find_profitable_setup():
    first_reads = {}
    for base, quote, symbol in PAIRS:
        p1 = fetch_pair_price(base, quote)
        if p1 is not None:
            first_reads[symbol] = (base, quote, p1)

    time.sleep(5)

    best_candidate = None
    best_move = 0.0

    for symbol, (base, quote, p1) in first_reads.items():
        p2 = fetch_pair_price(base, quote)
        if p2 is None:
            continue

        normalized_move = abs(p2 - p1) / (p1 if p1 else 1.0)
        if normalized_move >= MIN_MOVE_THRESHOLD and normalized_move > best_move:
            direction = "call" if p2 > p1 else "put"
            best_move = normalized_move
            # حساب نسبة الثقة ديناميكياً بين 85% و 96% حسب قوة الحركة
            confidence = min(96.0, 85.0 + (normalized_move / MIN_MOVE_THRESHOLD) * 3.0)
            best_candidate = {
                "base": base,
                "quote": quote,
                "pair": symbol,
                "direction": direction,
                "entry_price": p2,
                "forecast": f"{confidence:.2f}",
            }

    return best_candidate


def execute_signal(setup: dict):
    pair = setup["pair"]
    base = setup["base"]
    quote = setup["quote"]
    direction = setup["direction"]
    entry_price = setup["entry_price"]
    forecast = setup["forecast"]
    decimals = 3 if quote == "JPY" else 5

    resp = post_webhook(
        {
            "secret": WEBHOOK_SECRET,
            "pair": pair,
            "direction": direction,
            "timeframe": TIMEFRAME,
            "forecast": forecast,
            "price": f"{entry_price:.{decimals}f}",
        }
    )

    if not resp or "message_id" not in resp:
        print("لم يتم استلام message_id من السيرفر.")
        return

    msg_id = resp["message_id"]
    print(f"تم نشر الصفقة {pair} ({direction}) | نسبة الربح {forecast}% | message_id={msg_id}")

    for step in range(1, TOTAL_STEPS + 1):
        stage_name = "الصفقة الأساسية" if step == 1 else f"صفقة التعويض {step - 1}"
        time.sleep(CANDLE_SECONDS)

        exit_price = fetch_pair_price(base, quote)
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

        entry_price = exit_price

    post_webhook(
        {
            "secret": WEBHOOK_SECRET,
            "type": "result",
            "result": "💔 Loss",
            "reply_to_message_id": msg_id,
        }
    )
    print("انتهت المراحل بخسارة، وتم نشر النتيجة: 💔 Loss")


def run_continuous_scanner():
    start_time = time.time()
    signals_sent = 0
    print("بدء جلسة المراقبة المستمرة لجميع الأزواج حسب فرص الربح...")

    while (time.time() - start_time) < SESSION_DURATION:
        setup = find_profitable_setup()
        if setup:
            execute_signal(setup)
            signals_sent += 1
            print(f"إجمالي الصفقات المنشورة في هذه الجلسة: {signals_sent} — العودة الفورية لمراقبة السوق...")
        else:
            print("السوق هادئ حالياً، إعادة الفحص بعد ثوانٍ...")
            time.sleep(SCAN_INTERVAL)

    print(f"انتهت جلسة المراقبة (25 دقيقة) بعد نشر {signals_sent} صفقة.")


if __name__ == "__main__":
    try:
        requests.get("https://alpha-quotex-bot.onrender.com", timeout=20)
        time.sleep(5)
    except Exception:
        pass
    run_continuous_scanner()
