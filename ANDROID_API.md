# Android API

Этот backend используется как общая основа для `web` и будущего `android`.

Базовый адрес при локальном запуске:

`http://127.0.0.1:5000`

## Общий формат ответа

Успех:

```json
{
  "ok": true
}
```

Ошибка:

```json
{
  "ok": false,
  "error": "Текст ошибки"
}
```

## 1. Главная

### `GET /api/dashboard`

Возвращает данные для главного экрана Android.

Поля:

- `dashboard.stats.total_products`
- `dashboard.stats.total_suppliers`
- `dashboard.stats.total_receipts`
- `dashboard.stats.total_purchase_sum`
- `dashboard.stats.low_stock_count`
- `dashboard.stats.today_checks_count`
- `dashboard.stats.today_sales_total`
- `dashboard.stats.print_queue_count`
- `dashboard.recent_receipts`
- `dashboard.top_products`

## 2. Касса

### `GET /api/kassa/products`

Список товаров для кассы.

Параметры:

- `q` - поиск по названию, `uuid` или штрихкоду

Возвращает:

- `count`
- `products[]`
- `recent_checks[]`

Поля товара:

- `uuid`
- `nazvanie`
- `kolichestvo`
- `tsena_zakupki`
- `tsena_prodazhi`
- `tsena_optom`
- `barcodes[]`
- `primary_barcode`

### `POST /api/kassa/checkout`

Оформляет продажу.

Пример тела:

```json
{
  "sale_mode": "retail",
  "payment_method": "cash",
  "cash_amount": 1000,
  "card_amount": 0,
  "debt_amount": 0,
  "paid_amount": 1000,
  "change_amount": 0,
  "client_debt": "",
  "items": [
    {
      "product_uuid": "uuid-1",
      "product_name": "Товар",
      "quantity": 2,
      "stock": 10,
      "cost_price": 100,
      "retail_price": 150,
      "wholesale_price": 140
    }
  ]
}
```

Возвращает:

- `check_uuid`
- `recent_checks[]`

## 3. Номенклатура

### `GET /api/nomenklatura`

Список товаров для экрана номенклатуры.

Параметры:

- `q` - поиск по названию, `uuid` или категории

Возвращает:

- `count`
- `products[]`

Поля товара:

- `uuid`
- `nazvanie`
- `kolichestvo`
- `tsena_zakupki`
- `tsena_prodazhi`
- `tsena_optom`
- `category`
- `barcodes[]`

## 4. Поступления

### `GET /api/postupleniya`

Список поступлений.

Параметры:

- `q` - поиск по контрагенту или `uuid`

Возвращает:

- `count`
- `receipts[]`

Поля:

- `id`
- `uuid`
- `kontragent`
- `data`
- `summa_postupleni`
- `summa_prodazhi`
- `summa_optom`

### `GET /api/postupleniya?uuid=<receipt_uuid>`

Детали одного поступления.

Возвращает:

- `receipt.uuid`
- `receipt.kontragent`
- `receipt.data`
- `receipt.summa_postupleni`
- `receipt.summa_prodazhi`
- `receipt.summa_optom`
- `receipt.is_draft`
- `receipt.items[]`

Поля строки:

- `id`
- `product_uuid`
- `nazvanie`
- `kolichestvo`
- `tsena_zakupki`
- `tsena_prodazhi`
- `tsena_optom`
- `summa_zakupki`
- `summa_prodazhi_itogo`
- `summa_optom_itogo`

## 5. Отчеты

### `GET /api/otchety`

Отчет по продажам.

Параметры:

- `date_from` - формат `YYYY-MM-DD`
- `date_to` - формат `YYYY-MM-DD`

Возвращает:

- `filters`
- `summary`
- `items[]`

Поля `summary`:

- `checks_count`
- `total_cost_sum`
- `total_sale_sum`
- `total_profit_sum`
- `cash_total`
- `card_total`
- `debt_total`

Поля `items[]`:

- `name`
- `quantity`
- `avg_cost`
- `avg_sale`
- `avg_profit`
- `cost_sum`
- `sale_sum`
- `total_profit`

## 6. Очередь печати

### `GET /api/barcode-print-queue`

Возвращает очередь печати штрихкодов.

Параметры:

- `limit` - от `1` до `500`

Возвращает:

- `count`
- `queue_rows[]`
- `queue_groups[]`

Поля `queue_rows[]`:

- `uuid`
- `job_uuid`
- `product_uuid`
- `barcode`
- `product_name`
- `price`
- `copies`
- `print_name`
- `print_price`
- `status`
- `created_at`
- `printed_at`
- `error_text`
- `source`

Поля `queue_groups[]`:

- `job_uuid`
- `created_at`
- `status`
- `items_count`
- `total_copies`

## Следующий этап

Следующий логичный шаг - создать Android-проект и сразу подключить эти маршруты:

1. `dashboard`
2. `kassa/products`
3. `kassa/checkout`
4. `nomenklatura`
5. `postupleniya`
6. `otchety`
7. `barcode-print-queue`
