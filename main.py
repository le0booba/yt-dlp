import logging
import os
import time
import yt_dlp
import telebot
from telebot import types
from flask import Flask, request

# --- 1. КОНФИГУРАЦИЯ ---

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Необходимо установить переменную окружения BOT_TOKEN")

APP_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if not APP_DOMAIN:
    raise ValueError("Не найдена переменная окружения RAILWAY_PUBLIC_DOMAIN")

WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{APP_DOMAIN}{WEBHOOK_URL_PATH}"

# --- ИЗМЕНЕНИЕ: Пути теперь указывают на Volume ---
# Путь монтирования, который вы указали в настройках Railway
VOLUME_PATH = "/data" 
# Папка для cookies будет создана внутри постоянного хранилища
COOKIES_DIR = os.path.join(VOLUME_PATH, "user_cookies")
# Папка для временных загрузок может остаться в эфемерной системе
DOWNLOAD_DIR = "downloads"

TELEGRAM_FILE_LIMIT_MB = 49

# --- 2. ИНИЦИАЛИЗАЦИЯ ---

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

user_data = {}

# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КЛАССЫ ---

def create_directories():
    # Эта функция теперь создает папки и в Volume, и в локальной системе
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COOKIES_DIR, exist_ok=True)
    logger.info(f"Проверка директорий: {DOWNLOAD_DIR}, {COOKIES_DIR}")

# ... (Класс VideoDownloader и функция cleanup_files остаются без изменений) ...
def cleanup_files(filepath, cookie_path=None, persistent_cookie=False):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Удален временный файл видео: {filepath}")
        # --- ИЗМЕНЕНИЕ: Не удаляем cookie, если он должен быть постоянным ---
        if cookie_path and os.path.exists(cookie_path) and not persistent_cookie:
            os.remove(cookie_path)
            logger.info(f"Удален временный файл cookie: {cookie_path}")
    except Exception as e:
        logger.error(f"Ошибка при удалении файлов: {e}")

class VideoDownloader:
    # ... (код класса без изменений) ...
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
                status_text = f"⏳ Скачивание...\n\n- Прогресс: {percent}\n- Скорость: {speed}\n- ETA: {eta}"
                try:
                    bot.edit_message_text(status_text, self.chat_id, self.message_id)
                    self.last_update_time = current_time
                except Exception as e:
                    pass
        elif d["status"] == "finished":
            bot.edit_message_text("✅ Загрузка завершена. Начинаю постобработку...", self.chat_id, self.message_id)

    def download(self, url, format_choice, cookie_file=None):
        ydl_opts = {"format": format_choice, "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"), "noplaylist": True, "progress_hooks": [self.progress_hook], "noprogress": True}
        if cookie_file and os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            logger.info(f"Используется файл cookie: {cookie_file}")
        
        filepath = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filepath = ydl.prepare_filename(info)
                ydl.download([url])
            
            bot.edit_message_text("📤 Загружаю видео в Telegram...", self.chat_id, self.message_id)
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if file_size_mb > TELEGRAM_FILE_LIMIT_MB:
                bot.send_message(self.chat_id, f"❌ Ошибка: Файл слишком большой ({file_size_mb:.2f} МБ).")
                return
            with open(filepath, "rb") as video_file:
                bot.send_document(self.chat_id, document=video_file, caption=info.get('title', 'video'))
        except Exception as e:
            logger.error(f"Ошибка при скачивании: {e}")
            bot.send_message(self.chat_id, f"❌ Произошла ошибка при скачивании: {str(e)[:1000]}")
        finally:
            # --- ИЗМЕНЕНИЕ: Указываем, что cookie-файл из Volume удалять не нужно ---
            is_persistent = cookie_file and COOKIES_DIR in cookie_file
            cleanup_files(filepath, persistent_cookie=is_persistent)


# --- 4. WEB-СЕРВЕР (обработка вебхуков) ---
# ... (код веб-сервера без изменений) ...
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

