import os
import requests
from fastapi import FastAPI, Request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_VERIFY_SID = os.getenv("TWILIO_VERIFY_SID")
IMEI_API_KEY = os.getenv("IMEI_API_KEY")

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)
dispatcher = Dispatcher(bot, None, workers=0)

twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

verified_users = set()
pending_otp = {}

# --- Commands ---
def start(update: Update, context):
    update.message.reply_text(
        "👋 Welcome! To use this bot, please verify your phone.\nSend /verify to begin."
    )

def verify(update: Update, context):
    update.message.reply_text("📱 Send your phone number with country code (e.g. +14155552671):")
    context.user_data["awaiting_phone"] = True

def handle_message(update: Update, context):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    # Step 1: phone number input
    if context.user_data.get("awaiting_phone"):
        phone = text
        try:
            twilio_client.verify.v2.services(TWILIO_VERIFY_SID).verifications.create(to=phone, channel="sms")
            pending_otp[user_id] = phone
            context.user_data["awaiting_phone"] = False
            context.user_data["awaiting_otp"] = True
            update.message.reply_text("✅ OTP sent! Please reply with the code you received.")
        except Exception as e:
            update.message.reply_text(f"⚠️ Error sending OTP: {e}")
        return

    # Step 2: OTP input
    if context.user_data.get("awaiting_otp"):
        code = text
        phone = pending_otp.get(user_id)
        try:
            result = twilio_client.verify.v2.services(TWILIO_VERIFY_SID).verification_checks.create(to=phone, code=code)
            if result.status == "approved":
                verified_users.add(user_id)
                context.user_data["awaiting_otp"] = False
                update.message.reply_text("🎉 Verification successful! Now send me an IMEI to check.")
            else:
                update.message.reply_text("❌ Incorrect code. Try again.")
        except Exception as e:
            update.message.reply_text(f"⚠️ Verification error: {e}")
        return

    # Step 3: IMEI check
    if user_id not in verified_users:
        update.message.reply_text("🔒 Please verify first using /verify.")
        return

    imei = text
    if len(imei) < 10 or not imei.isdigit():
        update.message.reply_text("❗ Please send a valid IMEI number.")
        return

    data = check_imei(imei)
    if data:
        reply = format_imei_info(data)
        update.message.reply_text(reply, parse_mode="Markdown")
    else:
        update.message.reply_text("⚠️ Unable to fetch IMEI info. Try again later.")

def check_imei(imei: str):
    url = f"https://api.imeicheck.com/imei-tac-database-info?imei={imei}&apikey={IMEI_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except Exception as e:
        print("Error fetching IMEI:", e)
        return None

def format_imei_info(data: dict):
    brand = data.get("brand", "Unknown")
    model = data.get("model", "Unknown")
    tac = data.get("tac", "Unknown")
    return (
        f"📱 *IMEI Information*\n"
        f"Brand: {brand}\n"
        f"Model: {model}\n"
        f"TAC: {tac}\n"
        f"✅ Data from ImeiCheck Free API"
    )

# --- Telegram Handlers ---
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("verify", verify))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return {"ok": True}

@app.get("/")
def home():
    return {"status": "Bot running!"}