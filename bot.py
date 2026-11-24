import asyncio
import logging
import os
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import pandas as pd
import requests
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

# ================= إعدادات القناة الأساسية =================
BOT_TOKEN = "9132434770:AAGulVo-sWXI6Mm_59yVQPXXraoAn-nOTRs"
CHANNEL_ID = -1003938306672
AFFILIATE_LINK = "https://broker-qx.pro/sign-up/?lid=2438595"

# أزواج العملات المدعومة (الأساس : المقابل)
PAIRS_LIST = [
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("USD", "CHF"),
    ("USD", "CAD"),
    ("AUD", "USD"),
    ("NZD", "USD"),
    ("EUR", "GBP"),
    ("EUR", "JPY"),
    ("EUR", "CHF"),
    ("EUR", "CAD"),
    ("EUR", "AUD"),
    ("GBP", "JPY"),
    ("GBP", "CHF"),
    ("GBP", "CAD"),
    ("AUD", "JPY"),
    ("CAD", "JPY"),
    ("CHF", "JPY"),
]

SCAN_INTERVAL_SECONDS = 60   # جمع الأسعار وفحص السوق كل دقيقة
TRADE_DURATION_MINUTES = 5   # مدة الصفقة (M5)
COOLDOWN_MINUTES = 20        # انتظار 20 دقيقة قبل تكرار نفس الزوج
MIN_HISTORY_POINTS = 25      # عدد نقاط السعر المطلوبة لحساب المؤشرات

price_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=120))
last_signal_time: dict[str, datetime] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# خادم ويب خفيف لقبول الخطة المجانية على Render
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass


def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def fetch_rates_snapshot() -> dict[str, float]:
    rates_out: dict[str, float] = {}
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest?from=EUR",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        eur_rates = data.get("rates", {})
        eur_rates["EUR"] = 1.0

        for base, quote in PAIRS_LIST:
            if base in eur_rates and quote in eur_rates:
                pair_name = f"{base}{quote}"
                rates_out[pair_name] = float(eur_rates[quote]) / float(eur_rates[base])
    except Exception as e:
        logger.warning("تعذر جلب أسعار الصرف اللحظية: %s", e)
    return rates_out


def analyze_series(prices: list[float]) -> dict | None:
    if len(prices) < MIN_HISTORY_POINTS:
        return None

    close = pd.Series(prices, dtype="float64")
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window=14).mean()
    loss = (-delta.clip(upper=0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    curr_price = float(close.iloc[-1])
    curr_ema9 = float(ema9.iloc[-1])
    curr_ema21 = float(ema21.iloc[-1])
    prev_ema9 = float(ema9.iloc[-2])
    prev_ema21 = float(ema21.iloc[-2])
    curr_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0

    if (prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21 and curr_rsi >= 45) or (
        curr_ema9 > curr_ema21 and curr_price > curr_ema9 and 45 <= curr_rsi <= 75
    ):
        forecast = min(95.0, round(72.0 + max(0.0, curr_rsi - 45) * 0.7, 2))
        return {
            "direction": "call",
            "arrow": "🔼",
            "tend": "Buy",
            "forecast": forecast,
            "entry_price": curr_price,
        }

    if (prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21 and curr_rsi <= 55) or (
        curr_ema9 < curr_ema21 and curr_price < curr_ema9 and 25 <= curr_rsi <= 55
    ):
        forecast = min(95.0, round(72.0 + max(0.0, 55 - curr_rsi) * 0.7, 2))
        return {
            "direction": "put",
            "arrow": "🔽",
            "tend": "Sell",
            "forecast": forecast,
            "entry_price": curr_price,
        }

    return None


async def verify_and_post_result(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    pair_name = data["pair_name"]
    entry_price = data["entry_price"]
    direction = data["direction"]
    message_id = data["message_id"]

    snapshot = await asyncio.to_thread(fetch_rates_snapshot)
    exit_price = snapshot.get(pair_name)
    if exit_price is None and price_history[pair_name]:
        exit_price = price_history[pair_name][-1]
    if exit_price is None:
        return

    is_win = (direction == "call" and exit_price > entry_price) or (
        direction == "put" and exit_price < entry_price
    )
    result_text = "WIN ✅" if is_win else "💔 Loss"

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=result_text,
            reply_to_message_id=message_id,
        )
    except TelegramError as e:
        logger.warning("تعذر نشر النتيجة في القناة: %s", e)


async def scan_market_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    snapshot = await asyncio.to_thread(fetch_rates_snapshot)
    if not snapshot:
        return

    for pair_name, current_price in snapshot.items():
        price_history[pair_name].append(current_price)

    for pair_name, _ in PAIRS_LIST:
        history = list(price_history[pair_name])
        if len(history) < MIN_HISTORY_POINTS:
            continue

        last_time = last_signal_time.get(pair_name)
        if last_time and (now - last_time) < timedelta(minutes=COOLDOWN_MINUTES):
            continue

        signal = analyze_series(history)
        if not signal:
            continue

        exec_time = (now + timedelta(minutes=1)).strftime("%H:%M:%S")
        post_text = (
            f"💳 {pair_name}\n"
            f"🔥 M{TRADE_DURATION_MINUTES}\n"
            f"⏳ {exec_time}\n"
            f"{signal['arrow']} {signal['direction']}\n\n"
            f"🚦 Tend: {signal['tend']}\n"
            f"📈 Forecast: {signal['forecast']:.2f}%\n"
            f" Entry: {signal['entry_price']:.5f}\n\n"
            f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
        )

        try:
            sent_msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                disable_web_page_preview=True,
            )
            last_signal_time[pair_name] = now
            logger.info("تم نشر صفقة جديدة: %s (%s)", pair_name, signal["direction"])

            context.job_queue.run_once(
                verify_and_post_result,
                when=TRADE_DURATION_MINUTES * 60,
                data={
                    "pair_name": pair_name,
                    "entry_price": signal["entry_price"],
                    "direction": signal["direction"],
                    "message_id": sent_msg.message_id,
                },
            )
            break
        except TelegramError as e:
            logger.error("فشل نشر الصفقة في القناة: %s", e)
            break


def main():
    threading.Thread(target=start_web_server, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()
    app.job_queue.run_repeating(
        scan_market_job,
        interval=SCAN_INTERVAL_SECONDS,
        first=5,
    )

    logger.info("🤖 نظام التحليل والنشر التلقائي يعمل الآن على قناتك...")
    app.run_polling()


if __name__ == "__main__":
    main()