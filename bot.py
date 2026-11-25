import logging
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
import requests

# ================= إعدادات القناة الأساسية =================
BOT_TOKEN = "9132434770:AAGulVo-sWXI6Mm_59yVQPXXraoAn-nOTRs"
CHANNEL_ID = -1003938306672
AFFILIATE_LINK = "https://broker-qx.pro/sign-up/?lid=2438595"
WEBHOOK_SECRET = "alpha_qx_2026"

app = Flask(__name__)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def send_telegram_message(text: str, reply_to_message_id: int | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


@app.route("/", methods=["GET"])
def home():
    return "Alpha Quotex Webhook Server is Active!", 200


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "No JSON received"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    msg_type = str(data.get("type", "signal")).strip().lower()

    # دعم إرسال نتيجة الصفقة كردّ بعد انتهاء دورة الصفقة ومحاولات التعويض
    if msg_type == "result":
        result_text = str(data.get("result", "")).strip()
        reply_to = data.get("reply_to_message_id")
        if not result_text:
            return jsonify({"status": "error", "message": "Missing result text"}), 400

        reply_id = int(reply_to) if str(reply_to).isdigit() else None
        tg_resp = send_telegram_message(result_text, reply_to_message_id=reply_id)
        logger.info("تم إرسال نتيجة الصفقة: %s", result_text)
        return jsonify({"status": "result_sent", "telegram": tg_resp}), 200

    pair = str(data.get("pair", "EURUSD")).strip().upper()
    direction = str(data.get("direction", "call")).strip().lower()
    timeframe = str(data.get("timeframe", "M5")).strip().upper()
    forecast_raw = data.get("forecast", "75.00")
    price = str(data.get("price", "")).strip()

    try:
        forecast_val = float(forecast_raw)
        forecast_formatted = f"{forecast_val:.2f}"
    except (ValueError, TypeError):
        forecast_formatted = str(forecast_raw)

    if direction in ["buy", "call", "up", "long"]:
        arrow = "🔼"
        dir_label = "call"
        tend = "Buy"
    else:
        arrow = "🔽"
        dir_label = "put"
        tend = "Sell"

    # احتساب توقيت تنفيذ الصفقة بدقة من الساعة الحالية
    exec_time = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M:%S")

    post_text = (
        f"💳 {pair}\n"
        f"🔥 {timeframe}\n"
        f"⏳ {exec_time}\n"
        f"{arrow} {dir_label}\n\n"
        f"🚦 Tend: {tend}\n"
        f"📈 Forecast: {forecast_formatted}%\n"
        f"🎯 Entry: {price}\n\n"
        f"🔗 REGISTER HERE: {AFFILIATE_LINK}"
    )

    try:
        tg_resp = send_telegram_message(post_text)
        sent_msg_id = tg_resp.get("result", {}).get("message_id")
        logger.info("تم نشر إشارة TradingView: %s (%s)", pair, dir_label)
        return jsonify(
            {
                "status": "sent",
                "message_id": sent_msg_id,
                "telegram": tg_resp,
            }
        ), 200
    except Exception as e:
        logger.error("فشل إرسال الرسالة إلى تيليغرام: %s", e)
        return jsonify({"status": "error", "details": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)