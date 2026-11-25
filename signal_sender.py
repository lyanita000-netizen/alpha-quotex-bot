"""
بوت إشارات تلقائي لقناة تيليجرام (يعمل كل 5 دقائق عبر GitHub Actions).

كل تشغيل:
  1) يفحص الصفقات المعلّقة: إذا انتهت، يقارن سعر الدخول بسعر الإغلاق الحقيقي.
       - ربح  → يرد WIN ✅ مع رقم المرحلة (¹ من المحاولة الأولى، ² من الثانية...)
       - خسارة → يرد Loss 💔، وإن بقيت مراحل تعويض ينشر إشارة تعويض على نفس الزوج
                 والاتجاه. الخسارة الأولى تبقى مسجّلة ولا تُخفى.
  2) يحلل عدة أزواج، ويختار أقوى الإشارات (حسب نسبة الاختبار التاريخي) مع توزيعها
     على عملات مختلفة، وينشرها إن تجاوزت الحد.

كل الإعدادات تأتي من متغيرات البيئة (Secrets) وليس من الكود.
"""
import json
import logging
import os
import random
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
DEFAULT_PAIRS = (
    "EURUSD,GBPUSD,AUDUSD,USDJPY,USDCAD,USDCHF,"
    "NZDUSD,EURJPY,GBPJPY,EURGBP,AUDCAD,EURAUD"
)
PAIRS = [
    p.strip().upper()
    for p in os.environ.get("PAIRS", DEFAULT_PAIRS).split(",")
    if p.strip()
]
MIN_FORECAST = float(os.environ.get("MIN_FORECAST", "50"))          # الحد الأدنى للنسبة %
MAX_SIGNALS_PER_DAY = int(os.environ.get("MAX_SIGNALS_PER_DAY", "8"))
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "2"))       # أقصى إشارات جديدة في التشغيل
MAX_PER_PAIR_PER_DAY = int(os.environ.get("MAX_PER_PAIR_PER_DAY", "2"))  # للتنويع بين العملات
MAX_STEPS = int(os.environ.get("MAX_STEPS", "2"))                   # الإشارة الأصلية + التعويض
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "30"))            # فاصل بين إشارتين لنفس الزوج
MAX_DELAY_MIN = int(os.environ.get("MAX_DELAY_MIN", "3"))           # أقصى تأخير بعد إغلاق الشمعة
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "-7"))                  # توقيت منصة Quotex (UTC-7)
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

CANDLE_MIN = 5            # فريم M5، والصفقة مدتها شمعة واحدة (5 دقائق)
RSI_PERIOD = 14
RSI_LOW = 30
RSI_HIGH = 70
MIN_SAMPLES = 15          # أقل عدد صفقات في الباكتست حتى نثق بالنسبة

