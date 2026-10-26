import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ============ تنظیمات: از Environment Variables های Vercel خوانده میشه ============
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL = os.environ["CHANNEL"]            # مثلا @my_channel
CHANNEL_LINK = os.environ["CHANNEL_LINK"]  # مثلا https://t.me/my_channel
DATABASE_URL = os.environ["DATABASE_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
# ==================================================================================

BOT_NAME = "Beigism Videos"
GRADES = {"12": "پایه دوازدهم", "11": "پایه یازدهم", "10": "پایه دهم"}
logging.basicConfig(level=logging.INFO)

# ---------------------------- دیتابیس ----------------------------
_conn = None


def _connect():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DATABASE_URL, autocommit=True)
        _conn.execute("""CREATE TABLE IF NOT EXISTS topics(
            id SERIAL PRIMARY KEY, grade TEXT, title TEXT)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS videos(
            id SERIAL PRIMARY KEY, topic_id INT, file_id TEXT, kind TEXT, caption TEXT)""")
        _conn.execute("CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY)")
        _conn.execute("CREATE TABLE IF NOT EXISTS states(user_id BIGINT PRIMARY KEY, data TEXT)")
    return _conn


def run(sql, params=(), fetch=None):
    global _conn
    for attempt in (1, 2):
        try:
            cur = _connect().execute(sql, params)
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            return None
        except psycopg.OperationalError:
            _conn = None
            if attempt == 2:
                raise


def st_get(uid):
    r = run("SELECT data FROM states WHERE user_id=%s", (uid,), "one")
    return json.loads(r[0]) if r else {}


def st_set(uid, **kw):
    run("INSERT INTO states VALUES(%s,%s) ON CONFLICT (user_id) DO UPDATE SET data=EXCLUDED.data",
        (uid, json.dumps(kw)))


def st_clear(uid):
    run("DELETE FROM states WHERE user_id=%s", (uid,))


# ---------------------------- کمکی‌ها ----------------------------
def is_admin(uid):
    if uid == OWNER_ID:
        return True
    return run("SELECT 1 FROM admins WHERE user_id=%s", (uid,), "one") is not None


async def is_member(context, uid):
    try:
        m = await context.bot.get_chat_member(CHANNEL, uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.OWNER)
    except Exception as e:
        logging.warning("membership check failed: %s", e)
        return False


def kb(rows):
    return InlineKeyboardMarkup(rows)


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def home_markup():
    return kb([[btn(GRADES[g], f"g:{g}")] for g in ("12", "11", "10")])


def join_markup():
    return kb([[InlineKeyboardButton("📢 عضویت در کانال", url=CHANNEL_LINK)],
               [btn("✅ عضو شدم", "chk")]])


JOIN_TEXT = f"به ربات {BOT_NAME} خوش اومدی 🎬\nبرای استفاده اول باید توی کانال عضو بشی 👇"


async def send_or_edit(update, text, markup):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            await q.message.reply_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


# ---------------------------- منوها ----------------------------
async def show_home(update, context):
    st_clear(update.effective_user.id)
    await send_or_edit(update, f"🎬 {BOT_NAME}\nپایه مورد نظرت رو انتخاب کن 👇", home_markup())


async def show_topics(update, context, grade):
    uid = update.effective_user.id
    topics = run("SELECT id,title FROM topics WHERE grade=%s ORDER BY id", (grade,), "all")
    rows = [[btn(t, f"t:{i}")] for i, t in topics]
    if is_admin(uid):
        rows.append([btn("➕ افزودن مبحث", f"addt:{grade}")])
    rows.append([btn("🔙 بازگشت", "home")])
    text = f"{GRADES[grade]}\nمبحث مورد نظر رو انتخاب کن 👇" if topics else f"{GRADES[grade]}\nهنوز مبحثی اضافه نشده."
    await send_or_edit(update, text, kb(rows))


async def send_topic(update, context, tid):
    uid = update.effective_user.id
    topic = run("SELECT grade,title FROM topics WHERE id=%s", (tid,), "one")
    msg = update.effective_message
    if not topic:
        await msg.reply_text("این مبحث وجود نداره.", reply_markup=home_markup())
        return
    grade, title = topic
    vids = run("SELECT file_id,kind,caption FROM videos WHERE topic_id=%s ORDER BY id", (tid,), "all")
    if not vids:
        await msg.reply_text("هنوز ویدیویی برای این مبحث آپلود نشده.")
    for file_id, kind, cap in vids:
        if kind == "video":
            await msg.reply_video(file_id, caption=cap or title)
        else:
            await msg.reply_document(file_id, caption=cap or title)
    rows = []
    if is_admin(uid):
        rows += [[btn("➕ افزودن ویدیو", f"addv:{tid}"), btn("🗑 حذف ویدیو", f"vlist:{tid}")],
                 [btn("✏️ تغییر نام مبحث", f"rent:{tid}"), btn("❌ حذف مبحث", f"delt:{tid}")]]
    rows.append([btn("🔙 بازگشت به مباحث", f"g:{grade}")])
    await msg.reply_text(f"📚 {title}", reply_markup=kb(rows))


# ---------------------------- دستورات ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_member(context, update.effective_user.id):
        await update.message.reply_text(JOIN_TEXT, reply_markup=join_markup())
        return
    await show_home(update, context)


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("استفاده: /addadmin 123456789")
        return
    run("INSERT INTO admins VALUES(%s) ON CONFLICT DO NOTHING", (int(context.args[0]),))
    await update.message.reply_text("✅ ادمین اضافه شد.")


