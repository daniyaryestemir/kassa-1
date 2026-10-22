from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from db import DatabaseError, fetch_all, fetch_one, run_transaction


BASE_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)


def money(value: Any) -> str:
    if value is None:
        value = 0
    if isinstance(value, Decimal):
        value = float(value)
    return f"{value:,.2f}".replace(",", " ")


def dt(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value)


app.jinja_env.filters["money"] = money
app.jinja_env.filters["dt"] = dt


def hint_group(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 3
    first = text[0]
    if ("A" <= first <= "Z") or ("a" <= first <= "z"):
        return 0
    if ("А" <= first <= "Я") or ("а" <= first <= "я") or first in ("Ё", "ё"):
        return 1
    if first.isdigit():
        return 2
    return 3


app.jinja_env.filters["hint_group"] = hint_group


def ensure_receipt_item_product_uuid_column(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM spisok_postupleniy LIKE 'product_uuid'")
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE spisok_postupleniy
                ADD COLUMN product_uuid VARCHAR(36) NULL AFTER postuplenie_uuid
                """
            )


def ensure_receipt_autosave_columns(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM postupleniya LIKE 'is_draft'")
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE postupleniya
                ADD COLUMN is_draft TINYINT(1) NOT NULL DEFAULT 0 AFTER is_deleted
                """
            )

        cursor.execute("SHOW COLUMNS FROM postupleniya LIKE 'autosave_payload'")
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE postupleniya
                ADD COLUMN autosave_payload LONGTEXT NULL AFTER is_draft
                """
            )

        cursor.execute("SHOW COLUMNS FROM postupleniya LIKE 'autosave_updated_at'")
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE postupleniya
                ADD COLUMN autosave_updated_at DATETIME NULL AFTER autosave_payload
                """
            )


def backfill_receipt_item_product_uuid(connection) -> None:
    ensure_receipt_autosave_columns(connection)
    ensure_receipt_item_product_uuid_column(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE spisok_postupleniy s
            JOIN (
                SELECT nazvanie, MIN(uuid) AS product_uuid
                FROM tovary
                WHERE is_deleted = 0
                GROUP BY nazvanie
                HAVING COUNT(*) = 1
            ) t ON t.nazvanie = s.nazvanie
            SET s.product_uuid = t.product_uuid
            WHERE s.is_deleted = 0 AND (s.product_uuid IS NULL OR s.product_uuid = '')
            """
        )


def ensure_barcode_print_tables(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_print_jobs (
                id INT PRIMARY KEY AUTO_INCREMENT,
                uuid VARCHAR(36) UNIQUE,
                last_modified DATETIME,
                sync_status VARCHAR(100) DEFAULT 'pending',
                is_deleted BOOLEAN DEFAULT 0,
                created_at DATETIME NOT NULL,
                status VARCHAR(50) DEFAULT 'queue',
                print_name TINYINT(1) DEFAULT 1,
                print_price TINYINT(1) DEFAULT 1,
                note TEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_print_queue (
                id INT PRIMARY KEY AUTO_INCREMENT,
                uuid VARCHAR(36) UNIQUE,
                last_modified DATETIME,
                sync_status VARCHAR(100) DEFAULT 'pending',
                is_deleted BOOLEAN DEFAULT 0,
                job_uuid VARCHAR(36),
                product_uuid VARCHAR(36),
                barcode VARCHAR(255) NOT NULL,
                product_name VARCHAR(255) NOT NULL,
                price DECIMAL(10, 2) DEFAULT 0,
                copies INT DEFAULT 1,
                print_name TINYINT(1) DEFAULT 1,
                print_price TINYINT(1) DEFAULT 1,
                status VARCHAR(50) DEFAULT 'queue',
                created_at DATETIME NOT NULL,
                printed_at DATETIME NULL,
                error_text TEXT,
                source VARCHAR(50) DEFAULT 'web'
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
            """
        )


def resolve_receipt_item_product(cursor, item: dict[str, Any], now: datetime) -> str:
    product_uuid = (item.get("product_uuid") or "").strip()
    if product_uuid:
        cursor.execute(
            """
            UPDATE tovary
            SET kolichestvo = kolichestvo - %s,
                last_modified = %s,
                sync_status = 'pending'
            WHERE uuid = %s AND is_deleted = 0
            """,
            (float(item["kolichestvo"]), now, product_uuid),
        )
        if cursor.rowcount == 0:
            raise DatabaseError("Не удалось найти товар из накладной для пересчета остатков.")
        return product_uuid

    cursor.execute(
        """
        SELECT uuid
        FROM tovary
        WHERE nazvanie = %s AND is_deleted = 0
        LIMIT 2
        """,
        [item["nazvanie"]],
    )
    matches = cursor.fetchall()
    if len(matches) != 1:
        raise DatabaseError(
            "Нельзя безопасно пересчитать остатки для старой накладной: у товара нет стабильной привязки."
        )

    product_uuid = matches[0]["uuid"]
    cursor.execute(
        """
        UPDATE tovary
        SET kolichestvo = kolichestvo - %s,
            last_modified = %s,
            sync_status = 'pending'
        WHERE uuid = %s AND is_deleted = 0
        """,
        (float(item["kolichestvo"]), now, product_uuid),
    )
    if cursor.rowcount == 0:
        raise DatabaseError("Не удалось найти товар из накладной для пересчета остатков.")
    return product_uuid


def load_dashboard() -> dict[str, Any]:
    run_transaction(lambda connection: ensure_receipt_autosave_columns(connection))
    stats = fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM tovary WHERE is_deleted = 0) AS total_products,
            (SELECT COUNT(*) FROM kontragenty WHERE is_deleted = 0) AS total_suppliers,
            (SELECT COUNT(*) FROM postupleniya WHERE is_deleted = 0 AND COALESCE(is_draft, 0) = 0) AS total_receipts,
            (SELECT COALESCE(SUM(summa_postupleni), 0) FROM postupleniya WHERE is_deleted = 0 AND COALESCE(is_draft, 0) = 0) AS total_purchase_sum,
            (SELECT COUNT(*) FROM tovary WHERE is_deleted = 0 AND kolichestvo <= 5) AS low_stock_count,
            (SELECT COALESCE(SUM(kolichestvo * tsena_zakupki), 0) FROM tovary WHERE is_deleted = 0) AS stock_cost_sum,
            (SELECT COALESCE(SUM(kolichestvo * tsena_prodazhi), 0) FROM tovary WHERE is_deleted = 0) AS stock_retail_sum
        """
    ) or {}

    today = datetime.now().date()
    period_start = today - timedelta(days=29)

    check_rows = fetch_all(
        """
        SELECT data, tovary, summa, total_cost, total_gross_profit, nalichnye, karta, dolg, oplata
        FROM cheki
        WHERE is_deleted = 0 AND DATE(data) >= %s
        ORDER BY data ASC, id ASC
        """,
        [period_start.isoformat()],
    )

    daily_map: dict[str, dict[str, float | int | str]] = {}
    for offset in range(30):
        day = period_start + timedelta(days=offset)
        key = day.isoformat()
        daily_map[key] = {
            "date": key,
            "label": day.strftime("%d.%m"),
            "sales": 0.0,
            "profit": 0.0,
            "checks": 0,
        }

    product_map: dict[str, dict[str, float | str]] = {}
    payments = {"cash_total": 0.0, "card_total": 0.0, "debt_total": 0.0}
    period_summary = {
        "checks_count": 0,
        "sales_total": 0.0,
        "cost_total": 0.0,
        "profit_total": 0.0,
        "items_quantity": 0.0,
        "today_checks_count": 0,
        "today_sales_total": 0.0,
        "today_profit_total": 0.0,
    }

    for check in check_rows:
        check_date_value = check.get("data")
        if isinstance(check_date_value, datetime):
            check_date = check_date_value.date()
        else:
            try:
                check_date = datetime.fromisoformat(str(check_date_value)).date()
            except (TypeError, ValueError):
                check_date = today

        day_key = check_date.isoformat()
        sale_sum = float(check.get("summa") or 0)
        cost_sum = float(check.get("total_cost") or 0)
        profit_sum = float(check.get("total_gross_profit") or (sale_sum - cost_sum))

        period_summary["checks_count"] += 1
        period_summary["sales_total"] += sale_sum
        period_summary["cost_total"] += cost_sum
        period_summary["profit_total"] += profit_sum
        payments["cash_total"] += float(check.get("nalichnye") or 0)
        payments["card_total"] += float(check.get("karta") or 0)
        payments["debt_total"] += float(check.get("dolg") or 0)

        if day_key in daily_map:
            daily_map[day_key]["sales"] = float(daily_map[day_key]["sales"]) + sale_sum
            daily_map[day_key]["profit"] = float(daily_map[day_key]["profit"]) + profit_sum
            daily_map[day_key]["checks"] = int(daily_map[day_key]["checks"]) + 1

        if check_date == today:
            period_summary["today_checks_count"] += 1
            period_summary["today_sales_total"] += sale_sum
            period_summary["today_profit_total"] += profit_sum

        try:
            items = json.loads(check.get("tovary") or "[]")
        except (TypeError, json.JSONDecodeError):
            items = []

        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product_name") or item.get("name") or "").strip()
            if not name:
                continue
            quantity = float(item.get("quantity") or 0)
            sale_price = float(item.get("sale_price") or item.get("retail_price") or 0)
            cost_price = float(item.get("cost_price") or 0)
            revenue = quantity * sale_price
            cost = quantity * cost_price
            profit = revenue - cost
            period_summary["items_quantity"] += quantity

            product = product_map.setdefault(
                name,
                {
                    "name": name,
                    "quantity": 0.0,
                    "revenue": 0.0,
                    "cost": 0.0,
                    "profit": 0.0,
                },
            )
            product["quantity"] = float(product["quantity"]) + quantity
            product["revenue"] = float(product["revenue"]) + revenue
            product["cost"] = float(product["cost"]) + cost
            product["profit"] = float(product["profit"]) + profit

    period_summary["avg_check"] = (
        period_summary["sales_total"] / period_summary["checks_count"]
        if period_summary["checks_count"]
        else 0.0
    )

    chart_max = max(
        [float(row["sales"]) for row in daily_map.values()] + [1.0]
    )
    sales_chart = [
        {
            **row,
            "sales": round(float(row["sales"]), 2),
            "profit": round(float(row["profit"]), 2),
            "height": round((float(row["sales"]) / chart_max) * 100, 2),
        }
        for row in daily_map.values()
    ]

    products = list(product_map.values())
    top_sold_products = sorted(products, key=lambda row: float(row["quantity"]), reverse=True)[:8]
    top_revenue_products = sorted(products, key=lambda row: float(row["revenue"]), reverse=True)[:8]
    top_profit_products = sorted(products, key=lambda row: float(row["profit"]), reverse=True)[:8]

    payment_total = sum(payments.values()) or 1.0
    payment_mix = [
        {"label": "Наличные", "value": payments["cash_total"], "share": payments["cash_total"] / payment_total * 100},
        {"label": "Перевод", "value": payments["card_total"], "share": payments["card_total"] / payment_total * 100},
        {"label": "Долг", "value": payments["debt_total"], "share": payments["debt_total"] / payment_total * 100},
    ]

    recent_receipts = fetch_all(
        """
        SELECT uuid, kontragent, data, summa_postupleni, summa_prodazhi
        FROM postupleniya
        WHERE is_deleted = 0 AND COALESCE(is_draft, 0) = 0
        ORDER BY data DESC
        LIMIT 6
        """
    )

    top_products = fetch_all(
        """
        SELECT nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi
        FROM tovary
        WHERE is_deleted = 0
        ORDER BY kolichestvo ASC, nazvanie ASC
        LIMIT 8
        """
    )

    return {
        "stats": stats,
        "period_summary": period_summary,
        "payments": payments,
        "payment_mix": payment_mix,
        "sales_chart": sales_chart,
        "top_sold_products": top_sold_products,
        "top_revenue_products": top_revenue_products,
        "top_profit_products": top_profit_products,
        "recent_receipts": recent_receipts,
        "top_products": top_products,
    }


def load_postupleniya(search: str) -> list[dict[str, Any]]:
    run_transaction(lambda connection: ensure_receipt_autosave_columns(connection))
    sql = """
        SELECT id, uuid, kontragent, data, summa_postupleni, summa_prodazhi, summa_optom
        FROM postupleniya
        WHERE is_deleted = 0 AND COALESCE(is_draft, 0) = 0
    """
    params: list[Any] = []
    if search:
        sql += " AND (kontragent LIKE %s OR uuid LIKE %s)"
        like_value = f"%{search}%"
        params.extend([like_value, like_value])
    sql += " ORDER BY data DESC, id DESC"
    return fetch_all(sql, params)


def load_postuplenie_detail(receipt_uuid: str) -> dict[str, Any] | None:
    run_transaction(lambda connection: backfill_receipt_item_product_uuid(connection))
    receipt = fetch_one(
        """
        SELECT
            id,
            uuid,
            kontragent,
            data,
            summa_postupleni,
            summa_prodazhi,
            summa_optom,
            COALESCE(is_draft, 0) AS is_draft,
            autosave_payload,
            autosave_updated_at
        FROM postupleniya
        WHERE uuid = %s AND is_deleted = 0
        """,
        [receipt_uuid],
    )
    if not receipt:
        return None

    items = fetch_all(
        """
        SELECT
            s.id,
            s.product_uuid,
            s.nazvanie,
            s.kolichestvo,
            s.tsena_zakupki,
            s.tsena_prodazhi,
            s.tsena_optom,
            t.uuid AS linked_product_uuid
        FROM spisok_postupleniy s
        LEFT JOIN tovary t ON t.uuid = s.product_uuid AND t.is_deleted = 0
        WHERE s.postuplenie_uuid = %s AND s.is_deleted = 0
        ORDER BY s.id ASC
        """,
        [receipt_uuid],
    )
    for item in items:
        item["product_uuid"] = item.get("product_uuid") or item.get("linked_product_uuid")
        item["summa_zakupki"] = float(item["kolichestvo"]) * float(item["tsena_zakupki"])
        item["summa_optom_itogo"] = float(item["kolichestvo"]) * float(item["tsena_optom"])
        item["summa_prodazhi_itogo"] = float(item["kolichestvo"]) * float(item["tsena_prodazhi"])
    receipt["items"] = items
    return receipt


def load_nomenklatura(search: str) -> list[dict[str, Any]]:
    sql = """
        SELECT uuid, nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
        FROM tovary
        WHERE is_deleted = 0
    """
    params: list[Any] = []
    if search:
        sql += """
            AND (
                nazvanie LIKE %s
                OR uuid LIKE %s
                OR category LIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM dop_shtrih_kody d
                    WHERE d.tovar_uuid = tovary.uuid
                      AND d.is_deleted = 0
                      AND d.shtrih_kod LIKE %s
                )
            )
        """
        like_value = f"%{search}%"
        params.extend([like_value, like_value, like_value, like_value])
    sql += " ORDER BY nazvanie ASC"
    return fetch_all(sql, params)


def load_product(product_uuid: str) -> dict[str, Any] | None:
    product = fetch_one(
        """
        SELECT uuid, nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
        FROM tovary
        WHERE uuid = %s AND is_deleted = 0
        """,
        [product_uuid],
    )
    if not product:
        return None
    product["barcode_rows"] = load_product_barcodes(product_uuid)
    return product


def load_product_barcodes(product_uuid: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT uuid, shtrih_kod
        FROM dop_shtrih_kody
        WHERE tovar_uuid = %s AND is_deleted = 0
        ORDER BY shtrih_kod ASC
        """,
        [product_uuid],
    )


