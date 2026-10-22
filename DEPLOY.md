# Как залить и запустить сайт на сервере

Проект уже подготовлен для Flask + MySQL. Самый простой вариант - загрузить архив на хостинг с поддержкой Python/Passenger.

## 1. Перед загрузкой

На компьютере открой PowerShell в папке проекта:

```powershell
cd C:\Users\Doni\Desktop\sait
```

Собери архив:

```powershell
.\build_deploy.ps1
```

Архив будет здесь:

```text
C:\Users\Doni\Desktop\sait\deploy\sait-deploy.zip
```

## 2. Что загрузить на хостинг

Загрузи содержимое архива в папку сайта на сервере.

Обычно это одна из папок:

```text
public_html
www
имя_домена/public_html
```

В корне сайта после распаковки должны лежать:

```text
app.py
db.py
passenger_wsgi.py
requirements.txt
mysql_config.json
templates/
static/
.htaccess
```

## 3. Настройка MySQL

На сервере создай базу MySQL и пользователя.

В файле `mysql_config.json` укажи данные от хостинга:

```json
{
  "host": "localhost",
  "port": 3306,
  "user": "db_user",
  "password": "db_password",
  "database": "db_name",
  "charset": "utf8mb4"
}
```

Если хостинг дал отдельный адрес MySQL, впиши его в `host`.

## 4. Установка зависимостей

Если на хостинге есть терминал/SSH:

```bash
cd ~/public_html
python3 -m pip install --user -r requirements.txt
```

Если SSH нет, оставь в проекте папку `_vendor`: `passenger_wsgi.py` умеет подключать зависимости из нее.

## 5. Настройка Python-приложения в панели хостинга

В панели хостинга выбери Python-приложение.

Основные значения:

```text
Папка приложения: папка, куда загружен сайт
Startup file: passenger_wsgi.py
Application object: application
Python version: 3.10 или новее
```

После сохранения перезапусти приложение в панели.

## 6. Проверка

Открой домен в браузере.

Если появилась ошибка:

- проверь данные в `mysql_config.json`;
- проверь, что база MySQL создана;
- проверь, что установлены Flask и PyMySQL;
- открой логи Python-приложения в панели хостинга.

## 7. Если это VPS

На VPS удобнее запускать через Gunicorn и Nginx:

```bash
cd /var/www/sait
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt gunicorn
gunicorn -w 2 -b 127.0.0.1:8000 wsgi:application
```

После этого настрой Nginx как reverse proxy на `127.0.0.1:8000`.
