"""
Telegram Image Tools Bot
- Force-subscribe (verified through Telegram API, so it can't be bypassed by just clicking the link)
- Admin: add/delete channels, activate/deactivate users, stats, broadcast
- Tools: compress, resize, convert, grayscale, rotate, flip, blur, sharpen,
         brightness, contrast, square crop, remove EXIF, sticker, PDF, invert, watermark

ACTIVE user   = exempt, does NOT need to join channels
DEACTIVE user = must join every channel you added (default for new users)
"""
import asyncio
import io
import logging
import os
import re
import sqlite3

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
from telegram.ext import ApplicationHandlerStop

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("imgbot")

# ───────────────────────── Database ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
    active INTEGER DEFAULT 0, joined TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS channels(
    chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
""")
db.commit()


def upsert_user(u):
    db.execute("INSERT OR IGNORE INTO users(user_id,name,username) VALUES(?,?,?)",
               (u.id, u.full_name, u.username))
    db.execute("UPDATE users SET name=?, username=? WHERE user_id=?",
               (u.full_name, u.username, u.id))
    db.commit()


def is_active(uid):
    r = db.execute("SELECT active FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["active"])


def get_channels():
    return db.execute("SELECT * FROM channels").fetchall()


# ───────────────────────── Force-subscribe ─────────────────────────
_alerted = set()


async def missing_channels(bot, uid):
    missing = []
    for ch in get_channels():
        try:
            m = await bot.get_chat_member(ch["chat_id"], uid)
            joined = m.status in ("member", "administrator", "creator") or \
                (m.status == "restricted" and getattr(m, "is_member", False))
            if not joined:
                missing.append(ch)
        except TelegramError as e:
            # Usually: bot is not admin in that channel. Tell the admins.
            log.error("Cannot check channel %s: %s", ch["chat_id"], e)
            missing.append(ch)  # stay locked, never let users through by mistake
            if ch["chat_id"] not in _alerted:
                _alerted.add(ch["chat_id"])
                for a in ADMIN_IDS:
                    try:
                        await bot.send_message(a, f"⚠️ Can't verify channel {ch['title']} "
                                                  f"({ch['chat_id']}): {e}\nMake the bot ADMIN there.")
                    except TelegramError:
                        pass
    return missing


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user may use the bot."""
    u = update.effective_user
    upsert_user(u)
    if is_active(u.id):  # only users you activated are exempt (admins are checked too)
        return True
    missing = await missing_channels(context.bot, u.id)
    if not missing:
        return True

    kb = [[InlineKeyboardButton(f"📢 Subscribe: {c['title']}", url=c["link"])] for c in missing]
    kb.append([InlineKeyboardButton("✅ I've Subscribed – Verify", callback_data="chk")])
    text = ("🔒 <b>Access locked</b>\n\nTap the <b>Subscribe</b> button below, join the channel, "
            "then come back and tap <b>Verify</b>.\n"
            "The bot checks your subscription automatically.")
    q = update.callback_query
    if q:
        await q.answer("❌ You have not joined all channels yet!", show_alert=True)
        if q.data != "chk":
            await q.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.effective_message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    return False


# ───────────────────────── Image tools ─────────────────────────
TOOLS = {
    "compress": ("🗜 Compress", [("Extreme (20%)", "20"), ("High (40%)", "40"),
                                 ("Medium (60%)", "60"), ("Light (80%)", "80"), ("✍️ Custom", "custom")]),
    "resize": ("📐 Resize", None),
    "convert": ("🔄 Convert", [("JPG", "JPEG"), ("PNG", "PNG"), ("WEBP", "WEBP")]),
    "grayscale": ("⚫ Black & White", None),
    "invert": ("🎭 Invert", None),
    "rotate": ("🔃 Rotate", [("90° →", "90"), ("180°", "180"), ("90° ←", "270"), ("✍️ Custom", "custom")]),
    "flip": ("↔️ Flip", [("Horizontal", "h"), ("Vertical", "v")]),
    "blur": ("🌫 Blur", [("Light", "2"), ("Medium", "5"), ("Strong", "10")]),
    "sharpen": ("🔪 Sharpen", [("Light", "1"), ("Strong", "3")]),
    "brightness": ("💡 Brightness", [("Darker", "0.7"), ("Brighter", "1.3"), ("Much brighter", "1.6")]),
    "contrast": ("🌗 Contrast", [("Lower", "0.7"), ("Higher", "1.3"), ("Much higher", "1.7")]),
    "square": ("⏹ Square crop", None),
    "strip": ("🧹 Remove EXIF/metadata", None),
    "sticker": ("🏷 Make sticker (512px)", None),
    "pdf": ("📄 Convert to PDF", None),
    "watermark": ("💧 Add text watermark", None),
}


def flat(img):
    """Remove alpha for JPEG."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB") if img.mode != "RGB" and img.mode != "L" else img


def add_watermark(img, text):
    img = img.convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = max(16, img.width // 18)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    x, y = img.width - w - 20, img.height - h - 20
    d.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 140))
    d.text((x, y), text, font=font, fill=(255, 255, 255, 210))
    return Image.alpha_composite(img, layer)


def process(data: bytes, tool: str, opt: str = "", text: str = ""):
    src = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(src)
    fmt, ext, kw = "JPEG", "jpg", {}

    if tool == "compress":
        img = flat(img)
        kw = dict(quality=int(opt), optimize=True)
    elif tool == "resize":
        w, h = (int(x) for x in opt.split("x"))
        if h == 0:  # only width given -> keep aspect ratio
            h = max(1, round(img.height * w / img.width))
        img = img.resize((w, h), Image.LANCZOS)
        fmt, ext = ("PNG", "png") if img.mode == "RGBA" else ("JPEG", "jpg")
        kw = dict(quality=92) if fmt == "JPEG" else {}
    elif tool == "convert":
        fmt = opt
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
        if fmt == "JPEG":
            img = flat(img)
            kw = dict(quality=95)
    elif tool == "grayscale":
        img = ImageOps.grayscale(img)
    elif tool == "invert":
        img = ImageOps.invert(flat(img).convert("RGB"))
    elif tool == "rotate":
        img = img.rotate(-float(opt), expand=True)
    elif tool == "flip":
        img = ImageOps.mirror(img) if opt == "h" else ImageOps.flip(img)
    elif tool == "blur":
        img = img.filter(ImageFilter.GaussianBlur(int(opt)))
    elif tool == "sharpen":
        for _ in range(int(opt)):
            img = img.filter(ImageFilter.SHARPEN)
    elif tool == "brightness":
        img = ImageEnhance.Brightness(flat(img)).enhance(float(opt))
    elif tool == "contrast":
        img = ImageEnhance.Contrast(flat(img)).enhance(float(opt))
    elif tool == "square":
        s = min(img.size)
        img = ImageOps.fit(img, (s, s), Image.LANCZOS)
    elif tool == "strip":
        img = Image.frombytes(img.mode, img.size, img.tobytes())
        fmt, ext = ("PNG", "png") if img.mode == "RGBA" else ("JPEG", "jpg")
    elif tool == "sticker":
        img = img.convert("RGBA")
        img.thumbnail((512, 512), Image.LANCZOS)
        fmt, ext = "WEBP", "webp"
    elif tool == "pdf":
        img = flat(img).convert("RGB")
        fmt, ext = "PDF", "pdf"
    elif tool == "watermark":
        img = add_watermark(img, text)
        fmt, ext = "PNG", "png"

    if fmt == "JPEG":
        img = flat(img)
    out = io.BytesIO()
    img.save(out, format=fmt, **kw)
    return out.getvalue(), f"{tool}.{ext}"


PROMPTS = {
    "resize": ("📐 Send the new size as <b>WIDTHxHEIGHT</b> in pixels.\n"
               "Example: <code>800x600</code>\n"
               "Send only <code>800</code> to set the width and keep the ratio."),
    "watermark": "✍️ Send the watermark text now:",
    "compress": "🗜 Send quality from <b>1</b> (smallest) to <b>95</b> (best). Example: <code>35</code>",
    "rotate": "🔃 Send the angle in degrees. Example: <code>45</code> or <code>-30</code>",
}


def parse_input(tool, text):
    t = text.strip().lower().replace("×", "x").replace("*", "x").replace(" ", "")
    try:
        if tool == "resize":
            m = re.fullmatch(r"(\d{1,5})(?:x(\d{1,5}))?", t)
            if not m:
                return None
            w, h = int(m[1]), int(m[2] or 0)
            return f"{w}x{h}" if 1 <= w <= 10000 and h <= 10000 else None
        if tool == "compress":
            q = int(t)
            return str(q) if 1 <= q <= 95 else None
        if tool == "rotate":
            a = float(t)
            return str(a) if -360 <= a <= 360 else None
    except ValueError:
        return None
    return None


def fmt_size(n):
    return f"{n/1024:.1f} KB" if n < 1024 * 1024 else f"{n/1024/1024:.2f} MB"


# ───────────────────────── Keyboards ─────────────────────────
def tools_kb():
    keys = list(TOOLS)
    rows = [[InlineKeyboardButton(TOOLS[k][0], callback_data=f"t:{k}") for k in keys[i:i + 2]]
            for i in range(0, len(keys), 2)]
    return InlineKeyboardMarkup(rows)


def opts_kb(tool):
    opts = TOOLS[tool][1]
    rows = [[InlineKeyboardButton(l, callback_data=f"o:{tool}:{v}") for l, v in opts[i:i + 3]]
            for i in range(0, len(opts), 3)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


# ───────────────────────── User handlers ─────────────────────────
WELCOME = ("👋 <b>Welcome!</b>\n\nSend me <b>any number of photos</b> (as photo or as file) and "
           "choose a tool. Compress, resize, convert, watermark and more.\n\n"
           "📎 Tip: send as <i>File</i> to keep the original quality.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await gate(update, context):
        await update.message.reply_html(WELCOME)


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return
    m = update.message
    if m.photo:
        fid, size = m.photo[-1].file_id, m.photo[-1].file_size or 0
    else:
        fid, size = m.document.file_id, m.document.file_size or 0
    context.user_data.update(file_id=fid, size=size)
    context.user_data.pop("await", None)
    await m.reply_html(f"✅ Image received ({fmt_size(size)}). Choose a tool:",
                       reply_markup=tools_kb(), quote=True)


async def run_tool(update, context, tool, opt="", text=""):
    ud = context.user_data
    msg = update.effective_message
    if "file_id" not in ud:
        await msg.reply_text("Please send an image first.")
        return
    wait = await msg.reply_text("⏳ Processing...")
    try:
        f = await context.bot.get_file(ud["file_id"])
        data = bytes(await f.download_as_bytearray())
        out, name = await asyncio.to_thread(process, data, tool, opt, text)
        cap = f"✅ {TOOLS[tool][0]}\n📦 {fmt_size(len(data))} → {fmt_size(len(out))}"
        await context.bot.send_document(msg.chat_id, io.BytesIO(out), filename=name, caption=cap)
        await msg.reply_text("Choose another tool for the same image, or send a new one 👇",
                             reply_markup=tools_kb())
    except Exception as e:
        log.exception("process failed")
        await msg.reply_text(f"❌ Failed: {e}")
    finally:
        await wait.delete()


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await gate(update, context):
        return
    d = q.data
    if d.startswith("adm:"):
        return await admin_cb(update, context)
    if d == "chk":
        await q.answer("✅ Verified!")
        await q.message.edit_text("✅ Verified! You can use the bot now.\n\n"
                                  "Send me a photo to begin.")
    elif d == "back":
        await q.answer()
        await q.message.edit_text("Choose a tool:", reply_markup=tools_kb())
    elif d.startswith("t:"):
        tool = d[2:]
        await q.answer()
        if tool in ("resize", "watermark"):
            context.user_data["await"] = tool
            await q.message.reply_html(PROMPTS[tool])
        elif TOOLS[tool][1]:
            await q.message.edit_text(f"{TOOLS[tool][0]} – choose option:", reply_markup=opts_kb(tool))
        else:
            await run_tool(update, context, tool)
    elif d.startswith("o:"):
        _, tool, opt = d.split(":", 2)
        await q.answer()
        if opt == "custom":
            context.user_data["await"] = tool
            await q.message.reply_html(PROMPTS[tool])
        else:
            await run_tool(update, context, tool, opt)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return
    tool = context.user_data.get("await")
    if not tool:
        return await update.message.reply_text("Send me a photo 📷")
    if tool == "watermark":
        context.user_data.pop("await")
        return await run_tool(update, context, "watermark", text=update.message.text[:60])
    opt = parse_input(tool, update.message.text)
    if opt is None:
        return await update.message.reply_html("❌ Invalid value. Try again:\n\n" + PROMPTS[tool])
    context.user_data.pop("await")
    await run_tool(update, context, tool, opt)


# ───────────────────────── Admin ─────────────────────────
def admin_only(fn):
    async def wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, context)
    return wrap


def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel", callback_data="adm:addch"),
         InlineKeyboardButton("🗑 Delete Channel", callback_data="adm:del")],
        [InlineKeyboardButton("📢 Channels", callback_data="adm:list"),
         InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🟢 Activate User", callback_data="adm:act"),
         InlineKeyboardButton("🔒 Deactivate User", callback_data="adm:deact")],
        [InlineKeyboardButton("🟢 Activate ALL", callback_data="adm:actall"),
         InlineKeyboardButton("🔒 Deactivate ALL", callback_data="adm:deactall")],
        [InlineKeyboardButton("👥 Users", callback_data="adm:users")],
    ])


ADMIN_TEXT = ("🛠 <b>Admin panel</b>\n\n"
              "🟢 <b>Active</b> user = free access (no subscription)\n"
              "🔒 <b>Deactive</b> user = must subscribe to your channel(s)\n\n"
              "/check – why lock screen shows / doesn't for you\n"
              "/previewlock – see what new users see\n"
              "/broadcast &lt;text&gt;")


@admin_only
async def admin_help(update, context):
    context.user_data.pop("admin_await", None)
    await update.message.reply_html(ADMIN_TEXT, reply_markup=admin_panel_kb())


def parse_ref(text):
    """Accepts https://t.me/name, t.me/name, @name, name, or -100... id."""
    t = (text or "").strip()
    m = re.search(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})", t)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", t):
        return "@" + t.lstrip("@")
    if re.fullmatch(r"-100\d{5,}", t):
        return int(t)
    return None