def calculate_ean13_check_digit(code12: str) -> int:
    total = 0
    for index, char in enumerate(code12):
        digit = int(char)
        total += digit * 3 if (index + 1) % 2 == 0 else digit
    return (10 - (total % 10)) % 10


def generate_next_barcode() -> str:
    rows = fetch_all(
        """
        SELECT shtrih_kod
        FROM dop_shtrih_kody
        WHERE shtrih_kod LIKE '200%%' AND is_deleted = 0
        """
    )
    existing = {
        row["shtrih_kod"]
        for row in rows
        if str(row.get("shtrih_kod") or "").isdigit() and len(str(row["shtrih_kod"])) == 13
    }

    base_code = 200000000000
    max_code = 299999999999

    candidate = base_code
    while candidate <= max_code:
        code12 = str(candidate).zfill(12)
        barcode = f"{code12}{calculate_ean13_check_digit(code12)}"
        if barcode not in existing:
            return barcode
        candidate += 1

    raise DatabaseError("Не удалось найти свободный штрихкод.")


def load_reports(date_from: str = "", date_to: str = "") -> dict[str, Any]:
    where = ["is_deleted = 0"]
    params: list[Any] = []

    if date_from:
        where.append("DATE(data) >= %s")
        params.append(date_from)
    if date_to:
        where.append("DATE(data) <= %s")
        params.append(date_to)

    where_sql = " AND ".join(where)

    checks = fetch_all(
        f"""
        SELECT data, tovary, summa, total_cost, total_gross_profit, nalichnye, karta, dolg
        FROM cheki
        WHERE {where_sql}
        ORDER BY data DESC
        """,
        params,
    )

    product_map: dict[str, dict[str, float | str]] = {}
    total_cost_sum = 0.0
    total_sale_sum = 0.0
    total_profit_sum = 0.0
    cash_total = 0.0
    card_total = 0.0
    debt_total = 0.0

    for check in checks:
        total_cost_sum += float(check.get("total_cost") or 0)
        total_sale_sum += float(check.get("summa") or 0)
        total_profit_sum += float(check.get("total_gross_profit") or 0)
        cash_total += float(check.get("nalichnye") or 0)
        card_total += float(check.get("karta") or 0)
        debt_total += float(check.get("dolg") or 0)

        raw_items = check.get("tovary") or "[]"
        try:
            items = json.loads(raw_items)
        except (TypeError, json.JSONDecodeError):
            items = []

        for item in items:
            try:
                name = str(item.get("product_name") or "").strip()
                quantity = float(item.get("quantity") or 0)
                cost_price = float(item.get("cost_price") or 0)
                sale_price = float(item.get("sale_price") or 0)
            except (TypeError, ValueError):
                continue

            if not name or quantity <= 0:
                continue

            product = product_map.setdefault(
                name,
                {
                    "name": name,
                    "quantity": 0.0,
                    "cost_sum": 0.0,
                    "sale_sum": 0.0,
                },
            )
            product["quantity"] = float(product["quantity"]) + quantity
            product["cost_sum"] = float(product["cost_sum"]) + (quantity * cost_price)
            product["sale_sum"] = float(product["sale_sum"]) + (quantity * sale_price)

    product_rows: list[dict[str, Any]] = []
    for product in product_map.values():
        quantity = float(product["quantity"])
        cost_sum = float(product["cost_sum"])
        sale_sum = float(product["sale_sum"])
        avg_cost = cost_sum / quantity if quantity else 0.0
        avg_sale = sale_sum / quantity if quantity else 0.0
        avg_profit = avg_sale - avg_cost
        total_profit = sale_sum - cost_sum
        product_rows.append(
            {
                "name": product["name"],
                "quantity": quantity,
                "avg_cost": avg_cost,
                "cost_sum": cost_sum,
                "avg_sale": avg_sale,
                "sale_sum": sale_sum,
                "avg_profit": avg_profit,
                "total_profit": total_profit,
            }
        )

    product_rows.sort(key=lambda row: str(row["name"]).lower())

    return {
        "summary": {
            "receipts_count": len(checks),
            "total_cost_sum": total_cost_sum,
            "total_sale_sum": total_sale_sum,
            "total_profit_sum": total_profit_sum,
        },
        "payments": {
            "cash_total": cash_total,
            "card_total": card_total,
            "debt_total": debt_total,
        },
        "items": product_rows,
    }


def load_kontragenty() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT uuid, name, last_modified
        FROM kontragenty
        WHERE is_deleted = 0
        ORDER BY name ASC
        LIMIT 300
        """
    )


def load_postupleniya_kontragenty() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT uuid, name
        FROM kontragenty
        WHERE is_deleted = 0
        ORDER BY name ASC
        """
    )


def load_supplier(supplier_uuid: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT uuid, name, last_modified
        FROM kontragenty
        WHERE uuid = %s AND is_deleted = 0
        """,
        [supplier_uuid],
    )


def load_product_options() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT
            t.uuid,
            t.nazvanie,
            t.kolichestvo,
            t.tsena_zakupki,
            t.tsena_prodazhi,
            t.tsena_optom,
            GROUP_CONCAT(DISTINCT d.shtrih_kod ORDER BY d.shtrih_kod SEPARATOR ', ') AS barcodes
        FROM tovary t
        LEFT JOIN dop_shtrih_kody d ON d.tovar_uuid = t.uuid AND d.is_deleted = 0
        WHERE t.is_deleted = 0
        GROUP BY t.uuid, t.nazvanie, t.kolichestvo, t.tsena_zakupki, t.tsena_prodazhi, t.tsena_optom
        ORDER BY t.nazvanie ASC
        """
    )


def load_recent_checks(limit: int = 3) -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT uuid, data, summa, oplata, tovary
        FROM cheki
        WHERE is_deleted = 0
        ORDER BY data DESC, id DESC
        LIMIT %s
        """,
        [limit],
    )
    for row in rows:
        row["items_count"] = 0
        row["display_sum"] = float(row.get("summa") or 0)
        raw_items = row.get("tovary") or "[]"
        try:
            items = json.loads(raw_items)
        except Exception:
            items = []
        row["items_count"] = len(items) if isinstance(items, list) else 0
    return rows


def load_cash_products() -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT
            t.uuid,
            t.nazvanie,
            t.kolichestvo,
            t.tsena_zakupki,
            t.tsena_prodazhi,
            t.tsena_optom,
            GROUP_CONCAT(DISTINCT d.shtrih_kod ORDER BY d.shtrih_kod SEPARATOR ', ') AS barcodes
        FROM tovary t
        LEFT JOIN dop_shtrih_kody d ON d.tovar_uuid = t.uuid AND d.is_deleted = 0
        WHERE t.is_deleted = 0
        GROUP BY t.uuid, t.nazvanie, t.kolichestvo, t.tsena_zakupki, t.tsena_prodazhi, t.tsena_optom
        ORDER BY t.nazvanie ASC
        """
    )
    for row in rows:
        row["kolichestvo"] = float(row.get("kolichestvo") or 0)
        row["tsena_zakupki"] = float(row.get("tsena_zakupki") or 0)
        row["tsena_prodazhi"] = float(row.get("tsena_prodazhi") or 0)
        row["tsena_optom"] = float(row.get("tsena_optom") or 0)
        row["barcode_rows"] = [
            barcode.strip()
            for barcode in str(row.get("barcodes") or "").split(",")
            if barcode.strip()
        ]
    return rows


