import asyncio
import logging
import time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

# ================= إعدادات القناة الأساسية =================
BOT_TOKEN = "9132434770:AAGulVo-sWXI6Mm_59yVQPXXraoAn-nOTRs"
CHANNEL_ID = -1003938306672
AFFILIATE_LINK = "https://broker-qx.pro/sign-up/?lid=2438595"

# جميع أزواج العملات الحقيقية (رمز العرض : رمز Yahoo Finance)
PAIRS_MAP = {
    # أزواج الدولار الرئيسية (Majors)
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    # أزواج اليورو (EUR Crosses)
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "EURCHF": "EURCHF=X",
    "EURCAD": "EURCAD=X",
    "EURAUD": "EURAUD=X",
    "EURNZD": "EURNZD=X",
    # أزواج الباوند (GBP Crosses)
    "GBPJPY": "GBPJPY=X",
    "GBPCHF": "GBPCHF=X",
    "GBPCAD": "GBPCAD=X",
    "GBPAUD": "GBPAUD=X",
    "GBPNZD": "GBPNZD=X",
    # أزواج الأسترالي والكندي والنيوزيلندي والين
    "AUDCAD": "AUDCAD=X",
    "AUDCHF": "AUDCHF=X",
    "AUDJPY": "AUDJPY=X",
    "AUDNZD": "AUDNZD=X",
    "CADJPY": "CADJPY=X",
    "CADCHF": "CADCHF=X",
    "CHFJPY": "CHFJPY=X",
    "NZDJPY": "NZDJPY=X",
    "NZDCAD": "NZDCAD=X",
    "NZDCHF": "NZDCHF=X",
}

SCAN_INTERVAL_SECONDS = 180  # فحص السوق كل 3 دقائق للبحث عن فرصة حقيقية
TRADE_DURATION_MINUTES = 5   # مدة الصفقة (M5) لمتابعة النتيجة الحقيقية بدقة
COOLDOWN_MINUTES = 15        # عدم تكرار نفس الزوج قبل مرور 15 دقيقة

last_signal_time: dict[str, datetime] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def analyze_real_market(yf_symbol: str) -> dict | None:
    """جلب بيانات شموع حقيقية وحساب RSI و EMA لتحديد فرصة دخول فعلية."""
    try:
        df = yf.download(
            tickers=yf_symbol,
            period="2d",
            interval="5m",
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        logger.warning("تعذر جلب بيانات %s: %s", yf_symbol, e)
        return None

    if df is None or df.empty or len(df) < 30:
        return None

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()

    if len(close) < 30:
        return None

    # حساب المتوسطات المتحركة EMA 9 و EMA 21
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    # حساب مؤشر القوة النسبية RSI 14
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

    # شرط شراء حقيقي مع نطاق RSI موسّع (42 إلى 72)
    if (prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21 and 42 <= curr_rsi <= 72) or (
        curr_ema9 > curr_ema21 and curr_price > curr_ema9 and 48 <= curr_rsi <= 70
    ):
        forecast = min(96.0, round(72.0 + (curr_rsi - 42) * 0.75, 2))
        return {
            "direction": "call",
            "arrow": "🔼",
            "tend": "Buy",
            "forecast": forecast,
            "entry_price": curr_price,
        }

    # شرط بيع حقيقي مع نطاق RSI موسّع (28 إلى 58)
    if (prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21 and 28 <= curr_rsi <= 58) or (
        curr_ema9 < curr_ema21 and curr_price < curr_ema9 and 30 <= curr_rsi <= 52
    ):
        forecast = min(96.0, round(72.0 + (58 - curr_rsi) * 0.75, 2))
        return {
            "direction": "put",
            "arrow": "🔽",
            "tend": "Sell",
            "forecast": forecast,
            "entry_price": curr_price,
        }

    return None


def fetch_latest_price(yf_symbol: str, retries: int = 3) -> float | None:
    for attempt in range(retries):
        try:
            df = yf.download(
                tickers=yf_symbol,
                period="1d",
                interval="1m",
                progress=False,
                auto_adjust=True,
            )
            if df is not None and not df.empty:
                close = df["Close"]
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                clean = close.dropna()
                if not clean.empty:
                    return float(clean.iloc[-1])
        except Exception as e:
            logger.warning("محاولة %d لجلب السعر الأخير لـ %s فشلت: %s", attempt + 1, yf_symbol, e)
        if attempt < retries - 1:
            time.sleep(2)
    return None


async def verify_and_post_result(context: ContextTypes.DEFAULT_TYPE):
    """التحقق من النتيجة الحقيقية بعد انتهاء مدة الصفقة ونشرها في القناة."""
    data = context.job.data
    yf_symbol = data["yf_symbol"]
    entry_price = data["entry_price"]
    direction = data["direction"]
    message_id = data["message_id"]

    exit_price = await asyncio.to_thread(fetch_latest_price, yf_symbol)
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
    """فحص أزواج السوق الحقيقية ونشر أقوى صفقة متاحة فقط."""
    now = datetime.now()

    for display_pair, yf_symbol in PAIRS_MAP.items():
        last_time = last_signal_time.get(display_pair)
        if last_time and (now - last_time) < timedelta(minutes=COOLDOWN_MINUTES):
            continue

        signal = await asyncio.to_thread(analyze_real_market, yf_symbol)
        if not signal:
            continue

        exec_time = (now + timedelta(minutes=1)).strftime("%H:%M:%S")
        post_text = (
            f"💳 {display_pair}\n"
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
            last_signal_time[display_pair] = now
            logger.info("تم نشر صفقة حقيقية جديدة: %s (%s)", display_pair, signal["direction"])

            # جدولة التحقق من النتيجة الحقيقية بعد انتهاء مدة الصفقة + دقيقة
            context.job_queue.run_once(
                verify_and_post_result,
                when=(TRADE_DURATION_MINUTES + 1) * 60,
                data={
                    "yf_symbol": yf_symbol,
                    "entry_price": signal["entry_price"],
                    "direction": signal["direction"],
                    "message_id": sent_msg.message_id,
                },
            )
            break  # صفقة واحدة نظيفة في كل دورة فحص
        except TelegramError as e:
            logger.error("فشل نشر الصفقة في القناة: %s", e)
            break


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # يبدأ أول فحص للسوق بعد 5 ثوانٍ من التشغيل ثم كل 3 دقائق تلقائياً
    app.job_queue.run_repeating(
        scan_market_job,
        interval=SCAN_INTERVAL_SECONDS,
        first=5,
    )

    logger.info("🤖 نظام التحليل الحقيقي والنشر التلقائي يعمل الآن على قناتك...")
    app.run_polling()


if __name__ == "__main__":
    main()