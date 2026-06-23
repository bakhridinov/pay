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

BOT_TOKEN = "8837669936:AAEadzLMjkKY-o_VFreu_mpTZ_SXZaOClTk"
USERBOT_API_ID = 37386003
USERBOT_API_HASH = "31f7b57e49ca12e6ac083fccc73e5aa0"
USERBOT_PHONE = "+998336421983"
CARDXABAR_BOT = "CardXabarBot"
DB_FILE = "elderpay.db"
API_HOST = "0.0.0.0"
API_PORT = 8080

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("elderpay.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── DATABASE ────────────────────────────────────────────────────────────────

def db_init():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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

# ─── PARSER ──────────────────────────────────────────────────────────────────

def parse_payment(text):
    if not text:
        return None
    # Faqat kirimlarni qayta ishlash
    if "🟢" not in text:
        return None
    keywords = ["perevod na kartu", "перевод на карту", "зачисление", "kirim", "to'lov"]
    if not any(k in text.lower() for k in keywords):
        return None

    p = {"raw_message": text}

    # Miqdor: ➕ 1 000.00 UZS
    m = re.search(r"➕\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if m:
        p["amount"] = int(float(m.group(1).replace(" ", "")))
        p["currency"] = m.group(2)
    else:
        return None

    # Karta: ***7404
    m = re.search(r"\*{2,3}(\d{4})", text)
    if m:
        p["card_last4"] = m.group(1)
    else:
        return None

    # Joy
    m = re.search(r"📍\s*(.+?)(?:🕓|$)", text)
    if m:
        p["location"] = m.group(1).strip()

    # Vaqt: 23.06.26 14:01
    m = re.search(r"🕓\s*(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}:\d{2})", text)
    if m:
        p["transaction_date"] = m.group(1)
        p["transaction_time"] = m.group(2)

    # Qoldiq: 💵 11 970.00 UZS
    m = re.search(r"💵\s*([\d\s]+\.?\d*)\s*(UZS|USD|EUR|RUB)", text)
    if m:
        p["balance_after"] = int(float(m.group(1).replace(" ", "")))

    return p

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

async def async_post(host, path, data, headers=None, timeout=15):
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
        log.error("HTTP POST xato: {}".format(e))
        return None

async def async_get(host, path, timeout=35):
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
        log.error("HTTP GET xato: {}".format(e))
        return None

async def tg_post(method, data):
    path = "/bot{}/{}".format(BOT_TOKEN, method)
    return await async_post("api.telegram.org", path, data)

async def tg_updates(offset, timeout=25):
    path = "/bot{}/getUpdates?offset={}&timeout={}".format(BOT_TOKEN, offset, timeout)
    return await async_get("api.telegram.org", path, timeout=timeout + 10)

async def send_msg(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return await tg_post("sendMessage", data)

async def edit_msg(chat_id, message_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return await tg_post("editMessageText", data)

async def answer_cb(callback_query_id, text=""):
    return await tg_post("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

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
            "card_last4": order.get("card_last4", ""),
            "paid_at": order.get("paid_at", ""),
        }
        sig = hmac.new(shop["token"].encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        payload["sign"] = sig
        result = await async_post(host, path, payload, {"Authorization": "Bearer " + shop["token"]})
        log.info("Callback yuborildi: {} → {}".format(order["order_code"], result))
    except Exception as e:
        log.error("Callback xato: {}".format(e))

# ─── PAYMENT PROCESSOR ───────────────────────────────────────────────────────

async def process_payment(payment):
    card_last4 = payment.get("card_last4")
    amount = payment.get("amount")
    if not card_last4 or not amount:
        return

    card = db_one(
        "SELECT c.shop_id FROM cards c WHERE c.card_last4=?", (card_last4,))
    if not card:
        log.info("Karta ***{} hech bir do'konga ulanmagan".format(card_last4))
        return

    shop_id = card["shop_id"]
    shop = db_one("SELECT * FROM shops WHERE id=?", (shop_id,))
    if not shop:
        return

    # Shu miqdordagi pending buyurtmani topamiz
    order = db_one(
        "SELECT * FROM orders WHERE shop_id=? AND amount=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (shop_id, amount))

    if not order:
        log.info("Kutilgan buyurtma topilmadi: do'kon={} miqdor={}".format(shop_id, amount))
        return

    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_run("UPDATE orders SET status='paid', card_last4=?, paid_at=? WHERE id=?",
           (card_last4, paid_at, order["id"]))

    updated_order = db_one("SELECT * FROM orders WHERE id=?", (order["id"],))
    log.info("To'lov tasdiqlandi: order={} miqdor={}".format(order["order_code"], amount))

    await send_callback(shop, updated_order)

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
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    log.info("API server ishga tushdi: http://{}:{}".format(API_HOST, API_PORT))

# ─── BOT ─────────────────────────────────────────────────────────────────────

user_states = {}

def main_menu(user_id):
    shops = db_get("SELECT * FROM shops WHERE owner_id=?", (user_id,))
    if not shops:
        return "👋 Xush kelibsiz!\n\nHali do'koningiz yo'q.", {
            "inline_keyboard": [[{"text": "➕ Yangi do'kon", "callback_data": "new_shop"}]]
        }
    text = "🏪 <b>Do'konlaringiz:</b>\n\n"
    buttons = []
    for s in shops:
        text += "• <b>{}</b> (ID: {})\n".format(s["shop_name"], s["id"])
        buttons.append([{"text": "🏪 " + s["shop_name"], "callback_data": "shop_{}".format(s["id"])}])
    buttons.append([{"text": "➕ Yangi do'kon", "callback_data": "new_shop"}])
    return text, {"inline_keyboard": buttons}

def shop_menu(shop_id):
    shop = db_one("SELECT * FROM shops WHERE id=?", (shop_id,))
    if not shop:
        return "Do'kon topilmadi.", None
    cards = db_get("SELECT * FROM cards WHERE shop_id=?", (shop_id,))
    card_text = "\n".join("  💳 ***{}".format(c["card_last4"]) for c in cards) or "  Yo'q"
    text = (
        "🏪 <b>{}</b>\n\n"
        "🆔 Shop ID: <code>{}</code>\n"
        "🔑 Shop Key: <code>{}</code>\n"
        "💳 Kartalar:\n{}\n"
        "🔗 Callback URL: {}\n\n"
        "📄 API docs: /docs"
    ).format(shop["shop_name"], shop["id"], shop["token"],
             card_text, shop["callback_url"] or "Yo'q")
    buttons = {
        "inline_keyboard": [
            [{"text": "➕ Karta qo'shish", "callback_data": "addcard_{}".format(shop_id)}],
            [{"text": "🔗 Callback URL o'rnatish", "callback_data": "setcb_{}".format(shop_id)}],
            [{"text": "🔙 Orqaga", "callback_data": "back"}]
        ]
    }
    return text, buttons

async def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()

    state_info = user_states.get(user_id, {})
    state = state_info.get("state")
    data = state_info.get("data", {})

    if text == "/start":
        user_states.pop(user_id, None)
        t, kb = main_menu(user_id)
        await send_msg(chat_id, t, kb)
        return

    if text == "/docs":
        server_ip = "208.110.72.148"
        docs = (
            "📄 <b>ELDERPAY API Dokumentatsiya</b>\n\n"
            "🔗 Base URL: <code>http://{}:{}/api</code>\n\n"
            "<b>1. Buyurtma yaratish</b>\n"
            "POST /api?method=create\n"
            "Parametrlar:\n"
            "  shop_id = [sizning ID]\n"
            "  shop_key = [sizning token]\n"
            "  amount = [miqdor so'mda]\n\n"
            "Javob:\n"
            "<code>{{\"status\":\"success\",\"order\":\"ABC123\",\"insert_id\":1,\"amount\":5000}}</code>\n\n"
            "<b>2. Buyurtma tekshirish</b>\n"
            "GET /api?method=check&order=ABC123\n\n"
            "Javob:\n"
            "<code>{{\"status\":\"success\",\"data\":{{\"status\":\"paid\",...}}}}</code>\n\n"
            "<b>3. Callback</b>\n"
            "To'lov tasdiqlanganda callback URL ga POST yuboriladi:\n"
            "<code>{{\"shop_id\":1,\"order_code\":\"ABC\",\"amount\":5000,\"status\":\"paid\"}}</code>"
        ).format(server_ip, API_PORT)
        await send_msg(chat_id, docs)
        return

    if state == "wait_shop_name":
        user_states[user_id] = {"state": "wait_card", "data": {"name": text}}
        await send_msg(chat_id, "💳 Kartangizning oxirgi 4 raqamini kiriting:\n(masalan: 7404)")
        return

    if state == "wait_card":
        if not re.match(r"^\d{4}$", text):
            await send_msg(chat_id, "❌ Faqat 4 ta raqam kiriting!")
            return
        data["card"] = text
        user_states[user_id] = {"state": "wait_callback", "data": data}
        await send_msg(chat_id, "🔗 Callback URL kiriting (PHP endpoint):\n\nMasalan: https://sizningsayt.uz/payment.php\n\nYo'q bo'lsa /skip yozing")
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
            "💳 Karta: ***{}\n\n"
            "📄 API docs uchun: /docs".format(data["name"], shop_id, token, data["card"]))
        return

    if state == "wait_addcard":
        shop_id = data.get("shop_id")
        if not re.match(r"^\d{4}$", text):
            await send_msg(chat_id, "❌ Faqat 4 ta raqam kiriting!")
            return
        db_run("INSERT INTO cards (shop_id, card_last4) VALUES (?,?)", (shop_id, text))
        user_states.pop(user_id, None)
        await send_msg(chat_id, "✅ Karta ***{} qo'shildi!".format(text))
        return

    if state == "wait_callback_url":
        shop_id = data.get("shop_id")
        cb_url = None if text == "/skip" else text
        db_run("UPDATE shops SET callback_url=? WHERE id=?", (cb_url, shop_id))
        user_states.pop(user_id, None)
        await send_msg(chat_id, "✅ Callback URL saqlandi!")
        return

async def handle_callback(cb):
    query_id = cb["id"]
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cdata = cb.get("data", "")

    await answer_cb(query_id)

    if cdata == "back":
        user_states.pop(user_id, None)
        t, kb = main_menu(user_id)
        await edit_msg(chat_id, msg_id, t, kb)
        return

    if cdata == "new_shop":
        user_states[user_id] = {"state": "wait_shop_name", "data": {}}
        await edit_msg(chat_id, msg_id, "🏪 Do'kon nomini kiriting:")
        return

    if cdata.startswith("shop_"):
        shop_id = int(cdata.split("_")[1])
        t, kb = shop_menu(shop_id)
        await edit_msg(chat_id, msg_id, t, kb)
        return

    if cdata.startswith("addcard_"):
        shop_id = int(cdata.split("_")[1])
        user_states[user_id] = {"state": "wait_addcard", "data": {"shop_id": shop_id}}
        await edit_msg(chat_id, msg_id, "💳 Yangi kartaning oxirgi 4 raqamini kiriting:")
        return

    if cdata.startswith("setcb_"):
        shop_id = int(cdata.split("_")[1])
        user_states[user_id] = {"state": "wait_callback_url", "data": {"shop_id": shop_id}}
        await edit_msg(chat_id, msg_id, "🔗 Callback URL kiriting:\n\nYo'q bo'lsa /skip yozing")
        return

async def handle_update(update):
    if "message" in update:
        await handle_message(update["message"])
    elif "callback_query" in update:
        await handle_callback(update["callback_query"])

async def run_bot():
    offset = 0
    log.info("Bot polling boshlandi...")
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

# ─── USERBOT ─────────────────────────────────────────────────────────────────

async def run_userbot():
    from telethon import TelegramClient, events

    client = TelegramClient(
        "elderpay_session",
        USERBOT_API_ID,
        USERBOT_API_HASH,
    )

    @client.on(events.NewMessage(from_users=CARDXABAR_BOT))
    async def on_message(event):
        text = event.message.text or ""
        log.info("CardXabarBot xabari: {}...".format(text[:80]))
        payment = parse_payment(text)
        if payment:
            log.info("To'lov aniqlandi: {}".format(payment))
            await process_payment(payment)

    await client.connect()
    if not await client.is_user_authorized():
        log.error("Userbot avtorizatsiya qilinmagan! Avval login qiling.")
        return
    me = await client.get_me()
    log.info("Userbot ulandi: {}".format(me.first_name))
    await client.run_until_disconnected()

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    db_init()
    log.info("ELDERPAY ishga tushmoqda...")
    await asyncio.gather(run_bot(), run_userbot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