def load_barcode_print_queue(limit: int = 300) -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT
            q.uuid,
            q.job_uuid,
            q.product_uuid,
            q.barcode,
            q.product_name,
            q.price,
            q.copies,
            q.print_name,
            q.print_price,
            q.status,
            q.created_at,
            q.printed_at,
            q.error_text,
            q.source
        FROM barcode_print_queue q
        WHERE q.is_deleted = 0
        ORDER BY q.created_at DESC, q.id DESC
        LIMIT %s
        """,
        [limit],
    )
    for row in rows:
        row["price"] = float(row.get("price") or 0)
        row["copies"] = int(row.get("copies") or 1)
    return rows


def build_barcode_print_queue_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_job: dict[str, dict[str, Any]] = {}
    fallback_index = 0

    for row in rows:
        job_uuid = str(row.get("job_uuid") or "").strip()
        if not job_uuid:
            fallback_index += 1
            job_uuid = f"single::{fallback_index}"

        group = by_job.get(job_uuid)
        if group is None:
            group = {
                "job_uuid": job_uuid,
                "created_at": row.get("created_at"),
                "status": row.get("status") or "",
                "items": [],
            }
            by_job[job_uuid] = group
            groups.append(group)

        group["items"].append(row)

        current_status = str(group.get("status") or "")
        row_status = str(row.get("status") or "")
        if current_status != row_status:
            if "processing" in (current_status, row_status):
                group["status"] = "processing"
            elif "error" in (current_status, row_status):
                group["status"] = "error"
            elif "queue" in (current_status, row_status):
                group["status"] = "queue"
            elif row_status:
                group["status"] = row_status

    for group in groups:
        group["items_count"] = len(group["items"])
        group["total_copies"] = sum(int(item.get("copies") or 0) for item in group["items"])

    return groups


def build_barcode_print_products(preselected_ids: set[str] | None = None) -> list[dict[str, Any]]:
    latest_rows = load_barcode_print_queue(limit=500)
    latest_map: dict[str, dict[str, Any]] = {}
    for row in latest_rows:
        product_uuid = str(row.get("product_uuid") or "").strip()
        if product_uuid and product_uuid not in latest_map:
            latest_map[product_uuid] = row

    products = load_cash_products()
    if preselected_ids:
        products = [product for product in products if product["uuid"] in preselected_ids]
    prepared: list[dict[str, Any]] = []
    selected_set = preselected_ids or set()
    for product in products:
        primary_barcode = product["barcode_rows"][0] if product.get("barcode_rows") else ""
        latest_state = latest_map.get(product["uuid"], {})
        prepared.append(
            {
                **product,
                "primary_barcode": primary_barcode,
                "selected": product["uuid"] in selected_set,
                "queue_status": latest_state.get("status") or "",
                "queue_created_at": latest_state.get("created_at"),
                "queue_printed_at": latest_state.get("printed_at"),
                "queue_copies": latest_state.get("copies") or 1,
            }
        )

    prepared.sort(
        key=lambda item: (
            0 if item["selected"] else 1,
            0 if item.get("primary_barcode") else 1,
            str(item.get("nazvanie") or "").lower(),
        )
    )
    return prepared


def build_receipt_barcode_print_entries(preselected_ids: set[str]) -> list[dict[str, Any]]:
    products = [product for product in load_cash_products() if product["uuid"] in preselected_ids]
    prepared: list[dict[str, Any]] = []
    for product in products:
        barcode_rows = product.get("barcode_rows") or []
        if barcode_rows:
            for barcode in barcode_rows:
                prepared.append(
                    {
                        **product,
                        "entry_id": f"{product['uuid']}::{barcode}",
                        "barcode": barcode,
                        "selected": True,
                        "queue_copies": 1,
                    }
                )
        else:
            prepared.append(
                {
                    **product,
                    "entry_id": f"{product['uuid']}::",
                    "barcode": "",
                    "selected": True,
                    "queue_copies": 1,
                }
            )

    prepared.sort(
        key=lambda item: (
            0 if item.get("barcode") else 1,
            str(item.get("nazvanie") or "").lower(),
            str(item.get("barcode") or ""),
        )
    )
    return prepared


def create_barcode_print_job(
    selected_product_uuids: list[str],
    copies_map: dict[str, int],
    print_name: bool,
    print_price: bool,
    from_receipt: bool = False,
) -> str:
    selected_set = {str(value or "").strip() for value in selected_product_uuids if str(value or "").strip()}
    if not selected_set:
        raise ValueError("Выберите хотя бы один товар для очереди печати.")

    products = {product["uuid"]: product for product in load_cash_products()}
    rows_to_create: list[dict[str, Any]] = []
    for product_uuid in selected_product_uuids:
        normalized_uuid = str(product_uuid or "").strip()
        if not normalized_uuid or normalized_uuid not in selected_set:
            continue
        resolved_product_uuid = normalized_uuid
        selected_barcode = ""
        if from_receipt:
            resolved_product_uuid, _, selected_barcode = normalized_uuid.partition("::")
            resolved_product_uuid = resolved_product_uuid.strip()
            selected_barcode = selected_barcode.strip()
        product = products.get(resolved_product_uuid)
        if not product:
            continue
        barcode_rows = product.get("barcode_rows") or []
        primary_barcode = barcode_rows[0] if barcode_rows else ""
        barcode_to_print = selected_barcode if from_receipt else primary_barcode
        if not barcode_to_print:
            raise ValueError(f"У товара '{product['nazvanie']}' нет штрихкода для печати.")
        if from_receipt and barcode_to_print not in barcode_rows:
            raise ValueError(f"Штрихкод '{barcode_to_print}' у товара '{product['nazvanie']}' не найден.")
        copies = max(1, int(copies_map.get(normalized_uuid, 1) or 1))
        rows_to_create.append(
            {
                "product_uuid": resolved_product_uuid,
                "barcode": barcode_to_print,
                "product_name": product["nazvanie"],
                "price": float(product.get("tsena_prodazhi") or 0),
                "copies": copies,
            }
        )

    if not rows_to_create:
        raise ValueError("Не удалось подготовить товары для очереди печати.")

    job_uuid = str(uuid4())
    now = datetime.now()

    def _transaction(connection):
        ensure_barcode_print_tables(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO barcode_print_jobs (
                    uuid, last_modified, sync_status, is_deleted, created_at,
                    status, print_name, print_price, note
                ) VALUES (%s, %s, 'pending', 0, %s, 'queue', %s, %s, %s)
                """,
                (job_uuid, now, now, 1 if print_name else 0, 1 if print_price else 0, ""),
            )

            for row in rows_to_create:
                cursor.execute(
                    """
                    INSERT INTO barcode_print_queue (
                        uuid, last_modified, sync_status, is_deleted, job_uuid,
                        product_uuid, barcode, product_name, price, copies,
                        print_name, print_price, status, created_at, printed_at,
                        error_text, source
                    ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, %s, %s, %s, 'queue', %s, NULL, '', 'web')
                    """,
                    (
                        str(uuid4()),
                        now,
                        job_uuid,
                        row["product_uuid"],
                        row["barcode"],
                        row["product_name"],
                        row["price"],
                        row["copies"],
                        1 if print_name else 0,
                        1 if print_price else 0,
                        now,
                    ),
                )

    run_transaction(_transaction)
    return job_uuid


def parse_receipt_form(form_data) -> tuple[str, list[dict[str, Any]]]:
    kontragent = form_data.get("kontragent", "").strip()
    product_uuids = form_data.getlist("product_uuid")
    names = form_data.getlist("nazvanie")
    quantities = form_data.getlist("kolichestvo")
    purchase_prices = form_data.getlist("tsena_zakupki")
    retail_prices = form_data.getlist("tsena_prodazhi")
    wholesale_prices = form_data.getlist("tsena_optom")

    rows: list[dict[str, Any]] = []
    for index in range(len(names)):
        name = (names[index] or "").strip()
        qty_raw = (quantities[index] or "").strip()
        if not name and not qty_raw:
            continue
        if not name:
            raise ValueError(f"В строке {index + 1} не указано название товара.")

        try:
            quantity = float(qty_raw or "0")
            purchase_price = float((purchase_prices[index] or "0").strip())
            retail_price = float((retail_prices[index] or "0").strip())
            wholesale_price = float((wholesale_prices[index] or "0").strip())
        except ValueError as exc:
            raise ValueError(f"В строке {index + 1} есть неверное число.") from exc

        if quantity <= 0:
            raise ValueError(f"В строке {index + 1} количество должно быть больше нуля.")

        rows.append(
            {
                "product_uuid": (product_uuids[index] or "").strip() or None,
                "nazvanie": name,
                "kolichestvo": quantity,
                "tsena_zakupki": purchase_price,
                "tsena_prodazhi": retail_price,
                "tsena_optom": wholesale_price,
            }
        )

    if not kontragent:
        raise ValueError("Укажите контрагента.")
    if not rows:
        raise ValueError("Добавьте хотя бы один товар в поступление.")

    merged_rows: list[dict[str, Any]] = []
    row_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["product_uuid"] or row["nazvanie"].strip().lower()
        if key in row_map:
            row_map[key]["kolichestvo"] += row["kolichestvo"]
            row_map[key]["tsena_zakupki"] = row["tsena_zakupki"]
            row_map[key]["tsena_prodazhi"] = row["tsena_prodazhi"]
            row_map[key]["tsena_optom"] = row["tsena_optom"]
        else:
            normalized_row = dict(row)
            row_map[key] = normalized_row
            merged_rows.append(normalized_row)

    return kontragent, merged_rows


def normalize_receipt_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    raw_rows = source.get("rows")
    rows: list[dict[str, Any]] = []
    if isinstance(raw_rows, list):
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            rows.append(
                {
                    "product_uuid": str(raw_row.get("product_uuid") or "").strip(),
                    "nazvanie": str(raw_row.get("nazvanie") or "").strip(),
                    "kolichestvo": str(raw_row.get("kolichestvo") or "").strip(),
                    "tsena_zakupki": str(raw_row.get("tsena_zakupki") or "").strip(),
                    "tsena_prodazhi": str(raw_row.get("tsena_prodazhi") or "").strip(),
                    "tsena_optom": str(raw_row.get("tsena_optom") or "").strip(),
                }
            )
    return {
        "kontragent": str(source.get("kontragent") or "").strip(),
        "rows": rows,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }


def parse_receipt_autosave_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return normalize_receipt_snapshot(payload)


def load_receipt_snapshot(receipt: dict[str, Any] | None) -> dict[str, Any]:
    if not receipt:
        return {"kontragent": "", "rows": []}

    raw_payload = receipt.get("autosave_payload")
    if raw_payload:
        try:
            return normalize_receipt_snapshot(json.loads(raw_payload))
        except Exception:
            pass

    return {
        "kontragent": receipt.get("kontragent", "") or "",
        "rows": [
            {
                "product_uuid": item.get("product_uuid") or "",
                "nazvanie": item.get("nazvanie") or "",
                "kolichestvo": str(item.get("kolichestvo") or ""),
                "tsena_zakupki": str(item.get("tsena_zakupki") or ""),
                "tsena_prodazhi": str(item.get("tsena_prodazhi") or ""),
                "tsena_optom": str(item.get("tsena_optom") or ""),
            }
            for item in receipt.get("items", [])
        ],
    }


def serialize_cash_product(product: dict[str, Any]) -> dict[str, Any]:
    barcode_rows = [
        barcode.strip()
        for barcode in (product.get("barcode_rows") or [])
        if str(barcode or "").strip()
    ]
    return {
        "uuid": product["uuid"],
        "nazvanie": product["nazvanie"],
        "kolichestvo": float(product.get("kolichestvo") or 0),
        "tsena_zakupki": float(product.get("tsena_zakupki") or 0),
        "tsena_prodazhi": float(product.get("tsena_prodazhi") or 0),
        "tsena_optom": float(product.get("tsena_optom") or 0),
        "barcodes": barcode_rows,
        "primary_barcode": barcode_rows[0] if barcode_rows else "",
    }


def serialize_nomenklatura_product(product: dict[str, Any]) -> dict[str, Any]:
    barcode_rows = load_product_barcodes(product["uuid"])
    barcodes = [
        str(row.get("shtrih_kod") or "").strip()
        for row in barcode_rows
        if str(row.get("shtrih_kod") or "").strip()
    ]
    return {
        "uuid": product["uuid"],
        "nazvanie": product["nazvanie"],
        "kolichestvo": float(product.get("kolichestvo") or 0),
        "tsena_zakupki": float(product.get("tsena_zakupki") or 0),
        "tsena_prodazhi": float(product.get("tsena_prodazhi") or 0),
        "tsena_optom": float(product.get("tsena_optom") or 0),
        "category": product.get("category") or "",
        "barcodes": barcodes,
    }


