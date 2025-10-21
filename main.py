#!/usr/bin/env python3
"""
Werleed Assistant - Full production-ready bot (Part 1 of 3)

Features:
- Twilio SMS primary OTP, Telegram fallback OTP
- Phone normalization (+234/+233) and prompts
- Local AI-like conversational replies, tone & mood
- IMEI lookup via optional IMEI_API_KEY or web-scraping fallback
- Daily briefing (weather + exchange rates)
- Offline queue for OTPs and unsent messages with retry
- Admin tools: /users, /stats, /broadcast, /logs, /maintenance
- User features: mood detection, daily planner, feedback collection, mini-games placeholder
- Persistence via SQLite + JSON backup
- Keepalive server for Replit + optional pinger
"""

import os
import re
import time
import json
import random
import logging
import sqlite3
import threading
import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List

# NLP & translation
from deep_translator import GoogleTranslator
from langdetect import detect, LangDetectException
from bs4 import BeautifulSoup

# dotenv
from dotenv import load_dotenv

# telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Twilio
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except Exception:
    TwilioClient = None
    TWILIO_AVAILABLE = False

# FastAPI for keepalive
try:
    from fastapi import FastAPI
    import uvicorn
    FASTAPI_AVAILABLE = True
except Exception:
    FASTAPI_AVAILABLE = False

# Optional OpenAI (disabled by default)
try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_VERIFY_SID = os.getenv("TWILIO_VERIFY_SID")
IMEI_API_KEY = os.getenv("IMEI_API_KEY") or ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
ADMIN_ID = int(os.getenv("ADMIN_ID") or 7003416998)
DAILY_BRIEF_HOUR = int(os.getenv("DAILY_BRIEF_HOUR") or 8)
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL") or ""
BOT_NAME = os.getenv("BOT_NAME") or "Werleed Assistant"
BACKUP_JSON = "users_backup.json"
PORT = int(os.getenv("PORT") or os.getenv("REPL_PORT") or 3000)

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN missing in .env")

if OPENAI_AVAILABLE and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# Twilio client init
twilio_client = None
if TWILIO_AVAILABLE and TWILIO_SID and TWILIO_AUTH:
    try:
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH)
    except Exception:
        twilio_client = None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("werleed_assistant")

