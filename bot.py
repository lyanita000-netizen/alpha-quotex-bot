import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = "-1003938306672"
WEBHOOK_SECRET = "alpha_qx_2026"
REF_LINK = "https://broker-qx.pro/sign-up/?lid=2438595"


def send_telegram_message(text: str, reply_to_message_id: int | None = None) -> int | None:
    if not BOT_TOKEN:
        print("BOT_TOKEN غير مضبوط")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = int(reply_to_message_id)

    r = requests.post(url, json=payload, timeout=15)
    data = r.json()
    if data.get("ok"):
        return data["result"]["message_id"]
    print("خطأ Telegram:", data)
    return None


@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Alpha Quotex Webhook Server is Active!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if data.get("type") == "result":
        result_text = data.get("result", "")
        reply_id = data.get("reply_to_message_id")
        msg_id = send_telegram_message(f"<b>{result_text}</b>", reply_to_message_id=reply_id)
        return jsonify({"ok": msg_id is not None, "message_id": msg_id}), 200

    pair = data.get("pair", "EURUSD")
    direction = data.get("direction", "call").lower()
    timeframe = data.get("timeframe", "M1")
    forecast = data.get("forecast", "85.00")
    price = data.get("price", "0.00000")
    trend = "Buy" if direction == "call" else "Sell"

    msg = (
        "<b>Alpha Quotex Trading | تحليلات وصفقات</b>\n"
        f"💶 <b>{pair}</b>\n"
        f"🔥 <b>{timeframe}</b>\n"
        f"🔼 <b>{direction}</b>\n\n"
        f"🚦 Tend: <b>{trend}</b>\n"
        f"📈 Forecast: <b>{forecast}%</b>\n"
        f"🎯 Entry: <b>{price}</b>\n\n"
        f"🔗 REGISTER HERE: {REF_LINK}"
    )

    msg_id = send_telegram_message(msg)
    if msg_id is None:
        return jsonify({"ok": False, "error": "telegram_send_failed"}), 500

    print(f"تم نشر إشارة: {pair} ({direction}) | msg_id={msg_id}")
    return jsonify({"ok": True, "message_id": msg_id}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
