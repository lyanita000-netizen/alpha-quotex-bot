"""
اختبار تاريخي صادق (v2): 27 زوجاً حقيقياً × عدة استراتيجيات، شموع M5 لآخر ~58 يوماً.

القاعدة: الدخول عند إغلاق شمعة الإشارة، والخروج عند إغلاق الشمعة التالية.
الربح إن تحرك السعر في اتجاه الإشارة، ويُستبعد التعادل. لا نظر للمستقبل (no lookahead).

لأننا نجرّب استراتيجيات كثيرة (واحدة منها قد تنجح صدفة)، شروط القبول أشد:
  - عدد الإشارات >= 300
  - الحد الأدنى لفاصل الثقة 99% (Wilson) أعلى من نقطة التعادل
  - آخر ثلث من الزمن (لم تُضبط عليه الشروط) فيه >= 100 إشارة ونسبته أعلى من نقطة التعادل
"""
import math
import time

import numpy as np
import pandas as pd

PAIRS = (
    "EURUSD,GBPUSD,AUDUSD,NZDUSD,USDJPY,USDCAD,USDCHF,"
    "EURJPY,GBPJPY,AUDJPY,NZDJPY,CADJPY,CHFJPY,"
    "EURGBP,EURAUD,EURNZD,EURCAD,EURCHF,"
    "GBPAUD,GBPCAD,GBPCHF,GBPNZD,"
    "AUDCAD,AUDCHF,AUDNZD,NZDCAD,CADCHF"
).split(",")
PAYOUT = 0.85
BREAK_EVEN = 100 / (1 + PAYOUT)      # ≈ 54.05%
MIN_N = 300
MIN_TEST_N = 100
Z99 = 2.576


def load(pair: str) -> pd.DataFrame | None:
    import yfinance as yf

    for attempt in range(3):
        try:
            df = yf.download(f"{pair}=X", period="58d", interval="5m", progress=False, auto_adjust=False)
            break
        except Exception:
            df = None
            time.sleep(2)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna().astype(float)
    df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    return df


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.where(loss != 0)
    out = 100 - 100 / (1 + rs)
    return out.where(~((loss == 0) & (gain > 0)), 100.0)


def m15_rsi(close: pd.Series) -> pd.Series:
    """RSI على شموع M15 المكتملة فقط، ومتاح عند شمعة M5 الأخيرة من الشمعة (بلا نظر للمستقبل)."""
    c15 = close.resample("15min", label="left", closed="left").last().dropna()
    r = rsi(c15)
    r.index = r.index + pd.Timedelta(minutes=10)      # تُعرف قيمتها عند إغلاق آخر M5 داخلها
    return r.reindex(close.index, method="ffill", limit=3)


def features(df: pd.DataFrame) -> dict:
    c, o, h, l = df["Close"], df["Open"], df["High"], df["Low"]
    m = c.rolling(20).mean()
    sd = c.rolling(20).std()
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    return {
        "c": c, "o": o, "h": h, "l": l,
        "rsi": rsi(c), "rsi7": rsi(c, 7), "rsi15": m15_rsi(c),
        "z": (c - m) / sd.where(sd != 0),
        "ema20": c.ewm(span=20, adjust=False).mean(),
        "ema50": c.ewm(span=50, adjust=False).mean(),
        "ema200": c.ewm(span=200, adjust=False).mean(),
        "bull": c > o, "bear": c < o,
        "lower_wick": (pd.concat([o, c], axis=1).min(axis=1) - l) / rng,
        "upper_wick": (h - pd.concat([o, c], axis=1).max(axis=1)) / rng,
        "body_ratio": body / rng,
        "hour": pd.Series(c.index.hour, index=c.index),
    }


def sig(call, put) -> pd.Series:
    return call.fillna(False).astype(int) - put.fillna(False).astype(int)


def in_hours(f, a=7, b=17):
    return (f["hour"] >= a) & (f["hour"] < b)


def engulf_up(f):
    pb = f["bear"].shift(1, fill_value=False)
    return f["bull"] & pb & (f["c"] > f["o"].shift(1)) & (f["o"] <= f["c"].shift(1))


def engulf_dn(f):
    pb = f["bull"].shift(1, fill_value=False)
    return f["bear"] & pb & (f["c"] < f["o"].shift(1)) & (f["o"] >= f["c"].shift(1))