# SQLite DB init
DB_FILE = "werleed_assistant_full.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# Create tables
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    tg_name TEXT,
    phone TEXT,
    verified_at INTEGER,
    lang TEXT DEFAULT 'en',
    tone TEXT DEFAULT 'friendly',
    mood TEXT DEFAULT '',
    last_seen INTEGER DEFAULT 0,
    last_lang_prompt INTEGER DEFAULT 0
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    ts INTEGER
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS otps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone TEXT,
    code TEXT,
    ts INTEGER,
    sent INTEGER DEFAULT 0
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS unsent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    ts INTEGER,
    tries INTEGER DEFAULT 0
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS planner (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    note TEXT,
    remind_at INTEGER
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    message TEXT,
    rating INTEGER,
    ts INTEGER
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    message TEXT,
    ts INTEGER
)""")
conn.commit()

# backup/restore
def backup_users_to_json():
    try:
        cur.execute("SELECT user_id,tg_name,phone,verified_at,lang,tone,mood,last_seen FROM users")
        rows = cur.fetchall()
        arr = []
        for r in rows:
            arr.append({
                "user_id": r[0], "tg_name": r[1], "phone": r[2], "verified_at": r[3],
                "lang": r[4], "tone": r[5], "mood": r[6], "last_seen": r[7]
            })
        with open(BACKUP_JSON, "w", encoding="utf-8") as f:
            json.dump(arr, f, indent=2)
        logger.info("Backed up %d users", len(arr))
    except Exception:
        logger.exception("backup failed")

def restore_users_from_json():
    if not os.path.exists(BACKUP_JSON):
        return
    try:
        with open(BACKUP_JSON, "r", encoding="utf-8") as f:
            arr = json.load(f)
        for u in arr:
            cur.execute("""INSERT INTO users(user_id,tg_name,phone,verified_at,lang,tone,mood,last_seen)
                           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                           tg_name=excluded.tg_name, phone=excluded.phone, verified_at=excluded.verified_at, lang=excluded.lang, tone=excluded.tone, mood=excluded.mood, last_seen=excluded.last_seen""",
                        (u.get("user_id"), u.get("tg_name"), u.get("phone"), u.get("verified_at"), u.get("lang","en"), u.get("tone","friendly"), u.get("mood",""), u.get("last_seen",0)))
        conn.commit()
        logger.info("Restored %d users from backup", len(arr))
    except Exception:
        logger.exception("restore failed")

restore_users_from_json()

# DB helpers
def save_user(user_id:int, tg_name:str):
    cur.execute("INSERT INTO users(user_id,tg_name,last_seen) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET tg_name=excluded.tg_name, last_seen=excluded.last_seen",
                (user_id, tg_name, int(time.time())))
    conn.commit()

def get_user(user_id:int) -> dict:
    cur.execute("SELECT user_id,tg_name,phone,verified_at,lang,tone,mood,last_seen,last_lang_prompt FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"user_id":r[0],"tg_name":r[1],"phone":r[2],"verified_at":r[3],"lang":r[4],"tone":r[5],"mood":r[6],"last_seen":r[7],"last_lang_prompt": r[8]}

def set_last_seen(user_id:int):
    cur.execute("UPDATE users SET last_seen=? WHERE user_id=?", (int(time.time()), user_id))
    conn.commit()

def mark_verified(user_id:int, phone:str):
    now = int(time.time())
    cur.execute("INSERT INTO users(user_id,tg_name,phone,verified_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET phone=excluded.phone, verified_at=excluded.verified_at",
                (user_id, get_user(user_id).get("tg_name",""), phone, now))
    conn.commit()
    backup_users_to_json()
    log("INFO", f"User {user_id} verified with {phone}")

def set_user_lang(user_id:int, lang_code:str):
    cur.execute("UPDATE users SET lang=?, last_lang_prompt=? WHERE user_id=?", (lang_code, int(time.time()), user_id))
    conn.commit()
    backup_users_to_json()

def set_user_tone(user_id:int, tone:str):
    cur.execute("UPDATE users SET tone=? WHERE user_id=?", (tone, user_id))
    conn.commit()
    backup_users_to_json()

def set_user_mood(user_id:int, mood:str):
    cur.execute("UPDATE users SET mood=? WHERE user_id=?", (mood, user_id))
    conn.commit()
    backup_users_to_json()

def store_message(user_id:int, text:str):
    cur.execute("INSERT INTO messages(user_id,text,ts) VALUES(?,?,?)", (user_id, text, int(time.time())))
    conn.commit()

def get_message_count(user_id:int)->int:
    cur.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else 0

def clear_messages(user_id:int):
    cur.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
    conn.commit()

def log(level:str, message:str):
    try:
        cur.execute("INSERT INTO logs(level,message,ts) VALUES(?,?,?)", (level, message, int(time.time())))
        conn.commit()
    except Exception:
        logger.exception("log failed")

def get_logs(limit=50)->List[Tuple[int,str,str,int]]:
    cur.execute("SELECT id,level,message,ts FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    return cur.fetchall()

# helpers: phone normalization and Twilio OTP
DIAL_BY_COUNTRY = {"NG":"+234", "GH":"+233"}

def normalize_phone(raw:str, hint_country:Optional[str]=None) -> Tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, "Empty phone."
    n = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if n.lower().startswith("sms:") or n.lower().startswith("whatsapp:"):
        n = n.split(":",1)[1]
    if n.startswith("+") and n[1:].isdigit():
        return n, None
    digits = "".join(ch for ch in n if ch.isdigit())
    if len(digits) < 6:
        return None, "Number too short. Send full number with country code, e.g., +2348012345678"
    if hint_country and hint_country.upper() in DIAL_BY_COUNTRY:
        code = DIAL_BY_COUNTRY[hint_country.upper()]
        if digits.startswith("0"):
            digits = digits[1:]
        return code + digits, None
    try:
        j = requests.get("https://ipapi.co/json/", timeout=5).json()
        cc = j.get("country")
        if cc and cc in DIAL_BY_COUNTRY:
            code = DIAL_BY_COUNTRY[cc]
            if digits.startswith("0"):
                digits = digits[1:]
            return code + digits, None
    except Exception:
        pass
    if digits.startswith("234") or digits.startswith("233"):
        return "+" + digits, None
    if len(digits) in (10,11) and digits.startswith("0"):
        return "+234" + digits[1:], None
    return None, "Ambiguous number. Please send international format starting with + (e.g., +2348012345678)."# Part 2 of 3 — AI, IMEI, OTP, queues, keepalive

def twilio_send_sms_verification(phone_e164:str) -> Tuple[bool,str]:
    if not twilio_client or not TWILIO_VERIFY_SID:
        return False, "Twilio not configured."
    try:
        twilio_client.verify.services(TWILIO_VERIFY_SID).verifications.create(to=phone_e164, channel="sms")
        return True, f"OTP sent to {phone_e164} via SMS."
    except Exception as e:
        logger.exception("Twilio SMS error: %s", e)
        log("ERROR", f"Twilio SMS error: {e}")
        return False, f"Twilio SMS error: {e}"

def send_telegram_backup_otp(app, user_id:int, code:str):
    try:
        app.bot.send_message(chat_id=user_id, text=f"🔐 Backup OTP (Telegram): {code}\nReply with /code <otp>")
        return True
    except Exception:
        logger.exception("Telegram backup OTP failed")
        log("ERROR", "Telegram backup OTP failed")
        return False

# OTP queue functions
def queue_otp(user_id:int, phone_e164:str, code:str):
    cur.execute("INSERT INTO otps(user_id,phone,code,ts,sent) VALUES(?,?,?,?,0)", (user_id, phone_e164, code, int(time.time())))
    conn.commit()

def mark_otp_sent(otp_id:int):
    cur.execute("UPDATE otps SET sent=1 WHERE id=?", (otp_id,))
    conn.commit()

def find_pending_otp_by_user(user_id:int):
    cur.execute("SELECT id,user_id,phone,code,ts,sent FROM otps WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    return cur.fetchone()

# Planner & feedback
def add_planner_note(user_id:int, note:str, remind_at_ts:int):
    cur.execute("INSERT INTO planner(user_id,note,remind_at) VALUES(?,?,?)", (user_id, note, remind_at_ts))
    conn.commit()

def get_due_plans(now_ts:int)->List[Tuple[int,int,str,int]]:
    cur.execute("SELECT id,user_id,note,remind_at FROM planner WHERE remind_at <= ?", (now_ts,))
    return cur.fetchall()

def add_feedback(user_id:int, message:str, rating:int):
    cur.execute("INSERT INTO feedback(user_id,message,rating,ts) VALUES(?,?,?,?)", (user_id, message, rating, int(time.time())))
    conn.commit()

# AI heuristics & translation
def detect_lang_safe(text:str)->str:
    try:
        return detect(text)
    except LangDetectException:
        return "en"
    except Exception:
        return "en"

def to_en(text:str)->Tuple[str,str]:
    lang = detect_lang_safe(text)
    if lang == "en":
        return text, "en"
    try:
        t = GoogleTranslator(source='auto', target='en').translate(text)
        return t, lang
    except Exception:
        return text, lang

def from_en(text_en:str, dest_lang:str)->str:
    if not dest_lang or dest_lang == "en":
        return text_en
    try:
        return GoogleTranslator(source='auto', target=dest_lang).translate(text_en)
    except Exception:
        return text_en

def mood_from_text(text:str)->str:
    t = text.lower()
    if any(w in t for w in ["happy","great","good","awesome","😊","😄","😁","love","yay"]):
        return "happy"
    if any(w in t for w in ["sad","unhappy","down","depressed","😢","😞","😭","miss"]):
        return "sad"
    if any(w in t for w in ["angry","mad","😠","😡"]):
        return "angry"
    if any(w in t for w in ["bored"," bored ","tired","😴"]):
        return "tired"
    return "neutral"

def local_ai_reply(user_id:int, text:str)->str:
    en_text, lang = to_en(text)
    t = en_text.lower()
    u = get_user(user_id)
    tone = (u.get("tone") if u else "friendly") or "friendly"
    mood = mood_from_text(text)
    # greetings
    if any(g in t for g in ["hi","hello","hey","good morning","good afternoon","good evening"]):
        if tone == "formal":
            return from_en(f"Hello {u.get('tg_name','friend')}. I am {BOT_NAME}. How may I assist you?", lang)
        else:
            return from_en(f"Hi {u.get('tg_name','friend')}! I'm {BOT_NAME}. How can I help?", lang)
    # IMEI
    if "imei" in t or (t.isdigit() and len("".join(ch for ch in t if ch.isdigit())) >= 14):
        imei = "".join(ch for ch in t if ch.isdigit())
        return from_en(f"IMEI lookup:\n{scrape_imei(imei)}", lang)
    # rates
    if "rate" in t or "exchange" in t:
        loc = get_location_data()
        curc = loc.get("currency") or "NGN"
        return from_en(fetch_rate("USD", curc), lang)
    # planner add
    if t.startswith("remind me") or t.startswith("remind"):
        return from_en("Use the planner feature: send /plan <YYYY-MM-DD HH:MM> <note>", lang)
    # mood response
    if mood == "happy":
        return from_en("That's awesome! 😊 Tell me more or ask me something.", lang)
    if mood == "sad":
        return from_en("I'm sorry you're feeling down. If you want, tell me what's wrong or type 'help'.", lang)
    # wiki-like
    if any(t.startswith(k) for k in ["who","what","when","where","tell me about","define"]):
        try:
            wiki = requests.get("https://en.wikipedia.org/w/api.php", params={"format":"json","action":"query","prop":"extracts","exintro":"","explaintext":"","titles":en_text}, timeout=6).json()
            pages = wiki.get("query",{}).get("pages",{})
            for v in pages.values():
                if v.get("extract"):
                    return from_en(v["extract"][:900], lang)
        except Exception:
            pass
        return from_en("I couldn't find a concise summary. Try rephrasing.", lang)
    # fallback
    return from_en(random.choice([
        "Interesting — tell me more!",
        "I can help with IMEI checks, weather, exchange rates, or quick research. What would you like?",
        "Would you like me to search the web for this?"
    ]), lang)

def openai_reply(text:str, user_id:int)->Optional[str]:
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    try:
        if hasattr(openai, "ChatCompletion"):
            resp = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=[{"role":"user","content":text}], max_tokens=300)
            return resp.choices[0].message.content.strip()
        else:
            resp = openai.Completion.create(engine="text-davinci-003", prompt=text, max_tokens=300)
            return resp.choices[0].text.strip()
    except Exception:
        logger.exception("OpenAI error")
        return None

def choose_reply(user_id:int, text:str)->str:
    online = openai_reply(text, user_id) if (OPENAI_AVAILABLE and OPENAI_API_KEY) else None
    if online:
        return online
    return local_ai_reply(user_id, text)

# IMEI helpers
def scrape_imei(imei:str)->str:
    if IMEI_API_KEY:
        try:
            r = requests.get(f"https://api.imei.io/imei/{imei}?key={IMEI_API_KEY}", timeout=8)
            if r.status_code == 200:
                return json.dumps(r.json(), indent=2)
        except Exception:
            pass
    headers = {"User-Agent":"Mozilla/5.0"}
    sources = [f"https://www.imei.info/?imei={imei}", f"https://imei24.com/check/{imei}", f"https://www.google.com/search?q=IMEI+{imei}+specs"]
    for url in sources:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            meta = soup.find("meta", {"name":"description"})
            if meta and meta.get("content"):
                return meta.get("content")
            p = soup.find("p")
            if p and p.text.strip():
                return p.text.strip()
            title = soup.find("title")
            if title and title.text.strip():
                return title.text.strip()
        except Exception:
            continue
    return "No public IMEI data found"

# location, weather, rates
def get_location_data()->dict:
    try:
        j = requests.get("https://ipapi.co/json/", timeout=5).json()
        return {"city": j.get("city"), "region": j.get("region"), "country": j.get("country_name"), "country_code": j.get("country"), "currency": j.get("currency")}
    except Exception:
        return {"city":None,"region":None,"country":None,"country_code":None,"currency":None}

def get_weather_by_city(city):
    try:
        if not city:
            city = "your area"
        r = requests.get(f"https://wttr.in/{city}?format=3", timeout=6)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return "Weather unavailable"

def fetch_rate(base="USD", target="NGN"):
    try:
        j = requests.get(f"https://api.exchangerate.host/convert?from={base}&to={target}", timeout=6).json()
        rate = j.get("info", {}).get("rate")
        if rate:
            return f"1 {base} = {rate:.4f} {target}"
        return "Rate unavailable"
    except Exception:
        return "Rate error"

# offline retry threads
def retry_queues(app):
    while True:
        try:
            cur.execute("SELECT id,user_id,phone,code,ts,sent FROM otps WHERE sent=0")
            rows = cur.fetchall()
            for r in rows:
                otp_id, uid, phone, code, ts, sent = r
                ok, msg = twilio_send_sms_verification(phone)
                if ok:
                    mark_otp_sent(otp_id)
                else:
                    send_telegram_backup_otp(app, uid, code)
                    mark_otp_sent(otp_id)
                time.sleep(1)
            # unsent messages
            cur.execute("SELECT id,user_id,text,ts,tries FROM unsent_messages WHERE tries < 5")
            rows2 = cur.fetchall()
            for r in rows2:
                mid, uid, text, ts, tries = r
                try:
                    app.bot.send_message(chat_id=uid, text=text)
                    cur.execute("DELETE FROM unsent_messages WHERE id=?", (mid,))
                    conn.commit()
                except Exception:
                    cur.execute("UPDATE unsent_messages SET tries=tries+1 WHERE id=?", (mid,))
                    conn.commit()
                time.sleep(0.5)
            # planner reminders
            now_ts = int(time.time())
            due = get_due_plans(now_ts)
            for plan in due:
                pid, uid, note, remind_at = plan
                try:
                    app.bot.send_message(chat_id=uid, text=f"⏰ Reminder: {note}")
                    cur.execute("DELETE FROM planner WHERE id=?", (pid,))
                    conn.commit()
                except Exception:
                    logger.exception("Failed to send planner reminder")
        except Exception:
            logger.exception("retry_queues error")
        time.sleep(60)

# keepalive server + pinger
def start_keepalive_server():
    if not FASTAPI_AVAILABLE:
        logger.info("FastAPI not installed - skipping keepalive server")
        return
    app = FastAPI()
    @app.get("/")
    def root():
        return {"status":"ok","bot":BOT_NAME}
    def run_uvicorn():
        try:
            uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
        except Exception:
            logger.exception("uvicorn failed")
    t = threading.Thread(target=run_uvicorn, daemon=True)
    t.start()
    logger.info("Keepalive server started")

def start_keepalive_pinger():
    if not KEEPALIVE_URL:
        logger.info("KEEPALIVE_URL not set; skipping pinger")
        return
    def pinger():
        while True:
            try:
                requests.get(KEEPALIVE_URL, timeout=10)
            except Exception:
                logger.exception("Keepalive ping failed")
            time.sleep(9*60)
    t = threading.Thread(target=pinger, daemon=True)
    t.start()
    logger.info("Keepalive pinger started")# Part 3 of 3 — handlers, admin, main

# Telegram handlers
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    save_user(uid, user.first_name or user.username or "friend")
    set_last_seen(uid)
    await update.message.reply_text(f"👋 Hello {user.first_name or 'friend'}! I'm {BOT_NAME}.")
    u = get_user(uid)
    if not u or not u.get("lang"):
        kb = [
            [InlineKeyboardButton("Friendly", callback_data="tone_friendly"), InlineKeyboardButton("Formal", callback_data="tone_formal")],
            [InlineKeyboardButton("English (en)", callback_data="lang_en"), InlineKeyboardButton("Français (fr)", callback_data="lang_fr")]
        ]
        await update.message.reply_text("Which tone & language would you prefer?", reply_markup=InlineKeyboardMarkup(kb))
        return
    if not is_verified(uid):
        await update.message.reply_text("🔐 Please verify your phone using /verify +countryphone (valid 7 days).")
    await show_menu(update)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data; uid = q.from_user.id
    if data.startswith("lang_"):
        code = data.split("_",1)[1]; set_user_lang(uid, code); await q.message.reply_text(f"✅ Language set to {code}"); return
    if data.startswith("tone_"):
        code = data.split("_",1)[1]; set_user_tone(uid, code); await q.message.reply_text(f"✅ Tone set to {code}"); return
    if data == "VERIFY":
        await q.message.reply_text("Send your phone number with country code (e.g., +2348012345678) or use /verify.")
        context.user_data['flow'] = 'PHONE'; return
    if data == "MENU_IMEI":
        if not is_verified(uid): await q.message.reply_text("🔒 Verify first with /verify."); return
        context.user_data['flow'] = 'IMEI'; await q.message.reply_text("Send IMEI now:"); return
    if data == "MENU_RATE":
        loc = get_location_data(); curc = loc.get("currency") or "NGN"; await q.message.reply_text(fetch_rate("USD", curc)); return
    if data == "MENU_BRIEF":
        loc = get_location_data(); await q.message.reply_text(f"☀️ Briefing:\n{get_weather_by_city(loc.get('city'))}\n{fetch_rate('USD','NGN')}"); return
    if data == "MENU_HELP":
        await q.message.reply_text("I can check IMEI, give exchange rates, weather, and chat. Use /stats (admin).")

async def show_menu(update_obj):
    kb = [
        [InlineKeyboardButton("🔐 Verify", callback_data="VERIFY")],
        [InlineKeyboardButton("📱 IMEI", callback_data="MENU_IMEI"), InlineKeyboardButton("💱 Rate", callback_data="MENU_RATE")],
        [InlineKeyboardButton("🌤 Briefing", callback_data="MENU_BRIEF"), InlineKeyboardButton("ℹ️ Help", callback_data="MENU_HELP")]
    ]
    try:
        await update_obj.message.reply_text("Choose an option:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass

# verify & code handlers
async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args; uid = update.effective_user.id
    if not args:
        context.user_data['flow'] = 'PHONE'
        await update.message.reply_text("Send phone number with country code (e.g., +2348012345678) or use /verify +number now.")
        return
    phone_raw = args[0].strip()
    phone, msg = normalize_phone(phone_raw, hint_country=None)
    if not phone:
        await update.message.reply_text(f"❌ {msg}"); return
    code = "%06d" % random.randint(0, 999999)
    queue_otp(uid, phone, code)
    ok, tw_msg = twilio_send_sms_verification(phone)
    pending = find_pending_otp_by_user(uid)
    if ok:
        if pending: mark_otp_sent(pending[0])
        await update.message.reply_text(tw_msg + " If you don't receive it, I will send a Telegram backup.")
    else:
        send_telegram_backup_otp(context.application, uid, code)
        if pending: mark_otp_sent(pending[0])
        await update.message.reply_text("⚠️ SMS failed — backup OTP sent via Telegram. Use /code <otp> to verify.")

async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; args = context.args
    if not args:
        await update.message.reply_text("Usage: /code <123456>"); return
    code = args[0].strip()
    pending = find_pending_otp_by_user(uid)
    if not pending:
        await update.message.reply_text("No pending OTP found. Use /verify first."); return
    otp_id, _, phone, expected_code, ts, sent = pending
    # Twilio verification
    if twilio_client and TWILIO_VERIFY_SID:
        try:
            res = twilio_client.verify.services(TWILIO_VERIFY_SID).verification_checks.create(to=phone, code=code)
            status = getattr(res, "status", None)
            if status == "approved":
                mark_verified(uid, phone); await update.message.reply_text("🎉 Verified successfully. You can now use all features for 7 days."); return
            else:
                await update.message.reply_text("❌ Code incorrect or not approved. Please try again."); return
        except Exception:
            logger.exception("Twilio verification check failed; falling back to local code")
    # local fallback
    if expected_code == code:
        mark_verified(uid, phone); await update.message.reply_text("🎉 Verified successfully (backup). You can now use all features for 7 days.")
    else:
        await update.message.reply_text("❌ Verification failed. The code is incorrect.")

# planner, feedback, admin
async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /plan <YYYY-MM-DD HH:MM> <note>")
        return
    try:
        dt_str = context.args[0] + " " + context.args[1]
        note = " ".join(context.args[2:])
        remind_at = int(datetime.strptime(dt_str, "%Y-%m-%d %H:%M").timestamp())
        add_planner_note(update.effective_user.id, note, remind_at)
        await update.message.reply_text("✅ Planner note saved.")
    except Exception:
        await update.message.reply_text("Invalid date. Use format YYYY-MM-DD HH:MM")

async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /feedback <message> (optional rating with /ratefeedback <1-5>)")
        return
    add_feedback(update.effective_user.id, text, 0)
    await update.message.reply_text("Thanks for your feedback! 🙏")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Access denied."); return
    cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE verified_at IS NOT NULL"); verified = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE ts > ?", (int(time.time()) - 86400,)); recent = cur.fetchone()[0]
    await update.message.reply_text(f"📊 Users: {total}\n✅ Verified: {verified}\n💬 Messages last 24h: {recent}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Access denied."); return
    cur.execute("SELECT user_id,tg_name,phone,verified_at FROM users ORDER BY verified_at DESC")
    rows = cur.fetchall(); out=[]
    for r in rows[:200]:
        uid,name,phone,vt = r
        vtstr = datetime.utcfromtimestamp(vt).strftime("%Y-%m-%d %H:%M") if vt else "Not verified"
        out.append(f"{name} ({uid}) — {phone or 'no phone'} — {vtstr}")
    await update.message.reply_text("\n".join(out) if out else "No users yet.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Only admin can broadcast."); return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    cur.execute("SELECT user_id FROM users WHERE verified_at IS NOT NULL")
    rows = cur.fetchall(); count=0
    for (uid,) in rows:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📣 Broadcast:\n\n{text}")
            count+=1
        except Exception:
            cur.execute("INSERT INTO unsent_messages(user_id,text,ts,tries) VALUES(?,?,?,0)", (uid, text, int(time.time())))
            conn.commit()
    await update.message.reply_text(f"Broadcast started; queued or sent to {count} users.")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Access denied."); return
    rows = get_logs(50)
    out = []
    for r in rows:
        out.append(f"{r[0]} [{r[1]}] {r[2]} ({datetime.utcfromtimestamp(r[3]).strftime('%Y-%m-%d %H:%M')})")
    await update.message.reply_text("\n".join(out) if out else "No logs")

# main message handler - chat and flows
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    if not text: return
    save_user(uid, user.first_name or user.username or "friend"); store_message(uid, text); set_last_seen(uid)
    # flows
    if context.user_data.get('await_language'):
        code = text.strip().lower(); set_user_lang(uid, code); context.user_data['await_language']=False; await update.message.reply_text(f"✅ Language set to {code}."); return
    flow = context.user_data.get('flow')
    if flow == 'PHONE':
        raw = text.strip(); phone, msg = normalize_phone(raw, hint_country=None)
        if not phone: await update.message.reply_text(f"❌ {msg}"); return
        code = "%06d" % random.randint(0, 999999); queue_otp(uid, phone, code)
        ok, twmsg = twilio_send_sms_verification(phone)
        pending = find_pending_otp_by_user(uid)
        if ok:
            if pending: mark_otp_sent(pending[0])
            await update.message.reply_text(twmsg + " If you don't receive it, I will send a Telegram backup.")
        else:
            send_telegram_backup_otp(context.application, uid, code)
            if pending: mark_otp_sent(pending[0])
            await update.message.reply_text("⚠️ SMS failed — backup OTP sent via Telegram. Use /code <otp> to verify.")
        context.user_data['flow']=None; return
    if flow == 'IMEI':
        imei = "".join(ch for ch in text if ch.isdigit()); context.user_data['flow']=None; res = scrape_imei(imei); await update.message.reply_text(res); return

    # language prompt
    u = get_user(uid)
    if not u or not u.get("lang"):
        last_prompt = u.get("last_lang_prompt") if u else 0
        if int(time.time()) - int(last_prompt or 0) > 3600:
            await update.message.reply_text("Which language would you like me to use? Reply with language code (e.g., en, fr, es) or use /setlang")
            cur.execute("UPDATE users SET last_lang_prompt=? WHERE user_id=?", (int(time.time()), uid)); conn.commit(); return

    # verification required
    if not is_verified(uid):
        await update.message.reply_text("🔒 Please verify with /verify +countryphone first (valid 7 days)."); return

    # IMEI direct detection
    digits = "".join(ch for ch in text if ch.isdigit())
    if ("imei" in text.lower() and digits) or (digits and len(digits) >= 14):
        res = scrape_imei(digits); await update.message.reply_text(res); return

    # currency pair
    parts = [p for p in text.replace("/", " ").split() if p]
    if len(parts) >= 2 and all(len(p) == 3 and p.isalpha() for p in parts[:2]):
        r = fetch_rate(parts[0].upper(), parts[1].upper()); await update.message.reply_text(r); return

    # planner quick add: handled via /plan
    # mood detection and store
    m = mood_from_text(text); set_user_mood(uid, m)

    # AI reply
    reply = None
    if OPENAI_AVAILABLE and OPENAI_API_KEY:
        try: reply = openai_reply(text, uid)
        except Exception: reply = None
    if not reply: reply = choose_reply(uid, text)
    try:
        await update.message.reply_text(reply)
    except Exception:
        cur.execute("INSERT INTO unsent_messages(user_id,text,ts,tries) VALUES(?,?,?,0)", (uid, reply, int(time.time()))); conn.commit()

# helper checks
def is_verified(user_id:int)->bool:
    u = get_user(user_id)
    if not u or not u.get("verified_at"):
        return False
    return (int(time.time()) - int(u["verified_at"])) <= 7*24*3600

# setlang command
async def setlang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /setlang <lang_code> (e.g., /setlang en)"); return
    code = context.args[0].lower(); set_user_lang(update.effective_user.id, code); await update.message.reply_text(f"Language set to {code}")

# helper to get due plans
def get_due_plans(now_ts:int):
    cur.execute("SELECT id,user_id,note,remind_at FROM planner WHERE remind_at <= ?", (now_ts,))
    return cur.fetchall()

# scheduler - daily briefing and planner reminders
def daily_briefing(app):
    cur.execute("SELECT user_id FROM users WHERE verified_at IS NOT NULL")
    rows = cur.fetchall()
    for (uid,) in rows:
        try:
            loc = get_location_data(); city = loc.get("city")
            app.bot.send_message(chat_id=uid, text=f"☀️ Daily briefing:\n{get_weather_by_city(city)}\n{fetch_rate('USD','NGN')}\n\n— {BOT_NAME}")
        except Exception:
            logger.exception("daily_briefing send fail")

def run_scheduler(app):
    while True:
        now = datetime.now()
        if now.hour == DAILY_BRIEF_HOUR and now.minute == 0:
            try: daily_briefing(app)
            except Exception: logger.exception("daily job failed")
            time.sleep(61)
        time.sleep(10)

# build and run
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("code", code_cmd))
    app.add_handler(CommandHandler("imei", imei_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("setlang", setlang_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # background threads
    t = threading.Thread(target=retry_queues, args=(app,), daemon=True); t.start()
    s = threading.Thread(target=run_scheduler, args=(app,), daemon=True); s.start()
    start_keepalive_server(); start_keepalive_pinger()

    logger.info("Starting Werleed Assistant (full) — polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
