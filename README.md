# Kassa Online

Веб-сайт кассовой программы на Flask с подключением к MySQL.

## Что внутри

- `app.py` - Flask-приложение.
- `db.py` - подключение к MySQL.
- `templates/` - HTML-шаблоны.
- `static/` - CSS и JS для страниц Flask.
- `mysql_config.json` - настройки базы данных для текущего окружения.
- `passenger_wsgi.py` - входной файл для shared-хостинга с Passenger.
- `requirements.txt` - Python-зависимости.
- `build_deploy.ps1` - сборка архива для загрузки на сервер.
- `DEPLOY.md` - пошаговая инструкция по загрузке и запуску.

## Локальный запуск

```powershell
cd C:\Users\Doni\Desktop\sait
python -m pip install -r requirements.txt
python app.py
```

После запуска открой:

```text
http://127.0.0.1:5000
```

## Настройка базы

Для работы нужен файл `mysql_config.json`:

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

Можно скопировать `mysql_config.example.json` и вписать реальные данные от хостинга.

## Архив для загрузки

```powershell
.\build_deploy.ps1
```

Готовый архив появится здесь:

```text
deploy\sait-deploy.zip
```

Дальше следуй инструкции в `DEPLOY.md`.