def parse_cash_sale_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Чек передан в неверном формате.")

    sale_mode = str(payload.get("sale_mode", "retail") or "retail").strip().lower()
    if sale_mode not in {"retail", "wholesale"}:
        sale_mode = "retail"

    payment_method = str(payload.get("payment_method", "cash") or "cash").strip().lower()
    if payment_method not in {"cash", "card", "debt", "mixed"}:
        payment_method = "cash"

    try:
        cash_amount = float(str(payload.get("cash_amount", "0")).strip() or "0")
        card_amount = float(str(payload.get("card_amount", "0")).strip() or "0")
        debt_amount = float(str(payload.get("debt_amount", "0")).strip() or "0")
        paid_amount = float(str(payload.get("paid_amount", "0")).strip() or "0")
        change_amount = float(str(payload.get("change_amount", "0")).strip() or "0")
    except ValueError as exc:
        raise ValueError("Проверьте суммы оплаты.") from exc

    client_debt = str(payload.get("client_debt", "") or "").strip() or None

    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        product_uuid = str(raw_item.get("product_uuid", "") or "").strip()
        name = str(raw_item.get("product_name", "") or "").strip()
        if not product_uuid or not name:
            raise ValueError(f"В строке {index} не удалось определить товар.")
        try:
            quantity = float(str(raw_item.get("quantity", "0")).strip() or "0")
            cost_price = float(str(raw_item.get("cost_price", "0")).strip() or "0")
            retail_price = float(str(raw_item.get("retail_price", "0")).strip() or "0")
            wholesale_price = float(str(raw_item.get("wholesale_price", "0")).strip() or "0")
            stock = float(str(raw_item.get("stock", "0")).strip() or "0")
        except ValueError as exc:
            raise ValueError(f"В строке {index} есть неверное число.") from exc

        if quantity <= 0:
            raise ValueError(f"В строке {index} количество должно быть больше нуля.")

        sale_price = wholesale_price if sale_mode == "wholesale" else retail_price
        if sale_price < 0:
            raise ValueError(f"В строке {index} цена не может быть отрицательной.")

        items.append(
            {
                "product_uuid": product_uuid,
                "product_name": name,
                "quantity": quantity,
                "stock": stock,
                "cost_price": cost_price,
                "retail_price": retail_price,
                "wholesale_price": wholesale_price,
                "sale_price": sale_price,
                "gross_profit": sale_price - cost_price,
            }
        )

    if not items:
        raise ValueError("Добавьте хотя бы один товар в чек.")

    total_sum = sum(item["quantity"] * item["sale_price"] for item in items)
    total_cost = sum(item["quantity"] * item["cost_price"] for item in items)
    total_gross_profit = total_sum - total_cost

    if payment_method == "cash":
        if cash_amount <= 0:
            cash_amount = paid_amount
        card_amount = 0.0
        debt_amount = 0.0
    elif payment_method == "card":
        if card_amount <= 0:
            card_amount = paid_amount
        cash_amount = 0.0
        debt_amount = 0.0
    elif payment_method == "debt":
        debt_amount = total_sum
        cash_amount = 0.0
        card_amount = 0.0
        paid_amount = 0.0
        change_amount = 0.0

    allocated_total = cash_amount + card_amount + debt_amount
    if payment_method != "debt" and allocated_total <= 0:
        raise ValueError("Укажите сумму оплаты.")
    if payment_method == "mixed" and abs(allocated_total - total_sum) > 0.01:
        raise ValueError("Для смешанной оплаты сумма частей должна совпадать с итогом чека.")
    if payment_method in {"cash", "card"} and allocated_total + 0.01 < total_sum:
        raise ValueError("Недостаточно суммы для оплаты чека.")
    if payment_method == "debt" and not client_debt:
        raise ValueError("Для продажи в долг укажите клиента.")

    if payment_method == "cash":
        paid_amount = cash_amount if paid_amount <= 0 else paid_amount
        change_amount = max(paid_amount - total_sum, 0)
    elif payment_method == "card":
        paid_amount = card_amount if paid_amount <= 0 else paid_amount
        change_amount = 0.0
    elif payment_method == "mixed":
        paid_amount = cash_amount + card_amount
        change_amount = 0.0

    return {
        "sale_mode": sale_mode,
        "payment_method": payment_method,
        "cash_amount": round(cash_amount, 2),
        "card_amount": round(card_amount, 2),
        "debt_amount": round(debt_amount, 2),
        "paid_amount": round(paid_amount, 2),
        "change_amount": round(change_amount, 2),
        "client_debt": client_debt,
        "items": items,
        "total_sum": round(total_sum, 2),
        "total_cost": round(total_cost, 2),
        "total_gross_profit": round(total_gross_profit, 2),
    }


def create_cash_sale(data: dict[str, Any]) -> str:
    now = datetime.now()
    check_uuid = str(uuid4())

    payment_label_map = {
        "cash": "Наличные",
        "card": "Переводом",
        "debt": "В долг",
        "mixed": "Смешанная",
    }
    oplata = payment_label_map.get(data["payment_method"], "Наличные")

    serialized_items = json.dumps(
        [
            {
                "product_uuid": item["product_uuid"],
                "product_name": item["product_name"],
                "quantity": item["quantity"],
                "cost_price": item["cost_price"],
                "sale_price": item["sale_price"],
                "gross_profit": round(item["gross_profit"], 2),
            }
            for item in data["items"]
        ],
        ensure_ascii=False,
    )

    def _transaction(connection):
        with connection.cursor() as cursor:
            for item in data["items"]:
                cursor.execute(
                    """
                    SELECT uuid, nazvanie, kolichestvo
                    FROM tovary
                    WHERE uuid = %s AND is_deleted = 0
                    FOR UPDATE
                    """,
                    [item["product_uuid"]],
                )
                product = cursor.fetchone()
                if product is None:
                    raise DatabaseError(f"Товар '{item['product_name']}' не найден.")

            for item in data["items"]:
                cursor.execute(
                    """
                    UPDATE tovary
                    SET kolichestvo = kolichestvo - %s,
                        last_modified = %s,
                        sync_status = 'pending'
                    WHERE uuid = %s AND is_deleted = 0
                    """,
                    (item["quantity"], now, item["product_uuid"]),
                )

            cursor.execute(
                """
                INSERT INTO cheki (
                    uuid, last_modified, sync_status, is_deleted, data, tovary, summa, oplata,
                    skidka, total_cost, total_gross_profit, nalichnye, karta, dolg, klient_dolg,
                    spent_bonuses, vnesennaya_summa, sdacha, klient_id
                ) VALUES (
                    %s, %s, 'pending', 0, %s, %s, %s, %s,
                    0, %s, %s, %s, %s, %s, %s,
                    0, %s, %s, NULL
                )
                """,
                (
                    check_uuid,
                    now,
                    now,
                    serialized_items,
                    data["total_sum"],
                    oplata,
                    data["total_cost"],
                    data["total_gross_profit"],
                    data["cash_amount"],
                    data["card_amount"],
                    data["debt_amount"],
                    data["client_debt"],
                    data["paid_amount"],
                    data["change_amount"],
                ),
            )

        return check_uuid

    return run_transaction(_transaction)


