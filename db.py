from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

import pymysql


BASE_DIR = Path(__file__).resolve().parent
MYSQL_CONFIG_PATH = BASE_DIR / "mysql_config.json"


class DatabaseError(RuntimeError):
    pass


def load_mysql_config() -> dict[str, Any]:
    if not MYSQL_CONFIG_PATH.exists():
        raise DatabaseError(f"Файл настроек MySQL не найден: {MYSQL_CONFIG_PATH}")

    with MYSQL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    required_keys = ["host", "user", "password", "database", "port", "charset"]
    for key in required_keys:
        if key not in config:
            raise DatabaseError(f"В mysql_config.json отсутствует поле '{key}'")

    return config


def get_connection():
    config = load_mysql_config()
    try:
        return pymysql.connect(
            host=config["host"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            port=int(config["port"]),
            charset=config["charset"],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseError(f"Не удалось подключиться к MySQL: {exc}") from exc


def fetch_all(query: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, list(params or []))
                return cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise DatabaseError(f"Ошибка запроса к MySQL: {exc}") from exc


def fetch_one(query: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(query, params)
    return rows[0] if rows else None


def run_transaction(callback: Callable[[Any], Any]) -> Any:
    config = load_mysql_config()
    connection = None
    try:
        connection = pymysql.connect(
            host=config["host"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            port=int(config["port"]),
            charset=config["charset"],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        result = callback(connection)
        connection.commit()
        return result
    except pymysql.MySQLError as exc:
        if connection is not None:
            connection.rollback()
        raise DatabaseError(f"Ошибка записи в MySQL: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
