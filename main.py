import logging
import os
import time
import yt_dlp
import telebot
from telebot import types # для создания кнопок

# --- Конфигурация ---
BOT_TOKEN = "8266534937:AAGl5x7bMrattP0RUAAo_HRw1iE6PORx-jE"  # ВАЖНО: Замените на ваш токен
DOWNLOAD_DIR = "downloads"
COOKIES_DIR = "user_cookies"
TELEGRAM_FILE_LIMIT_MB = 49

# Настройка логирования для отладки
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN)

# Словарь для хранения данных пользователей между шагами
user_data = {}

# --- Вспомогательные функции ---
def create_directories():
    """Создает необходимые директории, если их нет."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COOKIES_DIR, exist_ok=True)

def cleanup_files(filepath, cookie_path=None):
    """Удаляет временные файлы (видео и cookies)."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Удален файл: {filepath}")
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)
            logger.info(f"Удален файл cookies: {cookie_path}")
    except Exception as e:
        logger.error(f"Ошибка при удалении файлов: {e}")

# --- Основной класс для скачивания ---
class VideoDownloader:
    def __init__(self, chat_id: int, message_id: int):
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_update_time = 0

    def progress_hook(self, d):
        """Хук для отслеживания прогресса и обновления сообщения."""
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
        """Основной метод для запуска скачивания."""
        
        ydl_opts = {
            "format": format_choice,
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [self.progress_hook],
            "noprogress": True,
        }
        
        if cookie_file and os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            logger.info(f"Используются cookies: {cookie_file}")

        filepath = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filepath = ydl.prepare_filename(info)
                ydl.download([url])

            bot.edit_message_text("📤 Загружаю видео в Telegram...", self.chat_id, self.message_id)
            
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if file_size_mb > TELEGRAM_FILE_LIMIT_MB:
                bot.send_message(
                    self.chat_id,
                    f"❌ Ошибка: Файл слишком большой ({file_size_mb:.2f} МБ). "
                    f"Telegram позволяет загружать файлы до {TELEGRAM_FILE_LIMIT_MB} МБ."
                )
                return

            with open(filepath, "rb") as video_file:
                bot.send_document(self.chat_id, video_file)

        except Exception as e:
            logger.error(f"Ошибка при скачивании: {e}")
            bot.send_message(self.chat_id, f"❌ Произошла ошибка при скачивании: {e}")
        finally:
            cleanup_files(filepath, cookie_file)


# --- Обработчики команд и сообщений бота ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Отправляет приветственное сообщение."""
    bot.reply_to(
        message,
        f"Привет, {message.from_user.first_name}!\n\n"
        f"Отправь мне ссылку на видео, и я его скачаю. "
        f"Ты сможешь выбрать формат и использовать cookies для приватных видео.\n\n"
        f"⚠️ **Помни**: я могу отправлять файлы размером не более {TELEGRAM_FILE_LIMIT_MB} МБ.",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_link(message):
    """Принимает ссылку и предлагает выбрать формат."""
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
    """Обрабатывает выбор формата и спрашивает про cookies."""
    user_id = call.from_user.id
    if user_id not in user_data:
        bot.answer_callback_query(call.id, "Сессия истекла, отправьте ссылку заново.", show_alert=True)
        return

    chosen_format = call.data.split(':')[1]
    user_data[user_id]['format'] = chosen_format
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_yes = types.InlineKeyboardButton("Да", callback_data="cookie:yes")
    btn_no = types.InlineKeyboardButton("Нет", callback_data="cookie:no")
    markup.add(btn_yes, btn_no)
    
    bot.edit_message_text(
        "Использовать cookies (для приватных видео)?",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cookie:'))
def handle_cookie_selection(call):
    """Обрабатывает выбор использования cookies."""
    user_id = call.from_user.id
    if user_id not in user_data or 'format' not in user_data[user_id]:
        bot.answer_callback_query(call.id, "Сессия истекла, отправьте ссылку заново.", show_alert=True)
        return

    choice = call.data.split(':')[1]
    
    if choice == 'no':
        msg = bot.edit_message_text("🚀 Начинаю подготовку к скачиванию...", call.message.chat.id, call.message.message_id)
        
        downloader = VideoDownloader(chat_id=call.message.chat.id, message_id=msg.message_id)
        downloader.download(user_data[user_id]['url'], user_data[user_id]['format'])
        
        del user_data[user_id] # Очищаем данные пользователя
    else:
        bot.edit_message_text("Хорошо. Теперь отправь мне файл `cookies.txt`.", call.message.chat.id, call.message.message_id)
        # Регистрируем следующий шаг: ожидаем от пользователя документ
        bot.register_next_step_handler(call.message, handle_cookie_file)
        
    bot.answer_callback_query(call.id)


def handle_cookie_file(message):
    """Принимает файл cookies и запускает скачивание."""
    user_id = message.from_user.id
    if user_id not in user_data:
        bot.reply_to(message, "Сессия истекла, отправьте ссылку заново.")
        return

    if not message.document or message.document.mime_type != 'text/plain':
        bot.reply_to(message, "Пожалуйста, отправьте файл в формате .txt. Попробуйте еще раз.")
        bot.register_next_step_handler(message, handle_cookie_file) # Снова ждем файл
        return

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        cookie_path = os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")
        with open(cookie_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        msg = bot.reply_to(message, "Файл cookies получен. 🚀 Начинаю подготовку к скачиванию...")
        
        downloader = VideoDownloader(chat_id=message.chat.id, message_id=msg.message_id)
        downloader.download(
            url=user_data[user_id]['url'],
            format_choice=user_data[user_id]['format'],
            cookie_file=cookie_path
        )
        
        del user_data[user_id] # Очищаем данные

    except Exception as e:
        logger.error(f"Ошибка при обработке файла cookies: {e}")
        bot.reply_to(message, "Не удалось обработать файл cookies. Попробуйте снова.")


if __name__ == "__main__":
    create_directories()
    logger.info("Бот запущен...")
    # bot.polling() - это и есть long polling.
    # infinity_polling автоматически перезапускается при ошибках.
    bot.infinity_polling()