def create_receipt(kontragent: str, rows: list[dict[str, Any]], receipt_uuid: str | None = None) -> str:
    now = datetime.now()
    postuplenie_uuid = receipt_uuid or str(uuid4())
    purchase_sum = sum(row["kolichestvo"] * row["tsena_zakupki"] for row in rows)
    retail_sum = sum(row["kolichestvo"] * row["tsena_prodazhi"] for row in rows)
    wholesale_sum = sum(row["kolichestvo"] * row["tsena_optom"] for row in rows)

    def _transaction(connection):
        ensure_receipt_autosave_columns(connection)
        ensure_receipt_item_product_uuid_column(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM kontragenty
                WHERE name = %s AND is_deleted = 0
                LIMIT 1
                """,
                [kontragent],
            )
            supplier = cursor.fetchone()
            if supplier is None:
                cursor.execute(
                    """
                    INSERT INTO kontragenty (
                        name, uuid, last_modified, sync_status, is_deleted
                    ) VALUES (%s, %s, %s, 'pending', 0)
                    """,
                    (kontragent, str(uuid4()), now),
                )

            if receipt_uuid:
                cursor.execute(
                    """
                    UPDATE postupleniya
                    SET kontragent = %s,
                        data = %s,
                        summa_postupleni = %s,
                        summa_prodazhi = %s,
                        summa_optom = %s,
                        last_modified = %s,
                        sync_status = 'pending',
                        is_draft = 0,
                        autosave_payload = NULL,
                        autosave_updated_at = NULL
                    WHERE uuid = %s AND is_deleted = 0
                    """,
                    (
                        kontragent,
                        now,
                        purchase_sum,
                        retail_sum,
                        wholesale_sum,
                        now,
                        postuplenie_uuid,
                    ),
                )
                if cursor.rowcount == 0:
                    raise DatabaseError("Накладная для сохранения не найдена.")
            else:
                cursor.execute(
                    """
                    INSERT INTO postupleniya (
                        uuid, kontragent, data, summa_postupleni,
                        summa_prodazhi, summa_optom, last_modified, sync_status, is_deleted,
                        is_draft, autosave_payload, autosave_updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 0, 0, NULL, NULL)
                    """,
                    (
                        postuplenie_uuid,
                        kontragent,
                        now,
                        purchase_sum,
                        retail_sum,
                        wholesale_sum,
                        now,
                    ),
                )

            for row in rows:
                product_uuid = row["product_uuid"]
                if product_uuid:
                    cursor.execute(
                        """
                        SELECT uuid
                        FROM tovary
                        WHERE uuid = %s AND is_deleted = 0
                        """,
                        [product_uuid],
                    )
                    product = cursor.fetchone()
                else:
                    product = None

                if product is None:
                    cursor.execute(
                        """
                        SELECT uuid
                        FROM tovary
                        WHERE nazvanie = %s AND is_deleted = 0
                        LIMIT 2
                        """,
                        [row["nazvanie"]],
                    )
                    products_by_name = cursor.fetchall()
                    if len(products_by_name) == 1:
                        product_uuid = products_by_name[0]["uuid"]
                        product = products_by_name[0]
                    elif len(products_by_name) > 1:
                        raise DatabaseError(
                            f"Найдено несколько товаров с названием '{row['nazvanie']}'. Сначала объедините дубли."
                        )

                if product is None:
                    product_uuid = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO tovary (
                            uuid, last_modified, sync_status, is_deleted,
                            nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
                        ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, NULL)
                        """,
                        (
                            product_uuid,
                            now,
                            row["nazvanie"],
                            row["kolichestvo"],
                            row["tsena_zakupki"],
                            row["tsena_prodazhi"],
                            row["tsena_optom"],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE tovary
                        SET kolichestvo = kolichestvo + %s,
                            tsena_zakupki = %s,
                            tsena_prodazhi = %s,
                            tsena_optom = %s,
                            last_modified = %s,
                            sync_status = 'pending',
                            nazvanie = %s
                        WHERE uuid = %s AND is_deleted = 0
                        """,
                        (
                            row["kolichestvo"],
                            row["tsena_zakupki"],
                            row["tsena_prodazhi"],
                            row["tsena_optom"],
                            now,
                            row["nazvanie"],
                            product_uuid,
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO spisok_postupleniy (
                        uuid, last_modified, sync_status, is_deleted, postuplenie_uuid, product_uuid,
                        nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom
                    ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        now,
                        postuplenie_uuid,
                        product_uuid,
                        row["nazvanie"],
                        row["kolichestvo"],
                        row["tsena_zakupki"],
                        row["tsena_prodazhi"],
                        row["tsena_optom"],
                    ),
                )

        return postuplenie_uuid

    return run_transaction(_transaction)


def update_receipt(receipt_uuid: str, kontragent: str, rows: list[dict[str, Any]]) -> str:
    now = datetime.now()
    purchase_sum = sum(row["kolichestvo"] * row["tsena_zakupki"] for row in rows)
    retail_sum = sum(row["kolichestvo"] * row["tsena_prodazhi"] for row in rows)
    wholesale_sum = sum(row["kolichestvo"] * row["tsena_optom"] for row in rows)

    def _transaction(connection):
        backfill_receipt_item_product_uuid(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM kontragenty
                WHERE name = %s AND is_deleted = 0
                LIMIT 1
                """,
                [kontragent],
            )
            supplier = cursor.fetchone()
            if supplier is None:
                cursor.execute(
                    """
                    INSERT INTO kontragenty (
                        name, uuid, last_modified, sync_status, is_deleted
                    ) VALUES (%s, %s, %s, 'pending', 0)
                    """,
                    (kontragent, str(uuid4()), now),
                )

            cursor.execute(
                """
                SELECT uuid
                FROM postupleniya
                WHERE uuid = %s AND is_deleted = 0
                """,
                [receipt_uuid],
            )
            if cursor.fetchone() is None:
                raise DatabaseError("Поступление для редактирования не найдено.")

            cursor.execute(
                """
                SELECT product_uuid, nazvanie, kolichestvo
                FROM spisok_postupleniy
                WHERE postuplenie_uuid = %s AND is_deleted = 0
                """,
                [receipt_uuid],
            )
            for item in cursor.fetchall():
                resolve_receipt_item_product(cursor, item, now)

            cursor.execute(
                """
                UPDATE postupleniya
                SET kontragent = %s,
                    summa_postupleni = %s,
                    summa_prodazhi = %s,
                    summa_optom = %s,
                    last_modified = %s,
                    sync_status = 'pending',
                    is_draft = 0,
                    autosave_payload = NULL,
                    autosave_updated_at = NULL
                WHERE uuid = %s AND is_deleted = 0
                """,
                (kontragent, purchase_sum, retail_sum, wholesale_sum, now, receipt_uuid),
            )

            cursor.execute(
                """
                DELETE FROM spisok_postupleniy
                WHERE postuplenie_uuid = %s
                """,
                [receipt_uuid],
            )

            for row in rows:
                product_uuid = row["product_uuid"]
                product = None
                if product_uuid:
                    cursor.execute(
                        """
                        SELECT uuid
                        FROM tovary
                        WHERE uuid = %s AND is_deleted = 0
                        """,
                        [product_uuid],
                    )
                    product = cursor.fetchone()

                if product is None:
                    cursor.execute(
                        """
                        SELECT uuid
                        FROM tovary
                        WHERE nazvanie = %s AND is_deleted = 0
                        LIMIT 1
                        """,
                        [row["nazvanie"]],
                    )
                    product = cursor.fetchone()
                    product_uuid = product["uuid"] if product else None

                if product is None:
                    cursor.execute(
                        """
                        SELECT uuid
                        FROM tovary
                        WHERE nazvanie = %s AND is_deleted = 0
                        LIMIT 2
                        """,
                        [row["nazvanie"]],
                    )
                    products_by_name = cursor.fetchall()
                    if len(products_by_name) == 1:
                        product_uuid = products_by_name[0]["uuid"]
                        product = products_by_name[0]
                    elif len(products_by_name) > 1:
                        raise DatabaseError(
                            f"Найдено несколько товаров с названием '{row['nazvanie']}'. Сначала объедините дубли."
                        )

                if product is None:
                    product_uuid = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO tovary (
                            uuid, last_modified, sync_status, is_deleted,
                            nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
                        ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, NULL)
                        """,
                        (
                            product_uuid,
                            now,
                            row["nazvanie"],
                            row["kolichestvo"],
                            row["tsena_zakupki"],
                            row["tsena_prodazhi"],
                            row["tsena_optom"],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE tovary
                        SET kolichestvo = kolichestvo + %s,
                            tsena_zakupki = %s,
                            tsena_prodazhi = %s,
                            tsena_optom = %s,
                            last_modified = %s,
                            sync_status = 'pending',
                            nazvanie = %s
                        WHERE uuid = %s AND is_deleted = 0
                        """,
                        (
                            row["kolichestvo"],
                            row["tsena_zakupki"],
                            row["tsena_prodazhi"],
                            row["tsena_optom"],
                            now,
                            row["nazvanie"],
                            product_uuid,
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO spisok_postupleniy (
                        uuid, last_modified, sync_status, is_deleted, postuplenie_uuid, product_uuid,
                        nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom
                    ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        now,
                        receipt_uuid,
                        product_uuid,
                        row["nazvanie"],
                        row["kolichestvo"],
                        row["tsena_zakupki"],
                        row["tsena_prodazhi"],
                        row["tsena_optom"],
                    ),
                )

        return receipt_uuid

    return run_transaction(_transaction)


def build_edit_form_data(receipt: dict[str, Any] | None) -> dict[str, Any]:
    return load_receipt_snapshot(receipt)


def autosave_receipt_snapshot(snapshot: dict[str, Any], receipt_uuid: str | None = None) -> str:
    now = datetime.now()
    normalized = normalize_receipt_snapshot(snapshot)
    payload_json = json.dumps(normalized, ensure_ascii=False)
    target_uuid = receipt_uuid or str(uuid4())

    def _transaction(connection):
        ensure_receipt_autosave_columns(connection)
        with connection.cursor() as cursor:
            if receipt_uuid:
                cursor.execute(
                    """
                    UPDATE postupleniya
                    SET kontragent = %s,
                        last_modified = %s,
                        sync_status = 'pending',
                        autosave_payload = %s,
                        autosave_updated_at = %s
                    WHERE uuid = %s AND is_deleted = 0
                    """,
                    (
                        normalized["kontragent"],
                        now,
                        payload_json,
                        now,
                        target_uuid,
                    ),
                )
                if cursor.rowcount == 0:
                    raise DatabaseError("Накладная для автосохранения не найдена.")
            else:
                cursor.execute(
                    """
                    INSERT INTO postupleniya (
                        uuid, kontragent, data, summa_postupleni, summa_prodazhi, summa_optom,
                        last_modified, sync_status, is_deleted, is_draft, autosave_payload, autosave_updated_at
                    ) VALUES (%s, %s, %s, 0, 0, 0, %s, 'pending', 0, 1, %s, %s)
                    """,
                    (
                        target_uuid,
                        normalized["kontragent"],
                        now,
                        now,
                        payload_json,
                        now,
                    ),
                )
        return target_uuid

    return run_transaction(_transaction)


def parse_product_form(form_data) -> dict[str, Any]:
    name = form_data.get("nazvanie", "").strip()
    category = form_data.get("category", "").strip() or None

    if not name:
        raise ValueError("Укажите название товара.")

    try:
        quantity = float((form_data.get("kolichestvo", "0") or "0").strip())
        purchase_price = float((form_data.get("tsena_zakupki", "0") or "0").strip())
        retail_price = float((form_data.get("tsena_prodazhi", "0") or "0").strip())
        wholesale_price = float((form_data.get("tsena_optom", "0") or "0").strip())
    except ValueError as exc:
        raise ValueError("Проверьте количество и цены, там должно быть число.") from exc

    return {
        "nazvanie": name,
        "kolichestvo": quantity,
        "tsena_zakupki": purchase_price,
        "tsena_prodazhi": retail_price,
        "tsena_optom": wholesale_price,
        "category": category,
        "barcode_rows": parse_product_barcodes(form_data),
    }


def parse_product_barcodes(form_data) -> list[dict[str, Any]]:
    barcode_uuids = form_data.getlist("barcode_uuid")
    barcodes = form_data.getlist("barcodes")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index in range(max(len(barcode_uuids), len(barcodes))):
        barcode_uuid = (barcode_uuids[index] if index < len(barcode_uuids) else "") or ""
        barcode = (barcodes[index] if index < len(barcodes) else "") or ""
        barcode = barcode.strip()
        if not barcode:
            continue
        if barcode in seen:
            raise ValueError(f"Штрихкод '{barcode}' указан несколько раз.")
        seen.add(barcode)
        rows.append(
            {
                "uuid": barcode_uuid.strip() or None,
                "shtrih_kod": barcode,
            }
        )

    return rows


def parse_barcode_rows_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = payload.get("barcode_rows")
    if not isinstance(raw_rows, list):
        raw_rows = []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue

        barcode_uuid = str(raw_row.get("uuid", "") or "").strip() or None
        barcode = str(raw_row.get("shtrih_kod", "") or "").strip()
        if not barcode:
            continue
        if barcode in seen:
            raise ValueError(f"Штрихкод '{barcode}' указан несколько раз.")
        seen.add(barcode)
        rows.append(
            {
                "uuid": barcode_uuid,
                "shtrih_kod": barcode,
            }
        )

    return rows


def parse_quick_product_form(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("nazvanie", "")).strip()
    barcode = str(payload.get("shtrih_kod", "")).strip()

    if not name:
        raise ValueError("Укажите название товара.")

    try:
        purchase_price = float(str(payload.get("tsena_zakupki", "0")).strip() or "0")
        wholesale_price = float(str(payload.get("tsena_optom", "0")).strip() or "0")
        retail_price = float(str(payload.get("tsena_prodazhi", "0")).strip() or "0")
    except ValueError as exc:
        raise ValueError("Проверьте цены, там должно быть число.") from exc

    return {
        "nazvanie": name,
        "shtrih_kod": barcode,
        "tsena_zakupki": purchase_price,
        "tsena_optom": wholesale_price,
        "tsena_prodazhi": retail_price,
    }


def parse_picker_product_update_form(payload: dict[str, Any], existing_product: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("nazvanie", "")).strip()
    if not name:
        raise ValueError("Укажите название товара.")

    try:
        purchase_price = float(str(payload.get("tsena_zakupki", "0")).strip() or "0")
        wholesale_price = float(str(payload.get("tsena_optom", "0")).strip() or "0")
        retail_price = float(str(payload.get("tsena_prodazhi", "0")).strip() or "0")
    except ValueError as exc:
        raise ValueError("Проверьте цены, там должно быть число.") from exc

    return {
        "nazvanie": name,
        "kolichestvo": float(existing_product.get("kolichestvo") or 0),
        "tsena_zakupki": purchase_price,
        "tsena_prodazhi": retail_price,
        "tsena_optom": wholesale_price,
        "category": str(existing_product.get("category") or "").strip(),
        "barcode_rows": parse_barcode_rows_payload(payload),
    }


def parse_supplier_form(form_data) -> dict[str, Any]:
    name = form_data.get("name", "").strip()
    if not name:
        raise ValueError("Укажите название контрагента.")
    return {"name": name}


def sync_product_barcodes(cursor, product_uuid: str, barcode_rows: list[dict[str, Any]], now: datetime) -> None:
    cursor.execute(
        """
        SELECT uuid, shtrih_kod
        FROM dop_shtrih_kody
        WHERE tovar_uuid = %s AND is_deleted = 0
        """,
        [product_uuid],
    )
    active_rows = cursor.fetchall()
    existing_map = {row["uuid"]: row["shtrih_kod"] for row in active_rows}

    keep_ids = [row["uuid"] for row in barcode_rows if row["uuid"]]
    if keep_ids:
        placeholders = ", ".join(["%s"] * len(keep_ids))
        cursor.execute(
            f"""
            UPDATE dop_shtrih_kody
            SET is_deleted = 1,
                last_modified = %s,
                sync_status = 'pending'
            WHERE tovar_uuid = %s
              AND is_deleted = 0
              AND uuid NOT IN ({placeholders})
            """,
            [now, product_uuid, *keep_ids],
        )
    else:
        cursor.execute(
            """
            UPDATE dop_shtrih_kody
            SET is_deleted = 1,
                last_modified = %s,
                sync_status = 'pending'
            WHERE tovar_uuid = %s AND is_deleted = 0
            """,
            (now, product_uuid),
        )

    for row in barcode_rows:
        barcode_uuid = row["uuid"]
        barcode = row["shtrih_kod"]
        if barcode_uuid and barcode_uuid in existing_map:
            if existing_map[barcode_uuid] != barcode:
                cursor.execute(
                    """
                    UPDATE dop_shtrih_kody
                    SET shtrih_kod = %s,
                        last_modified = %s,
                        sync_status = 'pending'
                    WHERE uuid = %s
                    """,
                    (barcode, now, barcode_uuid),
                )
        else:
            cursor.execute(
                """
                INSERT INTO dop_shtrih_kody (
                    uuid, last_modified, sync_status, is_deleted, tovar_uuid, shtrih_kod
                ) VALUES (%s, %s, 'pending', 0, %s, %s)
                """,
                (str(uuid4()), now, product_uuid, barcode),
            )


def create_product(data: dict[str, Any]) -> str:
    now = datetime.now()
    product_uuid = str(uuid4())

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM tovary
                WHERE nazvanie = %s AND is_deleted = 0
                LIMIT 1
                """,
                [data["nazvanie"]],
            )
            if cursor.fetchone():
                raise DatabaseError("Товар с таким названием уже существует.")
            cursor.execute(
                """
                INSERT INTO tovary (
                    uuid, last_modified, sync_status, is_deleted,
                    nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
                ) VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s, %s, %s)
                """,
                (
                    product_uuid,
                    now,
                    data["nazvanie"],
                    data["kolichestvo"],
                    data["tsena_zakupki"],
                    data["tsena_prodazhi"],
                    data["tsena_optom"],
                    data["category"],
                ),
            )
            sync_product_barcodes(cursor, product_uuid, data["barcode_rows"], now)
        return product_uuid

    return run_transaction(_transaction)


def create_quick_product(data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now()
    product_uuid = str(uuid4())
    barcode_uuid = str(uuid4()) if data["shtrih_kod"] else None

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM tovary
                WHERE nazvanie = %s AND is_deleted = 0
                LIMIT 1
                """,
                [data["nazvanie"]],
            )
            if cursor.fetchone():
                raise DatabaseError("Товар с таким названием уже существует.")

            cursor.execute(
                """
                INSERT INTO tovary (
                    uuid, last_modified, sync_status, is_deleted,
                    nazvanie, kolichestvo, tsena_zakupki, tsena_prodazhi, tsena_optom, category
                ) VALUES (%s, %s, 'pending', 0, %s, 0, %s, %s, %s, NULL)
                """,
                (
                    product_uuid,
                    now,
                    data["nazvanie"],
                    data["tsena_zakupki"],
                    data["tsena_prodazhi"],
                    data["tsena_optom"],
                ),
            )

            if data["shtrih_kod"]:
                cursor.execute(
                    """
                    INSERT INTO dop_shtrih_kody (
                        uuid, last_modified, sync_status, is_deleted, tovar_uuid, shtrih_kod
                    ) VALUES (%s, %s, 'pending', 0, %s, %s)
                    """,
                    (barcode_uuid, now, product_uuid, data["shtrih_kod"]),
                )

        return {
            "uuid": product_uuid,
            "nazvanie": data["nazvanie"],
            "barcodes": data["shtrih_kod"],
            "tsena_zakupki": data["tsena_zakupki"],
            "tsena_optom": data["tsena_optom"],
            "tsena_prodazhi": data["tsena_prodazhi"],
        }

    return run_transaction(_transaction)