async def add_channel_core(bot, ref):
    try:
        chat = await bot.get_chat(ref)
    except TelegramError as e:
        return False, f"❌ Channel not found: {e}"
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_admin = me.status in ("administrator", "creator")
    except TelegramError:
        is_admin = False
    if not is_admin:
        return False, (f"❌ Make the bot an <b>Admin</b> in <b>{chat.title}</b> first "
                       f"(add @{bot.username} as admin), then send the channel again.")
    try:
        link = f"https://t.me/{chat.username}" if chat.username else \
            (chat.invite_link or await bot.export_chat_invite_link(chat.id))
    except TelegramError as e:
        return False, f"❌ Could not get an invite link: {e}"
    db.execute("INSERT OR REPLACE INTO channels VALUES(?,?,?)", (chat.id, chat.title, link))
    db.commit()
    return True, f"✅ Channel added: <b>{chat.title}</b>\nUsers now see a <b>Subscribe</b> button for it."


ADD_HELP = ("➕ <b>Add channel</b>\n\nSend me ONE of these:\n"
            "• the channel link, e.g. <code>https://t.me/Strexx_Crypto_Signals</code>\n"
            "• the username, e.g. <code>@Strexx_Crypto_Signals</code>\n"
            "• or <b>forward any post</b> from the channel\n\n"
            "Before that, make sure this bot is <b>Admin</b> in the channel.\n"
            "/cancel to stop.")


