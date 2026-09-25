import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DEFAULT_CHANNEL = os.getenv("DEFAULT_CHANNEL", "").strip()
AUTO_PACKS = [p.strip() for p in os.getenv("AUTO_PACKS", "").split(",") if p.strip()]
REQ_CHANNEL_ENV = os.getenv("REQ_CHANNEL", "").strip()
DB_PATH = os.getenv("DB_PATH", "./bot_data.db")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO)
log = logging.getLogger("premium-bot")

HTML = ParseMode.HTML
PER_PAGE = 20
_lock = threading.Lock()

BOT_USERNAME = ""
UI_E = {}
REQ = {"id": None, "title": None, "invite": None}

UI_CHAR_MAP = {
    "👋": "wave", "🧩": "browse", "✍️": "draft", "✍": "draft",
    "➕": "addpack", "📦": "packs", "📄": "tpls", "⭐": "favs",
    "📢": "channels", "⏰": "sched", "📊": "stats", "❓": "help",
    "🏠": "home", "📤": "send", "🖼": "media", "💾": "save",
    "🗑": "clear", "🔍": "search", "📋": "list", "✅": "check",
    "❌": "cross", "🔥": "fire", "❤️": "heart", "❤": "heart",
    "💀": "skull", "🦇": "bat", "🎉": "party", "💎": "gem",
    "👑": "crown", "💯": "hundred", "🌟": "star2", "⚡": "bolt",
    "🎬": "video", "👁": "eye", "✏️": "edit", "✏": "edit",
    "⚙️": "gear", "⚙": "gear", "🚀": "rocket", "🤖": "bot",
    "🕐": "clock", "🎨": "paint", "🤔": "think", "🥳": "party",
}

EMOJI_KEYWORDS = [
    (["تحذير", "خلي بالك", "انتبه", "احذر"], ["cross", "eye"]),
    (["ممنوع", "خطر"], ["cross", "bolt"]),
    (["مهم", "ضروري", "أساسي", "لازم"], ["star2", "fire"]),
    (["جديد", "حصري", "أول مرة"], ["rocket", "bolt"]),
    (["استمتع", "استمتعوا", "مرح", "فرح"], ["party", "star2"]),
    (["هدية", "جايزة", "جائزة", "ربح", "فلوس"], ["gem", "hundred"]),
    (["ملك", "تاج", "VIP", "vip", "أمير"], ["crown"]),
    (["قلب", "حب", "عشق"], ["heart"]),
    (["نار", "قوي", "أقوى", "ناري"], ["fire", "bolt"]),
    (["سريع", "سرعة", "بسرعة"], ["rocket", "bolt"]),
    (["اشترك", "قناة", "اشتراك"], ["channels"]),
    (["رابط", "لينك"], ["channels"]),
    (["مبروك", "تهانينا", "هنيئا"], ["party", "star2"]),
    (["شكرًا", "شكرا", "تسلم", "مشكور"], ["heart", "star2"]),
    (["تم", "خلص", "انتهى", "جاهز"], ["check"]),
    (["مجانًا", "مجانا", "ببلاش", "فري"], ["hundred", "gem"]),
    (["عرض", "خصم", "تخفيض", "خصومات"], ["gem", "hundred"]),
    (["مليون", "ألف", "كثير", "كتير"], ["hundred"]),
    (["جميل", "حلو", "رائع", "تحفة"], ["star2", "heart"]),
    (["الله", "الحمد", "سبحان"], ["star2"]),
    (["رمضان", "عيد", "مبارك"], ["party", "star2"]),
]


# ==================== HEALTH ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"VANTA awake")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_web_server():
    try:
        srv = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("health server on port %d", PORT)
    except Exception as e:
        log.warning("web server failed: %s", e)