def update_product(product_uuid: str, data: dict[str, Any]) -> str:
    now = datetime.now()

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM tovary
                WHERE uuid = %s AND is_deleted = 0
                """,
                [product_uuid],
            )
            if cursor.fetchone() is None:
                raise DatabaseError("Товар для редактирования не найден.")

            cursor.execute(
                """
                SELECT uuid
                FROM tovary
                WHERE nazvanie = %s AND uuid <> %s AND is_deleted = 0
                LIMIT 1
                """,
                (data["nazvanie"], product_uuid),
            )
            if cursor.fetchone():
                raise DatabaseError("Товар с таким названием уже существует.")

            cursor.execute(
                """
                UPDATE tovary
                SET nazvanie = %s,
                    kolichestvo = %s,
                    tsena_zakupki = %s,
                    tsena_prodazhi = %s,
                    tsena_optom = %s,
                    category = %s,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE uuid = %s AND is_deleted = 0
                """,
                (
                    data["nazvanie"],
                    data["kolichestvo"],
                    data["tsena_zakupki"],
                    data["tsena_prodazhi"],
                    data["tsena_optom"],
                    data["category"],
                    now,
                    product_uuid,
                ),
            )
            sync_product_barcodes(cursor, product_uuid, data["barcode_rows"], now)
        return product_uuid

    return run_transaction(_transaction)


def create_supplier(data: dict[str, Any]) -> str:
    now = datetime.now()
    supplier_uuid = str(uuid4())

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO kontragenty (
                    name, uuid, last_modified, sync_status, is_deleted
                ) VALUES (%s, %s, %s, 'pending', 0)
                """,
                (data["name"], supplier_uuid, now),
            )
        return supplier_uuid

    return run_transaction(_transaction)


def update_supplier(supplier_uuid: str, data: dict[str, Any]) -> str:
    now = datetime.now()

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM kontragenty
                WHERE uuid = %s AND is_deleted = 0
                """,
                [supplier_uuid],
            )
            if cursor.fetchone() is None:
                raise DatabaseError("Контрагент для редактирования не найден.")

            cursor.execute(
                """
                UPDATE kontragenty
                SET name = %s,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE uuid = %s AND is_deleted = 0
                """,
                (data["name"], now, supplier_uuid),
            )
        return supplier_uuid

    return run_transaction(_transaction)


def delete_receipt(receipt_uuid: str) -> None:
    now = datetime.now()

    def _transaction(connection):
        backfill_receipt_item_product_uuid(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM postupleniya
                WHERE uuid = %s AND is_deleted = 0
                """,
                [receipt_uuid],
            )
            if cursor.fetchone() is None:
                raise DatabaseError("Поступление для удаления не найдено.")

            cursor.execute(
                """
                SELECT product_uuid, nazvanie, kolichestvo
                FROM spisok_postupleniy
                WHERE postuplenie_uuid = %s AND is_deleted = 0
                """,
                [receipt_uuid],
            )
            for item in cursor.fetchall():
                resolve_receipt_item_product(cursor, item, now)

            cursor.execute(
                """
                UPDATE spisok_postupleniy
                SET is_deleted = 1,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE postuplenie_uuid = %s AND is_deleted = 0
                """,
                (now, receipt_uuid),
            )

            cursor.execute(
                """
                UPDATE postupleniya
                SET is_deleted = 1,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE uuid = %s AND is_deleted = 0
                """,
                (now, receipt_uuid),
            )

    run_transaction(_transaction)


def delete_product(product_uuid: str) -> None:
    now = datetime.now()

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tovary
                SET is_deleted = 1,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE uuid = %s AND is_deleted = 0
                """,
                (now, product_uuid),
            )
            if cursor.rowcount == 0:
                raise DatabaseError("Товар для удаления не найден.")

    run_transaction(_transaction)


def delete_supplier(supplier_uuid: str) -> None:
    now = datetime.now()

    def _transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE kontragenty
                SET is_deleted = 1,
                    last_modified = %s,
                    sync_status = 'pending'
                WHERE uuid = %s AND is_deleted = 0
                """,
                (now, supplier_uuid),
            )
            if cursor.rowcount == 0:
                raise DatabaseError("Контрагент для удаления не найден.")

    run_transaction(_transaction)


@app.route("/")
def dashboard():
    data = {
        "stats": {
            "total_products": 0,
            "total_suppliers": 0,
            "total_receipts": 0,
            "total_purchase_sum": 0,
            "low_stock_count": 0,
            "stock_cost_sum": 0,
            "stock_retail_sum": 0,
        },
        "period_summary": {
            "checks_count": 0,
            "sales_total": 0,
            "cost_total": 0,
            "profit_total": 0,
            "items_quantity": 0,
            "today_checks_count": 0,
            "today_sales_total": 0,
            "today_profit_total": 0,
            "avg_check": 0,
        },
        "payments": {"cash_total": 0, "card_total": 0, "debt_total": 0},
        "payment_mix": [
            {"label": "Наличные", "value": 0, "share": 0},
            {"label": "Перевод", "value": 0, "share": 0},
            {"label": "Долг", "value": 0, "share": 0},
        ],
        "sales_chart": [
            {"date": "", "label": "", "sales": 0, "profit": 0, "checks": 0, "height": 0}
            for _ in range(30)
        ],
        "top_sold_products": [],
        "top_revenue_products": [],
        "top_profit_products": [],
        "recent_receipts": [],
        "top_products": [],
    }
    db_error = None
    try:
        data = load_dashboard()
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template("dashboard.html", active_page="dashboard", dashboard=data, db_error=db_error)


@app.route("/postupleniya")
def postupleniya():
    search = request.args.get("q", "").strip()
    receipts: list[dict[str, Any]] = []
    db_error = None
    try:
        receipts = load_postupleniya(search)
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template(
        "postupleniya.html",
        active_page="postupleniya",
        receipts=receipts,
        search=search,
        deleted=request.args.get("deleted") == "1",
        delete_error=request.args.get("delete_error", "").strip(),
        db_error=db_error,
    )


