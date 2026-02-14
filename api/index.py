import os
import re
import asyncio
import logging
from datetime import datetime
from functools import wraps
import certifi

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)
from pymongo import MongoClient
from telegram import Bot, Update

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://qwerty:test@cluster0.ewyddvb.mongodb.net/",
)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-this-secret")

# --- Flask ---
_template_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates",
)
app = Flask(__name__, template_folder=_template_dir)
app.secret_key = os.environ.get("SECRET_KEY", "flask-secret-change-me")

# --- MongoDB ---


_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
_db = _client["filter_bot"]
filters_col = _db["filters"]
logs_col = _db["logs"]
pending_col = _db["pending_edits"]

# Lazy index creation — runs once then skips
_indexes_created = False


def _ensure_indexes():
    global _indexes_created
    if _indexes_created:
        return
    try:
        pending_col.create_index("created_at", expireAfterSeconds=300)
        _indexes_created = True
    except Exception as e:
        logger.warning(f"Index creation skipped: {e}")


# ===================================================================
# DATABASE HELPERS
# ===================================================================
def get_filters() -> dict:
    _ensure_indexes()
    doc = filters_col.find_one({"_id": "main"})
    if not doc:
        default = {
            "_id": "main",
            "global": [],
            "video_photo": [],
            "animation": [],
            "sticker": [],
            "whitelisted_ids": [],
            "blocked_ids": [],
        }
        filters_col.insert_one(default)
        return default
    return doc


def save_filter_data(data: dict):
    data["_id"] = "main"
    filters_col.replace_one({"_id": "main"}, data, upsert=True)


def add_log(entry: dict):
    entry["created_at"] = datetime.utcnow()
    logs_col.insert_one(entry)


def get_logs(limit: int = 200) -> list:
    return list(logs_col.find().sort("created_at", -1).limit(limit))


def clear_all_logs():
    logs_col.delete_many({})


# ===================================================================
# UTILITIES
# ===================================================================
def clean_filename(filename: str | None) -> str:
    if not filename:
        return ""
    name = os.path.splitext(filename)[0]
    name = re.sub(r"[-_.]", " ", name)
    return " ".join(name.split())


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


# ===================================================================
# TELEGRAM WEBHOOK PROCESSING
# ===================================================================
async def process_update(update_data: dict):
    async with Bot(token=BOT_TOKEN) as b:
        update = Update.de_json(update_data, b)

        if not update or not update.message:
            return

        msg = update.message

        # /setwebhook helper
        if msg.text and msg.text.startswith("/setwebhook"):
            await b.send_message(
                msg.chat.id,
                "Use the /setup_webhook admin route instead.",
            )
            return

        # /block command
        if msg.text and msg.text.startswith("/block"):
            await handle_block_command(b, msg)
            return

        # Pending keyword edit
        if msg.text and not msg.text.startswith("/"):
            await handle_pending_edit(b, msg)
            return

        # Media filtering
        if (
            msg.photo
            or msg.video
            or msg.animation
            or msg.document
            or msg.sticker
        ):
            await check_media(b, msg)


