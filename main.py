import asyncio
import re
import logging
import secrets
import sqlite3
import ssl
import json
from datetime import datetime

USERBOT_API_ID   = 37386003
USERBOT_API_HASH = "31f7b57e49ca12e6ac083fccc73e5aa0"
USERBOT_PHONE    = "+998336421983"
BOT_TOKEN = "8837669936:AAEadzLMjkKY-o_VFreu_mpTZ_SXZaOClTk"
CARDXABAR_BOT = "CardXabarBot"
DB_FILE = "elderpay.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("elderpay.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def db_init():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS shops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE NOT NULL,
        shop_name TEXT,
        token TEXT UNIQUE NOT NULL,
        webhook_url TEXT,
        bot_token TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        card_last4 TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (shop_id) REFERENCES shops(id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER,
        card_last4 TEXT,
        amount REAL,
        currency TEXT,
        location TEXT,
        transaction_date TEXT,
        transaction_time TEXT,
        balance_after REAL,
        raw_message TEXT,
        sent_webhook INTEGER DEFAULT 0,
        sent_telegram INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (shop_id) REFERENCES shops(id))""")
    conn.commit()
    conn.close()

def db_get(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_one(query, params=()):
    rows = db_get(query, params)
    return rows[0] if rows else None

def db_run(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    last_id = c.lastrowid
    conn.commit()
    conn.close()
    return last_id

def parse_payment(text):
    if not text:
        return None
    keywords = ["perevod na kartu", "перевод на карту", "зачисление", "to'lov", "kirim"]
    if not any(k in text.lower() for k in keywords):
        return None
    p = {"raw_message": text, "parsed_at": datetime.now().isoformat()}
    m = re.search(r"[🟢🔴🟡]\s*(.+)", text)
    if m: p["transaction_type"] = m.group(1).strip()
    m = re.search(r"[💵💸➕]\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if m:
        p["amount"] = float(m.group(1).replace(" ", ""))
        p["currency"] = m.group(2)
    m = re.search(r"🗂\s*\*+(\d{4})", text)
    if m: p["card_last4"] = m.group(1)
    m = re.search(r"📍\s*(.+)", text)
    if m: p["location"] = m.group(1).strip()
    m = re.search(r"🕐\s*(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}:\d{2})", text)
    if m:
        p["transaction_date"] = m.group(1)
        p["transaction_time"] = m.group(2)
    m = re.search(r"💰\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if m:
        p["balance_after"] = float(m.group(1).replace(" ", ""))
        p["balance_currency"] = m.group(2)
    if "amount" not in p:
        return None
    return p

async def async_https_post(host, path, data, headers=None, timeout=15):
    body = json.dumps(data).encode("utf-8")
    h = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Host": host,
        "Connection": "close",
    }
    if headers:
        h.update(headers)
    request = "POST {} HTTP/1.1\r\n".format(path)
    for k, v in h.items():
        request += "{}: {}\r\n".format(k, v)
    request += "\r\n"
    request = request.encode("utf-8") + body
    ssl_ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl_ctx), timeout=timeout)
        writer.write(request)
        await writer.drain()
        response = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                break
            response += chunk
        writer.close()
        parts = response.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return None
        body_bytes = parts[1]
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except Exception:
            try:
                idx = body_bytes.find(b"{")
                if idx >= 0:
                    return json.loads(body_bytes[idx:].decode("utf-8"))
            except Exception:
                pass
        return None
    except Exception as e:
        log.error("HTTPS xatosi {}:{} {}".format(host, path, e))
        return None

async def async_https_get(host, path, timeout=35):
    request = (
        "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n"
    ).format(path, host).encode("utf-8")
    ssl_ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl_ctx), timeout=timeout)
        writer.write(request)
        await writer.drain()
        response = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                if not chunk:
                    break
                response += chunk
            except asyncio.TimeoutError:
                break
        writer.close()
        parts = response.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return None
        body_bytes = parts[1]
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except Exception:
            try:
                idx = body_bytes.find(b"{")
                if idx >= 0:
                    return json.loads(body_bytes[idx:].decode("utf-8"))
            except Exception:
                pass
        return None
    except Exception as e:
        log.error("HTTPS GET xatosi: {}".format(e))
        return None

async def tg_post(token, method, data):
    path = "/bot{}/{}".format(token, method)
    return await async_https_post("api.telegram.org", path, data)

async def tg_get_updates(token, offset, timeout=25):
    path = "/bot{}/getUpdates?offset={}&timeout={}".format(token, offset, timeout)
    return await async_https_get("api.telegram.org", path, timeout=timeout + 10)

async def send_message(token, chat_id, text):
    return await tg_post(token, "sendMessage", {"chat_id": chat_id, "text": text})

async def send_webhook(url, payment, token):
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.netloc
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        headers = {"Authorization": "Bearer " + token}
        result = await async_https_post(host, path, payment, headers=headers)
        return result is not None
    except Exception as e:
        log.error("Webhook xatosi: {}".format(e))
        return False

async def send_telegram_notification(bot_token, chat_id, payment):
    text = "💳 Yangi tolov!\n💵 {} {}\n🗂 ***{}\n📍 {}\n🕐 {} {}\n💰 Qoldiq: {} {}".format(
        payment.get("amount", "?"), payment.get("currency", ""),
        payment.get("card_last4", "????"),
        payment.get("location", ""),
        payment.get("transaction_date", ""), payment.get("transaction_time", ""),
        payment.get("balance_after", ""), payment.get("balance_currency", ""))
    result = await send_message(bot_token, chat_id, text)
    return result and result.get("ok")

async def process_payment(payment):
    card_last4 = payment.get("card_last4")
    if not card_last4:
        return
    card = db_one(
        "SELECT c.*, s.token, s.webhook_url, s.bot_token, s.chat_id, s.shop_name, s.id as shop_id "
        "FROM cards c JOIN shops s ON c.shop_id = s.id WHERE c.card_last4 = ?", (card_last4,))
    if not card:
        log.info("Karta ***{} topilmadi".format(card_last4))
        return
    log.info("Dokon topildi: {} | ***{}".format(card["shop_name"], card_last4))
    pay_id = db_run(
        "INSERT INTO payments (shop_id, card_last4, amount, currency, location, "
        "transaction_date, transaction_time, balance_after, raw_message) VALUES (?,?,?,?,?,?,?,?,?)",
        (card["shop_id"], card_last4, payment.get("amount"), payment.get("currency"),
         payment.get("location"), payment.get("transaction_date"), payment.get("transaction_time"),
         payment.get("balance_after"), payment.get("raw_message")))
    payment["shop_token"] = card["token"]
    payment["payment_id"] = pay_id
    if card.get("webhook_url"):
        ok = await send_webhook(card["webhook_url"], payment, card["token"])
        if ok:
            db_run("UPDATE payments SET sent_webhook=1 WHERE id=?", (pay_id,))
    if card.get("bot_token"):
        ok = await send_telegram_notification(card["bot_token"], card["chat_id"], payment)
        if ok:
            db_run("UPDATE payments SET sent_telegram=1 WHERE id=?", (pay_id,))

user_states = {}

async def handle_update(update):
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    user_id = msg["from"]["id"]
    state_info = user_states.get(user_id, {})
    state = state_info.get("state")
    data = state_info.get("data", {})

    if text == "/start":
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        if shop:
            await send_message(BOT_TOKEN, chat_id,
                "Xush kelibsiz, {}!\nTokeningiz: {}\n\n/cards - kartalar\n/history - tolovlar\n/addcard - karta qoshish\n/settings - sozlamalar".format(
                    shop["shop_name"], shop["token"]))
        else:
            await send_message(BOT_TOKEN, chat_id, "ELDER PAY ga xush kelibsiz!\nRoyxatdan otish uchun /register")
        return

    if text == "/register":
        if db_one("SELECT id FROM shops WHERE chat_id=?", (user_id,)):
            await send_message(BOT_TOKEN, chat_id, "Allaqachon royxatdan otgansiz! /start")
            return
        user_states[user_id] = {"state": "reg_name", "data": {}}
        await send_message(BOT_TOKEN, chat_id, "Dokon nomini kiriting:")
        return

    if text == "/cards":
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        if not shop:
            await send_message(BOT_TOKEN, chat_id, "Avval royxatdan oting: /register")
            return
        cards = db_get("SELECT * FROM cards WHERE shop_id=?", (shop["id"],))
        if not cards:
            await send_message(BOT_TOKEN, chat_id, "Kartalar yoq. /addcard")
            return
        await send_message(BOT_TOKEN, chat_id,
            "Kartalaringiz:\n" + "\n".join("• ***{}".format(c["card_last4"]) for c in cards))
        return

    if text == "/history":
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        if not shop:
            await send_message(BOT_TOKEN, chat_id, "Avval royxatdan oting: /register")
            return
        pays = db_get("SELECT * FROM payments WHERE shop_id=? ORDER BY id DESC LIMIT 10", (shop["id"],))
        if not pays:
            await send_message(BOT_TOKEN, chat_id, "Tolovlar yoq hali.")
            return
        t = "Songgi tolovlar:\n\n"
        for p in pays:
            t += "{} {} | ***{} | {} {}\n".format(
                p["amount"], p["currency"], p["card_last4"],
                p["transaction_date"] or "", p["transaction_time"] or "")
        await send_message(BOT_TOKEN, chat_id, t)
        return

    if text == "/settings":
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        if not shop:
            await send_message(BOT_TOKEN, chat_id, "Avval royxatdan oting: /register")
            return
        await send_message(BOT_TOKEN, chat_id,
            "Sozlamalar:\nDokon: {}\nToken: {}\nWebhook: {}\nBot: {}".format(
                shop["shop_name"], shop["token"],
                shop["webhook_url"] or "Yoq",
                "Ulangan" if shop["bot_token"] else "Yoq"))
        return

    if text == "/mytoken":
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        if not shop:
            await send_message(BOT_TOKEN, chat_id, "Avval royxatdan oting: /register")
            return
        await send_message(BOT_TOKEN, chat_id, "Tokeningiz: {}".format(shop["token"]))
        return

    if text == "/addcard":
        if not db_one("SELECT id FROM shops WHERE chat_id=?", (user_id,)):
            await send_message(BOT_TOKEN, chat_id, "Avval royxatdan oting: /register")
            return
        user_states[user_id] = {"state": "addcard", "data": {}}
        await send_message(BOT_TOKEN, chat_id, "Yangi kartaning oxirgi 4 raqamini kiriting:")
        return

    if state == "reg_name":
        user_states[user_id] = {"state": "reg_card", "data": {"name": text}}
        await send_message(BOT_TOKEN, chat_id, "Karta raqamining oxirgi 4 raqamini kiriting (masalan: 7404):")
        return

    if state == "reg_card":
        if not re.match(r"^\d{4}$", text):
            await send_message(BOT_TOKEN, chat_id, "Faqat 4 ta raqam kiriting!")
            return
        if db_one("SELECT id FROM cards WHERE card_last4=?", (text,)):
            await send_message(BOT_TOKEN, chat_id, "Bu karta allaqachon royxatda!")
            return
        data["card"] = text
        user_states[user_id] = {"state": "reg_webhook", "data": data}
        await send_message(BOT_TOKEN, chat_id, "Webhook URL kiriting (yoq bolsa /skip):")
        return

    if state == "reg_webhook":
        data["webhook"] = None if text == "/skip" else text
        user_states[user_id] = {"state": "reg_tgtoken", "data": data}
        await send_message(BOT_TOKEN, chat_id, "Oz botingiz tokenini kiriting (yoq bolsa /skip):")
        return

    if state == "reg_tgtoken":
        tg_token = None if text == "/skip" else text
        token = secrets.token_hex(16)
        shop_id = db_run(
            "INSERT INTO shops (chat_id, shop_name, token, webhook_url, bot_token) VALUES (?,?,?,?,?)",
            (user_id, data["name"], token, data.get("webhook"), tg_token))
        db_run("INSERT INTO cards (shop_id, card_last4) VALUES (?,?)", (shop_id, data["card"]))
        user_states.pop(user_id, None)
        await send_message(BOT_TOKEN, chat_id,
            "Muvaffaqiyatli!\nDokon: {}\nKarta: ***{}\nToken: {}\n\nTolov kelganda xabar olasiz!".format(
                data["name"], data["card"], token))
        return

    if state == "addcard":
        if not re.match(r"^\d{4}$", text):
            await send_message(BOT_TOKEN, chat_id, "Faqat 4 ta raqam kiriting!")
            return
        if db_one("SELECT id FROM cards WHERE card_last4=?", (text,)):
            await send_message(BOT_TOKEN, chat_id, "Bu karta allaqachon royxatda!")
            return
        shop = db_one("SELECT * FROM shops WHERE chat_id=?", (user_id,))
        db_run("INSERT INTO cards (shop_id, card_last4) VALUES (?,?)", (shop["id"], text))
        user_states.pop(user_id, None)
        await send_message(BOT_TOKEN, chat_id, "Karta ***{} qoshildi!".format(text))
        return

async def run_bot():
    offset = 0
    log.info("Bot polling boshlandi...")
    while True:
        try:
            result = await tg_get_updates(BOT_TOKEN, offset, timeout=25)
            if result and result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        await handle_update(update)
                    except Exception as e:
                        log.error("Update xatosi: {}".format(e))
            else:
                await asyncio.sleep(2)
        except Exception as e:
            log.error("Polling xatosi: {}".format(e))
            await asyncio.sleep(5)

async def run_userbot():
    from telethon import TelegramClient, events
    from telethon.network import ConnectionTcpFull
    client = TelegramClient(
        "elderpay_session",
        USERBOT_API_ID,
        USERBOT_API_HASH,
        connection=ConnectionTcpFull,
        connection_retries=5,
        retry_delay=3,
        auto_reconnect=True,
        loop=asyncio.get_event_loop()
    )

    @client.on(events.NewMessage(from_users=CARDXABAR_BOT))
    async def on_message(event):
        text = event.message.text or ""
        log.info("Xabar keldi: {}...".format(text[:60]))
        payment = parse_payment(text)
        if payment:
            await process_payment(payment)

    await client.connect()
    if not await client.is_user_authorized():
        log.error("Userbot avtorizatsiya qilinmagan!")
        return
    me = await client.get_me()
    log.info("Userbot ulandi: {}".format(me.first_name))
    await client.run_until_disconnected()

async def main():
    db_init()
    log.info("ELDER PAY ishga tushmoqda...")
    await asyncio.gather(run_bot(), run_userbot())

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