SUPERSCRIPT = {1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵"}

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
    """شموع M5 الحقيقية، بتوقيت UTC (الفهرس = وقت فتح الشمعة)."""
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
    return {"pending": [], "last_signal": {}, "daily": {"date": "", "count": 0, "pairs": {}}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def local_time(dt: datetime) -> str:
    return (dt + timedelta(hours=TZ_OFFSET)).strftime("%H:%M:%S")


# ================= نص الإشارة =================
def build_signal_text(pair, side, close_time, entry, step, rate=None, samples=None) -> str:
    digits = 3 if pair.endswith("JPY") else 5
    arrow, tend = ("🔼", "Buy") if side == "call" else ("🔽", "Sell")
    lines = [
        f"💳 {pair}",
        f"🔥 M{CANDLE_MIN}",
        f"⏳ {local_time(close_time)}",
        f"{arrow} {side}",
        "",
    ]
    if step == 1:
        lines += [
            f"🚦 Tend: {tend}",
            f"📈 Forecast: {rate:.2f}% (backtest: {samples} trades)",
        ]
    else:
        lines += [f"🔁 Compensation {step - 1}", f"🚦 Tend: {tend}"]
    lines += [f"🎯 Entry: {entry:.{digits}f}", "", f"🔗 REGISTER HERE: {AFFILIATE_LINK}"]
    return "\n".join(lines)


# ================= النتائج والتعويض =================
def check_results(state: dict, now: datetime, get_df) -> None:
    still_pending = []
    for sig in list(state["pending"]):
        step = int(sig.get("step", 1))
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

        if outcome == "win":
            text = f"WIN ✅{SUPERSCRIPT.get(step, '')}"
        elif outcome == "draw":
            text = "Draw ➖"
        else:
            text = "Loss 💔"

        try:
            send_telegram(text, reply_to=sig["message_id"])
            log.info("نتيجة %s (مرحلة %d): %s (دخول %s ← خروج %s)",
                     sig["pair"], step, outcome, sig["entry"], exit_price)
        except Exception as exc:
            log.error("فشل إرسال النتيجة: %s", exc)
            still_pending.append(sig)
            continue

        # تعويض: فقط بعد خسارة، وإن بقيت مراحل، وبسعر طازج
        if outcome != "loss" or step >= MAX_STEPS:
            continue
        close_time = result_open + timedelta(minutes=CANDLE_MIN)
        if now - close_time > timedelta(minutes=MAX_DELAY_MIN):
            log.info("%s: تعذر نشر التعويض لأن السعر أصبح قديماً", sig["pair"])
            continue
        try:
            text = build_signal_text(sig["pair"], sig["side"], close_time, exit_price, step + 1)
            resp = send_telegram(text, reply_to=sig["message_id"])
        except Exception as exc:
            log.error("فشل نشر التعويض: %s", exc)
            continue
        still_pending.append(
            {
                "pair": sig["pair"],
                "side": sig["side"],
                "entry": exit_price,
                "candle_open": result_open.isoformat(),
                "message_id": resp.get("result", {}).get("message_id"),
                "step": step + 1,
            }
        )
        log.info("تم نشر تعويض %d لـ %s", step, sig["pair"])
    state["pending"] = still_pending


# ================= الإشارات الجديدة =================
def publish_signals(state: dict, now: datetime, get_df) -> None:
    today = (now + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "count": 0, "pairs": {}}
    daily = state["daily"]
    daily.setdefault("pairs", {})

    busy = {s["pair"] for s in state["pending"]}      # زوج فيه صفقة/تعويض قيد الانتظار
    pairs = PAIRS[:]
    random.shuffle(pairs)

    candidates = []
    for pair in pairs:
        if pair in busy:
            continue
        if daily["pairs"].get(pair, 0) >= MAX_PER_PAIR_PER_DAY:
            continue

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

        candidates.append(
            {
                "pair": pair, "side": side, "rate": rate, "samples": samples,
                "entry": float(close.iloc[-1]), "last_open": last_open,
                "close_time": close_time,
            }
        )

    candidates.sort(key=lambda c: c["rate"], reverse=True)   # الأقوى أولاً
    published = 0
    for c in candidates:
        if daily["count"] >= MAX_SIGNALS_PER_DAY:
            log.info("تم بلوغ الحد اليومي للإشارات")
            break
        if published >= MAX_NEW_PER_RUN:
            break

        text = build_signal_text(
            c["pair"], c["side"], c["close_time"], c["entry"], 1, c["rate"], c["samples"]
        )
        try:
            resp = send_telegram(text)
        except Exception as exc:
            log.error("فشل نشر إشارة %s: %s", c["pair"], exc)
            continue

        state["pending"].append(
            {
                "pair": c["pair"],
                "side": c["side"],
                "entry": c["entry"],
                "candle_open": c["last_open"].isoformat(),
                "message_id": resp.get("result", {}).get("message_id"),
                "step": 1,
            }
        )
        state["last_signal"][c["pair"]] = now.isoformat()
        daily["count"] += 1
        daily["pairs"][c["pair"]] = daily["pairs"].get(c["pair"], 0) + 1
        published += 1
        log.info("تم نشر إشارة %s %s (%.1f%%)", c["pair"], c["side"], c["rate"])


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