async def handle_block_command(b: Bot, msg):
    user_id = msg.from_user.id
    chat_id = msg.chat.id
    parts = (msg.text or "").split()
    args = parts[1:] if len(parts) > 1 else []

    if msg.chat.type != "private":
        member = await b.get_chat_member(chat_id, user_id)
        if member.status not in ("creator", "administrator"):
            await b.send_message(chat_id, "❌ Admins only.")
            return

    fd = get_filters()

    # --- REPLY MODE ---
    if msg.reply_to_message:
        target = msg.reply_to_message
        mode = args[0].lower() if args else "id_only"
        response_text = ""

        if target.sticker and target.sticker.set_name:
            pack = target.sticker.set_name
            if pack not in fd["sticker"]:
                fd["sticker"].append(pack)
                response_text = f"✅ Blocked Sticker Pack: {pack}"
            else:
                response_text = f"ℹ️ Pack {pack} already blocked."
        else:
            file_uid, f_name, cat = None, None, "video_photo"

            if target.photo:
                file_uid = target.photo[-1].file_unique_id
            elif target.video:
                file_uid = target.video.file_unique_id
                f_name = target.video.file_name
            elif target.animation:
                file_uid = target.animation.file_unique_id
                f_name = target.animation.file_name
                cat = "animation"
            elif target.document:
                file_uid = target.document.file_unique_id
                f_name = target.document.file_name
            elif target.sticker:
                file_uid = target.sticker.file_unique_id
                cat = "sticker"

            if not file_uid and not f_name:
                await b.send_message(chat_id, "❌ No ID or filename found.")
                return

            if mode in ("yes", "yesedit", "id_only"):
                if file_uid and file_uid not in fd["blocked_ids"]:
                    fd["blocked_ids"].append(file_uid)
                    response_text = f"✅ Blocked File ID: {file_uid}"
                else:
                    response_text = "ℹ️ ID already blocked."
            else:
                response_text = "ℹ️ Skipped File ID."

            if mode in ("yesedit", "noedit"):
                pending_col.replace_one(
                    {"_id": str(user_id)},
                    {
                        "_id": str(user_id),
                        "category": cat,
                        "chat_id": chat_id,
                        "created_at": datetime.utcnow(),
                    },
                    upsert=True,
                )
                response_text += f"\n✍️ Send the keyword to add to {cat}."
            elif mode in ("yes", "no") and f_name:
                cleaned = clean_filename(f_name)
                if cleaned.lower() not in [k.lower() for k in fd[cat]]:
                    fd[cat].append(cleaned)
                    response_text += f"\n✅ Blocked filename: {cleaned}"
                else:
                    response_text += (
                        f"\nℹ️ Filename {cleaned} already blocked."
                    )

        save_filter_data(fd)
        await b.send_message(chat_id, response_text)
        return

    # --- TEXT MODE ---
    if len(args) < 2:
        await b.send_message(
            chat_id,
            (
                "Usage:\n"
                "Reply: /block [yes|yesedit|no|noedit]\n"
                "Text:  /block [category] [keyword]"
            ),
        )
        return

    category = args[0].lower()
    keyword = " ".join(args[1:]).lower()
    valid = ("global", "video_photo", "animation", "sticker")

    if category not in valid:
        await b.send_message(
            chat_id, f"❌ Invalid category. Use: {', '.join(valid)}"
        )
        return

    if keyword not in [k.lower() for k in fd[category]]:
        fd[category].append(keyword)
        save_filter_data(fd)
        await b.send_message(chat_id, f"✅ Added '{keyword}' to {category}")
    else:
        await b.send_message(
            chat_id, f"ℹ️ '{keyword}' already in {category}"
        )


async def handle_pending_edit(b: Bot, msg):
    pending = pending_col.find_one_and_delete(
        {"_id": str(msg.from_user.id)}
    )
    if not pending:
        return

    category = pending["category"]
    keyword = msg.text.lower().strip()
    chat_id = msg.chat.id
    fd = get_filters()

    if keyword not in [k.lower() for k in fd[category]]:
        fd[category].append(keyword)
        save_filter_data(fd)
        await b.send_message(
            chat_id, f"✅ Added '{keyword}' to {category}"
        )
    else:
        await b.send_message(
            chat_id, f"ℹ️ '{keyword}' already in {category}"
        )


