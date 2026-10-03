import base64
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from notion_client import Client as NotionClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
TG_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_KEY          = os.getenv("GROQ_API_KEY")
NOTION_TOKEN      = os.getenv("NOTION_TOKEN")
NOTION_PROJECTS   = os.getenv("NOTION_PROJECTS_PAGE_ID")
NOTION_TASKS_DB   = os.getenv("NOTION_TASKS_DB_ID")
OBSIDIAN_VAULT    = Path(os.getenv("OBSIDIAN_VAULT", ""))
ALLOWED_USER_IDS  = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

if not ALLOWED_USER_IDS:
    raise SystemExit("Set ALLOWED_USER_IDS — the bot refuses to run open to everyone")

groq_client = Groq(api_key=GROQ_KEY)
notion      = NotionClient(auth=NOTION_TOKEN)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# httpx logs request URLs at INFO, and the Telegram URL contains the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Project context (loaded once at startup) ───────────────────────────────────
def _load_context() -> str:
    parts = []
    for name in ["👤 About Me.md", "🏠 Home.md"]:
        p = OBSIDIAN_VAULT / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8")[:2500])
    return "\n\n---\n\n".join(parts)

PROJECT_CONTEXT = _load_context()

SYSTEM = f"""You are the personal assistant of the bot owner.
Always respond in English. Structure content clearly and concisely.
Add minimal useful context — 1-2 sentences max. Never be verbose.
Always return valid JSON matching the requested schema exactly.

Owner's projects and context:
{PROJECT_CONTEXT[:4000]}
"""

# ── Keyboards ──────────────────────────────────────────────────────────────────
TYPE_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("💡 Idea",  callback_data="type:idea"),
        InlineKeyboardButton("✅ Task",  callback_data="type:task"),
        InlineKeyboardButton("📝 Note",  callback_data="type:note"),
    ],
    [
        InlineKeyboardButton("☑️ Todo",  callback_data="type:todo"),
        InlineKeyboardButton("📅 Date",  callback_data="type:date"),
    ],
])

APPROVE_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Save",    callback_data="act:save"),
    InlineKeyboardButton("🔄 Redo",   callback_data="act:redo"),
    InlineKeyboardButton("❌ Cancel", callback_data="act:cancel"),
]])

# ── Groq: transcription ────────────────────────────────────────────────────────
def transcribe(path: Path) -> str:
    with open(path, "rb") as f:
        r = groq_client.audio.transcriptions.create(
            file=(path.name, f),
            model="whisper-large-v3",
        )
    return r.text

# ── Groq: image analysis ───────────────────────────────────────────────────────
def analyze_image(path: Path) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = groq_client.chat.completions.create(
        model="llama-3.2-11b-vision-preview",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text",
                 "text": "Describe this image in detail. Extract all visible text, numbers, and key information."},
            ],
        }],
        max_tokens=1024,
    )
    return r.choices[0].message.content

# ── Groq: structuring ──────────────────────────────────────────────────────────
SCHEMAS = {
    "idea": '{"title":"","summary":"1-2 sentences","potential":"business/personal value","next_step":"one concrete action"}',
    "task": '{"title":"","description":"","due_date":"YYYY-MM-DD or empty","priority":"high|medium|low"}',
    "note": '{"title":"","content":"structured markdown","tags":["tag"]}',
    "todo": '{"title":"","items":["item1","item2","item3"]}',
    "date": '{"title":"","date":"YYYY-MM-DD","time":"HH:MM or empty","description":""}',
}

def structure(raw: str, kind: str) -> dict:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user",   "content": (
                f"Structure as '{kind}'. Schema: {SCHEMAS[kind]}\n"
                f"Add brief useful context (max 1-2 sentences). Keep it tight.\n\n"
                f"Input:\n{raw}"
            )},
        ],
        max_tokens=800,
    )
    return json.loads(r.choices[0].message.content)

# ── Preview ────────────────────────────────────────────────────────────────────
ICONS = {"idea": "💡", "task": "✅", "note": "📝", "todo": "☑️", "date": "📅"}

