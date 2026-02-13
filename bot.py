import logging
import json
import os
import asyncio
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# --- CONFIGURATION ---
BOT_TOKEN = "7479063361:AAF68TeismORaoVMRlTC3qiO8a4za5KcYbk"  # <--- PASTE YOUR TOKEN HERE
DB_FILE = "filters.json"
LOG_FILE = "logs.json"

# Global cache for filters
FILTERS_CACHE = {}
# Global cache for logs
LOGS_CACHE = {"last_reset": "", "entries": []}
# Tracks admins who are about to send an edited keyword
PENDING_EDITS = {}

# --- LOGGING SETUP ---
# We set the level to INFO to reduce console noise during high traffic.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO 
)

# Reduce noise from third-party libraries (optional, keeps console cleaner)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- DATABASE LOADER ---
def load_filters():
    global FILTERS_CACHE
    if not os.path.exists(DB_FILE):
        logger.info(f"Database file {DB_FILE} not found. Creating default.")
        default_data = {
            "global": [], 
            "video_photo": [], 
            "animation": [], 
            "sticker": [],
            "whitelisted_ids": [],
            "blocked_ids": [],
            "logs": []
        }
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=4)
        FILTERS_CACHE = default_data
        return default_data
    
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
            # Ensure all keys exist
            required_keys = ["global", "video_photo", "animation", "sticker", "whitelisted_ids", "blocked_ids", "logs"]
            for key in required_keys:
                if key not in data:
                    data[key] = []
            
            FILTERS_CACHE = data
            logger.info("Filters reloaded successfully.")
            return data
    except Exception as e:
        logger.error(f"CRITICAL: Error reading JSON: {e}")
        return FILTERS_CACHE

def save_filters(data):
    """Saves the filter data to the JSON file."""
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving filters: {e}")
        return False

def load_logs():
    global LOGS_CACHE
    if not os.path.exists(LOG_FILE):
        LOGS_CACHE = {"last_reset": datetime.now().strftime("%Y-%m-%d"), "entries": []}
        save_logs(LOGS_CACHE)
        return LOGS_CACHE
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            LOGS_CACHE = json.load(f)
            check_and_reset_logs()
            return LOGS_CACHE
    except Exception as e:
        logger.error(f"Error loading logs: {e}")
        return LOGS_CACHE

def save_logs(data):
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving logs: {e}")

def check_and_reset_logs():
    global LOGS_CACHE
    try:
        last_reset = datetime.strptime(LOGS_CACHE.get("last_reset", "2000-01-01"), "%Y-%m-%d")
        if datetime.now() - last_reset >= timedelta(days=7):
            logger.info("Weekly log reset triggered.")
            LOGS_CACHE["entries"] = []
            LOGS_CACHE["last_reset"] = datetime.now().strftime("%Y-%m-%d")
            save_logs(LOGS_CACHE)
    except Exception as e:
        logger.error(f"Error resetting logs: {e}")

async def watch_filters():
    """Polls the filter file for changes and reloads if needed."""
    last_mtime = 0
    while True:
        try:
            if os.path.exists(DB_FILE):
                current_mtime = os.path.getmtime(DB_FILE)
                if current_mtime > last_mtime:
                    if last_mtime != 0:
                        logger.info(f"Change detected in {DB_FILE}. Reloading...")
                    load_filters()
                    last_mtime = current_mtime
        except Exception as e:
            logger.error(f"Error watching {DB_FILE}: {e}")
        await asyncio.sleep(2)  # Check every 2 seconds

