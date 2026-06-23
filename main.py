import asyncio
import re
import logging
import secrets
import sqlite3
import ssl
import json
import hmac
import hashlib
from datetime import datetime
from aiohttp import web

# ─── SOZLAMALAR ───────────────────────────────────────────────────────────────
BOT_TOKEN      = "8837669936:AAEadzLMjkKY-o_VFreu_mpTZ_SXZaOClTk"
USERBOT_API_ID   = 37386003
USERBOT_API_HASH = "31f7b57e49ca12e6ac083fccc73e5aa0"
CARDXABAR_BOT  = "CardXabarBot"
DB_FILE        = "elderpay.db"
API_PORT       = 8080
SERVER_IP      = "208.110.72.148"
ADMIN_ID       = None  # Birinchi login qilgan admin bo'ladi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("elderpay.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Global userbot client
userbot_client = None
userbot_loop = None

# ─── DATABASE ────────────────────────────────────────────────────────────────

def db_init():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        shop_name TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        callback_url TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        card_last4 TEXT NOT NULL,
        FOREIGN KEY (shop_id) REFERENCES shops(id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        order_code TEXT UNIQUE NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        card_last4 TEXT,
        paid_at TEXT,
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

def setting_get(key):
    row = db_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None

def setting_set(key, value):
    db_run("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

# ─── PARSER ──────────────────────────────────────────────────────────────────

def parse_payment(text):
    if not text or "🟢" not in text:
        return None
    keywords = ["perevod na kartu", "перевод на карту", "зачисление", "kirim", "to'lov"]
    if not any(k in text.lower() for k in keywords):
        return None
    p = {"raw_message": text}
    m = re.search(r"➕\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if not m:
        return None
    p["amount"] = int(float(m.group(1).replace(" ", "")))
    p["currency"] = m.group(2)
    m = re.search(r"\*{2,3}(\d{4})", text)
    if not m:
        return None
    p["card_last4"] = m.group(1)
    m = re.search(r"📍\s*(.+?)(?:🕓|💵|$)", text)
    if m:
        p["location"] = m.group(1).strip()
    m = re.search(r"🕓\s*(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}:\d{2})", text)
    if m:
        p["transaction_date"] = m.group(1)
        p["transaction_time"] = m.group(2)
    m = re.search(r"💵\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if m:
        p["balance_after"] = int(float(m.group(1).replace(" ", "")))
    return p

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

async def https_post(host, path, data, headers=None, timeout=15):
    body = json.dumps(data).encode("utf-8")
    h = {"Content-Type": "application/json", "Content-Length": str(len(body)),
         "Host": host, "Connection": "close"}
    if headers:
        h.update(headers)
    req = "POST {} HTTP/1.1\r\n".format(path)
    for k, v in h.items():
        req += "{}: {}\r\n".format(k, v)
    req += "\r\n"
    req = req.encode() + body
    ssl_ctx = ssl.create_default_context()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl_ctx), timeout=timeout)
        w.write(req)
        await w.drain()
        resp = b""
        while True:
            chunk = await asyncio.wait_for(r.read(4096), timeout=timeout)
            if not chunk:
                break
            resp += chunk
        w.close()
        parts = resp.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return None
        try:
            return json.loads(parts[1].decode("utf-8"))
        except Exception:
            idx = parts[1].find(b"{")
            if idx >= 0:
                return json.loads(parts[1][idx:].decode("utf-8"))
        return None
    except Exception as e:
        log.error("HTTPS POST: {}".format(e))
        return None

async def https_get(host, path, timeout=35):
    req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n".format(path, host).encode()
    ssl_ctx = ssl.create_default_context()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl_ctx), timeout=timeout)
        w.write(req)
        await w.drain()
        resp = b""
        while True:
            try:
                chunk = await asyncio.wait_for(r.read(4096), timeout=timeout)
                if not chunk:
                    break
                resp += chunk
            except asyncio.TimeoutError:
                break
        w.close()
        parts = resp.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return None
        try:
            return json.loads(parts[1].decode("utf-8"))
        except Exception:
            idx = parts[1].find(b"{")
            if idx >= 0:
                return json.loads(parts[1][idx:].decode("utf-8"))
        return None
    except Exception as e:
        log.error("HTTPS GET: {}".format(e))
        return None

