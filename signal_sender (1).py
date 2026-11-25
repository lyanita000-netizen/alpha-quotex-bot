"""
بوت إشارات تلقائي لقناة تيليجرام.

كل مرة يعمل فيها السكربت (كل 5 دقائق عبر GitHub Actions):
  1) يفحص الصفقات المعلّقة: إذا انتهت، يقارن سعر الدخول بسعر الإغلاق الحقيقي
     وينشر النتيجة (WIN / Loss / Draw) كرد على رسالة الإشارة نفسها.
  2) يحلل الأزواج ببيانات سعر حقيقية، وإذا ظهرت إشارة قوية يعتمد نسبتها على
     نتيجة اختبار تاريخي (backtest) للاستراتيجية نفسها، وينشرها إن تجاوزت الحد.

كل الإعدادات تأتي من متغيرات البيئة (Secrets) وليس من الكود.
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# ================= الإعدادات =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
AFFILIATE_LINK = os.environ.get(
    "AFFILIATE_LINK", "https://broker-qx.pro/sign-up/?lid=2438595"
)
PAIRS = [
    p.strip().upper()
    for p in os.environ.get("PAIRS", "EURUSD,GBPUSD,AUDUSD,USDJPY,USDCAD").split(",")
    if p.strip()
]
MIN_FORECAST = float(os.environ.get("MIN_FORECAST", "50"))        # الحد الأدنى للنسبة %
MAX_SIGNALS_PER_DAY = int(os.environ.get("MAX_SIGNALS_PER_DAY", "8"))
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "30"))          # فاصل بين إشارتين لنفس الزوج
MAX_DELAY_MIN = int(os.environ.get("MAX_DELAY_MIN", "3"))         # أقصى تأخير مسموح بعد إغلاق الشمعة
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "1"))                 # فرق التوقيت المعروض (UTC+1)
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

CANDLE_MIN = 5            # فريم M5، والصفقة مدتها شمعة واحدة (5 دقائق)
RSI_PERIOD = 14
RSI_LOW = 30
RSI_HIGH = 70
MIN_SAMPLES = 15          # أقل عدد صفقات في الباكتست حتى نثق بالنسبة

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("signals")


# ================= التحليل =================
def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.where(loss != 0)
    out = 100 - 100 / (1 + rs)
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)   # لا خسائر إطلاقاً → RSI = 100
    return out


def side_from_rsi(value: float):
    if pd.isna(value):
        return None
    if value < RSI_LOW:
        return "call"
    if value > RSI_HIGH:
        return "put"
    return None


def backtest(close: pd.Series, side: str):
    """نسبة نجاح نفس الإشارة تاريخياً: هل أغلقت الشمعة التالية في الاتجاه المتوقع؟"""
    r = rsi(close)
    wins = total = 0
    for i in range(RSI_PERIOD * 3, len(close) - 1):
        if side_from_rsi(r.iloc[i]) != side:
            continue
        nxt, cur = close.iloc[i + 1], close.iloc[i]
        total += 1
        if (side == "call" and nxt > cur) or (side == "put" and nxt < cur):
            wins += 1
    rate = (wins / total * 100) if total else 0.0
    return rate, total


def decide_result(side: str, entry: float, exit_price: float) -> str:
    if exit_price == entry:
        return "draw"
    won = exit_price > entry if side == "call" else exit_price < entry
    return "win" if won else "loss"


# ================= البيانات =================
def fetch_candles(pair: str):
    """شموع M5 الحقيقية المغلقة فقط، بتوقيت UTC (الفهرس = وقت فتح الشمعة)."""
    import yfinance as yf

    df = yf.download(
        f"{pair}=X", period="30d", interval="5m", progress=False, auto_adjust=False
    )
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = (
        df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    )
    return df


def closed_only(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    return df[df.index + timedelta(minutes=CANDLE_MIN) <= now]


# ================= تيليجرام =================
def send_telegram(text: str, reply_to: int | None = None) -> dict:
    try:
        chat_id = int(CHANNEL_ID)
    except ValueError:
        chat_id = CHANNEL_ID
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


# ================= الحالة =================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("ملف الحالة تالف، سيتم البدء من جديد")
    return {"pending": [], "last_signal": {}, "daily": {"date": "", "count": 0}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def local_time(dt: datetime) -> str:
    return (dt + timedelta(hours=TZ_OFFSET)).strftime("%H:%M:%S")


# ================= النتائج =================
RESULT_TEXT = {"win": "WIN ✅", "loss": "Loss 💔", "draw": "Draw ➖"}


def check_results(state: dict, now: datetime, get_df) -> None:
    still_pending = []
    for sig in state["pending"]:
        open_ts = datetime.fromisoformat(sig["candle_open"])
        result_open = open_ts + timedelta(minutes=CANDLE_MIN)
        ready_at = result_open + timedelta(minutes=CANDLE_MIN, seconds=30)
        if now < ready_at:
            still_pending.append(sig)
            continue

        df = get_df(sig["pair"])
        row = None
        if df is not None and result_open in df.index:
            row = df.loc[result_open]
        if row is None:
            if now - ready_at > timedelta(hours=1):
                log.warning("تعذر إيجاد سعر الإغلاق لـ %s، تم إسقاط الصفقة", sig["pair"])
            else:
                still_pending.append(sig)
            continue

        exit_price = float(row["Close"])
        outcome = decide_result(sig["side"], sig["entry"], exit_price)
        try:
            send_telegram(RESULT_TEXT[outcome], reply_to=sig["message_id"])
            log.info("نتيجة %s: %s (دخول %s ← خروج %s)", sig["pair"], outcome, sig["entry"], exit_price)
        except Exception as exc:
            log.error("فشل إرسال النتيجة: %s", exc)
            still_pending.append(sig)
    state["pending"] = still_pending


# ================= الإشارات الجديدة =================
def publish_signals(state: dict, now: datetime, get_df) -> None:
    today = (now + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d")
    if state["daily"]["date"] != today:
        state["daily"] = {"date": today, "count": 0}

    for pair in PAIRS:
        if state["daily"]["count"] >= MAX_SIGNALS_PER_DAY:
            log.info("تم بلوغ الحد اليومي للإشارات")
            return

        df = get_df(pair)
        if df is None or len(df) < RSI_PERIOD * 5:
            log.info("%s: لا توجد بيانات كافية من مصدر الأسعار", pair)
            continue

        last_open = df.index[-1]
        close_time = last_open + timedelta(minutes=CANDLE_MIN)
        if now - close_time > timedelta(minutes=MAX_DELAY_MIN):
            log.info("%s: آخر شمعة قديمة (السوق مغلق أو تأخر التشغيل)", pair)
            continue

        last_sig = state["last_signal"].get(pair)
        if last_sig and now - datetime.fromisoformat(last_sig) < timedelta(minutes=COOLDOWN_MIN):
            continue

        close = df["Close"].astype(float)
        rsi_now = rsi(close).iloc[-1]
        side = side_from_rsi(rsi_now)
        if side is None:
            log.info("%s: RSI=%.1f (لا توجد إشارة الآن)", pair, rsi_now)
            continue

        rate, samples = backtest(close, side)
        if samples < MIN_SAMPLES or rate <= MIN_FORECAST:
            log.info("%s: إشارة %s مرفوضة (نسبة %.1f%% من %d صفقة)", pair, side, rate, samples)
            continue

        entry = float(close.iloc[-1])
        digits = 3 if pair.endswith("JPY") else 5
        arrow, tend = ("🔼", "Buy") if side == "call" else ("🔽", "Sell")
        text = (
            f"💳 {pair}\n"
            f"🔥 M{CANDLE_MIN}\n"
            f"⏳ {local_time(close_time)}\n"
            f"{arrow} {side}\n\n"
            f"🚦 Tend: {tend}\n"
            f"📈 Forecast: {rate:.2f}% (backtest: {samples} trades)\n"
            f"🎯 Entry: {entry:.{digits}f}\n\n"
            f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
        )
        try:
            resp = send_telegram(text)
        except Exception as exc:
            log.error("فشل نشر إشارة %s: %s", pair, exc)
            continue

        message_id = resp.get("result", {}).get("message_id")
        state["pending"].append(
            {
                "pair": pair,
                "side": side,
                "entry": entry,
                "candle_open": last_open.isoformat(),
                "message_id": message_id,
            }
        )
        state["last_signal"][pair] = now.isoformat()
        state["daily"]["count"] += 1
        log.info("تم نشر إشارة %s %s (%.1f%%)", pair, side, rate)


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN و CHANNEL_ID مطلوبان في متغيرات البيئة")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    state = load_state()
    cache: dict = {}

    def get_df(pair: str):
        if pair not in cache:
            try:
                raw = fetch_candles(pair)
            except Exception as exc:
                log.error("فشل جلب بيانات %s: %s", pair, exc)
                raw = None
            cache[pair] = closed_only(raw, now) if raw is not None else None
        return cache[pair]

    try:
        check_results(state, now, get_df)
        publish_signals(state, now, get_df)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