async def delete_after(msg, delay):
    """Deletes a message after a delay without blocking."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

def clean_filename(filename):
    """Removes extension and replaces -, _, etc with whitespace."""
    if not filename:
        return ""
    # Remove extension
    name = os.path.splitext(filename)[0]
    # Replace -, _, and . with spaces
    name = re.sub(r'[-_.]', ' ', name)
    # Collapse multiple spaces and trim
    return " ".join(name.split())

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to block media by ID (reply) or keyword (text)."""
    msg = update.message
    user_id = msg.from_user.id
    chat_id = msg.chat_id
    args = context.args

    # 1. ADMIN CHECK
    if msg.chat.type != "private":
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["creator", "administrator"]:
            await msg.reply_text("❌ This command is only for administrators.")
            return

    db = FILTERS_CACHE
    if not db:
        db = load_filters()

    # 2. CASE A: REPLY TO MESSAGE
    if msg.reply_to_message:
        target = msg.reply_to_message
        mode = args[0].lower() if args else "id_only" # Default mode if no args
        response_text = ""
        
        # Sticker Pack logic (always blocks pack name)
        if target.sticker and target.sticker.set_name:
            pack_name = target.sticker.set_name
            if pack_name not in db["sticker"]:
                db["sticker"].append(pack_name)
                response_text = f"✅ Blocked Sticker Pack: `{pack_name}`"
            else:
                response_text = f"ℹ️ Pack `{pack_name}` already blocked."
        else:
            # File ID logic
            file_uid = None
            f_name = None
            cat = "video_photo"

            if target.photo: file_uid = target.photo[-1].file_unique_id
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
                response = await msg.reply_text("❌ No Unique ID or Filename found.")
                asyncio.create_task(delete_after(msg, 5)); asyncio.create_task(delete_after(response, 5))
                return

            # Determine whether to block the File ID
            # Modes starting with 'yes' or being empty block the ID. 'no' modes do not.
            if mode in ["yes", "yesedit", "id_only"]:
                if file_uid and file_uid not in db["blocked_ids"]:
                    db["blocked_ids"].append(file_uid)
                    response_text = f"✅ Blocked File ID: `{file_uid}`"
                else:
                    response_text = f"ℹ️ ID already blocked."
            else:
                response_text = "ℹ️ Skipping File ID block."

            # Mode Logic for keywords/filenames
            if mode in ["yesedit", "noedit"]:
                PENDING_EDITS[user_id] = {'category': cat}
                response_text += f"\n✍️ Send the keyword/phrase you want to add to **{cat}**."
            elif mode in ["yes", "no"] and f_name:
                cleaned_name = clean_filename(f_name)
                if cleaned_name.lower() not in [k.lower() for k in db[cat]]:
                    db[cat].append(cleaned_name)
                    response_text += f"\n✅ Also blocked original filename: `{cleaned_name}`"
                else:
                    response_text += f"\nℹ️ Filename `{cleaned_name}` already blocked."

        save_filters(db)
        response = await msg.reply_text(response_text, parse_mode='Markdown')
        asyncio.create_task(delete_after(msg, 5)); asyncio.create_task(delete_after(response, 5))
        return

    # 3. CASE B: NO REPLY (Text Args)
    # Syntax: /block [category] [keyword]
    if len(args) < 2:
        response = await msg.reply_text("❌ Usage:\nReply: `/block [yes/yesedit/no/noedit]`\nText: `/block [category] [keyword]`", parse_mode='Markdown')
        asyncio.create_task(delete_after(msg, 5)); asyncio.create_task(delete_after(response, 5))
        return

    category = args[0].lower()
    keyword = " ".join(args[1:]).lower()

    if category not in ["global", "video_photo", "animation", "sticker"]:
        response = await msg.reply_text("❌ Invalid category. Use: global, video_photo, animation, sticker")
        asyncio.create_task(delete_after(msg, 5)); asyncio.create_task(delete_after(response, 5))
        return

    if keyword not in [k.lower() for k in db[category]]:
        db[category].append(keyword)
        save_filters(db)
        response = await msg.reply_text(f"✅ Added `{keyword}` to **{category}**", parse_mode='Markdown')
    else:
        response = await msg.reply_text(f"ℹ️ `{keyword}` already exists in **{category}**", parse_mode='Markdown')

    asyncio.create_task(delete_after(msg, 5)); asyncio.create_task(delete_after(response, 5))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text for keyword editing (after /block yesedit) or regular media checks if needed."""
    msg = update.message
    if not msg or not msg.text:
        return

    user_id = msg.from_user.id
    
    # Check if this user is waiting to send a custom keyword (from /block yesedit)
    if user_id in PENDING_EDITS:
        state = PENDING_EDITS.pop(user_id)
        category = state['category']
        keyword = msg.text.lower().strip()
        
        db = FILTERS_CACHE
        if not db:
            db = load_filters()
        
        if keyword not in [k.lower() for k in db[category]]:
            db[category].append(keyword)
            save_filters(db)
            res = await msg.reply_text(f"✅ Added `{keyword}` to **{category}**", parse_mode='Markdown')
        else:
            res = await msg.reply_text(f"ℹ️ `{keyword}` already exists in **{category}**", parse_mode='Markdown')
        
        asyncio.create_task(delete_after(msg, 5))
        asyncio.create_task(delete_after(res, 5))
        return

# --- FILTER LOGIC ---
async def check_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    logger.info(f"--- Processing message from User: {user.first_name} (ID: {user.id}) ---")

    # Use Cached DB instead of reading from disk every time
    db = FILTERS_CACHE
    if not db:
        db = load_filters()
    
    should_delete = False
    reason = ""
    content_str = ""
    check_list = []
    media_type = "Unknown"
    file_uid = None

    # 1. EXTRACT DATA BASED ON TYPE
    if msg.photo:
        media_type = "Photo"
        file_uid = msg.photo[-1].file_unique_id
        content_str = msg.caption or ""
        check_list = db.get("video_photo", [])

    elif msg.video:
        media_type = "Video"
        file_uid = msg.video.file_unique_id
        file_name = clean_filename(msg.video.file_name) or ""
        content_str = f"{file_name} {msg.caption or ''}".strip()
        check_list = db.get("video_photo", [])

    elif msg.animation:
        media_type = "Animation/GIF"
        file_uid = msg.animation.file_unique_id
        file_name = clean_filename(msg.animation.file_name) or ""
        content_str = f"{file_name} {msg.caption or ''}".strip()
        check_list = db.get("animation", [])

    elif msg.document:
        mime = msg.document.mime_type or ""
        file_name = clean_filename(msg.document.file_name) or ""
        file_uid = msg.document.file_unique_id
        content_str = f"{file_name} {msg.caption or ''}".strip()

        if mime.startswith('image/'):
            media_type = "Photo (File)"
            check_list = db.get("video_photo", [])
        elif mime.startswith('video/'):
            media_type = "Video (File)"
            check_list = db.get("video_photo", [])
        elif mime.startswith('audio/'):
            media_type = "Audio"
            check_list = db.get("global", [])
        else:
            media_type = "Document"
            check_list = db.get("global", [])

    elif msg.sticker:
        media_type = "Sticker"
        file_uid = msg.sticker.file_unique_id
        set_name = msg.sticker.set_name or "No Pack Name"
        emoji = msg.sticker.emoji or "No Emoji"
        content_str = set_name + " " + emoji
        check_list = db.get("sticker", [])

    logger.info(f"Analyzed {media_type} | ID: {file_uid}")

    # 2. PERFORM CHECK
    # Check Whitelist first
    if file_uid and file_uid in db.get("whitelisted_ids", []):
        logger.info(f"Result: ALLOWED (ID {file_uid} is in whitelist)")
        return

    # Check Blacklist ID
    if file_uid and file_uid in db.get("blocked_ids", []):
        should_delete = True
        reason = f"File ID {file_uid} is manually blocked"
        logger.warning(f"MATCH FOUND: {reason}")
    else:
        # Normalize to lowercase for case-insensitive matching
        searchable_text = content_str.lower()
        
        # Merge specific check_list with global list
        global_list = db.get("global", [])
        full_check_list = check_list + global_list
        
        logger.debug(f"Checking string: '{searchable_text}' against blocked list")

        for banned in full_check_list:
            if banned.lower() in searchable_text:
                should_delete = True
                reason = f"Found blocked keyword '{banned}' in {media_type}"
                logger.warning(f"MATCH FOUND: {reason} (File ID: {file_uid})")
                break
    
    if not should_delete:
        logger.info("Result: CLEAN (No blocked content found)")

    # 3. EXECUTE ACTION
    if should_delete:
        try:
            await msg.delete()
            logger.info(f"ACTION: Deleted message {msg.message_id} successfully.")
            
            # Add to separate activity logs
            logs_data = load_logs()
            log_entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": user.id,
                "username": user.username or user.first_name,
                "media_type": media_type,
                "reason": reason,
                "content": (content_str[:100] + '...') if len(content_str) > 100 else content_str
            }
            logs_data["entries"].insert(0, log_entry)
            # Keep only last 1000 logs in the file
            logs_data["entries"] = logs_data["entries"][:1000]
            save_logs(logs_data)

            # Send warning and schedule deletion without blocking this task
            warning = await msg.chat.send_message(f"⚠️ Message deleted: {reason}")
            asyncio.create_task(delete_after(warning, 5))
            
        except Exception as e:
            logger.error(f"FAILURE: Could not delete message. Error: {e}")

# --- MAIN RUNNER ---
async def main():
    # Initial load
    load_filters()
    load_logs()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    media_filter = (
        filters.PHOTO | 
        filters.VIDEO | 
        filters.ANIMATION | 
        filters.Sticker.ALL | 
        filters.Document.ALL
    )

    application.add_handler(MessageHandler(media_filter, check_media, block=False))
    application.add_handler(CommandHandler("block", block_command))
    # Handler for text inputs (required for the 'yesedit' follow-up keyword)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    # Start the file watcher as a background task
    asyncio.create_task(watch_filters())

    print("Bot is running. Blocked keywords will auto-apply when filters.json is saved.")
    
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass