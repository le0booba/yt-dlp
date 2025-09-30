import logging
import os
import time
import yt_dlp
import telebot
from telebot import types, apihelper
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")

if not BOT_TOKEN:
    raise ValueError("The BOT_TOKEN environment variable must be set.")
if not APP_DOMAIN:
    raise ValueError("The RAILWAY_PUBLIC_DOMAIN environment variable was not found.")

VOLUME_PATH = os.environ.get("VOLUME_PATH", "/data")
TELEGRAM_FILE_LIMIT_MB = int(os.environ.get("TELEGRAM_FILE_LIMIT_MB", 49))
COOKIE_LIFETIME_DAYS = 7
DOWNLOAD_DIR = "downloads"
COOKIES_DIR = os.path.join(VOLUME_PATH, "user_cookies")

WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{APP_DOMAIN}{WEBHOOK_URL_PATH}"

CB_FORMAT = "format:"
CB_COOKIE_PROMPT = "cookie:"
CB_COOKIE_ACTION = "use_cookie:"

# --- New Download Options ---
DOWNLOAD_OPTIONS = {
    "max_quality": {"label": "🏆 Max Quality (Best Video+Audio)", "format": "bestvideo+bestaudio/best"},
    "1080p_mp4": {"label": "🎞️ 1080p MP4 (Full HD)", "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"},
    "720p_mp4": {"label": "🎬 720p MP4 (HD Ready)", "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"},
    "best_audio_m4a": {"label": "🎵 Best Audio (M4A/Opus)", "format": "bestaudio/best"},
    "best_audio_mp3": {"label": "🎵 Best Audio (MP3)", "format": "bestaudio/best", "postprocessing": "mp3"}
}

# --- Initialization ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)
user_data = {}

# --- Helper Functions ---
def create_directories():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COOKIES_DIR, exist_ok=True)

def get_cookie_path(user_id):
    return os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")

def cleanup_files(filepath):
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
            logger.info(f"Removed temporary file: {filepath}")
        except OSError as e:
            logger.error(f"Error removing file {filepath}: {e}")

def cleanup_old_cookies():
    logger.info("Running scheduled cookie cleanup task...")
    cutoff = datetime.now() - timedelta(days=COOKIE_LIFETIME_DAYS)
    try:
        for filename in os.listdir(COOKIES_DIR):
            file_path = os.path.join(COOKIES_DIR, filename)
            if os.path.isfile(file_path):
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_mod_time < cutoff:
                    os.remove(file_path)
                    logger.info(f"Removed old cookie file: {filename}")
    except Exception as e:
        logger.error(f"Error during cookie cleanup: {e}")

# --- Core Logic ---
class VideoDownloader:
    def __init__(self, chat_id: int, message_id: int):
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_update_time = 0

    def progress_hook(self, d):
        if d["status"] != "downloading": return
        current_time = time.time()
        if current_time - self.last_update_time > 2:
            status_text = (f"⏳ Downloading...\n\n" f"- Progress: {d.get('_percent_str', 'N/A').strip()}\n" f"- Speed: {d.get('_speed_str', 'N/A').strip()}\n" f"- ETA: {d.get('_eta_str', 'N/A').strip()}")
            try:
                bot.edit_message_text(status_text, self.chat_id, self.message_id)
                self.last_update_time = current_time
            except apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e): logger.warning(f"Progress update failed: {e}")

    def download(self, url, option_key, cookie_file=None):
        option = DOWNLOAD_OPTIONS.get(option_key, DOWNLOAD_OPTIONS["max_quality"])

        ffmpeg_location = os.path.abspath('bin')
        
        ydl_opts = {
            "format": option["format"],
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [self.progress_hook],
            "noprogress": True,
            "cookiefile": cookie_file if cookie_file and os.path.exists(cookie_file) else None,
            "ffmpeg_location": ffmpeg_location,
        }
        
        if option.get("postprocessing") == "mp3":
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]

        filepath = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filepath = ydl.prepare_filename(info)
                bot.edit_message_text("✅ Download complete. Preparing to upload...", self.chat_id, self.message_id)
                ydl.download([url])

                if option.get("postprocessing") == "mp3":
                    filepath = os.path.splitext(filepath)[0] + '.mp3'
            
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if file_size_mb > TELEGRAM_FILE_LIMIT_MB:
                bot.send_message(self.chat_id, f"❌ Error: File is too large ({file_size_mb:.2f} MB).")
                return
            
            with open(filepath, "rb") as file_to_send:
                caption = info.get('title', 'video')
                if option.get("postprocessing") == "mp3":
                    bot.send_audio(self.chat_id, audio=file_to_send, caption=caption)
                else:
                    bot.send_document(self.chat_id, document=file_to_send, caption=caption)
        except Exception as e:
            logger.error(f"Error during download: {e}")
            bot.send_message(self.chat_id, f"❌ An error occurred during download: {str(e)[:1000]}")
        finally:
            cleanup_files(filepath)