def make_preview(data: dict, kind: str) -> str:
    icon  = ICONS[kind]
    title = data.get("title", "Untitled")
    lines = [f"{icon} *{title}*\n"]

    if kind == "idea":
        if data.get("summary"):   lines.append(data["summary"])
        if data.get("potential"): lines.append(f"\n📈 _{data['potential']}_")
        if data.get("next_step"): lines.append(f"\n▶️ {data['next_step']}")

    elif kind == "task":
        if data.get("description"): lines.append(data["description"])
        if data.get("priority"):    lines.append(f"\n⚡ Priority: *{data['priority']}*")
        if data.get("due_date"):    lines.append(f"📅 Due: {data['due_date']}")

    elif kind == "note":
        if data.get("content"): lines.append(data["content"][:500])
        tags = data.get("tags", [])
        if tags: lines.append("\n🏷 " + "  ".join(f"`#{t}`" for t in tags))

    elif kind == "todo":
        for item in data.get("items", [])[:8]:
            lines.append(f"☐ {item}")

    elif kind == "date":
        dt = data.get("date", "")
        tm = data.get("time", "")
        lines.append(f"📅 {dt}  {tm}".strip())
        if data.get("description"): lines.append(f"\n{data['description']}")

    return "\n".join(lines)

# ── Notion helpers ─────────────────────────────────────────────────────────────
def _rt(text: str) -> list:
    return [{"type": "text", "text": {"content": str(text)[:2000]}}]

def _para(text: str)       -> dict: return {"object":"block","type":"paragraph",   "paragraph":  {"rich_text":_rt(text)}}
def _h3(text: str)         -> dict: return {"object":"block","type":"heading_3",   "heading_3":  {"rich_text":_rt(text)}}
def _todo_b(text: str)     -> dict: return {"object":"block","type":"to_do",       "to_do":      {"rich_text":_rt(text),"checked":False}}
def _callout(text: str, emoji: str) -> dict:
    return {"object":"block","type":"callout","callout":{"rich_text":_rt(text),"icon":{"emoji":emoji}}}

# ── Notion save ────────────────────────────────────────────────────────────────
def save_notion(data: dict, kind: str):
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = data.get("title", "Untitled")
    stamp = _para(f"📱 {now} · Telegram Bot")

    if kind in ("task", "todo", "date"):
        props: dict = {"Task name": {"title": _rt(title)}}

        due = data.get("due_date") or data.get("date", "")
        if due and re.match(r"\d{4}-\d{2}-\d{2}", due):
            props["Due"] = {"date": {"start": due}}

        blocks = []
        if kind == "task":
            if data.get("description"): blocks.append(_para(data["description"]))
            if data.get("priority"):    blocks.append(_callout(f"Priority: {data['priority']}", "⚡"))
        elif kind == "todo":
            for item in data.get("items", []):
                blocks.append(_todo_b(item))
        elif kind == "date":
            if data.get("time"):        blocks.append(_callout(f"Time: {data['time']}", "🕐"))
            if data.get("description"): blocks.append(_para(data["description"]))

        blocks.append(stamp)
        notion.pages.create(
            parent={"database_id": NOTION_TASKS_DB},
            properties=props,
            children=blocks,
        )

    else:  # idea, note → page under Own Projects
        blocks = []
        if kind == "idea":
            if data.get("summary"):
                blocks += [_h3("Summary"), _para(data["summary"])]
            if data.get("potential"):
                blocks += [_h3("Potential"), _para(data["potential"])]
            if data.get("next_step"):
                blocks += [_h3("Next Step"), _todo_b(data["next_step"])]
        elif kind == "note":
            if data.get("content"):
                blocks.append(_para(data["content"]))
            tags = data.get("tags", [])
            if tags:
                blocks.append(_para("Tags: " + ", ".join(tags)))

        blocks.append(stamp)
        notion.pages.create(
            parent={"page_id": NOTION_PROJECTS},
            properties={"title": {"title": _rt(title)}},
            children=blocks,
        )