async def tg_call(method, data):
    path = "/bot{}/{}".format(BOT_TOKEN, method)
    return await https_post("api.telegram.org", path, data)

async def tg_updates(offset, timeout=25):
    path = "/bot{}/getUpdates?offset={}&timeout={}".format(BOT_TOKEN, offset, timeout)
    return await https_get("api.telegram.org", path, timeout=timeout + 10)

async def send_msg(chat_id, text, kb=None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if kb:
        data["reply_markup"] = json.dumps(kb)
    return await tg_call("sendMessage", data)

async def edit_msg(chat_id, msg_id, text, kb=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if kb:
        data["reply_markup"] = json.dumps(kb)
    return await tg_call("editMessageText", data)

async def answer_cb(cb_id, text=""):
    return await tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

# ─── CALLBACK TO PHP ──────────────────────────────────────────────────────────

async def send_callback(shop, order):
    if not shop.get("callback_url"):
        return
    try:
        from urllib.parse import urlparse
        p = urlparse(shop["callback_url"])
        host = p.netloc
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        payload = {
            "shop_id": shop["id"],
            "order_code": order["order_code"],
            "insert_id": order["id"],
            "amount": order["amount"],
            "status": order["status"],
            "card_last4": order.get("card_last4") or "",
            "paid_at": order.get("paid_at") or "",
        }
        sig = hmac.new(shop["token"].encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        payload["sign"] = sig
        result = await https_post(host, path, payload, {"Authorization": "Bearer " + shop["token"]})
        log.info("Callback: {} → {}".format(order["order_code"], result))
    except Exception as e:
        log.error("Callback xato: {}".format(e))

# ─── PAYMENT PROCESSOR ───────────────────────────────────────────────────────

async def process_payment(payment):
    card_last4 = payment.get("card_last4")
    amount = payment.get("amount")
    if not card_last4 or not amount:
        return
    card = db_one("SELECT shop_id FROM cards WHERE card_last4=?", (card_last4,))
    if not card:
        log.info("Karta ***{} topilmadi".format(card_last4))
        return
    shop = db_one("SELECT * FROM shops WHERE id=?", (card["shop_id"],))
    if not shop:
        return
    order = db_one(
        "SELECT * FROM orders WHERE shop_id=? AND amount=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (shop["id"], amount))
    if not order:
        log.info("Pending buyurtma topilmadi: shop={} amount={}".format(shop["id"], amount))
        return
    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_run("UPDATE orders SET status='paid', card_last4=?, paid_at=? WHERE id=?",
           (card_last4, paid_at, order["id"]))
    updated = db_one("SELECT * FROM orders WHERE id=?", (order["id"],))
    log.info("✅ To'lov tasdiqlandi: {} | {}so'm".format(order["order_code"], amount))
    await send_callback(shop, updated)

# ─── USERBOT ─────────────────────────────────────────────────────────────────

pending_login = {}  # user_id: {"phone": ..., "client": ..., "phone_code_hash": ...}

async def start_userbot(session_file="elderpay_session"):
    global userbot_client
    try:
        from telethon import TelegramClient, events
        client = TelegramClient(session_file, USERBOT_API_ID, USERBOT_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Userbot avtorizatsiya qilinmagan")
            return False

        @client.on(events.NewMessage(from_users=CARDXABAR_BOT))
        async def on_payment(event):
            text = event.message.text or ""
            log.info("CardXabar: {}...".format(text[:60]))
            payment = parse_payment(text)
            if payment:
                await process_payment(payment)

        userbot_client = client
        me = await client.get_me()
        log.info("✅ Userbot ulandi: {}".format(me.first_name))
        asyncio.create_task(client.run_until_disconnected())
        return True
    except Exception as e:
        log.error("Userbot xato: {}".format(e))
        return False

# ─── BOT MENUS ───────────────────────────────────────────────────────────────

def main_kb(user_id):
    shops = db_get("SELECT * FROM shops WHERE owner_id=?", (user_id,))
    buttons = []
    for s in shops:
        buttons.append([{"text": "🏪 " + s["shop_name"], "callback_data": "shop_{}".format(s["id"])}])
    buttons.append([{"text": "➕ Yangi do'kon yaratish", "callback_data": "new_shop"}])
    
    # Admin: userbot holati
    status = "🟢 Ulangan" if userbot_client and userbot_client.is_connected() else "🔴 Ulanmagan"
    buttons.append([{"text": "📡 Userbot: " + status, "callback_data": "userbot_status"}])
    return {"inline_keyboard": buttons}

def main_text(user_id):
    shops = db_get("SELECT * FROM shops WHERE owner_id=?", (user_id,))
    if not shops:
        return "👋 <b>ELDERPAY</b> ga xush kelibsiz!\n\nHali do'koningiz yo'q. Yangi do'kon yarating."
    text = "🏪 <b>Do'konlaringiz:</b>\n\n"
    for s in shops:
        cards = db_get("SELECT * FROM cards WHERE shop_id=?", (s["id"],))
        text += "• <b>{}</b> — {} ta karta\n".format(s["shop_name"], len(cards))
    return text

def shop_text(shop_id):
    shop = db_one("SELECT * FROM shops WHERE id=?", (shop_id,))
    if not shop:
        return "Do'kon topilmadi.", None
    cards = db_get("SELECT * FROM cards WHERE shop_id=?", (shop_id,))
    card_list = "\n".join("  💳 ***{}".format(c["card_last4"]) for c in cards) or "  Yo'q"
    orders_count = db_one("SELECT COUNT(*) as cnt FROM orders WHERE shop_id=?", (shop_id,))
    paid_count = db_one("SELECT COUNT(*) as cnt FROM orders WHERE shop_id=? AND status='paid'", (shop_id,))
    text = (
        "🏪 <b>{}</b>\n\n"
        "🆔 Shop ID: <code>{}</code>\n"
        "🔑 Shop Key: <code>{}</code>\n\n"
        "💳 <b>Kartalar:</b>\n{}\n\n"
        "🔗 Callback URL: {}\n\n"
        "📊 Jami buyurtmalar: {} | To'langan: {}"
    ).format(
        shop["shop_name"], shop["id"], shop["token"],
        card_list,
        shop["callback_url"] or "❌ Yo'q",
        orders_count["cnt"], paid_count["cnt"]
    )
    kb = {"inline_keyboard": [
        [{"text": "💳 Karta qo'shish", "callback_data": "addcard_{}".format(shop_id)},
         {"text": "🗑 Karta o'chirish", "callback_data": "delcard_{}".format(shop_id)}],
        [{"text": "🔗 Callback URL", "callback_data": "setcb_{}".format(shop_id)}],
        [{"text": "📄 API Docs", "callback_data": "docs_{}".format(shop_id)}],
        [{"text": "📋 PHP Qo'llanma", "callback_data": "phpguide_{}".format(shop_id)}],
        [{"text": "🔙 Orqaga", "callback_data": "back"}]
    ]}
    return text, kb

# ─── BOT HANDLERS ─────────────────────────────────────────────────────────────

user_states = {}

async def handle_msg(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()
    state_info = user_states.get(user_id, {})
    state = state_info.get("state", "")
    data = state_info.get("data", {})

    if text == "/start":
        user_states.pop(user_id, None)
        await send_msg(chat_id, main_text(user_id), main_kb(user_id))
        return

    # ── Userbot login ──
    if state == "login_phone":
        from telethon import TelegramClient
        phone = text.strip()
        client = TelegramClient("elderpay_session", USERBOT_API_ID, USERBOT_API_HASH)
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            pending_login[user_id] = {"phone": phone, "client": client, "hash": result.phone_code_hash}
            user_states[user_id] = {"state": "login_code", "data": {}}
            await send_msg(chat_id,
                "📱 Telefoningizga kod yuborildi!\n\n"
                "Kodni <b>nuqta bilan</b> kiriting:\n"
                "Masalan kod <code>12345</code> bo'lsa → <code>1.2.3.4.5</code>")
        except Exception as e:
            await send_msg(chat_id, "❌ Xato: {}".format(e))
        return

    if state == "login_code":
        raw = text.replace(".", "").replace(" ", "")
        login_data = pending_login.get(user_id)
        if not login_data:
            await send_msg(chat_id, "❌ Login sessiyasi topilmadi. Qayta /start")
            return
        client = login_data["client"]
        try:
            await client.sign_in(login_data["phone"], raw, phone_code_hash=login_data["hash"])
            me = await client.get_me()
            # Session saqlandi, userbotni ishga tushiramiz
            global userbot_client
            from telethon import events
            @client.on(events.NewMessage(from_users=CARDXABAR_BOT))
            async def on_payment(event):
                txt = event.message.text or ""
                payment = parse_payment(txt)
                if payment:
                    await process_payment(payment)
            userbot_client = client
            asyncio.create_task(client.run_until_disconnected())
            pending_login.pop(user_id, None)
            user_states.pop(user_id, None)
            await send_msg(chat_id,
                "✅ <b>Userbot ulandi!</b>\n\n"
                "👤 Akkaunt: <b>{}</b>\n"
                "📡 Endi to'lovlar avtomatik kuzatiladi!".format(me.first_name))
        except Exception as e:
            err = str(e)
            if "two-steps" in err.lower() or "password" in err.lower():
                user_states[user_id] = {"state": "login_2fa", "data": {}}
                await send_msg(chat_id, "🔐 2FA parolingizni kiriting:")
            else:
                await send_msg(chat_id, "❌ Kod xato: {}".format(e))
        return

    if state == "login_2fa":
        login_data = pending_login.get(user_id)
        if not login_data:
            await send_msg(chat_id, "❌ Login sessiyasi topilmadi.")
            return
        client = login_data["client"]
        try:
            await client.sign_in(password=text)
            me = await client.get_me()
            global userbot_client
            from telethon import events
            @client.on(events.NewMessage(from_users=CARDXABAR_BOT))
            async def on_payment2(event):
                txt = event.message.text or ""
                payment = parse_payment(txt)
                if payment:
                    await process_payment(payment)
            userbot_client = client
            asyncio.create_task(client.run_until_disconnected())
            pending_login.pop(user_id, None)
            user_states.pop(user_id, None)
            await send_msg(chat_id,
                "✅ <b>Userbot ulandi!</b>\n👤 {}".format(me.first_name))
        except Exception as e:
            await send_msg(chat_id, "❌ Parol xato: {}".format(e))
        return

    # ── Do'kon yaratish ──
    if state == "wait_shop_name":
        user_states[user_id] = {"state": "wait_card", "data": {"name": text}}
        await send_msg(chat_id,
            "💳 Kartangizning oxirgi <b>4 raqamini</b> kiriting:\n"
            "Masalan: <code>7404</code>")
        return

    if state == "wait_card":
        if not re.match(r"^\d{4}$", text):
            await send_msg(chat_id, "❌ Faqat 4 ta raqam kiriting!")
            return
        data["card"] = text
        user_states[user_id] = {"state": "wait_callback", "data": data}
        await send_msg(chat_id,
            "🔗 PHP callback URL kiriting:\n\n"
            "Masalan: <code>https://sizningsayt.uz/payment.php</code>\n\n"
            "Yo'q bo'lsa /skip yozing")
        return

    if state == "wait_callback":
        cb_url = None if text == "/skip" else text
        token = secrets.token_hex(16)
        shop_id = db_run(
            "INSERT INTO shops (owner_id, shop_name, token, callback_url) VALUES (?,?,?,?)",
            (user_id, data["name"], token, cb_url))
        db_run("INSERT INTO cards (shop_id, card_last4) VALUES (?,?)", (shop_id, data["card"]))
        user_states.pop(user_id, None)
        await send_msg(chat_id,
            "✅ <b>Do'kon yaratildi!</b>\n\n"
            "🏪 Nom: <b>{}</b>\n"
            "🆔 Shop ID: <code>{}</code>\n"
            "🔑 Shop Key: <code>{}</code>\n"
            "💳 Karta: <code>***{}</code>\n"
            "🔗 Callback: {}\n\n"
            "📄 /docs — API hujjati\n"
            "🏠 /start — Bosh menyu".format(
                data["name"], shop_id, token, data["card"],
                cb_url or "Yo'q"))
        return

    if state == "wait_addcard":
        shop_id = data.get("shop_id")
        if not re.match(r"^\d{4}$", text):
            await send_msg(chat_id, "❌ Faqat 4 ta raqam kiriting!")
            return
        existing = db_one("SELECT id FROM cards WHERE shop_id=? AND card_last4=?", (shop_id, text))
        if existing:
            await send_msg(chat_id, "❌ Bu karta allaqachon qo'shilgan!")
            return
        db_run("INSERT INTO cards (shop_id, card_last4) VALUES (?,?)", (shop_id, text))
        user_states.pop(user_id, None)
        t, kb = shop_text(shop_id)
        await send_msg(chat_id, "✅ Karta ***{} qo'shildi!\n\n".format(text) + t, kb)
        return

    if state == "wait_callback_url":
        shop_id = data.get("shop_id")
        cb_url = None if text == "/skip" else text
        db_run("UPDATE shops SET callback_url=? WHERE id=?", (cb_url, shop_id))
        user_states.pop(user_id, None)
        t, kb = shop_text(shop_id)
        await send_msg(chat_id, "✅ Callback URL saqlandi!\n\n" + t, kb)
        return

async def handle_cb(cb):
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cdata = cb.get("data", "")

    await answer_cb(cb_id)

    if cdata == "back":
        user_states.pop(user_id, None)
        await edit_msg(chat_id, msg_id, main_text(user_id), main_kb(user_id))
        return

    if cdata == "new_shop":
        user_states[user_id] = {"state": "wait_shop_name", "data": {}}
        await edit_msg(chat_id, msg_id, "🏪 Do'kon nomini kiriting:")
        return

    if cdata == "userbot_status":
        if userbot_client and userbot_client.is_connected():
            me = await userbot_client.get_me()
            await edit_msg(chat_id, msg_id,
                "📡 <b>Userbot holati:</b>\n\n"
                "✅ Ulangan\n"
                "👤 Akkaunt: <b>{}</b>\n\n"
                "Qayta ulash uchun /login".format(me.first_name),
                {"inline_keyboard": [[{"text": "🔙 Orqaga", "callback_data": "back"}]]})
        else:
            await edit_msg(chat_id, msg_id,
                "📡 <b>Userbot holati:</b>\n\n"
                "❌ Ulanmagan\n\n"
                "Ulash uchun /login buyrug'ini yuboring",
                {"inline_keyboard": [[{"text": "🔙 Orqaga", "callback_data": "back"}]]})
        return

    if cdata.startswith("shop_"):
        shop_id = int(cdata.split("_")[1])
        t, kb = shop_text(shop_id)
        await edit_msg(chat_id, msg_id, t, kb)
        return

    if cdata.startswith("addcard_"):
        shop_id = int(cdata.split("_")[1])
        user_states[user_id] = {"state": "wait_addcard", "data": {"shop_id": shop_id}}
        await edit_msg(chat_id, msg_id,
            "💳 Yangi kartaning oxirgi 4 raqamini kiriting:\nMasalan: <code>7404</code>")
        return

    if cdata.startswith("delcard_"):
        shop_id = int(cdata.split("_")[1])
        cards = db_get("SELECT * FROM cards WHERE shop_id=?", (shop_id,))
        if not cards:
            await answer_cb(cb_id, "Kartalar yo'q!")
            return
        buttons = [[{"text": "💳 ***{}".format(c["card_last4"]),
                     "callback_data": "rmcard_{}_{}".format(shop_id, c["id"])}] for c in cards]
        buttons.append([{"text": "🔙 Orqaga", "callback_data": "shop_{}".format(shop_id)}])
        await edit_msg(chat_id, msg_id, "🗑 O'chirmoqchi bo'lgan kartani tanlang:",
                       {"inline_keyboard": buttons})
        return

    if cdata.startswith("rmcard_"):
        parts = cdata.split("_")
        shop_id = int(parts[1])
        card_id = int(parts[2])
        card = db_one("SELECT * FROM cards WHERE id=?", (card_id,))
        db_run("DELETE FROM cards WHERE id=?", (card_id,))
        t, kb = shop_text(shop_id)
        await edit_msg(chat_id, msg_id,
            "✅ Karta ***{} o'chirildi!\n\n".format(card["card_last4"] if card else "?") + t, kb)
        return

    if cdata.startswith("setcb_"):
        shop_id = int(cdata.split("_")[1])
        user_states[user_id] = {"state": "wait_callback_url", "data": {"shop_id": shop_id}}
        await edit_msg(chat_id, msg_id,
            "🔗 Yangi callback URL kiriting:\n\n"
            "Masalan: <code>https://sizningsayt.uz/payment.php</code>\n\n"
            "O'chirish uchun /skip yozing")
        return

    if cdata.startswith("docs_"):
        shop_id = int(cdata.split("_")[1])
        shop = db_one("SELECT * FROM shops WHERE id=?", (shop_id,))
        docs = (
            "📄 <b>ELDERPAY API Dokumentatsiya</b>\n\n"
            "🔗 <b>Base URL:</b>\n"
            "<code>http://{}:{}/api</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Buyurtma yaratish</b>\n"
            "Method: <code>POST</code>\n"
            "URL: <code>/api?method=create</code>\n\n"
            "Parametrlar:\n"
            "<code>shop_id = {}</code>\n"
            "<code>shop_key = {}</code>\n"
            "<code>amount = 5000</code>\n\n"
            "Javob (success):\n"
            "<code>{{\n"
            '  "status": "success",\n'
            '  "order": "ABC12345",\n'
            '  "insert_id": 1,\n'
            '  "amount": 5000\n'
            "}}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "2️⃣ <b>Buyurtma tekshirish</b>\n"
            "Method: <code>GET</code>\n"
            "URL: <code>/api?method=check&order=ABC12345</code>\n\n"
            "Javob:\n"
            "<code>{{\n"
            '  "status": "success",\n'
            '  "data": {{\n'
            '    "order": "ABC12345",\n'
            '    "amount": 5000,\n'
            '    "status": "paid",\n'
            '    "paid_at": "2026-06-23 14:01:00"\n'
            '  }}\n'
            "}}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "3️⃣ <b>Callback (avtomatik)</b>\n"
            "To'lov tasdiqlanganda URL ga POST:\n"
            "<code>{{\n"
            '  "shop_id": {},\n'
            '  "order_code": "ABC12345",\n'
            '  "insert_id": 1,\n'
            '  "amount": 5000,\n'
            '  "status": "paid",\n'
            '  "card_last4": "7404",\n'
            '  "paid_at": "2026-06-23 14:01:00",\n'
            '  "sign": "hmac_sha256_signature"\n'
            "}}</code>"
        ).format(SERVER_IP, API_PORT, shop["id"], shop["token"], shop["id"])
        await edit_msg(chat_id, msg_id, docs,
                       {"inline_keyboard": [[{"text": "🔙 Orqaga", "callback_data": "shop_{}".format(shop_id)}]]})
        return

    if cdata.startswith("phpguide_"):
        shop_id = int(cdata.split("_")[1])
        shop = db_one("SELECT * FROM shops WHERE id=?", (shop_id,))
        guide = (
            "📋 <b>PHP Qo'llanma</b>\n\n"
            "PHP kodingizda quyidagi o'zgaruvchilarni o'rnating:\n\n"
            "<code>define('SHOP_ID', {});\n"
            "define('SHOP_KEY', '{}');\n"
            "define('API_URL', 'http://{}:{}/api');</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Buyurtma yaratish:</b>\n"
            "<code>$ch = curl_init(API_URL.'?method=create');\n"
            "curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);\n"
            "curl_setopt($ch, CURLOPT_POST, true);\n"
            "curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query([\n"
            "    'shop_id'  => SHOP_ID,\n"
            "    'shop_key' => SHOP_KEY,\n"
            "    'amount'   => $amount\n"
            "]));\n"
            "$res = json_decode(curl_exec($ch), true);\n"
            "curl_close($ch);\n"
            "// $res['order'] — buyurtma kodi\n"
            "// $res['insert_id'] — buyurtma ID</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Callback qabul qilish:</b>\n"
            "<code>$data = json_decode(file_get_contents('php://input'), true);\n"
            "if($data['status'] === 'paid') {{\n"
            "    $order_id = $data['insert_id'];\n"
            "    $amount = $data['amount'];\n"
            "    // Bazaga yozing, balans qo'shing\n"
            "}}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💳 To'lov uchun karta: <b>***{}</b>\n"
            "Foydalanuvchi shu kartaga pul o'tkazishi kerak!"
        ).format(shop["id"], shop["token"], SERVER_IP, API_PORT,
                 db_one("SELECT card_last4 FROM cards WHERE shop_id=? LIMIT 1", (shop_id,))["card_last4"] if db_get("SELECT card_last4 FROM cards WHERE shop_id=?", (shop_id,)) else "????")
        await edit_msg(chat_id, msg_id, guide,
                       {"inline_keyboard": [[{"text": "🔙 Orqaga", "callback_data": "shop_{}".format(shop_id)}]]})
        return

async def handle_update(update):
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        user_id = msg["from"]["id"]
        chat_id = msg["chat"]["id"]
        # /login komandasi
        if text == "/login":
            user_states[user_id] = {"state": "login_phone", "data": {}}
            await send_msg(chat_id,
                "📱 <b>Userbot ulash</b>\n\n"
                "Telegram raqamingizni kiriting:\n"
                "Masalan: <code>+998901234567</code>")
            return
        await handle_msg(msg)
    elif "callback_query" in update:
        await handle_cb(update["callback_query"])

# ─── API SERVER ───────────────────────────────────────────────────────────────

async def api_handler(request):
    method = request.rel_url.query.get("method", "")

    if method == "create":
        try:
            data = await request.post()
            shop_id = data.get("shop_id")
            shop_key = data.get("shop_key")
            amount = int(data.get("amount", 0))
            shop = db_one("SELECT * FROM shops WHERE id=? AND token=?", (shop_id, shop_key))
            if not shop:
                return web.json_response({"status": "error", "message": "Noto'g'ri shop_id yoki shop_key"})
            if amount < 1000:
                return web.json_response({"status": "error", "message": "Minimal miqdor 1000 so'm"})
            existing = db_one(
                "SELECT id FROM orders WHERE shop_id=? AND amount=? AND status='pending'",
                (shop["id"], amount))
            if existing:
                return web.json_response({"status": "error",
                    "message": "Bu miqdordagi to'lov allaqachon mavjud"})
            order_code = secrets.token_hex(8).upper()
            insert_id = db_run(
                "INSERT INTO orders (shop_id, order_code, amount) VALUES (?,?,?)",
                (shop["id"], order_code, amount))
            return web.json_response({
                "status": "success",
                "order": order_code,
                "insert_id": insert_id,
                "amount": amount
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)})

    elif method == "check":
        order_code = request.rel_url.query.get("order", "")
        order = db_one("SELECT * FROM orders WHERE order_code=?", (order_code,))
        if not order:
            return web.json_response({"status": "error", "message": "Buyurtma topilmadi"})
        return web.json_response({
            "status": "success",
            "data": {
                "order": order["order_code"],
                "amount": order["amount"],
                "status": order["status"],
                "card_last4": order["card_last4"],
                "paid_at": order["paid_at"],
                "created_at": order["created_at"]
            }
        })

    return web.json_response({"status": "error", "message": "Noto'g'ri method"})

async def run_api():
    app = web.Application()
    app.router.add_route("*", "/api", api_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    log.info("✅ API server: http://{}:{}".format(SERVER_IP, API_PORT))

# ─── BOT POLLING ─────────────────────────────────────────────────────────────

async def run_bot():
    offset = 0
    log.info("✅ Bot polling boshlandi...")
    while True:
        try:
            result = await tg_updates(offset, timeout=25)
            if result and result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        await handle_update(update)
                    except Exception as e:
                        log.error("Update xato: {}".format(e))
            else:
                await asyncio.sleep(2)
        except Exception as e:
            log.error("Polling xato: {}".format(e))
            await asyncio.sleep(5)

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    db_init()
    log.info("🚀 ELDERPAY ishga tushmoqda...")
    # Eski session bo'lsa userbotni avtomatik ulash
    ok = await start_userbot()
    if ok:
        log.info("✅ Userbot avtomatik ulandi")
    else:
        log.warning("⚠️ Userbot ulanmadi. Botda /login qiling.")
    await asyncio.gather(run_bot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
