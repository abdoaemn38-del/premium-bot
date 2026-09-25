import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ---------------- config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DEFAULT_CHANNEL = os.getenv("DEFAULT_CHANNEL", "").strip()
AUTO_PACKS = [p.strip() for p in os.getenv("AUTO_PACKS", "").split(",") if p.strip()]
DB_PATH = os.getenv("DB_PATH", "/tmp/bot_data.db")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
log = logging.getLogger("premium-bot")
HTML = ParseMode.HTML
PER_PAGE = 20
_lock = threading.Lock()


# ---------------- tiny web server (keeps Render happy) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"VANTA awake")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_web_server():
    try:
        srv = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        log.info("health server on port %d", PORT)
    except Exception as e:
        log.warning("web server failed: %s", e)


# ---------------- db ----------------
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
                favorite INTEGER DEFAULT 0, uses INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, body TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS draft (
                user_id INTEGER PRIMARY KEY, body TEXT DEFAULT '',
                media_type TEXT, media_file_id TEXT, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE, title TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_ids TEXT, body TEXT, run_at INTEGER, job_id TEXT);
            """)
            self.conn.commit()

    def ex(self, sql, p=()):
        with _lock:
            c = self.conn.execute(sql, p); self.conn.commit(); return c

    def q(self, sql, p=()):
        with _lock:
            return self.conn.execute(sql, p).fetchall()

    def q1(self, sql, p=()):
        with _lock:
            return self.conn.execute(sql, p).fetchone()

    def add_pack(self, name, title):
        r = self.q1("SELECT id FROM packs WHERE name=?", (name,))
        if r: return r["id"]
        c = self.ex("INSERT INTO packs(name,title,created_at) VALUES(?,?,?)",
                    (name, title, int(time.time())))
        return c.lastrowid

    def list_packs(self): return self.q("SELECT * FROM packs ORDER BY id")
    def get_pack(self, pid): return self.q1("SELECT * FROM packs WHERE id=?", (pid,))
    def count_pack(self, pid):
        return self.q1("SELECT COUNT(*) n FROM emojis WHERE pack_id=?", (pid,))["n"]

    def add_emoji(self, char, doc_id, pack_id):
        r = self.q1("SELECT num FROM emojis WHERE doc_id=?", (doc_id,))
        if r: return r["num"], False
        c = self.ex("INSERT INTO emojis(char,doc_id,pack_id) VALUES(?,?,?)",
                    (char, doc_id, pack_id))
        return c.lastrowid, True

    def get_emoji(self, num): return self.q1("SELECT * FROM emojis WHERE num=?", (str(num),))
    def del_emoji(self, num): self.ex("DELETE FROM emojis WHERE num=?", (str(num),))
    def bump_use(self, num): self.ex("UPDATE emojis SET uses=uses+1 WHERE num=?", (str(num),))
    def toggle_fav(self, num):
        self.ex("UPDATE emojis SET favorite=1-favorite WHERE num=?", (str(num),))
        r = self.q1("SELECT favorite FROM emojis WHERE num=?", (str(num),))
        return bool(r and r["favorite"])
    def list_favs(self): return self.q("SELECT * FROM emojis WHERE favorite=1 ORDER BY num")
    def top_used(self, n=10):
        return self.q("SELECT * FROM emojis WHERE uses>0 ORDER BY uses DESC LIMIT ?", (n,))
    def count_emojis(self, pid=None, search=None):
        if pid and search:
            return self.q1("SELECT COUNT(*) n FROM emojis WHERE pack_id=? AND char LIKE ?",
                           (pid, f"%{search}%"))["n"]
        if pid: return self.q1("SELECT COUNT(*) n FROM emojis WHERE pack_id=?", (pid,))["n"]
        if search: return self.q1("SELECT COUNT(*) n FROM emojis WHERE char LIKE ?",
                                  (f"%{search}%",))["n"]
        return self.q1("SELECT COUNT(*) n FROM emojis")["n"]
    def list_emojis(self, pid=None, search=None, page=0, per=20):
        off = page * per
        if pid and search:
            return self.q("SELECT * FROM emojis WHERE pack_id=? AND char LIKE ? ORDER BY num LIMIT ? OFFSET ?",
                          (pid, f"%{search}%", per, off))
        if pid:
            return self.q("SELECT * FROM emojis WHERE pack_id=? ORDER BY num LIMIT ? OFFSET ?",
                          (pid, per, off))
        if search:
            return self.q("SELECT * FROM emojis WHERE char LIKE ? ORDER BY num LIMIT ? OFFSET ?",
                          (f"%{search}%", per, off))
        return self.q("SELECT * FROM emojis ORDER BY num LIMIT ? OFFSET ?", (per, off))

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
        self.ex("UPDATE draft SET media_type=?,media_file_id=?,updated_at=? WHERE user_id=?",
                (mt, fid, int(time.time()), uid))
    def clear_draft(self, uid):
        self.get_draft(uid)
        self.ex("UPDATE draft SET body='',media_type=NULL,media_file_id=NULL WHERE user_id=?",
                (uid,))

    def save_tpl(self, name, body):
        try:
            self.ex("INSERT INTO templates(name,body,created_at) VALUES(?,?,?)",
                    (name, body, int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False
    def list_tpls(self): return self.q("SELECT * FROM templates ORDER BY id DESC")
    def get_tpl(self, tid): return self.q1("SELECT * FROM templates WHERE id=?", (tid,))
    def del_tpl(self, tid): self.ex("DELETE FROM templates WHERE id=?", (tid,))

    def add_channel(self, cid, title):
        try:
            self.ex("INSERT INTO channels(chat_id,title,created_at) VALUES(?,?,?)",
                    (cid, title, int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False
    def list_channels(self): return self.q("SELECT * FROM channels ORDER BY id")
    def get_channel(self, rid): return self.q1("SELECT * FROM channels WHERE id=?", (rid,))
    def del_channel(self, rid): self.ex("DELETE FROM channels WHERE id=?", (rid,))

    def add_sched(self, ids, uid, run_at, job_id=None):
        c = self.ex("INSERT INTO scheduled(chat_ids,body,run_at,job_id) VALUES(?,?,?,?)",
                    (ids, uid, run_at, job_id))
        return c.lastrowid
    def set_job(self, sid, jid): self.ex("UPDATE scheduled SET job_id=? WHERE id=?", (jid, sid))
    def list_sched(self):
        return self.q("SELECT * FROM scheduled WHERE run_at > ? ORDER BY run_at", (int(time.time()),))
    def del_sched(self, sid): self.ex("DELETE FROM scheduled WHERE id=?", (sid,))


db = DB(DB_PATH)


# ---------------- utils ----------------
TOKEN_RE = re.compile(r"\{(\d+)\}|\[\[([^\]|]+)\|([^\]]+)\]\]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

def esc(s): return html.escape(s or "", quote=False)
def nd(s): return s.translate(DIGITS)

def fmt(t):
    t = esc(t)
    t = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"<i>\1</i>", t)
    t = re.sub(r"~([^~\n]+)~", r"<s>\1</s>", t)
    t = re.sub(r"\|\|([^|\n]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t

def render(body, lookup):
    parts, buttons, missing, used = [], [], [], []
    last = 0
    for m in TOKEN_RE.finditer(body):
        parts.append(fmt(body[last:m.start()]))
        if m.group(1):
            num = nd(m.group(1))
            item = lookup(num)
            if item:
                parts.append(f'<emoji id="{item["doc_id"]}">{esc(item["char"])}</emoji>')
                used.append(num)
            else:
                missing.append(num)
                parts.append(esc(m.group(0)))
        else:
            buttons.append((m.group(2), m.group(3)))
        last = m.end()
    parts.append(fmt(body[last:]))
    return "".join(parts), buttons, missing, used

def kb_buttons(bs):
    if not bs: return None
    rows, row = [], []
    for label, url in bs[:20]:
        row.append(InlineKeyboardButton(label[:40], url=url))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def u16len(s): return len(s.encode("utf-16-le")) // 2
def u16char(t, off):
    c = 0
    for i, ch in enumerate(t):
        if c >= off: return i
        c += u16len(ch)
    return len(t)

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


# ---------------- keyboards ----------------
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 تصفّح", callback_data="m:browse"),
         InlineKeyboardButton("✍️ مسودتي", callback_data="m:draft")],
        [InlineKeyboardButton("➕ ضيف حزمة", callback_data="m:addpack"),
         InlineKeyboardButton("📦 حزمي", callback_data="m:packs")],
        [InlineKeyboardButton("📄 قوالبي", callback_data="m:tpls"),
         InlineKeyboardButton("⭐ المفضلة", callback_data="m:favs")],
        [InlineKeyboardButton("📢 قنواتي", callback_data="m:channels"),
         InlineKeyboardButton("⏰ المجدولة", callback_data="m:sched")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="m:stats"),
         InlineKeyboardButton("❓ مساعدة", callback_data="m:help")],
    ])

def kb_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة", callback_data="m:home")]])

def kb_grid(items, pid, page, pages, search=None):
    rows, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(f'{it["char"]} {it["num"]}',
                                        callback_data=f'p:{it["num"]}'))
        if len(row) == 4: rows.append(row); row = []
    if row: rows.append(row)
    s = search or ""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"b:{pid}:{page-1}:{s}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"b:{pid}:{page+1}:{s}"))
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
        [InlineKeyboardButton("🖼 ميديا", callback_data="d:media"),
         InlineKeyboardButton("💾 قالب", callback_data="d:save")],
        [InlineKeyboardButton("⏰ جدولة", callback_data="d:sched"),
         InlineKeyboardButton("🗑 مسح", callback_data="d:clr")],
        [InlineKeyboardButton("🏠 القائمة", callback_data="m:home")],
    ])

def kb_preview():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 إرسال", callback_data="d:send")],
        [InlineKeyboardButton("✏️ تعديل", callback_data="d:edit"),
         InlineKeyboardButton("💾 قالب", callback_data="d:save")],
        [InlineKeyboardButton("🏠 القائمة", callback_data="m:home")],
    ])

def kb_channels(rows, prefix="send"):
    kb = [[InlineKeyboardButton(f"📢 {(r['title'] or r['chat_id'])[:40]}",
                                callback_data=f"{prefix}:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton("➕ إضافة", callback_data="m:addch"),
               InlineKeyboardButton("🗑 مسح", callback_data="m:delch")])
    kb.append([InlineKeyboardButton("🏠 القائمة", callback_data="m:home")])
    return InlineKeyboardMarkup(kb)


# ---------------- guard ----------------
def owner_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if OWNER_ID and u and u.id != OWNER_ID: return
        return await fn(update, ctx)
    return w


# ---------------- pack loading ----------------
async def load_pack(ctx, raw, msg=None):
    name = clean_pack_name(raw)
    try:
        st = await ctx.bot.get_sticker_set(name)
    except TelegramError as e:
        if msg: await msg.reply_text(f"❌ مش لاقي الحزمة:\n{e}", parse_mode=HTML)
        return 0
    kind = getattr(st.sticker_type, "value", st.sticker_type)
    if kind != "custom_emoji":
        if msg: await msg.reply_text("❌ دي مش حزمة إيموجي بريميوم.", parse_mode=HTML)
        return 0
    pid = db.add_pack(st.name, st.title)
    added = 0
    for s in st.stickers:
        if s.custom_emoji_id:
            _, new = db.add_emoji(s.emoji, s.custom_emoji_id, pid)
            if new: added += 1
    return added


# ---------------- commands ----------------
@owner_only
async def c_start(update, ctx):
    await update.message.reply_text(
        "🦇 <b>بوت الإيموجي البريميوم</b>\n\n"
        "اختار من الأزرار تحت 👇",
        parse_mode=HTML, reply_markup=kb_main())

@owner_only
async def c_help(update, ctx):
    await update.message.reply_text(
        "<b>📖 المساعدة</b>\n\n"
        "<b>الكتابة:</b>\n"
        "• <code>{62}</code> = إيموجي رقم 62\n"
        "• <code>*bold*</code> <code>_italic_</code> <code>~strike~</code>\n"
        "• <code>||spoiler||</code>\n"
        "• <code>[نص](url)</code>\n"
        "• <code>[[زر|url]]</code>\n\n"
        "<b>الأوامر:</b>\n"
        "/addpack — ضيف حزمة\n"
        "/browse — تصفّح\n"
        "/draft — مسودتي\n"
        "/send — إرسال\n"
        "/schedule <دقائق>\n"
        "/save <اسم>\n"
        "/channels /addchannel\n"
        "/stats",
        parse_mode=HTML, reply_markup=kb_main())

@owner_only
async def c_addpack(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "pack"
        return await update.message.reply_text("ابعتلي اسم الحزمة أو رابط addemoji.")
    added = await load_pack(ctx, " ".join(ctx.args), update.message)
    if added:
        await update.message.reply_text(f"✅ انضاف {added} إيموجي.", parse_mode=HTML)

@owner_only
async def c_browse(update, ctx):
    pid = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    await show_browse(update.message, ctx, pid, 0, "")

async def show_browse(target, ctx, pid, page, search):
    total = db.count_emojis(pid, search or None)
    if total == 0:
        return await target.reply_text("مفيش إيموجي هنا.", reply_markup=kb_home())
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = db.list_emojis(pid, search or None, page, PER_PAGE)
    title = "كل الإيموجي"
    if pid:
        p = db.get_pack(pid); title = esc(p["title"]) if p else "?"
    extra = f" — بحث: {esc(search)}" if search else ""
    await target.reply_text(
        f"🧩 <b>{title}</b>{extra}\n{total} إيموجي — صفحة {page+1}/{pages}\n"
        "دوس على إيموجي يطلعلك رقمه.",
        parse_mode=HTML,
        reply_markup=kb_grid(items, pid or 0, page, pages, search or None))

@owner_only
async def c_packs(update, ctx):
    rows = db.list_packs()
    if not rows: return await update.message.reply_text("مفيش حزم.")
    lines = ["📦 <b>الحزم:</b>\n"]
    for p in rows:
        lines.append(f"• <code>{p['id']}</code> — {esc(p['title'])} ({db.count_pack(p['id'])})")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_draft(update, ctx):
    d = db.get_draft(update.effective_user.id)
    body = d["body"] or ""
    shown = (body[:3000] + "…") if len(body) > 3000 else body
    await update.message.reply_text(
        f"<b>مسودتك:</b>\n\n<code>{esc(shown) or '(فاضية)'}</code>",
        parse_mode=HTML, reply_markup=kb_draft())

@owner_only
async def c_new(update, ctx):
    db.clear_draft(update.effective_user.id)
    await update.message.reply_text("مسودة جديدة. ابعتلي النص.", reply_markup=kb_draft())

@owner_only
async def c_send(update, ctx):
    chs = db.list_channels()
    if not chs:
        if DEFAULT_CHANNEL: return await do_send(update.message, ctx, DEFAULT_CHANNEL)
        return await update.message.reply_text("مفيش قنوات. /addchannel")
    if len(chs) == 1: return await do_send(update.message, ctx, chs[0]["chat_id"])
    await update.message.reply_text("اختار قناة:", reply_markup=kb_channels(chs))

async def do_send(msg, ctx, chat_id):
    uid = msg.from_user.id
    d = db.get_draft(uid)
    body = d["body"] or ""
    if not body.strip(): return await msg.reply_text("مسودتك فاضية.")
    html_out, btns, _, used = render(body, db.get_emoji)
    for n in used: db.bump_use(n)
    kb = kb_buttons(btns)
    kw = dict(parse_mode=HTML)
    if kb: kw["reply_markup"] = kb
    try:
        if d["media_type"] == "photo":
            await ctx.bot.send_photo(chat_id, d["media_file_id"], caption=html_out, **kw)
        elif d["media_type"] == "video":
            await ctx.bot.send_video(chat_id, d["media_file_id"], caption=html_out, **kw)
        else:
            await ctx.bot.send_message(chat_id, html_out, **kw)
        await msg.reply_text("✅ اتبعت.")
    except TelegramError as e:
        await msg.reply_text(f"❌ فشل: {e}")

async def show_preview(target, ctx):
    uid = target.from_user.id
    d = db.get_draft(uid)
    body = d["body"] or ""
    if not body.strip(): return await target.reply_text("مسودتك فاضية.")
    html_out, btns, missing, used = render(body, db.get_emoji)
    for n in used: db.bump_use(n)
    kb = kb_buttons(btns)
    kw = dict(parse_mode=HTML)
    if kb: kw["reply_markup"] = kb
    try:
        if d["media_type"] == "photo":
            await target.reply_photo(d["media_file_id"], caption=html_out, **kw)
        elif d["media_type"] == "video":
            await target.reply_video(d["media_file_id"], caption=html_out, **kw)
        else:
            await target.reply_text(html_out, **kw)
    except TelegramError as e:
        return await target.reply_text(f"❌ {e}")
    note = "\n⚠️ أرقام مش موجودة: " + ", ".join(f"{{{m}}}" for m in missing) if missing else ""
    await target.reply_text(
        "<b>الكود الخام:</b>\n<code>" + esc(html_out) + "</code>" + note,
        parse_mode=HTML, reply_markup=kb_preview())

@owner_only
async def c_schedule(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "sched"
        return await update.message.reply_text("بعد كام دقيقة؟")
    await do_schedule(update.message, ctx, ctx.args[0])

async def do_schedule(msg, ctx, raw):
    try: minutes = int(nd(raw.strip()))
    except ValueError: return await msg.reply_text("اكتب رقم صحيح.")
    if minutes < 1: return await msg.reply_text("الحد دقيقة.")
    chs = db.list_channels()
    if not chs: return await msg.reply_text("مفيش قنوات.")
    ids = ",".join(c["chat_id"] for c in chs)
    run_at = int(time.time()) + minutes * 60
    sid = db.add_sched(ids, msg.from_user.id, run_at)

    async def job(context):
        row = db.q1("SELECT * FROM scheduled WHERE id=?", (sid,))
        if not row: return
        d = db.get_draft(row["body"])
        body = d["body"] or ""
        if not body.strip(): return
        html_out, btns, _, _ = render(body, db.get_emoji)
        kb = kb_buttons(btns)
        kw = dict(parse_mode=HTML)
        if kb: kw["reply_markup"] = kb
        for cid in row["chat_ids"].split(","):
            try: await context.bot.send_message(cid.strip(), html_out, **kw)
            except TelegramError as e: log.warning("sched %s", e)
        db.del_sched(sid)

    j = ctx.job_queue.run_once(job, when=minutes*60, name=f"sch{sid}")
    db.set_job(sid, str(j.id) if j and j.id else "")
    await msg.reply_text(f"⏰ هينشر بعد {minutes} دقيقة.")

@owner_only
async def c_sched_list(update, ctx):
    rows = db.list_sched()
    if not rows: return await update.message.reply_text("مفيش مجدولة.")
    lines = ["⏰ <b>المجدولة:</b>\n"]
    for r in rows:
        m = max(0, (r["run_at"] - int(time.time())) // 60)
        lines.append(f"• <code>{r['id']}</code> بعد ~{m} دقيقة")
    lines.append("\nمسح: /delsched <id>")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_delsched(update, ctx):
    if not ctx.args: return
    try: sid = int(ctx.args[0])
    except ValueError: return
    for j in ctx.job_queue.get_jobs_by_name(f"sch{sid}"): j.schedule_removal()
    db.del_sched(sid)
    await update.message.reply_text("اتمسحت.")

@owner_only
async def c_save(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "tplname"
        return await update.message.reply_text("اسم القالب؟")
    await do_save_tpl(update.message, ctx, " ".join(ctx.args))

async def do_save_tpl(msg, ctx, name):
    name = name.strip()[:60]
    if not name: return await msg.reply_text("اسم فاضي.")
    d = db.get_draft(msg.from_user.id)
    body = d["body"] or ""
    if not body.strip(): return await msg.reply_text("مسودتك فاضية.")
    if db.save_tpl(name, body): await msg.reply_text(f"✅ اتحفظ «{esc(name)}»", parse_mode=HTML)
    else: await msg.reply_text("الاسم موجود.")

@owner_only
async def c_tpls(update, ctx):
    rows = db.list_tpls()
    if not rows: return await update.message.reply_text("مفيش قوالب.")
    lines = ["📄 <b>القوالب:</b>\n"]
    for r in rows: lines.append(f"• <code>{r['id']}</code> — {esc(r['name'])}")
    lines.append("\nتحميل: /tpl <id>")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_tpl(update, ctx):
    if not ctx.args: return
    try: tid = int(ctx.args[0])
    except ValueError: return
    r = db.get_tpl(tid)
    if not r: return await update.message.reply_text("مش موجود.")
    db.set_draft(update.effective_user.id, r["body"])
    await update.message.reply_text(f"✅ اتحمّل في المسودة.")

@owner_only
async def c_favs(update, ctx):
    rows = db.list_favs()
    if not rows: return await update.message.reply_text("مفيش مفضلة.")
    lines = ["⭐ <b>المفضلة:</b>\n"]
    for r in rows[:60]:
        lines.append(f'<emoji id="{r["doc_id"]}">{esc(r["char"])}</emoji> <code>{{{r["num"]}}}</code>')
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_channels(update, ctx):
    rows = db.list_channels()
    if not rows: return await update.message.reply_text("مفيش قنوات. /addchannel")
    lines = ["📢 <b>القنوات:</b>\n"]
    for r in rows: lines.append(f"• <code>{r['id']}</code> — {esc(r['title'] or r['chat_id'])}")
    lines.append("\nمسح: /delchannel <id>")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_addchannel(update, ctx):
    if not ctx.args:
        ctx.user_data["s"] = "ch"
        return await update.message.reply_text("ابعت @username أو -100...")
    await do_addch(update.message, ctx, ctx.args[0])

async def do_addch(msg, ctx, raw):
    cid = raw.strip()
    try: chat = await ctx.bot.get_chat(cid)
    except TelegramError as e: return await msg.reply_text(f"❌ {e}")
    title = chat.title or chat.username or str(chat.id)
    if db.add_channel(str(chat.id), title): await msg.reply_text(f"✅ انضافت «{esc(title)}»", parse_mode=HTML)
    else: await msg.reply_text("موجودة.")

@owner_only
async def c_delchannel(update, ctx):
    if not ctx.args: return
    try: rid = int(ctx.args[0])
    except ValueError: return
    db.del_channel(rid)
    await update.message.reply_text("اتمسحت.")

@owner_only
async def c_stats(update, ctx):
    packs = db.list_packs(); total = db.count_emojis()
    favs = len(db.list_favs()); tpls = len(db.list_tpls())
    chs = len(db.list_channels()); sch = len(db.list_sched())
    top = db.top_used(10)
    lines = ["📊 <b>إحصائيات</b>\n", f"📦 حزم: {len(packs)}",
             f"😀 إيموجي: {total}", f"⭐ مفضلة: {favs}",
             f"📄 قوالب: {tpls}", f"📢 قنوات: {chs}", f"⏰ مجدولة: {sch}\n"]
    if top:
        lines.append("<b>الأكثر استخداماً:</b>")
        for r in top:
            lines.append(f'<emoji id="{r["doc_id"]}">{esc(r["char"])}</emoji> <code>{{{r["num"]}}}</code> × {r["uses"]}')
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)

@owner_only
async def c_export(update, ctx):
    data = {
        "packs": [dict(p) for p in db.list_packs()],
        "emojis": [dict(e) for e in db.list_emojis(per=100000)],
        "templates": [dict(t) for t in db.list_tpls()],
        "exported_at": int(time.time()),
    }
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    await ctx.bot.send_document(update.effective_chat.id, raw,
                                filename="backup.json",
                                caption=f"{len(data['emojis'])} إيموجي")


# ---------------- text/media ----------------
@owner_only
async def on_text(update, ctx):
    msg = update.message
    if not msg: return
    text = msg.text or msg.caption or ""
    state = ctx.user_data.pop("s", None)

    if state == "pack":
        added = await load_pack(ctx, text, msg)
        if added: await msg.reply_text(f"✅ انضاف {added} إيموجي.")
        return
    if state == "ch": return await do_addch(msg, ctx, text)
    if state == "tplname": return await do_save_tpl(msg, ctx, text)
    if state == "sched": return await do_schedule(msg, ctx, text)
    if state == "search": return await show_browse(msg, ctx, None, 0, text.strip())

    found = extract_emoji(msg)
    if found:
        pid = db.add_pack("inbox", "📥 الوارد")
        lines = ["✅ ضفتها للوارد:\n"]
        for char, doc_id in found:
            num, _ = db.add_emoji(char, doc_id, pid)
            lines.append(f'<emoji id="{doc_id}">{esc(char)}</emoji> → <code>{{{num}}}</code>')
        await msg.reply_text("\n".join(lines), parse_mode=HTML)
        return

    if "{" in text and "}" in text:
        db.set_draft(msg.from_user.id, text)
        return await show_preview(msg, ctx)

    await msg.reply_text("ابعتلي منشور فيه <code>{رقم}</code>، أو /start للقائمة.", parse_mode=HTML)

@owner_only
async def on_media(update, ctx):
    msg = update.message
    if not msg: return
    uid = msg.from_user.id
    if msg.photo:
        db.set_media(uid, "photo", msg.photo[-1].file_id)
        if msg.caption: db.set_draft(uid, msg.caption)
        await msg.reply_text("🖼 اتحفظت.")
    elif msg.video:
        db.set_media(uid, "video", msg.video.file_id)
        if msg.caption: db.set_draft(uid, msg.caption)
        await msg.reply_text("🎬 اتحفظ.")


# ---------------- callbacks ----------------
@owner_only
async def on_cb(update, ctx):
    q = update.callback_query
    d = q.data or ""
    try:
        if d == "noop": return await q.answer()

        if d == "m:home":
            await q.edit_message_text("🦇 <b>القائمة</b>", parse_mode=HTML,
                                      reply_markup=kb_main())
            return await q.answer()
        if d == "m:browse":
            await q.answer()
            return await show_browse(q.message, ctx, None, 0, "")
        if d == "m:addpack":
            ctx.user_data["s"] = "pack"
            await q.edit_message_text("ابعتلي اسم الحزمة أو الرابط.", reply_markup=kb_home())
            return await q.answer()
        if d == "m:packs":
            rows = db.list_packs()
            if not rows:
                await q.edit_message_text("مفيش حزم.", reply_markup=kb_home())
            else:
                kb = [[InlineKeyboardButton(f"🧩 {p['title'][:30]}",
                                            callback_data=f"b:{p['id']}:0:")] for p in rows]
                kb.append([InlineKeyboardButton("🏠 القائمة", callback_data="m:home")])
                await q.edit_message_text("📦 اختار حزمة:", parse_mode=HTML,
                                          reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d == "m:draft":
            row = db.get_draft(q.from_user.id)
            body = row["body"] or ""
            shown = (body[:3000] + "…") if len(body) > 3000 else body
            await q.edit_message_text(f"<b>مسودتك:</b>\n\n<code>{esc(shown) or '(فاضية)'}</code>",
                                      parse_mode=HTML, reply_markup=kb_draft())
            return await q.answer()
        if d == "m:tpls":
            rows = db.list_tpls()
            if not rows:
                await q.edit_message_text("مفيش قوالب.", reply_markup=kb_home())
            else:
                kb = [[InlineKeyboardButton(f"📄 {r['name'][:30]}", callback_data=f"tu:{r['id']}"),
                       InlineKeyboardButton("🗑", callback_data=f"td:{r['id']}")] for r in rows]
                kb.append([InlineKeyboardButton("🏠 القائمة", callback_data="m:home")])
                await q.edit_message_text("📄 القوالب", reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d == "m:favs":
            rows = db.list_favs()
            if not rows:
                await q.edit_message_text("مفيش مفضلة.", reply_markup=kb_home())
            else:
                lines = ["⭐ <b>المفضلة</b>\n"]
                for r in rows[:40]:
                    lines.append(f'<emoji id="{r["doc_id"]}">{esc(r["char"])}</emoji> <code>{{{r["num"]}}}</code>')
                await q.edit_message_text("\n".join(lines), parse_mode=HTML, reply_markup=kb_home())
            return await q.answer()
        if d == "m:channels":
            rows = db.list_channels()
            await q.edit_message_text("📢 <b>القنوات</b>",
                parse_mode=HTML, reply_markup=kb_channels(rows, "chd") if rows
                else InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة", callback_data="m:addch"),
                                            InlineKeyboardButton("🏠", callback_data="m:home")]]))
            return await q.answer()
        if d == "m:addch":
            ctx.user_data["s"] = "ch"
            await q.edit_message_text("ابعت @username أو -100...", reply_markup=kb_home())
            return await q.answer()
        if d == "m:delch":
            rows = db.list_channels()
            if not rows:
                await q.edit_message_text("مفيش قنوات.", reply_markup=kb_home())
            else:
                kb = [[InlineKeyboardButton(f"🗑 {r['title'][:30]}", callback_data=f"chx:{r['id']}")] for r in rows]
                kb.append([InlineKeyboardButton("🏠", callback_data="m:home")])
                await q.edit_message_text("اختار للمسح:", reply_markup=InlineKeyboardMarkup(kb))
            return await q.answer()
        if d == "m:sched":
            rows = db.list_sched()
            if not rows:
                await q.edit_message_text("مفيش مجدولة.", reply_markup=kb_home())
            else:
                lines = ["⏰ <b>المجدولة</b>\n"]
                for r in rows:
                    m = max(0, (r["run_at"] - int(time.time())) // 60)
                    lines.append(f"• <code>{r['id']}</code> بعد ~{m} دقيقة")
                await q.edit_message_text("\n".join(lines), parse_mode=HTML, reply_markup=kb_home())
            return await q.answer()
        if d == "m:stats":
            await q.answer()
            return await c_stats(update, ctx)
        if d == "m:help":
            await q.answer()
            return await c_help(update, ctx)

        if d.startswith("b:"):
            _, pid, pg, s = d.split(":", 3)
            pid = int(pid) if pid != "0" else None
            await q.answer()
            return await show_browse(q.message, ctx, pid, int(pg), s)

        if d.startswith("p:"):
            num = d.split(":", 1)[1]
            it = db.get_emoji(num)
            if not it: return await q.answer("مش موجود.", show_alert=True)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ مفضلة", callback_data=f"fv:{num}"),
                InlineKeyboardButton("📋 الرقم", callback_data="noop"),
            ]])
            await q.answer(f"{{{num}}} = {it['char']}", show_alert=True)
            try:
                await q.message.reply_text(
                    f'<emoji id="{it["doc_id"]}">{esc(it["char"])}</emoji> → <code>{{{num}}}</code>',
                    parse_mode=HTML, reply_markup=kb)
            except TelegramError: pass
            return

        if d.startswith("fv:"):
            num = d.split(":", 1)[1]
            state = db.toggle_fav(num)
            return await q.answer("⭐ اتضاف" if state else "اتشال")

        if d.startswith("s:"):
            ctx.user_data["s"] = "search"
            await q.answer()
            return await q.message.reply_text("ابعتلي كلمة البحث.")

        if d.startswith("ids:"):
            _, pid, s = d.split(":", 2)
            pid = int(pid) if pid != "0" else None
            items = db.list_emojis(pid, s or None, 0, 100000)
            if not items: return await q.answer("فاضي.", show_alert=True)
            lines = [f'{it["num"]}={it["char"]}' for it in items]
            chunk = "\n".join(lines)
            for i in range(0, len(chunk), 3500):
                await q.message.reply_text("<code>" + esc(chunk[i:i+3500]) + "</code>",
                                           parse_mode=HTML)
            return await q.answer()

        if d == "d:prev":
            await q.answer()
            return await show_preview(q.message, ctx)
        if d == "d:edit":
            await q.answer()
            return await q.message.reply_text("ابعت النص الجديد.")
        if d == "d:clr":
            db.clear_draft(q.from_user.id)
            return await q.answer("اتمسحت.")
        if d == "d:save":
            ctx.user_data["s"] = "tplname"
            await q.answer()
            return await q.message.reply_text("اسم القالب؟")
        if d == "d:media":
            await q.answer()
            return await q.message.reply_text("ابعت الصورة/الفيديو.")
        if d == "d:sched":
            ctx.user_data["s"] = "sched"
            await q.answer()
            return await q.message.reply_text("بعد كام دقيقة؟")
        if d == "d:send":
            await q.answer()
            chs = db.list_channels()
            if not chs:
                if DEFAULT_CHANNEL: return await do_send(q.message, ctx, DEFAULT_CHANNEL)
                return await q.message.reply_text("مفيش قنوات. /addchannel")
            if len(chs) == 1: return await do_send(q.message, ctx, chs[0]["chat_id"])
            return await q.message.reply_text("اختار قناة:", reply_markup=kb_channels(chs))

        if d.startswith("send:"):
            rid = int(d.split(":", 1)[1])
            ch = db.get_channel(rid)
            await q.answer()
            if ch: return await do_send(q.message, ctx, ch["chat_id"])
            return

        if d.startswith("chx:"):
            rid = int(d.split(":", 1)[1])
            db.del_channel(rid)
            return await q.answer("اتمسحت.")

        if d.startswith("tu:"):
            tid = int(d.split(":", 1)[1])
            t = db.get_tpl(tid)
            if not t: return await q.answer("مش موجود.", show_alert=True)
            db.set_draft(q.from_user.id, t["body"])
            await q.answer("اتحمّل")
            return await q.message.reply_text(f"✅ اتحمّل في المسودة.")
        if d.startswith("td:"):
            tid = int(d.split(":", 1)[1])
            db.del_tpl(tid)
            return await q.answer("اتمسح.")

    except Exception as e:
        log.exception("cb")
        try: await q.answer(f"خطأ: {e}", show_alert=True)
        except Exception: pass


# ---------------- startup ----------------
async def post_init(app):
    if AUTO_PACKS:
        log.info("loading %d auto packs", len(AUTO_PACKS))
        for name in AUTO_PACKS:
            try:
                n = await load_pack_from_bot(app.bot, name)
                log.info("loaded %s: %d emojis", name, n)
            except Exception as e:
                log.warning("auto pack %s failed: %s", name, e)

async def load_pack_from_bot(bot, name):
    st = await bot.get_sticker_set(clean_pack_name(name))
    kind = getattr(st.sticker_type, "value", st.sticker_type)
    if kind != "custom_emoji": return 0
    pid = db.add_pack(st.name, st.title)
    added = 0
    for s in st.stickers:
        if s.custom_emoji_id:
            _, new = db.add_emoji(s.emoji, s.custom_emoji_id, pid)
            if new: added += 1
    return added


def main():
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing")
    start_web_server()
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init).build())
    app.add_handler(CommandHandler("start", c_start))
    app.add_handler(CommandHandler("menu", c_start))
    app.add_handler(CommandHandler("help", c_help))
    app.add_handler(CommandHandler("addpack", c_addpack))
    app.add_handler(CommandHandler("packs", c_packs))
    app.add_handler(CommandHandler("browse", c_browse))
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
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & filters.ChatType.PRIVATE, on_media))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    log.info("bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