# --- 5. ЛОГИКА БОТА (обработчики сообщений) ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, f"Привет, {message.from_user.first_name}!\n\nОтправь мне ссылку на видео, и я его скачаю.", parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_link(message):
    user_id = message.from_user.id
    user_data[user_id] = {'url': message.text}
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🏆 Лучшее (видео+аудио)", callback_data="format:best"),
        types.InlineKeyboardButton("🎵 Только аудио (m4a)", callback_data="format:m4a/bestaudio/best"),
        types.InlineKeyboardButton("🎞️ MP4 (совместимый)", callback_data="format:bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4")
    )
    bot.send_message(message.chat.id, "Выберите формат:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('format:'))
def handle_format_selection(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Сессия истекла, отправьте ссылку заново.", show_alert=True)
        return
    user_data[user_id]['format'] = call.data.split(':')[1]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Да", callback_data="cookie:yes"), types.InlineKeyboardButton("Нет", callback_data="cookie:no"))
    bot.edit_message_text("Использовать cookies (для приватных видео)?", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cookie:'))
def handle_cookie_selection(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Сессия истекла.", show_alert=True)
        return
    
    if call.data.split(':')[1] == 'no':
        msg = bot.edit_message_text("🚀 Начинаю подготовку...", call.message.chat.id, call.message.message_id)
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'])
        del user_data[user_id]
    else:
        # --- ИЗМЕНЕНИЕ: Проверяем наличие сохраненного cookie ---
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        if os.path.exists(cookie_path):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("✅ Использовать сохраненный", callback_data="use_cookie:saved"),
                types.InlineKeyboardButton("📥 Загрузить новый", callback_data="use_cookie:upload")
            )
            bot.edit_message_text("У вас есть сохраненный файл cookie. Что делаем?", call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text("Хорошо. Теперь отправь мне файл `cookies.txt`.", call.message.chat.id, call.message.message_id)
            bot.register_next_step_handler(call.message, handle_cookie_file)
    bot.answer_callback_query(call.id)

# --- ИЗМЕНЕНИЕ: Новый обработчик для выбора действия с cookie ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('use_cookie:'))
def handle_use_cookie_choice(call):
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Сессия истекла.", show_alert=True)
        return
    
    choice = call.data.split(':')[1]
    if choice == 'upload':
        bot.edit_message_text("Хорошо. Жду новый файл `cookies.txt`.", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, handle_cookie_file)
    elif choice == 'saved':
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        msg = bot.edit_message_text(f"🚀 Использую сохраненный cookie. Начинаю подготовку...", call.message.chat.id, call.message.message_id)
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'], cookie_file=cookie_path)
        del user_data[user_id]
    bot.answer_callback_query(call.id)

def handle_cookie_file(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        bot.reply_to(message, "Сессия истекла, отправьте ссылку заново.")
        return
    if not message.document or not message.document.file_name.endswith('.txt'):
        bot.reply_to(message, "Пожалуйста, отправьте файл в формате .txt. Попробуйте еще раз.")
        bot.register_next_step_handler(message, handle_cookie_file)
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        # --- ИЗМЕНЕНИЕ: Сохраняем файл в постоянное хранилище (Volume) ---
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        with open(cookie_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        msg = bot.reply_to(message, "Файл cookies сохранен. 🚀 Начинаю подготовку к скачиванию...")
        downloader = VideoDownloader(chat_id=message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'], cookie_file=cookie_path)
        del user_data[user_id]
    except Exception as e:
        logger.error(f"Ошибка при обработке файла cookies: {e}")
        bot.reply_to(message, "Не удалось обработать файл cookies.")

# --- 6. ЗАПУСК ---

if __name__ == "__main__":
    create_directories()
    logger.info(f"Устанавливаю вебхук: {WEBHOOK_URL}")
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Вебхук успешно установлен.")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
