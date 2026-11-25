"""
اختبار تاريخي صادق للاستراتيجيات على أسعار حقيقية (شموع M5 لآخر ~58 يوماً).

القاعدة نفسها المستعملة في البوت: الدخول عند إغلاق شمعة، والخروج عند إغلاق الشمعة
التالية. يُحسب الربح إذا تحرك السعر في اتجاه الإشارة، وتُستبعد حالات التعادل.

للثقة: يُقسَّم الزمن إلى جزء تدريب (أول ثلثين) وجزء اختبار (آخر ثلث لم تُضبط عليه
أي شروط)، ولا تُعتبر الاستراتيجية ناجحة إلا إذا تجاوزت نسبة التعادل في الاثنين.
"""
import math

import numpy as np
import pandas as pd

PAIRS = (
    "EURUSD,GBPUSD,AUDUSD,USDJPY,USDCAD,USDCHF,"
    "NZDUSD,EURJPY,GBPJPY,EURGBP,AUDCAD,EURAUD"
).split(",")
PAYOUT = 0.85                      # عائد المنصة التقريبي عند الربح
BREAK_EVEN = 100 / (1 + PAYOUT)    # النسبة اللازمة حتى لا تخسر مع الوقت
MIN_N = 300                        # أقل عدد إشارات حتى نصدق النتيجة


def load(pair: str) -> pd.Series | None:
    import yfinance as yf

    df = yf.download(f"{pair}=X", period="58d", interval="5m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].dropna().astype(float)
    close.index = close.index.tz_localize("UTC") if close.index.tz is None else close.index.tz_convert("UTC")
    return close


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.where(loss != 0)
    out = 100 - 100 / (1 + rs)
    return out.where(~((loss == 0) & (gain > 0)), 100.0)


def features(close: pd.Series) -> dict:
    m = close.rolling(20).mean()
    sd = close.rolling(20).std()
    sign = np.sign(close.diff())
    return {
        "close": close,
        "rsi": rsi(close),
        "z": (close - m) / sd,
        "ema": close.ewm(span=200, adjust=False).mean(),
        "streak3": sign.rolling(3).sum(),
        "streak5": sign.rolling(5).sum(),
        "hour": pd.Series(close.index.hour, index=close.index),
    }


def sig(call: pd.Series, put: pd.Series) -> pd.Series:
    return call.astype(int) - put.astype(int)


def in_hours(f) -> pd.Series:
    return (f["hour"] >= 7) & (f["hour"] < 17)


STRATEGIES = {
    "RSI 30/70 (الحالية)": lambda f: sig(f["rsi"] < 30, f["rsi"] > 70),
    "RSI 25/75": lambda f: sig(f["rsi"] < 25, f["rsi"] > 75),
    "RSI 20/80": lambda f: sig(f["rsi"] < 20, f["rsi"] > 80),
    "RSI 30/70 + بولنجر 2σ": lambda f: sig((f["rsi"] < 30) & (f["z"] < -2), (f["rsi"] > 70) & (f["z"] > 2)),
    "RSI 25/75 + بولنجر 2.5σ": lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.5), (f["rsi"] > 75) & (f["z"] > 2.5)),
    "RSI 30/70 مع الاتجاه (EMA200)": lambda f: sig((f["rsi"] < 30) & (f["close"] > f["ema"]), (f["rsi"] > 70) & (f["close"] < f["ema"])),
    "RSI 30/70 ضد الاتجاه (EMA200)": lambda f: sig((f["rsi"] < 30) & (f["close"] < f["ema"]), (f["rsi"] > 70) & (f["close"] > f["ema"])),
    "RSI 30/70 ساعات لندن/نيويورك": lambda f: sig((f["rsi"] < 30) & in_hours(f), (f["rsi"] > 70) & in_hours(f)),
    "3 شموع متتالية → عكس": lambda f: sig(f["streak3"] == -3, f["streak3"] == 3),
    "3 شموع متتالية → استمرار": lambda f: sig(f["streak3"] == 3, f["streak3"] == -3),
    "5 شموع متتالية → عكس": lambda f: sig(f["streak5"] == -5, f["streak5"] == 5),
    "RSI 25/75 + بولنجر + ساعات": lambda f: sig((f["rsi"] < 25) & (f["z"] < -2) & in_hours(f), (f["rsi"] > 75) & (f["z"] > 2) & in_hours(f)),
}


def outcomes(close: pd.Series, side: pd.Series) -> pd.Series:
    diff = close.shift(-1) - close
    step = close.index.to_series().shift(-1) - close.index.to_series()
    valid = (side != 0) & (step == pd.Timedelta(minutes=5)) & diff.notna() & (diff != 0)
    return ((diff * side) > 0)[valid]


def wilson_low(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d * 100


def run(loader=load, pairs=PAIRS) -> None:
    data = {}
    for p in pairs:
        try:
            s = loader(p)
        except Exception as exc:
            print(f"! {p}: {exc}")
            s = None
        if s is not None and len(s) > 500:
            data[p] = s
    if not data:
        print("لا توجد بيانات")
        return

    start = min(s.index.min() for s in data.values())
    end = max(s.index.max() for s in data.values())
    cutoff = start + (end - start) * 2 / 3
    days = len({d for s in data.values() for d in s.index.date})
    print(f"الأزواج: {len(data)} | الأيام: {days} | تعادل الربح عند عائد {PAYOUT:.0%}: {BREAK_EVEN:.1f}%")
    print(f"{'الاستراتيجية':<34}{'إشارات':>7}{'نسبة':>7}{'حد أدنى':>9}{'اختبار':>8}{'يومياً':>8}  الحكم")

    for name, fn in STRATEGIES.items():
        wins = []
        for s in data.values():
            f = features(s)
            wins.append(outcomes(s, fn(f)))
        w = pd.concat(wins) if wins else pd.Series(dtype=bool)
        n = int(len(w))
        if n == 0:
            print(f"{name:<34}{0:>7}")
            continue
        rate = w.mean() * 100
        low = wilson_low(int(w.sum()), n)
        test = w[w.index >= cutoff]
        test_rate = test.mean() * 100 if len(test) else float("nan")
        per_day = n / days
        ok = n >= MIN_N and low > BREAK_EVEN and len(test) >= 50 and test_rate > BREAK_EVEN
        verdict = "✅ ناجحة" if ok else ("عينة صغيرة" if n < MIN_N else "بلا أفضلية")
        print(f"{name:<34}{n:>7}{rate:>6.1f}%{low:>8.1f}%{test_rate:>7.1f}%{per_day:>8.1f}  {verdict}")


if __name__ == "__main__":
    run()
