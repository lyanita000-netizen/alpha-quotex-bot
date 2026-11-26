"""
بوت إشارات تلقائي لقناة تيليجرام (يعمل كل 5 دقائق عبر GitHub Actions).

الاستراتيجية (اجتازت الاختبار التاريخي backtest.py):
    RSI(14) أقل من 25 + السعر تحت نطاق بولنجر بـ 2.5 انحراف  → call
    RSI(14) أعلى من 75 + السعر فوق نطاق بولنجر بـ 2.5 انحراف → put

النسبة المعروضة (Forecast) ليست رقماً مخترعاً: هي نسبة نجاح نفس الإشارة تاريخياً
مجمّعة على كل الأزواج (آخر ~58 يوماً)، وتُنشر الإشارة فقط إذا كانت النسبة أعلى من
نسبة التعادل (MIN_FORECAST).

كل تشغيل:
  1) يفحص الصفقات المعلّقة وينشر النتيجة (WIN ✅ / Loss 💔 / Draw ➖) كرد على الإشارة،
     وإن كان MAX_STEPS أكبر من 1 ينشر إشارة تعويض بعد الخسارة (الخسارة لا تُخفى).
  2) يختار أقوى الإشارات ويوزعها على عملات مختلفة.

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
MIN_FORECAST = float(os.environ.get("MIN_FORECAST", "54"))          # ≈ نسبة التعادل عند عائد 85%
MIN_SAMPLES = int(os.environ.get("MIN_SAMPLES", "300"))             # أقل عدد صفقات تاريخية نثق به
MAX_SIGNALS_PER_DAY = int(os.environ.get("MAX_SIGNALS_PER_DAY", "8"))
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "2"))
MAX_PER_PAIR_PER_DAY = int(os.environ.get("MAX_PER_PAIR_PER_DAY", "2"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "2"))                   # 2 = الصفقة + تعويض واحد (1 = بلا تعويض)
READY_BUFFER_SEC = int(os.environ.get("READY_BUFFER_SEC", "3"))        # انتظار بعد إغلاق شمعة النتيجة (الجدولة تبدأ بعد ~25 ثانية)
SUMMARY_HOUR_UTC = int(os.environ.get("SUMMARY_HOUR_UTC", "21"))    # 21:00 UTC = 22:00 الجزائر
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "30"))
MAX_DELAY_MIN = int(os.environ.get("MAX_DELAY_MIN", "3"))
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "-7"))                  # توقيت منصة Quotex (UTC-7)
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

CANDLE_MIN = 5            # فريم M5، والصفقة مدتها شمعة واحدة (5 دقائق)
RSI_PERIOD = 14
RSI_LOW = 25
RSI_HIGH = 75
BB_PERIOD = 20
BB_Z = 2.5

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
    return out.where(~((loss == 0) & (gain > 0)), 100.0)   # لا خسائر إطلاقاً → RSI = 100


def bb_z(close: pd.Series, period: int = BB_PERIOD) -> pd.Series:
    m = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return (close - m) / sd.where(sd != 0)


def signal_series(close: pd.Series) -> pd.Series:
    """+1 = call ، -1 = put ، 0 = لا إشارة (لكل شمعة)."""
    r = rsi(close)
    z = bb_z(close)
    call = (r < RSI_LOW) & (z < -BB_Z)
    put = (r > RSI_HIGH) & (z > BB_Z)
    return call.astype(int) - put.astype(int)


def pooled_backtest(closes: dict) -> dict:
    """نسبة نجاح الإشارة تاريخياً مجمّعة على كل الأزواج، لكل اتجاه.
    الدخول عند إغلاق شمعة الإشارة والخروج عند إغلاق الشمعة التالية، دون التعادل."""
    wins = {1: 0, -1: 0}
    total = {1: 0, -1: 0}
    for close in closes.values():
        side = signal_series(close)
        diff = close.shift(-1) - close
        step = close.index.to_series().shift(-1) - close.index.to_series()
        valid = (side != 0) & (step == pd.Timedelta(minutes=CANDLE_MIN)) & diff.notna() & (diff != 0)
        won = (diff * side) > 0
        for s in (1, -1):
            m = valid & (side == s)
            total[s] += int(m.sum())
            wins[s] += int((won & m).sum())
    return {
        ("call" if s == 1 else "put"): ((wins[s] / total[s] * 100) if total[s] else 0.0, total[s])
        for s in (1, -1)
    }


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
        f"{pair}=X", period="58d", interval="5m", progress=False, auto_adjust=False
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
def pretty_pair(pair: str) -> str:
    return f"{pair[:3]}/{pair[3:]}" if len(pair) == 6 else pair


def build_signal_text(pair, side, close_time, entry, step, rate=None, samples=None) -> str:
    digits = 3 if pair.endswith("JPY") else 5
    arrow, word = ("🔼", "CALL (BUY)") if side == "call" else ("🔽", "PUT (SELL)")
    lines = [f"💳 {pretty_pair(pair)}  •  ⏱ M{CANDLE_MIN}"]
    if step > 1:
        lines.append(f"🔁 Compensation {step - 1}")
    lines += [
        f"{arrow} {word}",
        "",
        f"⏳ Entry: {local_time(close_time)} (Quotex time)",
        f"🎯 Entry price: {entry:.{digits}f}",
        f"⌛ Duration: {CANDLE_MIN} minutes",
    ]
    if step == 1:
        lines.append(f"📊 Historical win rate: {rate:.2f}% (based on {samples} trades)")
    lines += [
        "",
        "⚠️ Trade with a small amount (1–2% of your balance). No profit is guaranteed.",
        f"🔗 Register here: {AFFILIATE_LINK}",
    ]
    return "\n".join(lines)


def record_chain(state: dict, sig: dict, final: str, steps: int, now: datetime) -> None:
    """يسجّل النتيجة النهائية لسلسلة الصفقة (للملخص اليومي)، بما فيها الخسائر."""
    state.setdefault("history", []).append(
        {"ts": now.isoformat(), "pair": sig["pair"], "final": final, "steps": steps}
    )
    state["history"] = state["history"][-500:]


# ================= النتائج والتعويض =================
def check_results(state: dict, now: datetime, get_df) -> None:
    still_pending = []
    for sig in list(state["pending"]):
        step = int(sig.get("step", 1))
        open_ts = datetime.fromisoformat(sig["candle_open"])
        result_open = open_ts + timedelta(minutes=CANDLE_MIN)
        ready_at = result_open + timedelta(minutes=CANDLE_MIN, seconds=READY_BUFFER_SEC)
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
        origin = sig.get("origin_id") or sig["message_id"]
        log.info("نتيجة %s (مرحلة %d): %s (دخول %s ← خروج %s)",
                 sig["pair"], step, outcome, sig["entry"], exit_price)

        def post_final(text: str, final: str) -> None:
            try:
                send_telegram(text, reply_to=origin)
            except Exception as exc:
                log.error("فشل إرسال النتيجة: %s", exc)
                still_pending.append(sig)
                return
            record_chain(state, sig, final, step, now)

        if outcome == "win":
            post_final("WIN ✅" + (SUPERSCRIPT.get(step - 1, "") if step > 1 else ""), "win")
            continue
        if outcome == "draw":
            post_final("Draw ➖", "draw")
            continue

        # خسارة: إن بقيت محاولات تعويض لا ننشر الخسارة الآن، بل ننتظر نتيجة التعويض
        if step < MAX_STEPS:
            close_time = result_open + timedelta(minutes=CANDLE_MIN)
            if now - close_time > timedelta(minutes=MAX_DELAY_MIN):
                log.info("%s: تعذر نشر التعويض لأن السعر أصبح قديماً", sig["pair"])
            else:
                try:
                    text = build_signal_text(sig["pair"], sig["side"], close_time, exit_price, step + 1)
                    resp = send_telegram(text, reply_to=origin)
                    still_pending.append(
                        {
                            "pair": sig["pair"],
                            "side": sig["side"],
                            "entry": exit_price,
                            "candle_open": result_open.isoformat(),
                            "message_id": resp.get("result", {}).get("message_id"),
                            "origin_id": origin,
                            "step": step + 1,
                        }
                    )
                    log.info("تم نشر تعويض %d لـ %s", step, sig["pair"])
                    continue
                except Exception as exc:
                    log.error("فشل نشر التعويض: %s", exc)
        post_final("Loss 💔", "loss")   # كل المحاولات خسرت (أو تعذر التعويض): الخسارة تُنشر ولا تُخفى
    state["pending"] = still_pending


# ================= الإشارات الجديدة =================
def publish_signals(state: dict, now: datetime, get_df) -> None:
    today = (now + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "count": 0, "pairs": {}}
    daily = state["daily"]
    daily.setdefault("pairs", {})

    closes = {}
    for pair in PAIRS:
        df = get_df(pair)
        if df is None or len(df) < BB_PERIOD * 3:
            log.info("%s: لا توجد بيانات كافية من مصدر الأسعار", pair)
            continue
        closes[pair] = df["Close"].astype(float)
    if not closes:
        return

    stats = pooled_backtest(closes)
    log.info("النسبة التاريخية المجمّعة: call %.1f%% (%d) | put %.1f%% (%d)",
             stats["call"][0], stats["call"][1], stats["put"][0], stats["put"][1])

    busy = {s["pair"] for s in state["pending"]}
    pairs = list(closes)
    random.shuffle(pairs)

    candidates = []
    for pair in pairs:
        if pair in busy:
            continue
        if daily["pairs"].get(pair, 0) >= MAX_PER_PAIR_PER_DAY:
            continue

        close = closes[pair]
        last_open = close.index[-1]
        close_time = last_open + timedelta(minutes=CANDLE_MIN)
        if now - close_time > timedelta(minutes=MAX_DELAY_MIN):
            log.info("%s: آخر شمعة قديمة (السوق مغلق أو تأخر التشغيل)", pair)
            continue

        last_sig = state["last_signal"].get(pair)
        if last_sig and now - datetime.fromisoformat(last_sig) < timedelta(minutes=COOLDOWN_MIN):
            continue

        s = int(signal_series(close).iloc[-1])
        if s == 0:
            log.info("%s: RSI=%.1f z=%.2f (لا توجد إشارة الآن)",
                     pair, rsi(close).iloc[-1], bb_z(close).iloc[-1])
            continue
        side = "call" if s == 1 else "put"

        rate, samples = stats[side]
        if samples < MIN_SAMPLES or rate <= MIN_FORECAST:
            log.info("%s: إشارة %s مرفوضة (نسبة تاريخية %.1f%% من %d صفقة)",
                     pair, side, rate, samples)
            continue

        candidates.append(
            {
                "pair": pair, "side": side, "rate": rate, "samples": samples,
                "entry": float(close.iloc[-1]), "last_open": last_open,
                "close_time": close_time, "strength": abs(float(bb_z(close).iloc[-1])),
            }
        )

    candidates.sort(key=lambda c: c["strength"], reverse=True)   # الأكثر تطرفاً أولاً
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
                "origin_id": resp.get("result", {}).get("message_id"),
                "step": 1,
            }
        )
        state["last_signal"][c["pair"]] = now.isoformat()
        daily["count"] += 1
        daily["pairs"][c["pair"]] = daily["pairs"].get(c["pair"], 0) + 1
        published += 1
        log.info("تم نشر إشارة %s %s (%.1f%%)", c["pair"], c["side"], c["rate"])


# ================= الملخص اليومي =================
def build_summary_text(items: list, day_label: str) -> str:
    first = sum(1 for i in items if i["final"] == "win" and i["steps"] == 1)
    comp = sum(1 for i in items if i["final"] == "win" and i["steps"] > 1)
    lost = sum(1 for i in items if i["final"] == "loss")
    draw = sum(1 for i in items if i["final"] == "draw")
    decided = first + comp + lost
    rate = f"{(first + comp) / decided * 100:.0f}%" if decided else "-"
    rate1 = f"{first / decided * 100:.0f}%" if decided else "-"
    return "\n".join([
        f"📊 Daily Summary | ملخص اليوم — {day_label}",
        "",
        f"Finished signals | إشارات منتهية: {len(items)}",
        f"✅ Wins (first trade) | ربح من الصفقة الأولى: {first}",
        f"🔁 Wins after compensation | ربح بعد التعويض: {comp}",
        f"💔 Losses (after compensation) | خسارة بعد التعويض: {lost}",
        f"➖ Draws | تعادل: {draw}",
        "",
        f"📈 Overall win rate | نسبة الربح الإجمالية: {rate}",
        f"🎯 First-trade win rate | نسبة الربح من الصفقة الأولى: {rate1}",
        "",
        "ℹ️ Real numbers, losses included. Compensation needs a larger stake, and a loss after it is larger.",
        "ℹ️ أرقام حقيقية وتشمل الخسائر. التعويض يتطلب مبلغاً أكبر، والخسارة بعده أكبر.",
        "",
        "⚠️ Trade with a small amount. No profit is guaranteed.",
        "⚠️ تداول بمبلغ صغير ولا ضمان للأرباح.",
    ])


def maybe_post_summary(state: dict, now: datetime) -> None:
    due = now.replace(hour=SUMMARY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now < due:
        return
    last = state.get("last_summary", "")
    if last and datetime.fromisoformat(last) >= due:
        return
    since = datetime.fromisoformat(last) if last else due - timedelta(days=1)
    items = [h for h in state.get("history", []) if since < datetime.fromisoformat(h["ts"]) <= now]
    if not items:
        state["last_summary"] = now.isoformat()
        log.info("لا توجد نتائج للملخص اليومي")
        return
    label = (now + timedelta(hours=1)).strftime("%d/%m/%Y")
    try:
        send_telegram(build_summary_text(items, label))
        state["last_summary"] = now.isoformat()
        log.info("تم نشر الملخص اليومي (%d إشارة)", len(items))
    except Exception as exc:
        log.error("فشل نشر الملخص: %s", exc)


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN و CHANNEL_ID مطلوبان في متغيرات البيئة")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    state = load_state()
    state.setdefault("history", [])
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
        maybe_post_summary(state, now)
        publish_signals(state, now, get_df)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