# ── Telegram handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Second Brain Bot*\n\n"
        "Send voice 🎙, text, or photo 📸 in any language.\n"
        "I'll structure it and save to Notion.\n\n"
        "💡 Idea  ·  ✅ Task  ·  📝 Note  ·  ☑️ Todo  ·  📅 Date",
        parse_mode="Markdown",
    )

async def _show_type_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, captured: str):
    ctx.user_data.clear()
    ctx.user_data["content"] = captured
    short = captured[:250] + ("..." if len(captured) > 250 else "")
    await update.message.reply_text(
        f"*Captured:*\n_{short}_\n\nChoose type:",
        parse_mode="Markdown",
        reply_markup=TYPE_KB,
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_type_menu(update, ctx, update.message.text)

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎙 Transcribing...")
    voice = update.message.voice or update.message.audio
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    tg_file = await ctx.bot.get_file(voice.file_id)
    await tg_file.download_to_drive(tmp_path)
    try:
        text = transcribe(tmp_path)
    except Exception as e:
        await msg.edit_text(f"❌ Transcription failed: {e}")
        return
    finally:
        tmp_path.unlink(missing_ok=True)
    ctx.user_data.clear()
    ctx.user_data["content"] = text
    short = text[:250] + ("..." if len(text) > 250 else "")
    await msg.edit_text(f"🗣 *Transcribed:*\n_{short}_", parse_mode="Markdown")
    await update.message.reply_text("Choose type:", reply_markup=TYPE_KB)

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🖼 Analyzing image...")
    photo = update.message.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    tg_file = await ctx.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(tmp_path)
    try:
        desc = analyze_image(tmp_path)
    except Exception as e:
        await msg.edit_text(f"❌ Image analysis failed: {e}")
        return
    finally:
        tmp_path.unlink(missing_ok=True)
    caption = (update.message.caption or "").strip()
    content = f"{caption}\n\n{desc}".strip() if caption else desc
    ctx.user_data.clear()
    ctx.user_data["content"] = content
    short = desc[:250] + ("..." if len(desc) > 250 else "")
    await msg.edit_text(f"🖼 *Analyzed:*\n_{short}_", parse_mode="Markdown")
    await update.message.reply_text("Choose type:", reply_markup=TYPE_KB)

async def on_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind = q.data.split(":")[1]
    ctx.user_data["type"] = kind
    await q.edit_message_text("⚙️ Structuring...")
    try:
        data = structure(ctx.user_data.get("content", ""), kind)
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
        return
    ctx.user_data["structured"] = data
    await q.edit_message_text(
        make_preview(data, kind) + "\n\n━━━━━━━━\nSave to Notion?",
        parse_mode="Markdown",
        reply_markup=APPROVE_KB,
    )

async def on_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act = q.data.split(":")[1]

    if act == "cancel":
        await q.edit_message_text("❌ Cancelled.")
        ctx.user_data.clear()
        return

    if act == "redo":
        kind = ctx.user_data.get("type", "note")
        await q.edit_message_text("🔄 Re-structuring...")
        try:
            data = structure(ctx.user_data.get("content", ""), kind)
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {e}")
            return
        ctx.user_data["structured"] = data
        await q.edit_message_text(
            make_preview(data, kind) + "\n\n━━━━━━━━\nSave to Notion?",
            parse_mode="Markdown",
            reply_markup=APPROVE_KB,
        )
        return

    # save
    data = ctx.user_data.get("structured", {})
    kind = ctx.user_data.get("type", "note")
    try:
        save_notion(data, kind)
    except Exception as e:
        await q.edit_message_text(f"❌ Notion error: {e}")
        return
    await q.edit_message_text(
        f"✅ *{data.get('title', 'Untitled')}*\n\nSaved to Notion.",
        parse_mode="Markdown",
    )
    ctx.user_data.clear()

def _owner_only(handler):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id in ALLOWED_USER_IDS:
            await handler(update, ctx)
    return wrapped

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TG_TOKEN).build()
    owner = filters.User(user_id=ALLOWED_USER_IDS)
    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(owner & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(owner & filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(_owner_only(on_type),   pattern="^type:"))
    app.add_handler(CallbackQueryHandler(_owner_only(on_action), pattern="^act:"))
    log.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