STRATEGIES = {
    "A  الحالية: RSI25/75 + بولنجر2.5": lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.5), (f["rsi"] > 75) & (f["z"] > 2.5)),
    "B  RSI25/75 + بولنجر2.0":          lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.0), (f["rsi"] > 75) & (f["z"] > 2.0)),
    "C  RSI30/70 + بولنجر2.5":          lambda f: sig((f["rsi"] < 30) & (f["z"] < -2.5), (f["rsi"] > 70) & (f["z"] > 2.5)),
    "D  RSI20/80 + بولنجر2.0":          lambda f: sig((f["rsi"] < 20) & (f["z"] < -2.0), (f["rsi"] > 80) & (f["z"] > 2.0)),
    "E  RSI(7) 15/85 + بولنجر2.0":      lambda f: sig((f["rsi7"] < 15) & (f["z"] < -2.0), (f["rsi7"] > 85) & (f["z"] > 2.0)),
    "F  A + RSI-M15 متطرف (30/70)":     lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.5) & (f["rsi15"] < 30), (f["rsi"] > 75) & (f["z"] > 2.5) & (f["rsi15"] > 70)),
    "G  B + RSI-M15 متطرف (35/65)":     lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.0) & (f["rsi15"] < 35), (f["rsi"] > 75) & (f["z"] > 2.0) & (f["rsi15"] > 65)),
    "H  ابتلاع عند بولنجر 2.0":         lambda f: sig(engulf_up(f) & (f["z"].shift(1) < -2.0), engulf_dn(f) & (f["z"].shift(1) > 2.0)),
    "I  ابتلاع + RSI<30/>70":           lambda f: sig(engulf_up(f) & (f["rsi"].shift(1) < 30), engulf_dn(f) & (f["rsi"].shift(1) > 70)),
    "J  ذيل طويل (pin) عند بولنجر 2.0": lambda f: sig((f["lower_wick"] > 0.6) & (f["body_ratio"] < 0.3) & (f["z"] < -2.0), (f["upper_wick"] > 0.6) & (f["body_ratio"] < 0.3) & (f["z"] > 2.0)),
    "K  ارتداد EMA20 مع اتجاه EMA50>200": lambda f: sig((f["ema50"] > f["ema200"]) & (f["rsi"] < 35) & (f["c"] < f["ema20"]), (f["ema50"] < f["ema200"]) & (f["rsi"] > 65) & (f["c"] > f["ema20"])),
    "L  A في ساعات لندن/نيويورك":       lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.5) & in_hours(f), (f["rsi"] > 75) & (f["z"] > 2.5) & in_hours(f)),
    "M  B في ساعات لندن/نيويورك":       lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.0) & in_hours(f), (f["rsi"] > 75) & (f["z"] > 2.0) & in_hours(f)),
    "N  B ضد الاتجاه الكبير (EMA200)":   lambda f: sig((f["rsi"] < 25) & (f["z"] < -2.0) & (f["c"] > f["ema200"]), (f["rsi"] > 75) & (f["z"] > 2.0) & (f["c"] < f["ema200"])),
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
            df = loader(p)
        except Exception as exc:
            print(f"! {p}: {exc}")
            df = None
        if df is not None and len(df) > 500:
            data[p] = df
    if not data:
        print("لا توجد بيانات")
        return

    feats = {p: features(df) for p, df in data.items()}
    start = min(df.index.min() for df in data.values())
    end = max(df.index.max() for df in data.values())
    cutoff = start + (end - start) * 2 / 3
    days = len({d for df in data.values() for d in df.index.date})
    print(f"الأزواج المحمّلة: {len(data)} من {len(pairs)} | الأيام: {days} | نقطة التعادل (عائد {PAYOUT:.0%}): {BREAK_EVEN:.1f}%")
    print(f"{'الاستراتيجية':<38}{'إشارات':>7}{'نسبة':>7}{'حد99':>7}{'اختبار':>8}{'n_اختبار':>9}{'يومياً':>8}  الحكم")

    per_pair_A = {}
    passed = []
    for name, fn in STRATEGIES.items():
        wins = []
        for p, f in feats.items():
            w = outcomes(f["c"], fn(f))
            wins.append(w)
            if name.startswith("A "):
                per_pair_A[p] = (int(w.sum()), int(len(w)))
        w = pd.concat(wins) if wins else pd.Series(dtype=bool)
        n = int(len(w))
        if n == 0:
            print(f"{name:<38}{0:>7}")
            continue
        rate = w.mean() * 100
        low99 = wilson_low(int(w.sum()), n, Z99)
        test = w[w.index >= cutoff]
        test_rate = test.mean() * 100 if len(test) else float("nan")
        per_day = n / days
        ok = n >= MIN_N and low99 > BREAK_EVEN and len(test) >= MIN_TEST_N and test_rate > BREAK_EVEN
        verdict = "✅ ناجحة" if ok else ("عينة صغيرة" if n < MIN_N else "بلا أفضلية مؤكدة")
        if ok:
            passed.append((name, n, rate, per_day))
        print(f"{name:<38}{n:>7}{rate:>6.1f}%{low99:>6.1f}%{test_rate:>7.1f}%{len(test):>9}{per_day:>8.1f}  {verdict}")

    print("\n--- الاستراتيجية A لكل زوج (للاطلاع فقط، العينات صغيرة) ---")
    for p, (w, n) in sorted(per_pair_A.items(), key=lambda kv: -kv[1][1]):
        print(f"{p:<8} إشارات={n:<4} نسبة={(w / n * 100 if n else 0):5.1f}%")

    print("\n=== الناجحة بشروط 99% ===")
    for name, n, rate, per_day in passed:
        print(f"{name}: {n} إشارة، {rate:.1f}%، {per_day:.1f}/يوم")
    if not passed:
        print("لا توجد استراتيجية نجحت بالشروط المشددة.")


if __name__ == "__main__":
    run()