# ==================== DB ====================
class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with _lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, title TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS emojis (
                num INTEGER PRIMARY KEY AUTOINCREMENT,
                char TEXT, doc_id TEXT UNIQUE, pack_id INTEGER,
                uses INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 0,
                name TEXT, body TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS draft (
                user_id INTEGER PRIMARY KEY, body TEXT DEFAULT '',
                media_type TEXT, media_file_id TEXT, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 0,
                chat_id TEXT, title TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 0,
                chat_ids TEXT, body TEXT, run_at INTEGER, job_id TEXT);
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER, num INTEGER,
                PRIMARY KEY (user_id, num));
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT,
                first_name TEXT, joined_at INTEGER);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS recent (
                user_id INTEGER, num INTEGER, used_at INTEGER,
                PRIMARY KEY (user_id, num));
            """)
            for tbl, col, ddl in [
                ("templates", "user_id", "ALTER TABLE templates ADD COLUMN user_id INTEGER DEFAULT 0"),
                ("channels",  "user_id", "ALTER TABLE channels  ADD COLUMN user_id INTEGER DEFAULT 0"),
                ("scheduled", "user_id", "ALTER TABLE scheduled ADD COLUMN user_id INTEGER DEFAULT 0"),
            ]:
                try:
                    self.conn.execute(ddl)
                except Exception:
                    pass
            try:
                old = self.conn.execute("SELECT num FROM emojis WHERE favorite=1").fetchall()
                for r in old:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO favorites(user_id,num) VALUES(?,?)",
                        (OWNER_ID, r["num"]))
            except Exception:
                pass
            self.conn.execute("UPDATE channels  SET user_id=? WHERE user_id=0", (OWNER_ID,))
            self.conn.execute("UPDATE templates SET user_id=? WHERE user_id=0", (OWNER_ID,))
            self.conn.commit()

    def ex(self, sql, p=()):
        with _lock:
            c = self.conn.execute(sql, p)
            self.conn.commit()
            return c

    def q(self, sql, p=()):
        with _lock:
            return self.conn.execute(sql, p).fetchall()

    def q1(self, sql, p=()):
        with _lock:
            return self.conn.execute(sql, p).fetchone()

    # ---- settings ----
    def get_setting(self, key, default=None):
        r = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.ex(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    def del_setting(self, key):
        self.ex("DELETE FROM settings WHERE key=?", (key,))

    # ---- users ----
    def touch_user(self, uid, username, first_name):
        self.ex(
            "INSERT INTO users(user_id,username,first_name,joined_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name",
            (uid, username or "", first_name or "", int(time.time())))
    def count_users(self):
        return self.q1("SELECT COUNT(*) n FROM users")["n"]

    # ---- packs ----
    def add_pack(self, name, title):
        r = self.q1("SELECT id FROM packs WHERE name=?", (name,))
        if r:
            return r["id"]
        return self.ex(
            "INSERT INTO packs(name,title,created_at) VALUES(?,?,?)",
            (name, title, int(time.time()))).lastrowid
    def list_packs(self): return self.q("SELECT * FROM packs ORDER BY id")
    def get_pack(self, pid): return self.q1("SELECT * FROM packs WHERE id=?", (pid,))
    def count_pack(self, pid):
        return self.q1("SELECT COUNT(*) n FROM emojis WHERE pack_id=?", (pid,))["n"]

    # ---- emojis ----
    def add_emoji(self, char, doc_id, pack_id):
        r = self.q1("SELECT num FROM emojis WHERE doc_id=?", (doc_id,))
        if r:
            return r["num"], False
        c = self.ex(
            "INSERT INTO emojis(char,doc_id,pack_id) VALUES(?,?,?)",
            (char, doc_id, pack_id))
        return c.lastrowid, True
    def get_emoji(self, num):
        return self.q1("SELECT * FROM emojis WHERE num=?", (str(num),))
    def bump_use(self, num):
        self.ex("UPDATE emojis SET uses=uses+1 WHERE num=?", (str(num),))
    def count_emojis(self, pid=None, search=None):
        if pid and search:
            return self.q1(
                "SELECT COUNT(*) n FROM emojis WHERE pack_id=? AND char LIKE ?",
                (pid, f"%{search}%"))["n"]
        if pid:
            return self.q1("SELECT COUNT(*) n FROM emojis WHERE pack_id=?", (pid,))["n"]
        if search:
            return self.q1("SELECT COUNT(*) n FROM emojis WHERE char LIKE ?",
                           (f"%{search}%",))["n"]
        return self.q1("SELECT COUNT(*) n FROM emojis")["n"]
    def list_emojis(self, pid=None, search=None, page=0, per=20):
        off = page * per
        if pid and search:
            return self.q(
                "SELECT * FROM emojis WHERE pack_id=? AND char LIKE ? "
                "ORDER BY num LIMIT ? OFFSET ?", (pid, f"%{search}%", per, off))
        if pid:
            return self.q(
                "SELECT * FROM emojis WHERE pack_id=? ORDER BY num LIMIT ? OFFSET ?",
                (pid, per, off))
        if search:
            return self.q(
                "SELECT * FROM emojis WHERE char LIKE ? ORDER BY num LIMIT ? OFFSET ?",
                (f"%{search}%", per, off))
        return self.q(
            "SELECT * FROM emojis ORDER BY num LIMIT ? OFFSET ?", (per, off))
    def top_used(self, n=10):
        return self.q(
            "SELECT * FROM emojis WHERE uses>0 ORDER BY uses DESC LIMIT ?", (n,))

    # ---- recent ----
    def add_recent(self, uid, num):
        self.ex(
            "INSERT INTO recent(user_id,num,used_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id,num) DO UPDATE SET used_at=excluded.used_at",
            (uid, str(num), int(time.time())))

    def list_recent(self, uid, n=20):
        return self.q(
            "SELECT e.* FROM emojis e "
            "JOIN recent r ON r.num = CAST(e.num AS TEXT) "
            "WHERE r.user_id=? ORDER BY r.used_at DESC LIMIT ?",
            (uid, n))

    # ---- favorites ----
    def toggle_fav(self, uid, num):
        r = self.q1("SELECT 1 FROM favorites WHERE user_id=? AND num=?",
                    (uid, str(num)))
        if r:
            self.ex("DELETE FROM favorites WHERE user_id=? AND num=?",
                    (uid, str(num)))
            return False
        self.ex("INSERT INTO favorites(user_id,num) VALUES(?,?)",
                (uid, str(num)))
        return True
    def list_favs(self, uid):
        return self.q(
            "SELECT e.* FROM emojis e "
            "JOIN favorites f ON f.num = CAST(e.num AS TEXT) "
            "WHERE f.user_id=? ORDER BY e.num", (uid,))

    # ---- draft ----
    def get_draft(self, uid):
        r = self.q1("SELECT * FROM draft WHERE user_id=?", (uid,))
        if not r:
            self.ex("INSERT INTO draft(user_id,body,updated_at) VALUES(?,?,?)",
                    (uid, "", int(time.time())))
            r = self.q1("SELECT * FROM draft WHERE user_id=?", (uid,))
        return r
    def set_draft(self, uid, body):
        self.get_draft(uid)
        self.ex("UPDATE draft SET body=?,updated_at=? WHERE user_id=?",
                (body, int(time.time()), uid))
    def set_media(self, uid, mt, fid):
        self.get_draft(uid)
        self.ex("UPDATE draft SET media_type=?,media_file_id=?,updated_at=? "
                "WHERE user_id=?", (mt, fid, int(time.time()), uid))
    def clear_draft(self, uid):
        self.get_draft(uid)
        self.ex("UPDATE draft SET body='',media_type=NULL,media_file_id=NULL "
                "WHERE user_id=?", (uid,))

    # ---- templates ----
    def save_tpl(self, uid, name, body):
        r = self.q1("SELECT id FROM templates WHERE user_id=? AND name=?",
                    (uid, name))
        if r:
            return False
        self.ex("INSERT INTO templates(user_id,name,body,created_at) "
                "VALUES(?,?,?,?)", (uid, name, body, int(time.time())))
        return True
    def list_tpls(self, uid):
        return self.q("SELECT * FROM templates WHERE user_id=? ORDER BY id DESC",
                      (uid,))
    def get_tpl(self, uid, tid):
        return self.q1("SELECT * FROM templates WHERE id=? AND user_id=?",
                       (tid, uid))
    def del_tpl(self, uid, tid):
        self.ex("DELETE FROM templates WHERE id=? AND user_id=?", (tid, uid))

    # ---- channels ----
    def add_channel(self, uid, cid, title):
        r = self.q1("SELECT id FROM channels WHERE user_id=? AND chat_id=?",
                    (uid, str(cid)))
        if r:
            return False
        self.ex("INSERT INTO channels(user_id,chat_id,title,created_at) "
                "VALUES(?,?,?,?)", (uid, str(cid), title, int(time.time())))
        return True
    def list_channels(self, uid):
        return self.q("SELECT * FROM channels WHERE user_id=? ORDER BY id",
                      (uid,))
    def get_channel(self, uid, rid):
        return self.q1("SELECT * FROM channels WHERE id=? AND user_id=?",
                       (rid, uid))
    def del_channel(self, uid, rid):
        self.ex("DELETE FROM channels WHERE id=? AND user_id=?", (rid, uid))

    # ---- scheduled ----
    def add_sched(self, uid, ids, body, run_at, job_id=None):
        return self.ex(
            "INSERT INTO scheduled(user_id,chat_ids,body,run_at,job_id) "
            "VALUES(?,?,?,?,?)", (uid, ids, body, run_at, job_id)).lastrowid
    def set_job(self, sid, jid):
        self.ex("UPDATE scheduled SET job_id=? WHERE id=?", (jid, sid))
    def list_sched(self, uid):
        return self.q("SELECT * FROM scheduled WHERE user_id=? AND run_at > ? "
                      "ORDER BY run_at", (uid, int(time.time())))
    def get_sched(self, sid):
        return self.q1("SELECT * FROM scheduled WHERE id=?", (sid,))
    def del_sched(self, sid):
        self.ex("DELETE FROM scheduled WHERE id=?", (sid,))


db = DB(DB_PATH)


# ==================== HELPERS ====================
def esc(s):
    return html.escape(s or "", quote=False)


def ui(key, fallback=""):
    item = UI_E.get(key)
    if item:
        return f'<tg-emoji emoji-id="{item["id"]}">{esc(item["char"])}</tg-emoji>'
    return fallback


def is_owner(uid):
    return OWNER_ID and uid == OWNER_ID


def owner_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not is_owner(u.id):
            if update.callback_query:
                await update.callback_query.answer(
                    "للمالك فقط.", show_alert=True)
            return
        return await fn(update, ctx)
    return w


async def build_ui_emojis():
    global UI_E
    rows = db.q("SELECT doc_id, char FROM emojis")
    for r in rows:
        k = UI_CHAR_MAP.get(r["char"])
        if k and k not in UI_E:
            UI_E[k] = {"id": r["doc_id"], "char": r["char"]}
    log.info("UI emojis loaded: %d", len(UI_E))


# ==================== REQUIRED CHANNEL ====================
async def refresh_req_channel(bot):
    cid = db.get_setting("require_channel") or REQ_CHANNEL_ENV or None
    REQ["id"] = cid
    REQ["title"] = None
    REQ["invite"] = None
    if not cid:
        return
    try:
        chat = await bot.get_chat(cid)
        REQ["title"] = chat.title or chat.username or str(cid)
        invite = chat.invite_link
        if not invite and chat.username:
            invite = f"https://t.me/{chat.username}"
        if not invite:
            try:
                invite = await bot.export_chat_invite_link(cid)
            except TelegramError:
                invite = None
        REQ["invite"] = invite
        log.info("required channel: %s (%s)", REQ["title"], cid)
    except TelegramError as e:
        log.warning("req channel fetch failed: %s", e)


async def is_subscribed(bot, uid):
    if not REQ["id"]:
        return True
    if is_owner(uid):
        return True
    try:
        m = await bot.get_chat_member(REQ["id"], uid)
        status = getattr(m.status, "value", str(m.status))
        return status not in ("left", "kicked")
    except TelegramError as e:
        log.warning("sub check %s: %s", uid, e)
        return False


async def require_sub(update, ctx):
    u = update.effective_user
    if not u or is_owner(u.id):
        return True
    if not REQ["id"]:
        return True
    if await is_subscribed(ctx.bot, u.id):
        return True

    kb = None
    if REQ["invite"]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 اشترك في القناة", url=REQ["invite"])]])
    title = REQ["title"] or REQ["id"]
    txt = (
        f"{ui('channels', '📢')} <b>لازم تشترك في القناة الأول</b>\n\n"
        f"القناة: <b>{esc(title)}</b>\n\n"
        f"<i>اشترك، وبعدها اضغط /start تاني.</i>"
    )
    try:
        if update.callback_query:
            await update.callback_query.answer(
                "لازم تشترك في القناة الأول!", show_alert=True)
        elif update.message:
            await update.message.reply_text(
                txt, parse_mode=HTML, reply_markup=kb)
    except Exception:
        pass
    return False


def gated(fn):
    async def w(update, ctx):
        if not await require_sub(update, ctx):
            return
        return await fn(update, ctx)
    return w


# ==================== COMPOSE ====================
TOKEN_RE = re.compile(r"\{(\d+)\}|\[\[([^\]|]+)\|([^\]]+)\]\]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def nd(s):
    return s.translate(DIGITS)


def u16len(s):
    return len(s.encode("utf-16-le")) // 2


def u16char(t, off):
    c = 0
    for i, ch in enumerate(t):
        if c >= off:
            return i
        c += u16len(ch)
    return len(t)


def _format_segments(text):
    out, entities, cur = [], [], 0
    pattern = (
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)"
        r"|\*([^*\n]+)\*"
        r"|(?<![\w])_([^_\n]+)_(?![\w])"
        r"|~([^~\n]+)~"
        r"|\|\|([^|\n]+)\|\|"
    )
    for m in re.finditer(pattern, text):
        out.append(text[cur:m.start()])
        base = u16len("".join(out))
        if m.group(1):
            content, url, typ = m.group(1), m.group(2), "text_link"
        elif m.group(3):
            content, url, typ = m.group(3), None, "bold"
        elif m.group(4):
            content, url, typ = m.group(4), None, "italic"
        elif m.group(5):
            content, url, typ = m.group(5), None, "strikethrough"
        else:
            content, url, typ = m.group(6), None, "spoiler"
        out.append(content)
        ent = MessageEntity(type=typ, offset=base, length=u16len(content))
        if url:
            ent.url = url
        entities.append(ent)
        cur = m.end()
    out.append(text[cur:])
    return "".join(out), entities


def render(body, lookup):
    parts, entities, buttons, missing, used = [], [], [], [], []

    def flush(seg):
        if not seg:
            return
        plain, ents = _format_segments(seg)
        base = u16len("".join(parts))
        parts.append(plain)
        for e in ents:
            e.offset += base
            entities.append(e)

    cur = 0
    for m in TOKEN_RE.finditer(body):
        flush(body[cur:m.start()])
        if m.group(1):
            num = nd(m.group(1))
            item = lookup(num)
            if item:
                base = u16len("".join(parts))
                parts.append(item["char"])
                entities.append(MessageEntity(
                    type="custom_emoji", offset=base,
                    length=u16len(item["char"]),
                    custom_emoji_id=item["doc_id"]))
                used.append(num)
            else:
                missing.append(num)
                flush(m.group(0))
        else:
            buttons.append((m.group(2), m.group(3)))
        cur = m.end()
    flush(body[cur:])
    return "".join(parts), entities, buttons, missing, used


def kb_buttons(bs):
    if not bs:
        return None
    rows, row = [], []
    for label, url in bs[:20]:
        row.append(InlineKeyboardButton(label[:40], url=url))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def extract_emoji(msg):
    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    out = []
    for e in ents:
        if e.type == "custom_emoji":
            s = u16char(text, e.offset)
            en = u16char(text, e.offset + e.length)
            out.append((text[s:en], e.custom_emoji_id))
    return out


def clean_pack_name(s):
    s = s.strip().rstrip("/")
    return s.split("/")[-1] if "/" in s else s


def _find_num_for_key(key, exclude=()):
    item = UI_E.get(key)
    if not item:
        return None
    row = db.q1("SELECT num FROM emojis WHERE doc_id=?", (item["id"],))
    if not row or str(row["num"]) in exclude:
        return None
    return row["num"]


def auto_format(text):
    """ينسّق النص: فواصل، بولد على العناوين، وإيموجي بريميوم مناسب."""
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    out, used = [], set()
    for line in lines:
        raw = line.strip()
        if not raw:
            out.append("")
            continue

        prefix = ""
        for words, keys in EMOJI_KEYWORDS:
            if any(w in raw for w in words):
                for k in keys:
                    if k in used:
                        continue
                    n = _find_num_for_key(k)
                    if n is not None:
                        prefix = f"{{{n}}} "
                        used.add(k)
                        break
                if prefix:
                    break

        # short lines or lines ending with : or ؟ → bold
        if len(raw) < 25 or raw.endswith(":") or raw.endswith("؟"):
            body = f"*{raw}*"
        else:
            body = raw

        out.append(prefix + body)

    result = "\n".join(out)
    # collapse 3+ newlines to 2
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ==================== KEYBOARDS ====================
def kb_main(uid):
    is_own = is_owner(uid)
    rows = []
    if is_own:
        rows.append([InlineKeyboardButton(
            "➕ ضيف حزمة", callback_data="m:addpack")])
    rows += [
        [InlineKeyboardButton("🧩 تصفّح الإيموجي", callback_data="m:browse"),
         InlineKeyboardButton("🕐 آخر المستخدم", callback_data="m:recent")],
        [InlineKeyboardButton("✍️ مسودتي", callback_data="m:draft"),
         InlineKeyboardButton("🎨 تنسيق النص", callback_data="m:format")],
        [InlineKeyboardButton("📦 الحزم", callback_data="m:packs"),
         InlineKeyboardButton("⭐ المفضلة", callback_data="m:favs")],
        [InlineKeyboardButton("📄 قوالي", callback_data="m:tpls"),
         InlineKeyboardButton("📢 قنواتي", callback_data="m:channels")],
        [InlineKeyboardButton("⏰ المجدولة", callback_data="m:sched"),
         InlineKeyboardButton("❓ مساعدة", callback_data="m:help")],
    ]
    if BOT_USERNAME:
        rows.append([InlineKeyboardButton(
            "🤖 ضيفني لقناتك",
            url=f"https://t.me/{BOT_USERNAME}?startchannel=true"
                f"&admin=post_messages+edit_messages+delete_messages")])
    if is_own:
        rows.append([InlineKeyboardButton(
            "⚙️ الإعدادات", callback_data="m:settings")])
    return InlineKeyboardMarkup(rows)


def kb_home():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 القائمة", callback_data="m:home")]])


def kb_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 قناة الاشتراك الإجباري",
                              callback_data="set:req")],
        [InlineKeyboardButton("🗑 إلغاء الاشتراك الإجباري",
                              callback_data="set:unreq")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="m:stats")],
        [InlineKeyboardButton("🏠 القائمة", callback_data="m:home")],
    ])


def kb_grid(items, pid, page, pages, search=None):
    rows, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(
            f'{it["char"]} {it["num"]}',
            callback_data=f'p:{it["num"]}'))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    s = search or ""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️", callback_data=f"b:{pid}:{page-1}:{s}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "▶️", callback_data=f"b:{pid}:{page+1}:{s}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔍 بحث", callback_data=f"s:{pid}"),
        InlineKeyboardButton("📋 الأرقام", callback_data=f"ids:{pid}:{s}"),
    ])
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def kb_draft():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 معاينة", callback_data="d:prev"),
         InlineKeyboardButton("📤 إرسال", callback_data="d:send")],
        [InlineKeyboardButton("🎨 تنسيق تلقائي", callback_data="d:fmt"),
         InlineKeyboardButton("🖼 ميديا", callback_data="d:media")],
        [InlineKeyboardButton("💾 قالب", callback_data="d:save"),
         InlineKeyboardButton("⏰ جدولة", callback_data="d:sched")],
        [InlineKeyboardButton("🗑 مسح", callback_data="d:clr"),
         InlineKeyboardButton("🏠 القائمة", callback_data="m:home")],
    ])


def kb_preview():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 انشر الآن", callback_data="d:send")],
        [InlineKeyboardButton("✏️ تعديل", callback_data="d:edit"),
         InlineKeyboardButton("🎨 تنسيق", callback_data="d:fmt")],
        [InlineKeyboardButton("💾 قالب", callback_data="d:save"),
         InlineKeyboardButton("⏰ جدولة", callback_data="d:sched")],
        [InlineKeyboardButton("🗑 مسح", callback_data="d:clr"),
         InlineKeyboardButton("🏠 القائمة", callback_data="m:home")],
    ])


def kb_channels(rows, prefix="send"):
    kb = [[InlineKeyboardButton(
        f"📢 {(r['title'] or r['chat_id'])[:40]}",
        callback_data=f"{prefix}:{r['id']}")] for r in rows]
    kb.append([
        InlineKeyboardButton("➕ إضافة", callback_data="m:addch"),
        InlineKeyboardButton("🗑 مسح", callback_data="m:delch")])
    kb.append([InlineKeyboardButton("🏠 القائمة", callback_data="m:home")])
    return InlineKeyboardMarkup(kb)


def kb_join():
    if not BOT_USERNAME:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🤖 ضيفني لقناتك",
            url=f"https://t.me/{BOT_USERNAME}?startchannel=true"
                f"&admin=post_messages+edit_messages+delete_messages")],
        [InlineKeyboardButton(
            "👥 ضيفني لجروب",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true")]])


# ==================== PACK LOADING ====================
async def load_pack_from_bot(bot, name):
    st = await bot.get_sticker_set(clean_pack_name(name))
    kind = getattr(st.sticker_type, "value", st.sticker_type)
    if kind != "custom_emoji":
        return 0
    pid = db.add_pack(st.name, st.title)
    added = 0
    for s in st.stickers:
        if s.custom_emoji_id:
            _, new = db.add_emoji(s.emoji, s.custom_emoji_id, pid)
            if new:
                added += 1
    return added


# ==================== COMMANDS ====================
@gated
async def c_start(update, ctx):
    u = update.effective_user
    if not u:
        return
    db.touch_user(u.id, u.username, u.first_name)
    name = esc(u.first_name or "يا صاحبي")
    txt = (
        f"{ui('bat', '🦇')} <b>أهلاً {name}!</b>\n\n"
        f"{ui('browse', '🧩')} <b>تصفّح الإيموجي</b> — دوس على أي إيموجي يطلعلك رقمه\n"
        f"{ui('clock', '🕐')} <b>آخر المستخدم</b> — الإيموجي اللي استعملتها مؤخراً\n"
        f"{ui('paint', '🎨')} <b>تنسيق النص</b> — ابعت كلام مبعثر وأنا أنسّقه\n"
        f"{ui('draft', '✍️')} <b>مسودتي</b> — اكتب منشورك بالنص اللي جاهز\n"
        f"{ui('send', '📤')} <b>انشر</b> — على قناتك بضغطة\n\n"
        f"<i>أول حاجة: اضغط 🤖 ضيفني لقناتك.</i>"
    )
    try:
        await update.message.reply_text(
            txt, parse_mode=HTML, reply_markup=kb_main(u.id))
    except TelegramError as e:
        log.warning("start err: %s", e)


@gated
async def c_help(update, ctx):
    await update.message.reply_text(
        f"{ui('help', '❓')} <b>المساعدة</b>\n\n"
        "<b>الخطوات:</b>\n"
        "1. اضغط 🤖 ضيفني لقناتك → خلّي البوت أدمن\n"
        "2. اضغط 🧩 تصفّح → دوس على إيموجي يطلعلك رقمه\n"
        "3. ابعت منشورك بالأرقام:\n"
        "   <code>استمتعوا {5} {12} {30}</code>\n"
        "4. البوت يرجّعلك المعاينة + زر 📤 انشر الآن\n\n"
        "<b>🎨 تنسيق تلقائي:</b>\n"
        "ابعت كلام مبعثر → البوت ينسّقه، يحط بولد على العناوين، "
        "ويحط إيموجي بريميوم مناسب لكل جزء.\n\n"
        "<b>الكتابة:</b>\n"
        "• <code>{62}</code> = إيموجي رقم 62\n"
        "• <code>*bold*</code> • <code>_italic_</code> • <code>~strike~</code>\n"
        "• <code>||spoiler||</code>\n"
        "• <code>[نص](url)</code> — لينك\n"
        "• <code>[[زر|url]]</code> — زر شفاف\n\n"
        "<b>أوامر:</b>\n"
        "/format — تنسيق نص\n"
        "/recent — آخر المستخدم\n"
        "/browse /draft /send /addme",
        parse_mode=HTML, reply_markup=kb_home())


@gated
async def c_addme(update, ctx):
    kb = kb_join()
    if not kb:
        return await update.message.reply_text("حاول تاني بعد شوية.")
    await update.message.reply_text(
        f"{ui('bot', '🤖')} <b>ضيفني لقناتك</b>\n\n"
        "1. اضغط الزر تحت\n"
        "2. اختار قناتك\n"
        "3. خلّي البوت <b>أدمن</b>\n"
        "4. خلاص — البوت هيتضاف تلقائي في قنواتك",
        parse_mode=HTML, reply_markup=kb)


@owner_only
async def c_setchannel(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "reqchannel"
        return await update.message.reply_text(
            "ابعت @username أو -100... للقناة اللي عايز الناس تشترك فيها.")
    await do_setreq(update.message, ctx, ctx.args[0])


async def do_setreq(msg, ctx, raw):
    cid = raw.strip()
    try:
        chat = await ctx.bot.get_chat(cid)
    except TelegramError as e:
        return await msg.reply_text(f"❌ {e}")
    db.set_setting("require_channel", str(chat.id))
    await refresh_req_channel(ctx.bot)
    title = REQ["title"] or chat.title or cid
    await msg.reply_text(
        f"{ui('check', '✅')} تم. الاشتراك الإجباري في:\n"
        f"<b>{esc(title)}</b>", parse_mode=HTML)


@owner_only
async def c_unsetchannel(update, ctx):
    db.del_setting("require_channel")
    REQ["id"] = None
    REQ["title"] = None
    REQ["invite"] = None
    await update.message.reply_text(
        f"{ui('check', '✅')} اتلغى الاشتراك الإجباري — البوت متاح للكل الآن.",
        parse_mode=HTML)


async def c_addpack(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "pack"
        return await update.message.reply_text(
            f"{ui('addpack', '➕')} ابعتلي اسم الحزمة أو رابط addemoji.")
    try:
        added = await load_pack_from_bot(ctx.bot, " ".join(ctx.args))
    except TelegramError as e:
        return await update.message.reply_text(f"❌ {e}")
    if added:
        await build_ui_emojis()
        await update.message.reply_text(
            f"{ui('check', '✅')} انضاف <b>{added}</b> إيموجي للكل.",
            parse_mode=HTML)
    else:
        await update.message.reply_text("⚠️ مفيش إيموجي جديد.")


@gated
async def c_browse(update, ctx):
    pid = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    await show_browse(update.message, ctx, pid, 0, "")


async def show_browse(target, ctx, pid, page, search):
    total = db.count_emojis(pid, search or None)
    if total == 0:
        return await target.reply_text(
            "مفيش إيموجي هنا.", reply_markup=kb_home())
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = db.list_emojis(pid, search or None, page, PER_PAGE)
    title = "كل الإيموجي"
    if pid:
        p = db.get_pack(pid)
        title = esc(p["title"]) if p else "?"
    extra = f" — بحث: <b>{esc(search)}</b>" if search else ""
    await target.reply_text(
        f"{ui('browse', '🧩')} <b>{title}</b>{extra}\n"
        f"{ui('list', '📋')} {total} إيموجي — صفحة {page+1}/{pages}\n"
        f"<i>دوس على إيموجي يطلعلك رقمه للنسخ.</i>",
        parse_mode=HTML,
        reply_markup=kb_grid(items, pid or 0, page, pages, search or None))


@gated
async def c_recent(update, ctx):
    uid = update.effective_user.id
    rows = db.list_recent(uid, 20)
    if not rows:
        return await update.message.reply_text(
            f"{ui('think', '🤔')} لسه مستخدمتش أي إيموجي.\n"
            "تصفّح الإيموجي واستخدم بعضهم الأول.")
    lines = [f"{ui('clock', '🕐')} <b>آخر ما استخدمته:</b>\n"]
    for r in rows:
        lines.append(
            f'<tg-emoji emoji-id="{r["doc_id"]}">{esc(r["char"])}</tg-emoji> '
            f'<code>{{{r["num"]}}}</code>')
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_format(update, ctx):
    ctx.user_data["s"] = "format"
    await update.message.reply_text(
        f"{ui('paint', '🎨')} <b>ابعتلي النص بتاعك</b>\n\n"
        "<i>هننسقه: فواصل + بولد على العناوين + إيموجي بريميوم مناسب لكل جزء.</i>",
        parse_mode=HTML, reply_markup=kb_home())


async def do_format(msg, ctx, raw_text):
    formatted = auto_format(raw_text)
    db.set_draft(msg.from_user.id, formatted)
    if "{" in formatted and "}" in formatted:
        await show_preview(msg, ctx)
        await msg.reply_text(
            f"{ui('edit', '✏️')} <i>لو عايز تعدّل: /draft</i>",
            parse_mode=HTML)
    else:
        await msg.reply_text(
            f"{ui('think', '🤔')} النص اتنّسق، بس مفيهوش إيموجي بريميوم.\n"
            f"تصفّح الإيموجي ({ui('browse', '🧩')}) وحط أرقامهم في النص.",
            parse_mode=HTML, reply_markup=kb_draft())


@gated
async def c_packs(update, ctx):
    rows = db.list_packs()
    if not rows:
        return await update.message.reply_text("مفيش حزم بعد.")
    lines = [f"{ui('packs', '📦')} <b>الحزم:</b>\n"]
    for p in rows:
        lines.append(
            f"• <code>{p['id']}</code> — {esc(p['title'])} "
            f"({db.count_pack(p['id'])})")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_draft(update, ctx):
    d = db.get_draft(update.effective_user.id)
    body = d["body"] or ""
    shown = (body[:3000] + "…") if len(body) > 3000 else body
    media = ""
    if d["media_type"]:
        media = f"\n{ui('media', '🖼')} فيه ميديا محفوظة"
    await update.message.reply_text(
        f"{ui('draft', '✍️')} <b>مسودتك:</b>{media}\n\n"
        f"<code>{esc(shown) or '(فاضية)'}</code>",
        parse_mode=HTML, reply_markup=kb_draft())


@gated
async def c_new(update, ctx):
    db.clear_draft(update.effective_user.id)
    await update.message.reply_text(
        f"{ui('draft', '✍️')} مسودة جديدة. ابعتلي النص.",
        parse_mode=HTML, reply_markup=kb_draft())


@gated
async def c_send(update, ctx):
    uid = update.effective_user.id
    chs = db.list_channels(uid)
    if not chs:
        if DEFAULT_CHANNEL and is_owner(uid):
            return await do_send(update.message, ctx, DEFAULT_CHANNEL)
        kb = kb_join()
        return await update.message.reply_text(
            f"{ui('channels', '📢')} مفيش قنوات مضافة.\n"
            "اضغط الزر تحت، أو /addchannel",
            parse_mode=HTML, reply_markup=kb)
    if len(chs) == 1:
        return await do_send(update.message, ctx, chs[0]["chat_id"])
    await update.message.reply_text(
        f"{ui('send', '📤')} اختار قناة:", reply_markup=kb_channels(chs))


async def do_send(msg, ctx, chat_id):
    uid = msg.from_user.id
    d = db.get_draft(uid)
    body = d["body"] or ""
    if not body.strip():
        return await msg.reply_text("مسودتك فاضية.")
    plain, ents, btns, _, used = render(body, db.get_emoji)
    for n in used:
        db.bump_use(n)
        db.add_recent(uid, n)
    kb = kb_buttons(btns)
    kw = {}
    if kb:
        kw["reply_markup"] = kb
    try:
        if d["media_type"] == "photo":
            await ctx.bot.send_photo(
                chat_id, d["media_file_id"],
                caption=plain, caption_entities=ents, **kw)
        elif d["media_type"] == "video":
            await ctx.bot.send_video(
                chat_id, d["media_file_id"],
                caption=plain, caption_entities=ents, **kw)
        else:
            await ctx.bot.send_message(chat_id, plain, entities=ents, **kw)
        await msg.reply_text(
            f"{ui('check', '✅')} <b>اتنشر على القناة بنجاح.</b>",
            parse_mode=HTML)
    except TelegramError as e:
        await msg.reply_text(f"❌ فشل النشر: {e}")


async def show_preview(target, ctx):
    uid = target.from_user.id
    d = db.get_draft(uid)
    body = d["body"] or ""
    if not body.strip():
        return await target.reply_text("مسودتك فاضية.")
    plain, ents, btns, missing, used = render(body, db.get_emoji)
    for n in used:
        db.bump_use(n)
        db.add_recent(uid, n)
    kb = kb_buttons(btns)
    kw = {}
    if kb:
        kw["reply_markup"] = kb
    try:
        if d["media_type"] == "photo":
            await target.reply_photo(
                d["media_file_id"], caption=plain,
                caption_entities=ents, **kw)
        elif d["media_type"] == "video":
            await target.reply_video(
                d["media_file_id"], caption=plain,
                caption_entities=ents, **kw)
        else:
            await target.reply_text(plain, entities=ents, **kw)
    except TelegramError as e:
        return await target.reply_text(f"❌ {e}")
    note = ""
    if missing:
        note = "\n⚠️ أرقام مش موجودة: " + ", ".join(
            f"{{{m}}}" for m in missing)
    await target.reply_text(
        f"{ui('eye', '👁')} <b>ده الشكل النهائي فوق</b>{note}\n"
        f"<i>اضغط 📤 انشر الآن للنشر، أو ✏️ تعديل.</i>",
        parse_mode=HTML, reply_markup=kb_preview())


@gated
async def c_schedule(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "sched"
        return await update.message.reply_text("بعد كام دقيقة؟")
    await do_schedule(update.message, ctx, ctx.args[0])


async def do_schedule(msg, ctx, raw):
    uid = msg.from_user.id
    try:
        minutes = int(nd(raw.strip()))
    except ValueError:
        return await msg.reply_text("اكتب رقم صحيح.")
    if minutes < 1:
        return await msg.reply_text("الحد دقيقة.")
    chs = db.list_channels(uid)
    if not chs:
        return await msg.reply_text("مفيش قنوات.")
    ids = ",".join(c["chat_id"] for c in chs)
    run_at = int(time.time()) + minutes * 60
    sid = db.add_sched(uid, ids, uid, run_at)

    async def job(context):
        row = db.get_sched(sid)
        if not row:
            return
        d = db.get_draft(row["user_id"])
        body = d["body"] or ""
        if not body.strip():
            return
        plain, ents, btns, _, _ = render(body, db.get_emoji)
        kb = kb_buttons(btns)
        kw = {}
        if kb:
            kw["reply_markup"] = kb
        for cid in row["chat_ids"].split(","):
            try:
                await context.bot.send_message(
                    cid.strip(), plain, entities=ents, **kw)
            except TelegramError as e:
                log.warning("sched %s", e)
        db.del_sched(sid)

    j = ctx.job_queue.run_once(job, when=minutes * 60, name=f"sch{sid}")
    db.set_job(sid, str(j.id) if j and j.id else "")
    await msg.reply_text(
        f"{ui('sched', '⏰')} هينشر بعد <b>{minutes}</b> دقيقة.",
        parse_mode=HTML)


@gated
async def c_sched_list(update, ctx):
    rows = db.list_sched(update.effective_user.id)
    if not rows:
        return await update.message.reply_text("مفيش مجدولة.")
    lines = [f"{ui('sched', '⏰')} <b>المجدولة:</b>\n"]
    for r in rows:
        m = max(0, (r["run_at"] - int(time.time())) // 60)
        lines.append(f"• <code>{r['id']}</code> بعد ~{m} دقيقة")
    lines.append("\nمسح: /delsched &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_delsched(update, ctx):
    if not ctx.args:
        return
    try:
        sid = int(ctx.args[0])
    except ValueError:
        return
    r = db.get_sched(sid)
    if not r or r["user_id"] != update.effective_user.id:
        return
    for j in ctx.job_queue.get_jobs_by_name(f"sch{sid}"):
        j.schedule_removal()
    db.del_sched(sid)
    await update.message.reply_text("اتمسحت.")


@gated
async def c_save(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "tplname"
        return await update.message.reply_text("اسم القالب؟")
    await do_save_tpl(update.message, ctx, " ".join(ctx.args))


async def do_save_tpl(msg, ctx, name):
    name = name.strip()[:60]
    if not name:
        return await msg.reply_text("اسم فاضي.")
    d = db.get_draft(msg.from_user.id)
    body = d["body"] or ""
    if not body.strip():
        return await msg.reply_text("مسودتك فاضية.")
    if db.save_tpl(msg.from_user.id, name, body):
        await msg.reply_text(
            f"{ui('save', '💾')} اتحفظ «{esc(name)}»", parse_mode=HTML)
    else:
        await msg.reply_text("الاسم موجود.")


@gated
async def c_tpls(update, ctx):
    rows = db.list_tpls(update.effective_user.id)
    if not rows:
        return await update.message.reply_text("مفيش قوالب.")
    lines = [f"{ui('tpls', '📄')} <b>القوالب:</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['id']}</code> — {esc(r['name'])}")
    lines.append("\nتحميل: /tpl &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_tpl(update, ctx):
    if not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        return
    r = db.get_tpl(update.effective_user.id, tid)
    if not r:
        return await update.message.reply_text("مش موجود.")
    db.set_draft(update.effective_user.id, r["body"])
    await update.message.reply_text(
        f"{ui('check', '✅')} اتحمّل في المسودة.", parse_mode=HTML)


@gated
async def c_favs(update, ctx):
    rows = db.list_favs(update.effective_user.id)
    if not rows:
        return await update.message.reply_text("مفيش مفضلة.")
    lines = [f"{ui('favs', '⭐')} <b>المفضلة:</b>\n"]
    for r in rows[:60]:
        lines.append(
            f'<tg-emoji emoji-id="{r["doc_id"]}">{esc(r["char"])}</tg-emoji> '
            f'<code>{{{r["num"]}}}</code>')
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_channels(update, ctx):
    rows = db.list_channels(update.effective_user.id)
    if not rows:
        return await update.message.reply_text(
            "مفيش قنوات. /addchannel أو اضغط 🤖 ضيفني لقناتك")
    lines = [f"{ui('channels', '📢')} <b>القنوات:</b>\n"]
    for r in rows:
        lines.append(
            f"• <code>{r['id']}</code> — {esc(r['title'] or r['chat_id'])}")
    lines.append("\nمسح: /delchannel &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@gated
async def c_addchannel(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "ch"
        return await update.message.reply_text(
            "ابعت @username أو -100... القناة.")
    await do_addch(update.message, ctx, ctx.args[0])


async def do_addch(msg, ctx, raw):
    uid = msg.from_user.id
    cid = raw.strip()
    try:
        chat = await ctx.bot.get_chat(cid)
    except TelegramError as e:
        return await msg.reply_text(f"❌ {e}")
    try:
        member = await ctx.bot.get_chat_member(chat.id, uid)
        status = getattr(member.status, "value", str(member.status))
        if status not in ("administrator", "creator"):
            return await msg.reply_text(
                "❌ لازم تكون <b>أدمن</b> في القناة.", parse_mode=HTML)
    except TelegramError as e:
        return await msg.reply_text(f"❌ {e}")
    title = chat.title or chat.username or str(chat.id)
    if db.add_channel(uid, str(chat.id), title):
        await msg.reply_text(
            f"{ui('check', '✅')} انضافت «{esc(title)}»", parse_mode=HTML)
    else:
        await msg.reply_text("موجودة من قبل.")


@gated
async def c_delchannel(update, ctx):
    if not ctx.args:
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        return
    db.del_channel(update.effective_user.id, rid)
    await update.message.reply_text("اتمسحت.")


@owner_only
async def c_stats(update, ctx):
    packs = db.list_packs()
    total = db.count_emojis()
    users = db.count_users()
    req = db.get_setting("require_channel") or REQ_CHANNEL_ENV or "—"
    req_title = REQ["title"] or req
    lines = [
        f"{ui('stats', '📊')} <b>إحصائيات</b>\n",
        f"{ui('packs', '📦')} حزم: <b>{len(packs)}</b>",
        f"{ui('browse', '🧩')} إيموجي: <b>{total}</b>",
        f"👥 مستخدمين: <b>{users}</b>",
        f"📢 قناة إجبارية: <b>{esc(req_title)}</b>\n",
    ]
    top = db.top_used(10)
    if top:
        lines.append(f"{ui('fire', '🔥')} <b>الأكثر استخداماً:</b>")
        for r in top:
            lines.append(
                f'<tg-emoji emoji-id="{r["doc_id"]}">{esc(r["char"])}</tg-emoji> '
                f'<code>{{{r["num"]}}}</code> × {r["uses"]}')
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@owner_only
async def c_export(update, ctx):
    data = {
        "packs": [dict(p) for p in db.list_packs()],
        "emojis": [dict(e) for e in db.list_emojis(per=100000)],
        "exported_at": int(time.time()),
    }
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    await ctx.bot.send_document(
        update.effective_chat.id, raw, filename="backup.json",
        caption=f"{len(data['emojis'])} إيموجي")


# ==================== TEXT / MEDIA ====================
async def on_text(update, ctx):
    msg = update.message
    if not msg:
        return
    u = msg.from_user
    if not u:
        return
    db.touch_user(u.id, u.username, u.first_name)

    text = msg.text or msg.caption or ""
    state = ctx.user_data.pop("s", None)

    if state == "reqchannel":
        if is_owner(u.id):
            return await do_setreq(msg, ctx, text)
        return
    if state == "pack":
        try:
            added = await load_pack_from_bot(ctx.bot, text)
        except TelegramError as e:
            return await msg.reply_text(f"❌ {e}")
        if added:
            await build_ui_emojis()
            await msg.reply_text(
                f"{ui('check', '✅')} انضاف <b>{added}</b> إيموجي.",
                parse_mode=HTML)
        else:
            await msg.reply_text("⚠️ مفيش إيموجي جديد.")
        return

    if not await require_sub(update, ctx):
        return

    if state == "ch":
        return await do_addch(msg, ctx, text)
    if state == "tplname":
        return await do_save_tpl(msg, ctx, text)
    if state == "sched":
        return await do_schedule(msg, ctx, text)
    if state == "search":
        return await show_browse(msg, ctx, None, 0, text.strip())
    if state == "format":
        return await do_format(msg, ctx, text)

    found = extract_emoji(msg)
    if found:
        pid = db.add_pack("inbox", "📥 الوارد")
        lines = [f"{ui('check', '✅')} ضفتها للوارد:\n"]
        for char, doc_id in found:
            num, _ = db.add_emoji(char, doc_id, pid)
            lines.append(
                f'<tg-emoji emoji-id="{doc_id}">{esc(char)}</tg-emoji> → '
                f'<code>{{{num}}}</code>')
        await build_ui_emojis()
        await msg.reply_text("\n".join(lines), parse_mode=HTML)
        return

    if "{" in text and "}" in text:
        db.set_draft(u.id, text)
        return await show_preview(msg, ctx)

    # fallback: suggest format
    await msg.reply_text(
        f"{ui('think', '🤔')} لو عايز إيموجي بريميوم، حط أرقامهم في النص "
        f"شكل <code>{{رقم}}</code>.\n"
        f"أو ابعتلي النص كده وأنا أنسّقه: /format",
        parse_mode=HTML, reply_markup=kb_home())


async def on_media(update, ctx):
    msg = update.message
    if not msg:
        return
    if not await require_sub(update, ctx):
        return
    uid = msg.from_user.id
    if msg.photo:
        db.set_media(uid, "photo", msg.photo[-1].file_id)
        if msg.caption:
            db.set_draft(uid, msg.caption)
        await msg.reply_text(
            f"{ui('media', '🖼')} اتحفظت الصورة.", parse_mode=HTML)
    elif msg.video:
        db.set_media(uid, "video", msg.video.file_id)
        if msg.caption:
            db.set_draft(uid, msg.caption)
        await msg.reply_text(
            f"{ui('video', '🎬')} اتحفظ الفيديو.", parse_mode=HTML)


# ==================== CALLBACKS ====================
async def on_cb(update, ctx):
    q = update.callback_query
    u = q.from_user
    if not u:
        return
    uid = u.id
    d = q.data or ""
    try:
        if not await require_sub(update, ctx):
            return

        if d == "noop":
            return await q.answer()
        if d == "m:home":
            await q.edit_message_text(
                f"{ui('bat', '🦇')} <b>القائمة</b>",
                parse_mode=HTML, reply_markup=kb_main(uid))
            return await q.answer()
        if d == "m:settings":
            if not is_owner(uid):
                return await q.answer("للمالك فقط.", show_alert=True)
            await q.edit_message_text(
                f"{ui('gear', '⚙️')} <b>الإعدادات</b>",
                parse_mode=HTML, reply_markup=kb_settings())
            return await q.answer()
        if d == "set:req":
            if not is_owner(uid):
                return await q.answer("للمالك فقط.", show_alert=True)
            ctx.user_data["s"] = "reqchannel"
            await q.edit_message_text(
                "ابعت @username أو -100... للقناة اللي عايز الناس تشترك فيها.",
                reply_markup=kb_home())
            return await q.answer()
        if d == "set:unreq":
            if not is_owner(uid):
                return await q.answer("للمالك فقط.", show_alert=True)
            db.del_setting("require_channel")
            REQ["id"] = None
            REQ["title"] = None
            REQ["invite"] = None
            await q.edit_message_text(
                f"{ui('check', '✅')} اتلغى الاشتراك الإجباري.",
                parse_mode=HTML, reply_markup=kb_home())
            return await q.answer()

        # browse
        if d == "m:browse":
            await q.answer()
            return await show_browse(q.message, ctx, None, 0, "")
        if d.startswith("b:"):
            _, pid, pg, s = d.split(":", 3)
            pid = int(pid) if pid != "0" else None
            await q.answer()
            return await show_browse(q.message, ctx, pid, int(pg), s)
        if d.startswith("s:"):
            ctx.user_data["s"] = "search"
            await q.answer()
            return await q.message.reply_text("ابعتلي كلمة البحث.")
        if d.startswith("ids:"):
            _, pid, s = d.split(":", 2)
            pid = int(pid) if pid != "0" else None
            items = db.list_emojis(pid, s or None, 0, 100000)
            if not items:
                return await q.answer("فاضي.", show_alert=True)
            lines = [f'{it["num"]}={it["char"]}' for it in items]
            chunk = "\n".join(lines)
            for i in range(0, len(chunk), 3500):
                await q.message.reply_text(
                    "<code>" + esc(chunk[i:i+3500]) + "</code>",
                    parse_mode=HTML)
            return await q.answer()
        if d.startswith("p:"):
            num = d.split(":", 1)[1]
            it = db.get_emoji(num)
            if not it:
                return await q.answer("مش موجود.", show_alert=True)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ مفضلة", callback_data=f"fv:{num}"),
                InlineKeyboardButton("📋 الرقم", callback_data="noop")]])
            await q.answer(f"{{{num}}} = {it['char']}", show_alert=True)
            try:
                await q.message.reply_text(
                    f'<tg-emoji emoji-id="{it["doc_id"]}">'
                    f'{esc(it["char"])}</tg-emoji> → '
                    f'<code>{{{num}}}</code>',
                    parse_mode=HTML, reply_markup=kb)
            except TelegramError:
                pass
            return
        if d.startswith("fv:"):
            num = d.split(":", 1)[1]
            state = db.toggle_fav(uid, num)
            return await q.answer("⭐ اتضاف" if state else "اتشال")

        # packs
        if d == "m:packs":
            rows = db.list_packs()
            if not rows:
                await q.edit_message_text("مفيش حزم.", reply_markup=kb_home())
            else:
                kb = [[InlineKeyboardButton(
                    f"🧩 {p['title'][:30]}",
                    callback_data=f"b:{p['id']}:0:")] for p in rows]
                kb.append([InlineKeyboardButton(
                    "🏠 القائمة", callback_data="m:home")])
                await q.edit_message_text(
                    "📦 اختار حزمة:", parse_mode=HTML,
                    reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d == "m:addpack":
            ctx.user_data["s"] = "pack"
            await q.edit_message_text(
                "ابعتلي اسم الحزمة أو الرابط.", reply_markup=kb_home())
            return await q.answer()

        # recent
        if d == "m:recent":
            await q.answer()
            return await c_recent(update, ctx)

        # format
        if d == "m:format":
            await q.answer()
            ctx.user_data["s"] = "format"
            return await q.message.reply_text(
                f"{ui('paint', '🎨')} ابعتلي النص بتاعك.",
                parse_mode=HTML)
        if d == "d:fmt":
            ctx.user_data["s"] = "format"
            await q.answer()
            return await q.message.reply_text(
                "ابعتلي النص اللي عايز ينسّق.")

        # draft
        if d == "m:draft":
            row = db.get_draft(uid)
            body = row["body"] or ""
            shown = (body[:3000] + "…") if len(body) > 3000 else body
            await q.edit_message_text(
                f"<b>مسودتك:</b>\n\n"
                f"<code>{esc(shown) or '(فاضية)'}</code>",
                parse_mode=HTML, reply_markup=kb_draft())
            return await q.answer()
        if d == "d:prev":
            await q.answer()
            return await show_preview(q.message, ctx)
        if d == "d:edit":
            await q.answer()
            return await q.message.reply_text(
                f"{ui('edit', '✏️')} ابعت النص الجديد.", parse_mode=HTML)
        if d == "d:clr":
            db.clear_draft(uid)
            return await q.answer("اتمسحت.")
        if d == "d:save":
            ctx.user_data["s"] = "tplname"
            await q.answer()
            return await q.message.reply_text("اسم القالب؟")
        if d == "d:media":
            await q.answer()
            return await q.message.reply_text("ابعت الصورة أو الفيديو.")
        if d == "d:sched":
            ctx.user_data["s"] = "sched"
            await q.answer()
            return await q.message.reply_text("بعد كام دقيقة؟")
        if d == "d:send":
            await q.answer()
            chs = db.list_channels(uid)
            if not chs:
                if DEFAULT_CHANNEL and is_owner(uid):
                    return await do_send(q.message, ctx, DEFAULT_CHANNEL)
                kb = kb_join()
                return await q.message.reply_text(
                    "مفيش قنوات مضافة. اضغط 🤖 ضيفني لقناتك",
                    reply_markup=kb)
            if len(chs) == 1:
                return await do_send(q.message, ctx, chs[0]["chat_id"])
            return await q.message.reply_text(
                "اختار قناة:", reply_markup=kb_channels(chs))
        if d.startswith("send:"):
            rid = int(d.split(":", 1)[1])
            ch = db.get_channel(uid, rid)
            await q.answer()
            if ch:
                return await do_send(q.message, ctx, ch["chat_id"])
            return

        # templates
        if d == "m:tpls":
            rows = db.list_tpls(uid)
            if not rows:
                await q.edit_message_text("مفيش قوالب.",
                                          reply_markup=kb_home())
            else:
                kb = [[
                    InlineKeyboardButton(
                        f"📄 {r['name'][:30]}",
                        callback_data=f"tu:{r['id']}"),
                    InlineKeyboardButton(
                        "🗑", callback_data=f"td:{r['id']}")] for r in rows]
                kb.append([InlineKeyboardButton(
                    "🏠 القائمة", callback_data="m:home")])
                await q.edit_message_text(
                    "📄 القوالب",
                    reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d.startswith("tu:"):
            tid = int(d.split(":", 1)[1])
            t = db.get_tpl(uid, tid)
            if not t:
                return await q.answer("مش موجود.", show_alert=True)
            db.set_draft(uid, t["body"])
            await q.answer("اتحمّل")
            return await q.message.reply_text(
                f"{ui('check', '✅')} اتحمّل في المسودة.", parse_mode=HTML)
        if d.startswith("td:"):
            tid = int(d.split(":", 1)[1])
            db.del_tpl(uid, tid)
            return await q.answer("اتمسح.")

        # favorites
        if d == "m:favs":
            rows = db.list_favs(uid)
            if not rows:
                await q.edit_message_text("مفيش مفضلة.",
                                          reply_markup=kb_home())
            else:
                lines = [f"{ui('favs', '⭐')} <b>المفضلة</b>\n"]
                for r in rows[:40]:
                    lines.append(
                        f'<tg-emoji emoji-id="{r["doc_id"]}">'
                        f'{esc(r["char"])}</tg-emoji> '
                        f'<code>{{{r["num"]}}}</code>')
                await q.edit_message_text(
                    "\n".join(lines), parse_mode=HTML,
                    reply_markup=kb_home())
            return await q.answer()

        # channels
        if d == "m:channels":
            rows = db.list_channels(uid)
            await q.edit_message_text(
                "📢 <b>القنوات</b>", parse_mode=HTML,
                reply_markup=kb_channels(rows, "chd") if rows
                else InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ إضافة", callback_data="m:addch"),
                    InlineKeyboardButton("🏠", callback_data="m:home")]]))
            return await q.answer()
        if d == "m:addch":
            ctx.user_data["s"] = "ch"
            await q.edit_message_text(
                "ابعت @username أو -100...", reply_markup=kb_home())
            return await q.answer()
        if d == "m:delch":
            rows = db.list_channels(uid)
            if not rows:
                await q.edit_message_text("مفيش قنوات.",
                                          reply_markup=kb_home())
            else:
                kb = [[InlineKeyboardButton(
                    f"🗑 {r['title'][:30]}", callback_data=f"chx:{r['id']}")]
                    for r in rows]
                kb.append([InlineKeyboardButton(
                    "🏠", callback_data="m:home")])
                await q.edit_message_text(
                    "اختار للمسح:", reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d.startswith("chx:"):
            rid = int(d.split(":", 1)[1])
            db.del_channel(uid, rid)
            return await q.answer("اتمسحت.")

        # scheduled
        if d == "m:sched":
            rows = db.list_sched(uid)
            if not rows:
                await q.edit_message_text("مفيش مجدولة.",
                                          reply_markup=kb_home())
            else:
                lines = [f"{ui('sched', '⏰')} <b>المجدولة</b>\n"]
                for r in rows:
                    m = max(0, (r["run_at"] - int(time.time())) // 60)
                    lines.append(f"• <code>{r['id']}</code> بعد ~{m} دقيقة")
                await q.edit_message_text(
                    "\n".join(lines), parse_mode=HTML,
                    reply_markup=kb_home())
            return await q.answer()

        # help / stats
        if d == "m:help":
            await q.answer()
            return await c_help(update, ctx)
        if d == "m:stats":
            if not is_owner(uid):
                return await q.answer("للمالك فقط.", show_alert=True)
            await q.answer()
            return await c_stats(update, ctx)

    except Exception as e:
        log.exception("cb")
        try:
            await q.answer(f"خطأ: {e}", show_alert=True)
        except Exception:
            pass


# ==================== STARTUP ====================
async def post_init(app):
    global BOT_USERNAME
    try:
        me = await app.bot.get_me()
        BOT_USERNAME = me.username or ""
        log.info("bot username: %s", BOT_USERNAME)
    except Exception as e:
        log.warning("get_me failed: %s", e)

    if AUTO_PACKS:
        log.info("loading %d auto packs", len(AUTO_PACKS))
        for name in AUTO_PACKS:
            try:
                n = await load_pack_from_bot(app.bot, name)
                log.info("loaded %s: %d emojis", name, n)
            except Exception as e:
                log.warning("auto pack %s failed: %s", name, e)
    await build_ui_emojis()
    await refresh_req_channel(app.bot)
    log.info("UI ready with %d premium emojis", len(UI_E))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    start_web_server()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", c_start))
    app.add_handler(CommandHandler("menu", c_start))
    app.add_handler(CommandHandler("help", c_help))
    app.add_handler(CommandHandler("addme", c_addme))
    app.add_handler(CommandHandler("setchannel", c_setchannel))
    app.add_handler(CommandHandler("unsetchannel", c_unsetchannel))
    app.add_handler(CommandHandler("addpack", c_addpack))
    app.add_handler(CommandHandler("packs", c_packs))
    app.add_handler(CommandHandler("browse", c_browse))
    app.add_handler(CommandHandler("recent", c_recent))
    app.add_handler(CommandHandler("format", c_format))
    app.add_handler(CommandHandler("new", c_new))
    app.add_handler(CommandHandler("draft", c_draft))
    app.add_handler(CommandHandler("send", c_send))
    app.add_handler(CommandHandler("schedule", c_schedule))
    app.add_handler(CommandHandler("scheduled", c_sched_list))
    app.add_handler(CommandHandler("delsched", c_delsched))
    app.add_handler(CommandHandler("save", c_save))
    app.add_handler(CommandHandler("templates", c_tpls))
    app.add_handler(CommandHandler("tpl", c_tpl))
    app.add_handler(CommandHandler("favs", c_favs))
    app.add_handler(CommandHandler("channels", c_channels))
    app.add_handler(CommandHandler("addchannel", c_addchannel))
    app.add_handler(CommandHandler("delchannel", c_delchannel))
    app.add_handler(CommandHandler("stats", c_stats))
    app.add_handler(CommandHandler("export", c_export))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO) & filters.ChatType.PRIVATE, on_media))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION)
        & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    log.info("bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