async def del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("استفاده: /deladmin 123456789")
        return
    run("DELETE FROM admins WHERE user_id=%s", (int(context.args[0]),))
    await update.message.reply_text("✅ ادمین حذف شد.")


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    ids = [str(r[0]) for r in run("SELECT user_id FROM admins", (), "all")]
    await update.message.reply_text("ادمین‌ها:\n" + ("\n".join(ids) if ids else "هنوز ادمینی نیست"))


# ---------------------------- دکمه‌ها ----------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()

    if not await is_member(context, uid):
        await send_or_edit(update, JOIN_TEXT, join_markup())
        return

    if data in ("chk", "home"):
        await show_home(update, context)
    elif data.startswith("g:"):
        st_clear(uid)
        await show_topics(update, context, data[2:])
    elif data.startswith("t:"):
        st_clear(uid)
        await send_topic(update, context, int(data[2:]))
    else:
        action, _, rest = data.partition(":")
        if not is_admin(uid):
            await q.answer("دسترسی نداری ⛔", show_alert=True)
            return

        if action == "addt":
            st_set(uid, state="add_topic", grade=rest)
            await send_or_edit(update, "اسم مبحث جدید رو بفرست ✍️", kb([[btn("🔙 لغو", f"g:{rest}")]]))

        elif action == "addv":
            st_set(uid, state="add_video", tid=int(rest), count=0)
            await send_or_edit(update, "ویدیوها رو یکی یکی بفرست (می‌تونی کپشن هم بذاری).\nوقتی تموم شد «اتمام» رو بزن.",
                               kb([[btn("✅ اتمام", f"done:{rest}")]]))

        elif action == "done":
            n = st_get(uid).get("count", 0)
            st_clear(uid)
            await q.message.reply_text(f"✅ {n} ویدیو ذخیره شد.")
            await send_topic(update, context, int(rest))

        elif action == "rent":
            st_set(uid, state="rename_topic", tid=int(rest))
            await send_or_edit(update, "اسم جدید مبحث رو بفرست ✍️", kb([[btn("🔙 لغو", f"t:{rest}")]]))

        elif action == "delt":
            await send_or_edit(update, "مطمئنی؟ مبحث و همه ویدیوهاش حذف میشه.",
                               kb([[btn("بله، حذف کن", f"delt_ok:{rest}"), btn("نه", f"t:{rest}")]]))

        elif action == "delt_ok":
            tid = int(rest)
            row = run("SELECT grade FROM topics WHERE id=%s", (tid,), "one")
            run("DELETE FROM videos WHERE topic_id=%s", (tid,))
            run("DELETE FROM topics WHERE id=%s", (tid,))
            await show_topics(update, context, row[0] if row else "12")

        elif action == "vlist":
            tid = int(rest)
            vids = run("SELECT id,caption FROM videos WHERE topic_id=%s ORDER BY id", (tid,), "all")
            rows = [[btn(f"🗑 ویدیو {n}" + (f" - {(c or '')[:20]}" if c else ""), f"delv:{tid}:{vid}")]
                    for n, (vid, c) in enumerate(vids, 1)]
            rows.append([btn("🔙 بازگشت", f"t:{tid}")])
            await send_or_edit(update, "کدوم ویدیو حذف بشه؟ (به ترتیب ارسال)" if vids else "ویدیویی نیست.", kb(rows))

        elif action == "delv":
            tid, vid = rest.split(":")
            run("DELETE FROM videos WHERE id=%s", (int(vid),))
            await q.message.reply_text("🗑 ویدیو حذف شد.")
            await send_topic(update, context, int(tid))


# ---------------------------- پیام‌ها ----------------------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    s = st_get(uid)
    state = s.get("state")
    if not state:
        return
    msg = update.message

    if state == "add_topic" and msg.text:
        run("INSERT INTO topics(grade,title) VALUES(%s,%s)", (s["grade"], msg.text.strip()))
        st_clear(uid)
        await msg.reply_text("✅ مبحث اضافه شد.")
        await show_topics(update, context, s["grade"])

    elif state == "rename_topic" and msg.text:
        run("UPDATE topics SET title=%s WHERE id=%s", (msg.text.strip(), s["tid"]))
        st_clear(uid)
        await msg.reply_text("✅ اسم مبحث تغییر کرد.")
        await send_topic(update, context, s["tid"])

    elif state == "add_video":
        if msg.video:
            file_id, kind = msg.video.file_id, "video"
        elif msg.document:
            file_id, kind = msg.document.file_id, "document"
        else:
            return
        run("INSERT INTO videos(topic_id,file_id,kind,caption) VALUES(%s,%s,%s,%s)",
            (s["tid"], file_id, kind, msg.caption))
        s["count"] = s.get("count", 0) + 1
        st_set(uid, **s)
        await msg.reply_text(f"✅ ذخیره شد ({s['count']}). بعدی رو بفرست یا اتمام رو بزن.",
                             reply_markup=kb([[btn("✅ اتمام", f"done:{s['tid']}")]]))


# ---------------------------- Webhook ----------------------------
def build_app():
    app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("deladmin", del_admin))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.VIDEO | filters.Document.ALL, on_message))
    return app


async def process(data):
    app = build_app()
    async with app:
        await app.process_update(Update.de_json(data, app.bot))


class handler(BaseHTTPRequestHandler):
    def _reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if qs.get("setup", [None])[0] == WEBHOOK_SECRET:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host")
            params = urllib.parse.urlencode({
                "url": f"https://{host}/api",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
            try:
                with urllib.request.urlopen(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?{params}") as r:
                    self._reply(200, r.read().decode())
            except Exception as e:
                self._reply(500, f"error: {e}")
            return
        self._reply(200, f"{BOT_NAME} is running")

    def do_POST(self):
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self._reply(403, "forbidden")
            return
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        try:
            asyncio.run(process(data))
        except Exception:
            logging.exception("update failed")
        self._reply(200, "ok")