@admin_only
async def add_channel(update, context):
    ref = parse_ref(" ".join(context.args)) if context.args else None
    if ref is None:
        context.user_data["admin_await"] = "addch"
        return await update.message.reply_html(ADD_HELP)
    ok, msg = await add_channel_core(context.bot, ref)
    await update.message.reply_html(msg)


@admin_only
async def del_channel(update, context):
    ref = parse_ref(" ".join(context.args)) if context.args else None
    if ref is None:
        return await update.message.reply_text("Use the 🗑 Delete Channel button in /admin, "
                                               "or: /delchannel @username")
    try:
        cid = (await context.bot.get_chat(ref)).id
    except TelegramError:
        cid = ref if isinstance(ref, int) else None
    cur = db.execute("DELETE FROM channels WHERE chat_id=?", (cid,))
    db.commit()
    await update.message.reply_text("🗑 Deleted." if cur.rowcount else "Channel not found.")


def channels_text():
    rows = get_channels()
    return "\n".join(f"• {r['title']}  ({r['chat_id']})" for r in rows) or "No channels added yet."


@admin_only
async def list_channels(update, context):
    await update.message.reply_text(channels_text())


def stats_text():
    t = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    a = db.execute("SELECT COUNT(*) c FROM users WHERE active=1").fetchone()["c"]
    return (f"👥 Users: {t}\n🟢 Active (free): {a}\n🔒 Deactive (must subscribe): {t - a}\n"
            f"📢 Channels: {len(get_channels())}")