async def check_media(b: Bot, msg):
    user = msg.from_user
    fd = get_filters()

    should_delete = False
    reason = ""
    content_str = ""
    check_list = []
    media_type = "Unknown"
    file_uid = None

    if msg.photo:
        media_type = "Photo"
        file_uid = msg.photo[-1].file_unique_id
        content_str = msg.caption or ""
        check_list = fd.get("video_photo", [])

    elif msg.video:
        media_type = "Video"
        file_uid = msg.video.file_unique_id
        fname = clean_filename(msg.video.file_name) or ""
        content_str = f"{fname} {msg.caption or ''}".strip()
        check_list = fd.get("video_photo", [])

    elif msg.animation:
        media_type = "Animation/GIF"
        file_uid = msg.animation.file_unique_id
        fname = clean_filename(msg.animation.file_name) or ""
        content_str = f"{fname} {msg.caption or ''}".strip()
        check_list = fd.get("animation", [])

    elif msg.document:
        mime = msg.document.mime_type or ""
        fname = clean_filename(msg.document.file_name) or ""
        file_uid = msg.document.file_unique_id
        content_str = f"{fname} {msg.caption or ''}".strip()

        if mime.startswith("image/"):
            media_type = "Photo (File)"
            check_list = fd.get("video_photo", [])
        elif mime.startswith("video/"):
            media_type = "Video (File)"
            check_list = fd.get("video_photo", [])
        elif mime.startswith("audio/"):
            media_type = "Audio"
            check_list = fd.get("global", [])
        else:
            media_type = "Document"
            check_list = fd.get("global", [])

    elif msg.sticker:
        media_type = "Sticker"
        file_uid = msg.sticker.file_unique_id
        set_name = msg.sticker.set_name or ""
        emoji = msg.sticker.emoji or ""
        content_str = f"{set_name} {emoji}"
        check_list = fd.get("sticker", [])

    # Whitelist check
    if file_uid and file_uid in fd.get("whitelisted_ids", []):
        return

    # Blocked ID check
    if file_uid and file_uid in fd.get("blocked_ids", []):
        should_delete = True
        reason = f"File ID {file_uid} is manually blocked"
    else:
        searchable = content_str.lower()
        full_list = check_list + fd.get("global", [])
        for banned in full_list:
            if banned.lower() in searchable:
                should_delete = True
                reason = f"Keyword '{banned}' in {media_type}"
                break

    if should_delete:
        try:
            await b.delete_message(msg.chat.id, msg.message_id)
            logger.info(f"Deleted msg {msg.message_id}: {reason}")

            add_log(
                {
                    "timestamp": datetime.utcnow().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "user_id": user.id,
                    "username": user.username or user.first_name,
                    "media_type": media_type,
                    "reason": reason,
                    "content": content_str[:100],
                }
            )

            warning = await b.send_message(
                msg.chat.id, f"⚠️ Message deleted: {reason}"
            )
            # Can't auto-delete in serverless; acceptable tradeoff
        except Exception as e:
            logger.error(f"Delete failed: {e}")


# ===================================================================
# FLASK ROUTES — WEBHOOK
# ===================================================================
@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    logger.info("Webhook update received.")
    asyncio.run(process_update(data))
    return "ok", 200


@app.route("/setup_webhook")
@login_required
def setup_webhook():
    """Visit this once after deployment to register the webhook."""
    base_url = request.url_root.rstrip("/")
    webhook_url = f"{base_url}/webhook/{WEBHOOK_SECRET}"

    async def _set():
        async with Bot(token=BOT_TOKEN) as b:
            await b.set_webhook(url=webhook_url)

    asyncio.run(_set())
    return f"Webhook set to: {webhook_url}", 200


# ===================================================================
# FLASK ROUTES — ADMIN PANEL
# ===================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        else:
            flash("Invalid password")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    fd = get_filters()
    display_db = {
        k: v
        for k, v in fd.items()
        if k not in ("_id", "logs")
    }
    logs_list = get_logs()
    logs_data = {"entries": logs_list}
    return render_template("index.html", db=display_db, logs=logs_data)


@app.route("/add_item/<category>", methods=["POST"])
@login_required
def add_item(category):
    item = request.form.get("item", "").strip()
    if item:
        fd = get_filters()
        if category in fd and item not in fd[category]:
            fd[category].append(item)
            save_filter_data(fd)
    return redirect(url_for("index"))


@app.route("/remove_item/<category>/<int:index_id>")
@login_required
def remove_item(category, index_id):
    fd = get_filters()
    if category in fd and 0 <= index_id < len(fd[category]):
        fd[category].pop(index_id)
        save_filter_data(fd)
    return redirect(url_for("index"))


@app.route("/edit_item/<category>/<int:index_id>", methods=["POST"])
@login_required
def edit_item(category, index_id):
    new_value = request.form.get("new_value", "").strip()
    if new_value:
        fd = get_filters()
        if category in fd and 0 <= index_id < len(fd[category]):
            fd[category][index_id] = new_value
            save_filter_data(fd)
    return redirect(url_for("index"))


@app.route("/clear_logs")
@login_required
def clear_logs():
    clear_all_logs()
    return redirect(url_for("index"))