def initiate_download_process(call, cookie_path=None):
    user_id = call.from_user.id
    session = user_data.pop(user_id, None)
    if not session:
        bot.answer_callback_query(call.id, "Session expired. Please send the link again.", show_alert=True)
        return

    text = "🚀 Preparing to download..." + (" (with cookie)" if cookie_path else "")
    msg = bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    
    downloader = VideoDownloader(chat_id=msg.chat.id, message_id=msg.message_id)
    downloader.download(session['url'], session['format_key'], cookie_file=cookie_path)

# --- Web Server and Webhooks ---
@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Bad Request', 400

@app.route('/health')
def health_check():
    return "OK", 200

# --- Telegram Bot Handlers ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, f"Hello, {message.from_user.first_name}!\n\nSend me a video link, and I will download it.")

@bot.message_handler(func=lambda message: message.text and (message.text.startswith('http://') or message.text.startswith('https://')))
def handle_link(message):
    user_data[message.from_user.id] = {'url': message.text}
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, value in DOWNLOAD_OPTIONS.items():
        markup.add(types.InlineKeyboardButton(value["label"], callback_data=f"{CB_FORMAT}{key}"))
    bot.send_message(message.chat.id, "Please select a download option:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith(CB_FORMAT))
def handle_format_selection(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired. Please send the link again.", show_alert=True)
        return
    
    user_data[user_id]['format_key'] = call.data[len(CB_FORMAT):]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Yes", callback_data=f"{CB_COOKIE_PROMPT}yes"), types.InlineKeyboardButton("No", callback_data=f"{CB_COOKIE_PROMPT}no"))
    bot.edit_message_text("Use cookies (for private videos)?", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(CB_COOKIE_PROMPT))
def handle_cookie_prompt(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    
    if call.data.endswith('no'):
        initiate_download_process(call)
    else:
        cookie_path = get_cookie_path(user_id)
        if os.path.exists(cookie_path):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Use Saved Cookie", callback_data=f"{CB_COOKIE_ACTION}saved"), types.InlineKeyboardButton("📥 Upload New Cookie", callback_data=f"{CB_COOKIE_ACTION}upload"))
            bot.edit_message_text("You have a saved cookie file. What would you like to do?", call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text("Okay. Please send me your `cookies.txt` file.", call.message.chat.id, call.message.message_id)
            bot.register_next_step_handler(call.message, handle_cookie_file)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(CB_COOKIE_ACTION))
def handle_cookie_action(call):
    if call.data.endswith('upload'):
        bot.edit_message_text("Okay. Awaiting a new `cookies.txt` file.", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, handle_cookie_file)
    elif call.data.endswith('saved'):
        initiate_download_process(call, cookie_path=get_cookie_path(call.from_user.id))
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
        cookie_path = get_cookie_path(user_id)
        with open(cookie_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        call_mock = types.CallbackQuery(id=0, from_user=message.from_user, data="", chat_instance="", json_string="", message=message)
        initiate_download_process(call_mock, cookie_path=cookie_path)
    except Exception as e:
        logger.error(f"Error processing cookie file: {e}")
        bot.reply_to(message, "Failed to process the cookie file.")

# --- Application Entry Point and Scheduler ---
create_directories()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(cleanup_old_cookies, 'interval', days=1)
scheduler.start()

bot.remove_webhook()
time.sleep(0.5)
bot.set_webhook(url=WEBHOOK_URL)
logger.info(f"Webhook set to {WEBHOOK_URL}")
