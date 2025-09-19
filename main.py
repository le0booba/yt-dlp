import logging
import os
import time
import yt_dlp
import telebot
from telebot import types
from flask import Flask, request

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("The BOT_TOKEN environment variable must be set.")

APP_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if not APP_DOMAIN:
    raise ValueError("The RAILWAY_PUBLIC_DOMAIN environment variable was not found.")

WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{APP_DOMAIN}{WEBHOOK_URL_PATH}"

VOLUME_PATH = "/data"
COOKIES_DIR = os.path.join(VOLUME_PATH, "user_cookies")
DOWNLOAD_DIR = "downloads"
TELEGRAM_FILE_LIMIT_MB = 49

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

user_data = {}

def create_directories():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COOKIES_DIR, exist_ok=True)
    logger.info(f"Verified directories: {DOWNLOAD_DIR}, {COOKIES_DIR}")

def cleanup_files(filepath, cookie_path=None, persistent_cookie=False):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Removed temporary video file: {filepath}")
        if cookie_path and os.path.exists(cookie_path) and not persistent_cookie:
            os.remove(cookie_path)
            logger.info(f"Removed temporary cookie file: {cookie_path}")
    except Exception as e:
        logger.error(f"Error during file cleanup: {e}")

class VideoDownloader:
    def __init__(self, chat_id: int, message_id: int):
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_update_time = 0

    def progress_hook(self, d):
        if d["status"] == "downloading":
            current_time = time.time()
            if current_time - self.last_update_time > 2:
                percent = d.get("_percent_str", "0.0%").strip()
                speed = d.get("_speed_str", "N/A").strip()
                eta = d.get("_eta_str", "N/A").strip()
                status_text = f"⏳ Downloading...\n\n- Progress: {percent}\n- Speed: {speed}\n- ETA: {eta}"
                try:
                    bot.edit_message_text(status_text, self.chat_id, self.message_id)
                    self.last_update_time = current_time
                except Exception:
                    pass
        elif d["status"] == "finished":
            bot.edit_message_text("✅ Download complete. Starting post-processing...", self.chat_id, self.message_id)

    def download(self, url, format_choice, cookie_file=None):
        ydl_opts = {"format": format_choice, "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"), "noplaylist": True, "progress_hooks": [self.progress_hook], "noprogress": True}
        if cookie_file and os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            logger.info(f"Using cookie file: {cookie_file}")
        
        filepath = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filepath = ydl.prepare_filename(info)
                ydl.download([url])
            
            bot.edit_message_text("📤 Uploading video to Telegram...", self.chat_id, self.message_id)
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if file_size_mb > TELEGRAM_FILE_LIMIT_MB:
                bot.send_message(self.chat_id, f"❌ Error: The file is too large ({file_size_mb:.2f} MB).")
                return
            with open(filepath, "rb") as video_file:
                bot.send_document(self.chat_id, document=video_file, caption=info.get('title', 'video'))
        except Exception as e:
            logger.error(f"Error during download: {e}")
            bot.send_message(self.chat_id, f"❌ An error occurred during download: {str(e)[:1000]}")
        finally:
            is_persistent = cookie_file and COOKIES_DIR in cookie_file
            cleanup_files(filepath, persistent_cookie=is_persistent)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return 'Bad Request', 400

@app.route('/health')
def health_check():
    return "OK", 200

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, f"Hello, {message.from_user.first_name}!\n\nSend me a link to a video, and I will download it.", parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_link(message):
    user_id = message.from_user.id
    user_data[user_id] = {'url': message.text}
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🏆 Best (Video+Audio)", callback_data="format:best"),
        types.InlineKeyboardButton("🎵 Audio Only (m4a)", callback_data="format:m4a/bestaudio/best"),
        types.InlineKeyboardButton("🎞️ MP4 (Compatible)", callback_data="format:bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4")
    )
    bot.send_message(message.chat.id, "Please select a format:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('format:'))
def handle_format_selection(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired. Please send the link again.", show_alert=True)
        return
    user_data[user_id]['format'] = call.data.split(':')[1]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Yes", callback_data="cookie:yes"), types.InlineKeyboardButton("No", callback_data="cookie:no"))
    bot.edit_message_text("Use cookies (for private videos)?", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cookie:'))
def handle_cookie_selection(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    
    if call.data.split(':')[1] == 'no':
        msg = bot.edit_message_text("🚀 Preparing to download...", call.message.chat.id, call.message.message_id)
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'])
        del user_data[user_id]
    else:
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        if os.path.exists(cookie_path):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("✅ Use Saved Cookie", callback_data="use_cookie:saved"),
                types.InlineKeyboardButton("📥 Upload New Cookie", callback_data="use_cookie:upload")
            )
            bot.edit_message_text("You have a saved cookie file. What would you like to do?", call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text("Okay. Now please send me your `cookies.txt` file.", call.message.chat.id, call.message.message_id)
            bot.register_next_step_handler(call.message, handle_cookie_file)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('use_cookie:'))
def handle_use_cookie_choice(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    
    choice = call.data.split(':')[1]
    if choice == 'upload':
        bot.edit_message_text("Okay. Awaiting a new `cookies.txt` file.", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, handle_cookie_file)
    elif choice == 'saved':
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        msg = bot.edit_message_text(f"🚀 Using the saved cookie. Preparing to download...", call.message.chat.id, call.message.message_id)
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'], cookie_file=cookie_path)
        del user_data[user_id]
    bot.answer_callback_query(call.id)

def handle_cookie_file(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        bot.reply_to(message, "Session expired. Please send the link again.")
        return
    if not message.document or not message.document.file_name.endswith('.txt'):
        bot.reply_to(message, "Please send a file in .txt format. Try again.")
        bot.register_next_step_handler(message, handle_cookie_file)
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        with open(cookie_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        msg = bot.reply_to(message, "Cookie file saved. 🚀 Preparing to download...")
        downloader = VideoDownloader(chat_id=message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'], cookie_file=cookie_path)
        del user_data[user_id]
    except Exception as e:
        logger.error(f"Error processing cookie file: {e}")
        bot.reply_to(message, "Failed to process the cookie file.")

if __name__ == "__main__":
    create_directories()
    logger.info(f"Setting webhook: {WEBHOOK_URL}")
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Webhook set successfully.")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