def users_text():
    rows = db.execute("SELECT * FROM users ORDER BY joined DESC LIMIT 30").fetchall()
    return "\n".join(f"{'🟢' if r['active'] else '🔒'} {r['user_id']} {r['name']} @{r['username'] or '-'}"
                     for r in rows) or "No users."


def set_active_db(uid, value):
    cur = db.execute("UPDATE users SET active=? WHERE user_id=?", (value, uid))
    db.commit()
    if not cur.rowcount:
        return "❌ User not found (they must press /start in the bot once)."
    return (f"🟢 User {uid} ACTIVATED (no subscription needed)." if value
            else f"🔒 User {uid} DEACTIVATED (must subscribe).")


@admin_only
async def activate(update, context):
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /activate <user_id>")
    await update.message.reply_text(set_active_db(int(context.args[0]), 1))


@admin_only
async def deactivate(update, context):
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /deactivate <user_id>")
    await update.message.reply_text(set_active_db(int(context.args[0]), 0))


async def admin_cb(update, context):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return await q.answer("Admins only", show_alert=True)
    d = q.data[4:]
    await q.answer()
    if d == "menu":
        context.user_data.pop("admin_await", None)
        await q.message.edit_text(ADMIN_TEXT, parse_mode="HTML", reply_markup=admin_panel_kb())
    elif d == "addch":
        context.user_data["admin_await"] = "addch"
        await q.message.reply_html(ADD_HELP)
    elif d == "del":
        rows = get_channels()
        if not rows:
            return await q.message.reply_text("No channels to delete.")
        kb = [[InlineKeyboardButton(f"🗑 {r['title']}", callback_data=f"adm:delc:{r['chat_id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:menu")])
        await q.message.edit_text("Tap a channel to delete it:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("delc:"):
        db.execute("DELETE FROM channels WHERE chat_id=?", (int(d[5:]),))
        db.commit()
        await q.message.edit_text("🗑 Channel deleted.", reply_markup=admin_panel_kb())
    elif d == "list":
        await q.message.reply_text(channels_text())
    elif d == "stats":
        await q.message.reply_text(stats_text())
    elif d == "users":
        await q.message.reply_text(users_text())
    elif d in ("actall", "deactall"):
        word = "ACTIVATE" if d == "actall" else "DEACTIVATE"
        n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Yes, {word} all", callback_data=f"adm:do{d}"),
                                    InlineKeyboardButton("❌ Cancel", callback_data="adm:menu")]])
        note = ("They will get free access (no subscription needed)." if d == "actall"
                else "They will have to subscribe to your channel(s) again.")
        await q.message.edit_text(f"⚠️ {word} all {n} users?\n{note}", reply_markup=kb)
    elif d in ("doactall", "dodeactall"):
        value = 1 if d == "doactall" else 0
        cur = db.execute("UPDATE users SET active=?", (value,))
        db.commit()
        await q.message.edit_text(
            f"{'🟢 Activated' if value else '🔒 Deactivated'} {cur.rowcount} users.",
            reply_markup=admin_panel_kb())
    elif d in ("act", "deact"):
        context.user_data["admin_await"] = d
        await q.message.reply_html(
            f"Send the <b>user ID</b> to {'ACTIVATE (free access)' if d == 'act' else 'DEACTIVATE (must subscribe)'}.\n"
            "/cancel to stop.")


async def admin_input(update, context):
    """Runs before normal handlers. Only acts when an admin is in the middle of an admin action."""
    u, m = update.effective_user, update.message
    mode = context.user_data.get("admin_await")
    if not u or u.id not in ADMIN_IDS or not mode or not m:
        return
    if mode == "addch":
        fo = getattr(m, "forward_origin", None)
        ref = fo.chat.id if fo is not None and getattr(fo, "chat", None) else parse_ref(m.text)
        if ref is None:
            await m.reply_html("❌ I couldn't read that.\n\n" + ADD_HELP)
        else:
            ok, msg = await add_channel_core(context.bot, ref)
            if ok:
                context.user_data.pop("admin_await", None)
            await m.reply_html(msg, reply_markup=admin_panel_kb() if ok else None)
    elif mode in ("act", "deact"):
        if m.text and m.text.strip().isdigit():
            context.user_data.pop("admin_await", None)
            await m.reply_text(set_active_db(int(m.text.strip()), 1 if mode == "act" else 0))
        else:
            await m.reply_text("❌ Send a numeric user ID, or /cancel.")
    raise ApplicationHandlerStop


async def cancel(update, context):
    context.user_data.pop("admin_await", None)
    context.user_data.pop("await", None)
    await update.message.reply_text("Cancelled.")


