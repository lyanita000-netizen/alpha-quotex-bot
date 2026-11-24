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
RENDER_SERVICE_URL = "https://alpha-quotex-bot.onrender.com"

# جميع أزواج العملات المدعومة (الأساس : المقابل)
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
    ("EUR", "NZD"),
    ("GBP", "JPY"),
    ("GBP", "CHF"),
    ("GBP", "CAD"),
    ("GBP", "AUD"),
    ("GBP", "NZD"),
    ("AUD", "CAD"),
    ("AUD", "CHF"),
    ("AUD", "JPY"),
    ("AUD", "NZD"),
    ("CAD", "JPY"),
    ("CAD", "CHF"),
    ("CHF", "JPY"),
    ("NZD", "JPY"),
    ("NZD", "CAD"),
    ("NZD", "CHF"),
]

SCAN_INTERVAL_SECONDS = 60         # جمع الأسعار وفحص السوق تلقائياً كل دقيقة
TRADE_DURATION_MINUTES = 5         # مدة الشمعة / الصفقة (M5)
MAX_MARTINGALE_STEPS = 3           # أقصى عدد لمحاولات التعويض (winn1, winn2, winn3)
SIGNAL_COOLDOWN_MINUTES = 15       # انتظار 15 دقيقة قبل تكرار صفقة نفس الزوج
ANALYSIS_COOLDOWN_MINUTES = 15     # الحد الأدنى بين تحليلات نفس الزوج
NEWS_INTERVAL_SECONDS = 1800       # نشر أخبار السوق والمتداولين كل 30 دقيقة
KEEP_ALIVE_INTERVAL_SECONDS = 480  # تنشيط رابط الخدمة كل 8 دقائق لتقليل الخمول
MIN_HISTORY_POINTS = 10            # عدد نقاط السعر المطلوبة للبدء السريع

MARKET_AND_TRADERS_NEWS = [
    (
        "📰 أخبار السوق والمتداولين — سلوك السيولة والانضباط",
        "يلاحظ المتداولون النشطون أن الفترات التي تشهد تذبذباً واضحاً تتطلب التزاماً صارماً بقواعد الدخول والخروج وتجنب القرارات العشوائية بعد حركة سريعة. "
        "المتداول المنضبط يركّز على جودة الإعداد الفني وإدارة حجم الصفقة قبل أي شيء.",
    ),
    (
        "📰 نبض السوق — كيف يتعامل المتداولون مع الزخم؟",
        "عندما يتسارع السعر على أزواج العملات الرئيسية، يميل بعض المتداولين إلى مطاردة الشموع المتأخرة، بينما ينتظر المحترفون تأكيد الاتجاه والزخم. "
        "قراءة سلوك السعر بهدوء تمنحك رؤية أوضح لحركة السوق القادمة.",
    ),
    (
        "📰 أخبار المتداولين — إدارة المخاطر في أوقات التقلب",
        "أحد أبرز الفروقات بين المتداول المبتدئ والمتداول المستمر هو الالتزام بإدارة رأس المال وتقسيم الرصيد وعدم رفع المخاطرة لتعويض حركة سابقة. "
        "الاستمرارية في السوق تأتي من الحفاظ على رأس المال أولاً.",
    ),
    (
        "📰 متابعة السوق — قراءة الاتجاه مقابل التصحيح",
        "كثيراً ما يختلط على المتداولين الفرق بين تصحيح سعري مؤقت وبين تغير حقيقي في الاتجاه. "
        "مراقبة المتوسطات المتحركة ومؤشر الزخم تساعد على فهم ما إذا كانت الحركة استمراراً للاتجاه أو مجرد استراحة مؤقتة للسعر.",
    ),
]

price_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=120))
last_signal_time: dict[str, datetime] = {}
last_analysis_time: dict[str, datetime] = {}
news_index: int = 0

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


def ping_self():
    try:
        requests.get(RENDER_SERVICE_URL, timeout=15)
    except Exception as e:
        logger.debug("تنشيط الخدمة الذاتي: %s", e)


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


