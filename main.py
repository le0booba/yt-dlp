import logging
import os
import time
import yt_dlp
import telebot
from telebot import types
from flask import Flask, request

# --- 1. КОНФИГУРАЦИЯ ---

# Безопасно получаем токен из переменных окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Необходимо установить переменную окружения BOT_TOKEN")

# Формируем URL для вебхука из переменных окружения Railway
# Railway предоставляет домен в переменной RAILWAY_PUBLIC_DOMAIN
APP_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if not APP_DOMAIN:
    # Запасной вариант, если переменная не найдена (замените на свой URL)
    # raise ValueError("Не найдена переменная окружения RAILWAY_PUBLIC_DOMAIN")
    print("ПРЕДУПРЕЖДЕНИЕ: Переменная RAILWAY_PUBLIC_DOMAIN не найдена. Вебхук может быть установлен неверно.")
    # Укажите ваш домен вручную, если это необходимо
    APP_DOMAIN = "your-app-name.up.railway.app" 

# Секретный путь, чтобы никто другой не мог отправлять запросы нашему боту
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{APP_DOMAIN}{WEBHOOK_URL_PATH}"

# Остальные настройки
DOWNLOAD_DIR = "downloads"
COOKIES_DIR = "user_cookies"
TELEGRAM_FILE_LIMIT_MB = 49

# --- 2. ИНИЦИАЛИЗАЦИЯ ---

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация Flask-приложения и Telebot
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Словарь для хранения данных пользователей между шагами
user_data = {}

# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КЛАССЫ (логика скачивания) ---
# Эта часть остается практически без изменений

def create_directories():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COOKIES_DIR, exist_ok=True)

def cleanup_files(filepath, cookie_path=None):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Удален файл: {filepath}")
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)
            logger.info(f"Удален файл cookies: {cookie_path}")
    except Exception as e:
        logger.error(f"Ошибка при удалении файлов: {e}")

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
                status_text = f"⏳ Скачивание...\n\n- Прогресс: {percent}\n- Скорость: {speed}\n- ETA: {eta}"
                try:
                    bot.edit_message_text(status_text, self.chat_id, self.message_id)
                    self.last_update_time = current_time
                except Exception as e:
                    logger.warning(f"Не удалось обновить сообщение: {e}")
        elif d["status"] == "finished":
            bot.edit_message_text("✅ Загрузка завершена. Начинаю постобработку...", self.chat_id, self.message_id)

    def download(self, url, format_choice, cookie_file=None):
        ydl_opts = {
            "format": format_choice,
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [self.progress_hook],
            "noprogress": True,
        }
        if cookie_file and os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
        
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
                bot.send_document(self.chat_id, video_file)
        except Exception as e:
            logger.error(f"Ошибка при скачивании: {e}")
            bot.send_message(self.chat_id, f"❌ Произошла ошибка при скачивании: {str(e)[:1000]}")
        finally:
            cleanup_files(filepath, cookie_file)

# --- 4. WEB-СЕРВЕР (обработка вебхуков) ---

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    """Этот эндпоинт принимает обновления от Telegram."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return 'Bad Request', 400

@app.route('/health')
def health_check():
    """Эндпоинт для проверки здоровья, чтобы Railway не усыплял сервис."""
    return "OK", 200

# --- 5. ЛОГИКА БОТА (обработчики сообщений) ---
# Эта часть остается полностью без изменений

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(
        message,
        f"Привет, {message.from_user.first_name}!\n\n"
        f"Отправь мне ссылку на видео, и я его скачаю.\n\n"
        f"⚠️ **Помни**: я могу отправлять файлы размером не более {TELEGRAM_FILE_LIMIT_MB} МБ.",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_link(message):
    user_id = message.from_user.id
    user_data[user_id] = {'url': message.text}
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn1 = types.InlineKeyboardButton("🏆 Лучшее (видео+аудио)", callback_data="format:best")
    btn2 = types.InlineKeyboardButton("🎵 Только аудио (m4a)", callback_data="format:m4a/bestaudio/best")
    btn3 = types.InlineKeyboardButton("🎞️ MP4 (совместимый)", callback_data="format:bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4")
    markup.add(btn1, btn2, btn3)
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
    if user_id not in user_data or 'format' not in user_data[user_id]:
        bot.answer_callback_query(call.id, "Сессия истекла, отправьте ссылку заново.", show_alert=True)
        return
    if call.data.split(':')[1] == 'no':
        msg = bot.edit_message_text("🚀 Начинаю подготовку к скачиванию...", call.message.chat.id, call.message.message_id)
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'])
        del user_data[user_id]
    else:
        bot.edit_message_text("Хорошо. Теперь отправь мне файл `cookies.txt`.", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, handle_cookie_file)
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
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        with open(cookie_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        msg = bot.reply_to(message, "Файл cookies получен. 🚀 Начинаю подготовку к скачиванию...")
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
    # Устанавливаем вебхук после небольшой задержки, чтобы сервер успел запуститься
    time.sleep(1) 
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Вебхук успешно установлен.")
    
    # Запускаем веб-сервер Flask
    # Railway автоматически предоставит порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