@app.route("/postupleniya/new", methods=["GET", "POST"])
def postupleniya_new():
    form_error = None
    form_success = None
    product_options: list[dict[str, Any]] = []
    suppliers: list[dict[str, Any]] = []
    form_data = {"kontragent": "", "rows": []}
    autosave_receipt_uuid = request.form.get("autosave_receipt_uuid", "").strip()
    try:
        product_options = load_product_options()
        suppliers = load_postupleniya_kontragenty()
    except DatabaseError as exc:
        return render_template(
            "postupleniya_new.html",
            active_page="postupleniya",
            product_options=[],
            suppliers=[],
            form_error=None,
            form_success=None,
            autosave_receipt_uuid=autosave_receipt_uuid,
            form_data=form_data,
            db_error=str(exc),
        )

    if request.method == "POST":
        try:
            kontragent, rows = parse_receipt_form(request.form)
            receipt_uuid = create_receipt(kontragent, rows, autosave_receipt_uuid or None)
            return redirect(url_for("postupleniya", created=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = {
                "kontragent": request.form.get("kontragent", ""),
                "rows": [
                    {
                        "product_uuid": request.form.getlist("product_uuid")[idx] if idx < len(request.form.getlist("product_uuid")) else "",
                        "nazvanie": request.form.getlist("nazvanie")[idx] if idx < len(request.form.getlist("nazvanie")) else "",
                        "kolichestvo": request.form.getlist("kolichestvo")[idx] if idx < len(request.form.getlist("kolichestvo")) else "",
                        "tsena_zakupki": request.form.getlist("tsena_zakupki")[idx] if idx < len(request.form.getlist("tsena_zakupki")) else "",
                        "tsena_prodazhi": request.form.getlist("tsena_prodazhi")[idx] if idx < len(request.form.getlist("tsena_prodazhi")) else "",
                        "tsena_optom": request.form.getlist("tsena_optom")[idx] if idx < len(request.form.getlist("tsena_optom")) else "",
                    }
                    for idx in range(len(request.form.getlist("nazvanie")))
                ],
            }

    selected_kontragent = (form_data.get("kontragent") or request.form.get("kontragent", "")).strip()
    if selected_kontragent and all((supplier.get("name") or "") != selected_kontragent for supplier in suppliers):
        suppliers = [{"name": selected_kontragent}] + suppliers

    if request.args.get("created") == "1":
        form_success = "Поступление успешно сохранено."

    return render_template(
        "postupleniya_new.html",
        active_page="postupleniya",
        product_options=product_options,
        suppliers=suppliers,
        form_error=form_error,
        form_success=form_success,
        form_data=form_data,
        autosave_receipt_uuid=autosave_receipt_uuid,
        db_error=None,
    )


@app.route("/postupleniya/<receipt_uuid>")
def postuplenie_detail(receipt_uuid: str):
    db_error = None
    receipt = None
    try:
        receipt = load_postuplenie_detail(receipt_uuid)
    except DatabaseError as exc:
        db_error = str(exc)
    if receipt is None and db_error is None:
        abort(404)
    return render_template(
        "postuplenie_detail.html",
        active_page="postupleniya",
        receipt=receipt,
        created=request.args.get("created") == "1",
        updated=request.args.get("updated") == "1",
        db_error=db_error,
    )


@app.route("/postupleniya/<receipt_uuid>/edit", methods=["GET", "POST"])
def postuplenie_edit(receipt_uuid: str):
    db_error = None
    form_error = None
    product_options: list[dict[str, Any]] = []
    suppliers: list[dict[str, Any]] = []
    receipt = None

    try:
        product_options = load_product_options()
        suppliers = load_postupleniya_kontragenty()
        receipt = load_postuplenie_detail(receipt_uuid)
    except DatabaseError as exc:
        db_error = str(exc)

    if receipt is None and db_error is None:
        abort(404)

    if receipt and receipt.get("kontragent"):
        kontragent_name = receipt["kontragent"]
        if all((supplier.get("name") or "") != kontragent_name for supplier in suppliers):
            suppliers = [{"name": kontragent_name}] + suppliers

    form_data = build_edit_form_data(receipt)

    if request.method == "POST" and db_error is None:
        try:
            kontragent, rows = parse_receipt_form(request.form)
            if receipt and receipt.get("is_draft"):
                updated_uuid = create_receipt(kontragent, rows, receipt_uuid)
            else:
                updated_uuid = update_receipt(receipt_uuid, kontragent, rows)
            return redirect(url_for("postupleniya", updated=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = {
                "kontragent": request.form.get("kontragent", ""),
                "rows": [
                    {
                        "product_uuid": request.form.getlist("product_uuid")[idx] if idx < len(request.form.getlist("product_uuid")) else "",
                        "nazvanie": request.form.getlist("nazvanie")[idx],
                        "kolichestvo": request.form.getlist("kolichestvo")[idx],
                        "tsena_zakupki": request.form.getlist("tsena_zakupki")[idx],
                        "tsena_prodazhi": request.form.getlist("tsena_prodazhi")[idx],
                        "tsena_optom": request.form.getlist("tsena_optom")[idx],
                    }
                    for idx in range(len(request.form.getlist("nazvanie")))
                ],
            }

    return render_template(
        "postupleniya_edit.html",
        active_page="postupleniya",
        receipt=receipt,
        product_options=product_options,
        suppliers=suppliers,
        form_error=form_error,
        form_data=form_data,
        db_error=db_error,
    )


@app.route("/api/postupleniya/autosave", methods=["POST"])
def api_postupleniya_autosave():
    payload = request.get_json(silent=True) or {}
    receipt_uuid = str(payload.get("receipt_uuid") or "").strip() or None

    try:
        snapshot = parse_receipt_autosave_payload(payload)
        saved_uuid = autosave_receipt_snapshot(snapshot, receipt_uuid)
        return jsonify(
            {
                "ok": True,
                "receipt_uuid": saved_uuid,
                "edit_url": url_for("postuplenie_edit", receipt_uuid=saved_uuid),
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/postupleniya/<receipt_uuid>/delete", methods=["POST"])
def postuplenie_delete(receipt_uuid: str):
    try:
        delete_receipt(receipt_uuid)
        return redirect(url_for("postupleniya", deleted=1))
    except DatabaseError as exc:
        return redirect(url_for("postupleniya", delete_error=str(exc)))


@app.route("/nomenklatura")
def nomenklatura():
    search = request.args.get("q", "").strip()
    products: list[dict[str, Any]] = []
    db_error = None
    try:
        products = load_nomenklatura(search)
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template(
        "nomenklatura.html",
        active_page="nomenklatura",
        products=products,
        search=search,
        deleted=request.args.get("deleted") == "1",
        delete_error=request.args.get("delete_error", "").strip(),
        db_error=db_error,
    )


@app.route("/nomenklatura/new", methods=["GET", "POST"])
def nomenklatura_new():
    form_error = None
    form_data = {
        "nazvanie": "",
        "kolichestvo": "",
        "tsena_zakupki": "",
        "tsena_prodazhi": "",
        "tsena_optom": "",
        "category": "",
        "barcode_rows": [],
    }

    if request.method == "POST":
        try:
            data = parse_product_form(request.form)
            product_uuid = create_product(data)
            return redirect(url_for("nomenklatura", created=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = dict(request.form)
            form_data["barcode_rows"] = parse_product_barcodes(request.form)

    return render_template(
        "nomenklatura_new.html",
        active_page="nomenklatura",
        form_title="Новый товар",
        submit_label="Сохранить товар",
        form_error=form_error,
        form_data=form_data,
        created=False,
        updated=False,
        db_error=None,
    )


@app.route("/nomenklatura/<product_uuid>/edit", methods=["GET", "POST"])
def nomenklatura_edit(product_uuid: str):
    db_error = None
    form_error = None
    product = None

    try:
        product = load_product(product_uuid)
    except DatabaseError as exc:
        db_error = str(exc)

    if product is None and db_error is None:
        abort(404)

    form_data = product or {
        "nazvanie": "",
        "kolichestvo": "",
        "tsena_zakupki": "",
        "tsena_prodazhi": "",
        "tsena_optom": "",
        "category": "",
        "barcode_rows": [],
    }

    if request.method == "POST" and db_error is None:
        try:
            data = parse_product_form(request.form)
            updated_uuid = update_product(product_uuid, data)
            return redirect(url_for("nomenklatura", updated=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = dict(request.form)
            form_data["barcode_rows"] = parse_product_barcodes(request.form)

    return render_template(
        "nomenklatura_form.html",
        active_page="nomenklatura",
        form_title="Редактировать товар",
        submit_label="Сохранить изменения",
        form_error=form_error,
        form_data=form_data,
        created=request.args.get("created") == "1",
        updated=request.args.get("updated") == "1",
        db_error=db_error,
    )


@app.route("/nomenklatura/<product_uuid>/delete", methods=["POST"])
def nomenklatura_delete(product_uuid: str):
    try:
        delete_product(product_uuid)
        return redirect(url_for("nomenklatura", deleted=1))
    except DatabaseError as exc:
        return redirect(url_for("nomenklatura", delete_error=str(exc)))


@app.route("/otchety")
def otchety():
    reports = {"summary": {}, "payments": {}, "items": []}
    db_error = None
    today = datetime.now().strftime("%Y-%m-%d")
    date_from = request.args.get("date_from", "").strip() or today
    date_to = request.args.get("date_to", "").strip() or today
    try:
        reports = load_reports(date_from=date_from, date_to=date_to)
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template(
        "otchety.html",
        active_page="otchety",
        reports=reports,
        date_from=date_from,
        date_to=date_to,
        db_error=db_error,
    )


@app.route("/kontragenty")
def kontragenty():
    suppliers: list[dict[str, Any]] = []
    db_error = None
    try:
        suppliers = load_kontragenty()
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template(
        "kontragenty.html",
        active_page="kontragenty",
        suppliers=suppliers,
        deleted=request.args.get("deleted") == "1",
        delete_error=request.args.get("delete_error", "").strip(),
        db_error=db_error,
    )


@app.route("/kontragenty/new", methods=["GET", "POST"])
def kontragenty_new():
    form_error = None
    form_data = {"name": ""}

    if request.method == "POST":
        try:
            data = parse_supplier_form(request.form)
            supplier_uuid = create_supplier(data)
            return redirect(url_for("kontragenty_edit", supplier_uuid=supplier_uuid, created=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = dict(request.form)

    return render_template(
        "kontragenty_form.html",
        active_page="kontragenty",
        form_title="Новый контрагент",
        submit_label="Сохранить контрагента",
        form_error=form_error,
        form_data=form_data,
        created=False,
        updated=False,
        db_error=None,
    )


@app.route("/kontragenty/<supplier_uuid>/edit", methods=["GET", "POST"])
def kontragenty_edit(supplier_uuid: str):
    db_error = None
    form_error = None
    supplier = None

    try:
        supplier = load_supplier(supplier_uuid)
    except DatabaseError as exc:
        db_error = str(exc)

    if supplier is None and db_error is None:
        abort(404)

    form_data = supplier or {"name": ""}

    if request.method == "POST" and db_error is None:
        try:
            data = parse_supplier_form(request.form)
            updated_uuid = update_supplier(supplier_uuid, data)
            return redirect(url_for("kontragenty_edit", supplier_uuid=updated_uuid, updated=1))
        except (ValueError, DatabaseError) as exc:
            form_error = str(exc)
            form_data = dict(request.form)

    return render_template(
        "kontragenty_form.html",
        active_page="kontragenty",
        form_title="Редактировать контрагента",
        submit_label="Сохранить изменения",
        form_error=form_error,
        form_data=form_data,
        created=request.args.get("created") == "1",
        updated=request.args.get("updated") == "1",
        db_error=db_error,
    )


@app.route("/kontragenty/<supplier_uuid>/delete", methods=["POST"])
def kontragenty_delete(supplier_uuid: str):
    try:
        delete_supplier(supplier_uuid)
        return redirect(url_for("kontragenty", deleted=1))
    except DatabaseError as exc:
        return redirect(url_for("kontragenty", delete_error=str(exc)))


@app.route("/api/products/quick-create", methods=["POST"])
def api_products_quick_create():
    try:
        payload = request.get_json(silent=True) or {}
        data = parse_quick_product_form(payload)
        product = create_quick_product(data)
        return jsonify({"ok": True, "product": product})
    except (ValueError, DatabaseError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/products/<product_uuid>")
def api_product_detail(product_uuid: str):
    try:
        product = load_product(product_uuid)
        if product is None:
            return jsonify({"ok": False, "error": "Товар не найден."}), 404
        return jsonify(
            {
                "ok": True,
                "product": {
                    "uuid": product["uuid"],
                    "nazvanie": product["nazvanie"],
                    "kolichestvo": product["kolichestvo"],
                    "tsena_zakupki": product["tsena_zakupki"],
                    "tsena_optom": product["tsena_optom"],
                    "tsena_prodazhi": product["tsena_prodazhi"],
                    "barcode_rows": [
                        {
                            "uuid": str(row.get("uuid") or "").strip(),
                            "shtrih_kod": str(row.get("shtrih_kod") or "").strip(),
                        }
                        for row in (product.get("barcode_rows") or [])
                        if str(row.get("shtrih_kod") or "").strip()
                    ],
                    "barcodes": ", ".join(
                        [
                            str(row.get("shtrih_kod") or "").strip()
                            for row in (product.get("barcode_rows") or [])
                            if str(row.get("shtrih_kod") or "").strip()
                        ]
                    ),
                },
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/products/<product_uuid>/picker-update", methods=["POST"])
def api_product_picker_update(product_uuid: str):
    try:
        existing_product = load_product(product_uuid)
        if existing_product is None:
            return jsonify({"ok": False, "error": "Товар не найден."}), 404

        payload = request.get_json(silent=True) or {}
        data = parse_picker_product_update_form(payload, existing_product)
        update_product(product_uuid, data)
        updated_product = load_product(product_uuid)
        if updated_product is None:
            return jsonify({"ok": False, "error": "Товар не найден после сохранения."}), 404

        return jsonify(
            {
                "ok": True,
                "product": {
                    "uuid": updated_product["uuid"],
                    "nazvanie": updated_product["nazvanie"],
                    "kolichestvo": updated_product["kolichestvo"],
                    "tsena_zakupki": updated_product["tsena_zakupki"],
                    "tsena_optom": updated_product["tsena_optom"],
                    "tsena_prodazhi": updated_product["tsena_prodazhi"],
                    "barcode_rows": [
                        {
                            "uuid": str(row.get("uuid") or "").strip(),
                            "shtrih_kod": str(row.get("shtrih_kod") or "").strip(),
                        }
                        for row in (updated_product.get("barcode_rows") or [])
                        if str(row.get("shtrih_kod") or "").strip()
                    ],
                    "barcodes": ", ".join(
                        [
                            str(row.get("shtrih_kod") or "").strip()
                            for row in (updated_product.get("barcode_rows") or [])
                            if str(row.get("shtrih_kod") or "").strip()
                        ]
                    ),
                },
            }
        )
    except (ValueError, DatabaseError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/products/next-barcode")
def api_products_next_barcode():
    try:
        return jsonify({"ok": True, "barcode": generate_next_barcode()})
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/dashboard")
def api_dashboard():
    try:
        dashboard = load_dashboard()
        today_stats = fetch_one(
            """
            SELECT
                COUNT(*) AS checks_count,
                COALESCE(SUM(summa), 0) AS sales_total
            FROM cheki
            WHERE is_deleted = 0 AND DATE(data) = CURDATE()
            """
        ) or {}
        queue_stats = fetch_one(
            """
            SELECT COUNT(*) AS queue_count
            FROM barcode_print_queue
            WHERE is_deleted = 0 AND status IN ('queue', 'processing')
            """
        ) or {}

        return jsonify(
            {
                "ok": True,
                "dashboard": {
                    "stats": {
                        "total_products": int(dashboard.get("stats", {}).get("total_products") or 0),
                        "total_suppliers": int(dashboard.get("stats", {}).get("total_suppliers") or 0),
                        "total_receipts": int(dashboard.get("stats", {}).get("total_receipts") or 0),
                        "total_purchase_sum": float(dashboard.get("stats", {}).get("total_purchase_sum") or 0),
                        "low_stock_count": int(dashboard.get("stats", {}).get("low_stock_count") or 0),
                        "today_checks_count": int(today_stats.get("checks_count") or 0),
                        "today_sales_total": float(today_stats.get("sales_total") or 0),
                        "print_queue_count": int(queue_stats.get("queue_count") or 0),
                    },
                    "recent_receipts": [
                        {
                            "uuid": row["uuid"],
                            "kontragent": row.get("kontragent") or "",
                            "data": dt(row.get("data")),
                            "summa_postupleni": float(row.get("summa_postupleni") or 0),
                            "summa_prodazhi": float(row.get("summa_prodazhi") or 0),
                        }
                        for row in dashboard.get("recent_receipts", [])
                    ],
                    "top_products": [
                        {
                            "nazvanie": row.get("nazvanie") or "",
                            "kolichestvo": float(row.get("kolichestvo") or 0),
                            "tsena_zakupki": float(row.get("tsena_zakupki") or 0),
                            "tsena_prodazhi": float(row.get("tsena_prodazhi") or 0),
                        }
                        for row in dashboard.get("top_products", [])
                    ],
                },
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/kassa/products")
def api_kassa_products():
    try:
        query = str(request.args.get("q", "") or "").strip().lower()
        products = [serialize_cash_product(product) for product in load_cash_products()]
        if query:
            products = [
                product
                for product in products
                if query in str(product.get("nazvanie") or "").lower()
                or query in str(product.get("uuid") or "").lower()
                or any(query in barcode.lower() for barcode in product.get("barcodes", []))
            ]

        return jsonify(
            {
                "ok": True,
                "count": len(products),
                "products": products,
                "recent_checks": [
                    {
                        "uuid": row["uuid"],
                        "data": dt(row.get("data")),
                        "summa": float(row.get("display_sum") or 0),
                        "oplata": row.get("oplata") or "",
                        "items_count": int(row.get("items_count") or 0),
                    }
                    for row in load_recent_checks()
                ],
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/nomenklatura")
def api_nomenklatura():
    try:
        search = str(request.args.get("q", "") or "").strip()
        products = [serialize_nomenklatura_product(product) for product in load_nomenklatura(search)]
        return jsonify({"ok": True, "count": len(products), "products": products})
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/postupleniya")
def api_postupleniya():
    try:
        search = str(request.args.get("q", "") or "").strip()
        receipt_uuid = str(request.args.get("uuid", "") or "").strip()

        if receipt_uuid:
            receipt = load_postuplenie_detail(receipt_uuid)
            if receipt is None:
                return jsonify({"ok": False, "error": "Поступление не найдено."}), 404
            return jsonify(
                {
                    "ok": True,
                    "receipt": {
                        "uuid": receipt["uuid"],
                        "kontragent": receipt.get("kontragent") or "",
                        "data": dt(receipt.get("data")),
                        "summa_postupleni": float(receipt.get("summa_postupleni") or 0),
                        "summa_prodazhi": float(receipt.get("summa_prodazhi") or 0),
                        "summa_optom": float(receipt.get("summa_optom") or 0),
                        "is_draft": bool(receipt.get("is_draft") or 0),
                        "items": [
                            {
                                "id": item.get("id"),
                                "product_uuid": item.get("product_uuid") or "",
                                "nazvanie": item.get("nazvanie") or "",
                                "kolichestvo": float(item.get("kolichestvo") or 0),
                                "tsena_zakupki": float(item.get("tsena_zakupki") or 0),
                                "tsena_prodazhi": float(item.get("tsena_prodazhi") or 0),
                                "tsena_optom": float(item.get("tsena_optom") or 0),
                                "summa_zakupki": float(item.get("summa_zakupki") or 0),
                                "summa_prodazhi_itogo": float(item.get("summa_prodazhi_itogo") or 0),
                                "summa_optom_itogo": float(item.get("summa_optom_itogo") or 0),
                            }
                            for item in receipt.get("items", [])
                        ],
                    },
                }
            )

        receipts = load_postupleniya(search)
        return jsonify(
            {
                "ok": True,
                "count": len(receipts),
                "receipts": [
                    {
                        "id": row.get("id"),
                        "uuid": row["uuid"],
                        "kontragent": row.get("kontragent") or "",
                        "data": dt(row.get("data")),
                        "summa_postupleni": float(row.get("summa_postupleni") or 0),
                        "summa_prodazhi": float(row.get("summa_prodazhi") or 0),
                        "summa_optom": float(row.get("summa_optom") or 0),
                    }
                    for row in receipts
                ],
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/otchety")
def api_otchety():
    try:
        date_from = str(request.args.get("date_from", "") or "").strip()
        date_to = str(request.args.get("date_to", "") or "").strip()
        report = load_reports(date_from=date_from, date_to=date_to)
        return jsonify(
            {
                "ok": True,
                "filters": {"date_from": date_from, "date_to": date_to},
                "summary": {
                    "checks_count": int(report.get("summary", {}).get("checks_count") or 0),
                    "total_cost_sum": float(report.get("summary", {}).get("total_cost_sum") or 0),
                    "total_sale_sum": float(report.get("summary", {}).get("total_sale_sum") or 0),
                    "total_profit_sum": float(report.get("summary", {}).get("total_profit_sum") or 0),
                    "cash_total": float(report.get("summary", {}).get("cash_total") or 0),
                    "card_total": float(report.get("summary", {}).get("card_total") or 0),
                    "debt_total": float(report.get("summary", {}).get("debt_total") or 0),
                },
                "items": [
                    {
                        "name": row.get("name") or "",
                        "quantity": float(row.get("quantity") or 0),
                        "avg_cost": float(row.get("avg_cost") or 0),
                        "avg_sale": float(row.get("avg_sale") or 0),
                        "avg_profit": float(row.get("avg_profit") or 0),
                        "cost_sum": float(row.get("cost_sum") or 0),
                        "sale_sum": float(row.get("sale_sum") or 0),
                        "total_profit": float(row.get("total_profit") or 0),
                    }
                    for row in report.get("items", [])
                ],
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/barcode-print-queue")
def api_barcode_print_queue():
    try:
        limit_raw = str(request.args.get("limit", "250") or "250").strip()
        try:
            limit = max(1, min(500, int(limit_raw)))
        except ValueError:
            limit = 250

        queue_rows = load_barcode_print_queue(limit=limit)
        queue_groups = build_barcode_print_queue_groups(queue_rows)
        return jsonify(
            {
                "ok": True,
                "count": len(queue_rows),
                "queue_rows": [
                    {
                        "uuid": row["uuid"],
                        "job_uuid": row.get("job_uuid") or "",
                        "product_uuid": row.get("product_uuid") or "",
                        "barcode": row.get("barcode") or "",
                        "product_name": row.get("product_name") or "",
                        "price": float(row.get("price") or 0),
                        "copies": int(row.get("copies") or 1),
                        "print_name": bool(row.get("print_name")),
                        "print_price": bool(row.get("print_price")),
                        "status": row.get("status") or "",
                        "created_at": dt(row.get("created_at")),
                        "printed_at": dt(row.get("printed_at")),
                        "error_text": row.get("error_text") or "",
                        "source": row.get("source") or "",
                    }
                    for row in queue_rows
                ],
                "queue_groups": [
                    {
                        "job_uuid": group.get("job_uuid") or "",
                        "created_at": dt(group.get("created_at")),
                        "status": group.get("status") or "",
                        "items_count": int(group.get("items_count") or 0),
                        "total_copies": int(group.get("total_copies") or 0),
                    }
                    for group in queue_groups
                ],
            }
        )
    except DatabaseError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/barcode-print-queue", methods=["GET", "POST"])
def barcode_print_queue():
    db_error = None
    form_error = None
    preselected_raw = request.args.get("products", "").strip()
    preselected_ids = {item.strip() for item in preselected_raw.split(",") if item.strip()}
    from_receipt = bool(preselected_ids)
    print_name_checked = ("print_name" in request.form) if request.method == "POST" else True
    print_price_checked = ("print_price" in request.form) if request.method == "POST" else True
    products: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    queue_groups: list[dict[str, Any]] = []

    try:
        run_transaction(lambda connection: ensure_barcode_print_tables(connection))
        if request.method == "POST":
            selected_products = request.form.getlist("selected_products")
            copies_map: dict[str, int] = {}
            for product_uuid in selected_products:
                normalized_uuid = str(product_uuid or "").strip()
                copies_raw = request.form.get(f"copies_{normalized_uuid}", "1")
                try:
                    copies_map[normalized_uuid] = max(1, int(copies_raw))
                except ValueError:
                    copies_map[normalized_uuid] = 1
            create_barcode_print_job(
                selected_product_uuids=selected_products,
                copies_map=copies_map,
                print_name=print_name_checked,
                print_price=print_price_checked,
                from_receipt=from_receipt,
            )
            redirect_params = {"queued": 1}
            if from_receipt:
                redirect_params["products"] = ",".join(sorted(preselected_ids))
            return redirect(url_for("barcode_print_queue", **redirect_params))

        products = build_receipt_barcode_print_entries(preselected_ids) if from_receipt else []
        queue_rows = [] if from_receipt else load_barcode_print_queue(limit=250)
        queue_groups = [] if from_receipt else build_barcode_print_queue_groups(queue_rows)
    except (ValueError, DatabaseError) as exc:
        if request.method == "POST":
            form_error = str(exc)
        else:
            db_error = str(exc)
        try:
            products = build_receipt_barcode_print_entries(preselected_ids) if from_receipt else []
            queue_rows = [] if from_receipt else load_barcode_print_queue(limit=250)
            queue_groups = [] if from_receipt else build_barcode_print_queue_groups(queue_rows)
        except DatabaseError:
            pass

    return render_template(
        "barcode_print_queue.html",
        active_page="barcode_print_queue",
        products=products,
        queue_rows=queue_rows,
        queue_groups=queue_groups,
        form_error=form_error,
        queued=request.args.get("queued") == "1",
        db_error=db_error,
        print_name_checked=print_name_checked,
        print_price_checked=print_price_checked,
        from_receipt=from_receipt,
    )


@app.route("/kassa")
def kassa():
    db_error = None
    products: list[dict[str, Any]] = []
    recent_checks: list[dict[str, Any]] = []
    try:
        products = load_cash_products()
        recent_checks = load_recent_checks()
    except DatabaseError as exc:
        db_error = str(exc)
    return render_template(
        "kassa.html",
        active_page="kassa",
        db_error=db_error,
        products=products,
        recent_checks=recent_checks,
    )


@app.route("/api/kassa/checkout", methods=["POST"])
def api_kassa_checkout():
    try:
        payload = request.get_json(silent=True) or {}
        data = parse_cash_sale_payload(payload)
        check_uuid = create_cash_sale(data)
        return jsonify(
            {
                "ok": True,
                "check_uuid": check_uuid,
                "recent_checks": [
                    {
                        "uuid": row["uuid"],
                        "data": dt(row.get("data")),
                        "summa": money(row.get("display_sum") or 0),
                        "oplata": row.get("oplata") or "",
                        "items_count": row.get("items_count") or 0,
                    }
                    for row in load_recent_checks()
                ],
            }
        )
    except (ValueError, DatabaseError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    app.run(debug=True)