def compute_indicators(prices: list[float]) -> dict | None:
    if len(prices) < MIN_HISTORY_POINTS:
        return None

    close = pd.Series(prices, dtype="float64")
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window=7, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(window=7, min_periods=1).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    curr_price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2]) if len(close) >= 2 else curr_price
    curr_ema9 = float(ema9.iloc[-1])
    curr_ema21 = float(ema21.iloc[-1])
    prev_ema9 = float(ema9.iloc[-2]) if len(ema9) >= 2 else curr_ema9
    prev_ema21 = float(ema21.iloc[-2]) if len(ema21) >= 2 else curr_ema21
    curr_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0

    return {
        "price": curr_price,
        "prev_price": prev_price,
        "ema9": curr_ema9,
        "ema21": curr_ema21,
        "prev_ema9": prev_ema9,
        "prev_ema21": prev_ema21,
        "rsi": curr_rsi,
    }


def build_description(pair_name: str, tend: str, rsi_val: float, ema_bullish: bool) -> str:
    trend_desc = "تقاطع إيجابي ودعم صاعد للمتوسطات" if ema_bullish else "ضغط بيعي وتمركز تحت المتوسطات"
    if rsi_val >= 65:
        momentum_desc = f"مؤشر RSI عند {rsi_val:.1f} يشير إلى تشبع شرائي قريب من مقاومة محتملة"
    elif rsi_val <= 35:
        momentum_desc = f"مؤشر RSI عند {rsi_val:.1f} يشير إلى تشبع بيعي واحتمال ارتداد أو استمرار زخم"
    else:
        momentum_desc = f"مؤشر RSI عند {rsi_val:.1f} يعكس حركة منتظمة تدعم اتجاه {tend}"
    return f"تحليل {pair_name}: {trend_desc}، و{momentum_desc}. يُنصح بالالتزام بإدارة رأس المال."


def analyze_trade_signal(pair_name: str, prices: list[float]) -> dict | None:
    ind = compute_indicators(prices)
    if not ind:
        return None

    curr_price = ind["price"]
    curr_ema9 = ind["ema9"]
    curr_ema21 = ind["ema21"]
    curr_rsi = ind["rsi"]

    if curr_ema9 >= curr_ema21 or curr_price >= curr_ema21:
        forecast = min(96.0, round(72.0 + max(0.0, curr_rsi - 42) * 0.75, 2))
        return {
            "direction": "call",
            "arrow": "🔼",
            "tend": "Buy",
            "forecast": forecast,
            "entry_price": curr_price,
            "description": build_description(pair_name, "Buy", curr_rsi, True),
        }

    forecast = min(96.0, round(72.0 + max(0.0, 58 - curr_rsi) * 0.75, 2))
    return {
        "direction": "put",
        "arrow": "🔽",
        "tend": "Sell",
        "forecast": forecast,
        "entry_price": curr_price,
        "description": build_description(pair_name, "Sell", curr_rsi, False),
    }


def build_market_analysis(pair_name: str, prices: list[float]) -> str | None:
    ind = compute_indicators(prices)
    if not ind:
        return None

    curr_price = ind["price"]
    prev_price = ind["prev_price"]
    curr_ema9 = ind["ema9"]
    curr_ema21 = ind["ema21"]
    curr_rsi = ind["rsi"]

    bullish = curr_ema9 >= curr_ema21 or curr_price >= curr_ema21
    if bullish:
        arrow = "🔼"
        direction = "call"
        tend = "Buy"
        forecast = min(96.0, round(72.0 + max(0.0, curr_rsi - 42) * 0.75, 2))
    else:
        arrow = "🔽"
        direction = "put"
        tend = "Sell"
        forecast = min(96.0, round(72.0 + max(0.0, 58 - curr_rsi) * 0.75, 2))

    exec_time = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M:%S")
    change_pct = ((curr_price - prev_price) / prev_price) * 100 if prev_price != 0 else 0.0
    description = build_description(pair_name, tend, curr_rsi, bullish)

    return (
        f"📊 تحليل السوق — {pair_name}\n"
        f"💳 {pair_name}\n"
        f"🔥 M{TRADE_DURATION_MINUTES}\n"
        f"⏳ {exec_time}\n"
        f"{arrow} {direction}\n\n"
        f"🚦 Tend: {tend}\n"
        f"📈 Forecast: {forecast:.2f}%\n"
        f" Entry: {curr_price:.5f} ({change_pct:+.3f}%)\n\n"
        f"📝 {description}\n\n"
        f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
    )