def lock_screen(channels):
    kb = [[InlineKeyboardButton(f"📢 Subscribe: {c['title']}", url=c["link"])] for c in channels]
    kb.append([InlineKeyboardButton("✅ I've Subscribed – Verify", callback_data="chk")])
    text = ("🔒 <b>Access locked</b>\n\nTap the <b>Subscribe</b> button below, join the channel, "
            "then come back and tap <b>Verify</b>.\n"
            "The bot checks your subscription automatically.")
    return text, InlineKeyboardMarkup(kb)


@admin_only
async def check_cmd(update, context):
    """Diagnose why the lock screen is / isn't showing for YOU."""
    uid = update.effective_user.id
    chs = get_channels()
    lines = [f"📂 Database: {os.path.abspath(DB_PATH)}",
             f"🆔 Your ID: {uid}",
             "Your status: " + ("🟢 ACTIVE (skips channels)" if is_active(uid) else "🔒 deactive (must join)"),
             f"📢 Channels saved: {len(chs)}"]
    for ch in chs:
        try:
            m = await context.bot.get_chat_member(ch["chat_id"], uid)
            lines.append(f"• {ch['title']}: you are '{m.status}'"
                         + ("  → already joined, so no lock screen for you" if m.status in
                            ("member", "administrator", "creator") else "  → lock screen WILL show"))
        except TelegramError as e:
            lines.append(f"• {ch['title']}: ERROR {e}")
    if not chs:
        lines.append("⚠️ No channels saved → nobody is asked to subscribe. Add one in /admin.")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def preview_lock(update, context):
    """Show admins exactly what a new user sees."""
    chs = get_channels()
    if not chs:
        return await update.message.reply_text("No channels saved yet. Add one in /admin.")
    text, kb = lock_screen(chs)
    await update.message.reply_html("👁 <b>Preview (what new users see):</b>\n\n" + text, reply_markup=kb)


@admin_only
async def users_cmd(update, context):
    rows = db.execute("SELECT * FROM users ORDER BY joined DESC LIMIT 30").fetchall()
    await update.message.reply_text("\n".join(
        f"{'🟢' if r['active'] else '🔒'} {r['user_id']} {r['name']} @{r['username'] or '-'}"
        for r in rows) or "No users.")


@admin_only
async def stats(update, context):
    t = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    a = db.execute("SELECT COUNT(*) c FROM users WHERE active=1").fetchone()["c"]
    await update.message.reply_text(
        f"👥 Users: {t}\n🟢 Active (free): {a}\n🔒 Deactive (must join): {t - a}\n"
        f"📢 Channels: {len(get_channels())}")


@admin_only
async def broadcast(update, context):
    text = " ".join(context.args)
    if not text:
        return await update.message.reply_text("Usage: /broadcast your message")
    ok = 0
    for r in db.execute("SELECT user_id FROM users").fetchall():
        try:
            await context.bot.send_message(r["user_id"], text)
            ok += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            pass
    await update.message.reply_text(f"📨 Sent to {ok} users.")


# ───────────────────────── Run ─────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    for name, fn in [("admin", admin_help), ("addchannel", add_channel), ("delchannel", del_channel),
                     ("channels", list_channels), ("activate", activate), ("deactivate", deactivate),
                     ("check", check_cmd), ("previewlock", preview_lock), ("users", users_cmd), ("stats", stats), ("broadcast", broadcast)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_input), group=-1)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started | DB: %s | channels saved: %d", os.path.abspath(DB_PATH), len(get_channels()))
    app.run_polling()


if __name__ == "__main__":
    main()
