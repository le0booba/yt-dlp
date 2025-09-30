import os


# Направляем логи доступа в stdout
accesslog = "-"

# Направляем логи ошибок (включая служебные) в stdout
errorlog = "-"

# Задаем адрес и порт для прослушивания
# Gunicorn автоматически подхватит порт из переменной $PORT на Railway
bind = "0.0.0.0:" + os.environ.get("PORT", "8080")

# Количество рабочих процессов (можно настроить)
workers = int(os.environ.get("WEB_CONCURRENCY", 2))