async def verify_and_post_result(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    pair_name = data["pair_name"]
    entry_price = data["entry_price"]
    direction = data["direction"]
    message_id = data["message_id"]
    step = data.get("step", 1)

    snapshot = await asyncio.to_thread(fetch_rates_snapshot)
    current_price = snapshot.get(pair_name)
    if current_price is None and price_history[pair_name]:
        current_price = price_history[pair_name][-1]
    if current_price is not None:
        price_history[pair_name].append(current_price)

    if current_price is not None:
        is_win = (direction == "call" and current_price > entry_price) or (
            direction == "put" and current_price < entry_price
        )
        if is_win:
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"winn{step} ✅",
                    reply_to_message_id=message_id,
                )
            except TelegramError as e:
                logger.warning("تعذر نشر نتيجة الفوز في القناة: %s", e)
            return

    if step < MAX_MARTINGALE_STEPS:
        next_step = step + 1
        next_entry = current_price if current_price is not None else entry_price
        context.job_queue.run_once(
            verify_and_post_result,
            when=TRADE_DURATION_MINUTES * 60,
            data={
                "pair_name": pair_name,
                "entry_price": next_entry,
                "direction": direction,
                "message_id": message_id,
                "step": next_step,
            },
        )
        logger.info(
            "لم تتحقق الصفقة في المحاولة %d لـ %s، بانتظار المحاولة %d...",
            step,
            pair_name,
            next_step,
        )
    else:
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text="💔 Loss",
                reply_to_message_id=message_id,
            )
        except TelegramError as e:
            logger.warning("تعذر نشر نتيجة الخسارة في القناة: %s", e)


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

        last_sig = last_signal_time.get(pair_name)
        if not last_sig or (now - last_sig) >= timedelta(minutes=SIGNAL_COOLDOWN_MINUTES):
            signal = analyze_trade_signal(pair_name, history)
            if signal:
                exec_time = (now + timedelta(minutes=1)).strftime("%H:%M:%S")
                post_text = (
                    f"💳 {pair_name}\n"
                    f"🔥 M{TRADE_DURATION_MINUTES}\n"
                    f"⏳ {exec_time}\n"
                    f"{signal['arrow']} {signal['direction']}\n\n"
                    f"🚦 Tend: {signal['tend']}\n"
                    f"📈 Forecast: {signal['forecast']:.2f}%\n"
                    f" Entry: {signal['entry_price']:.5f}\n\n"
                    f"📝 {signal['description']}\n\n"
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
                            "step": 1,
                        },
                    )
                    break
                except TelegramError as e:
                    logger.error("فشل نشر الصفقة في القناة: %s", e)
                    break

        last_ana = last_analysis_time.get(pair_name)
        if not last_ana or (now - last_ana) >= timedelta(minutes=ANALYSIS_COOLDOWN_MINUTES):
            analysis_text = build_market_analysis(pair_name, history)
            if analysis_text:
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=analysis_text,
                        disable_web_page_preview=True,
                    )
                    last_analysis_time[pair_name] = now
                    logger.info("تم نشر تحليل سوق تلقائي لزوج %s في القناة.", pair_name)
                    break
                except TelegramError as e:
                    logger.error("تعذر نشر تحليل السوق في القناة: %s", e)
                    break


async def post_market_news_job(context: ContextTypes.DEFAULT_TYPE):
    global news_index

    title, body = MARKET_AND_TRADERS_NEWS[news_index % len(MARKET_AND_TRADERS_NEWS)]
    news_index += 1
    now_str = datetime.now().strftime("%H:%M:%S")

    news_text = (
        f"{title}\n"
        f"🕒 {now_str}\n\n"
        f"📝 الوصف:\n"
        f"{body}\n\n"
        f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
    )

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=news_text,
            disable_web_page_preview=True,
        )
        logger.info("تم نشر خبر السوق والمتداولين تلقائياً في القناة.")
    except TelegramError as e:
        logger.error("تعذر نشر أخبار السوق في القناة: %s", e)


async def keep_alive_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(ping_self)


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
    app.job_queue.run_repeating(
        post_market_news_job,
        interval=NEWS_INTERVAL_SECONDS,
        first=120,
    )
    app.job_queue.run_repeating(
        keep_alive_job,
        interval=KEEP_ALIVE_INTERVAL_SECONDS,
        first=180,
    )

    logger.info("🤖 نظام الصفقات والتحليلات وأخبار السوق يعمل الآن تلقائياً على قناتك...")
    app.run_polling()


if __name__ == "__main__":
    main()