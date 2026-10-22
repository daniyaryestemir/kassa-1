# Android Plan

Этот документ описывает первую архитектуру Android-приложения для кассовой системы.

## Цель первой версии

Первая Android-версия должна работать с backend из папки `backend` и поддерживать разделы:

- `Главная`
- `Касса`
- `Поступления`
- `Номенклатура`
- `Отчет`
- `Очередь печати`

## Технология

Рекомендуемый стек:

- `Flutter`
- `Dart`
- `HTTP API` к текущему `Flask` backend

Почему `Flutter`:

- один код для Android
- быстрый современный UI
- удобно строить списки, формы и кассовые экраны
- легко подключать API

## Общая схема

Архитектура:

`Flutter app -> Flask API -> MySQL`

Desktop-касса и сайт продолжают работать с той же общей системой данных.

## Структура Android-приложения

Предлагаемая структура проекта:

```text
android_app/
  lib/
    main.dart
    app/
      app.dart
      router.dart
      theme.dart
    core/
      config/
        api_config.dart
      network/
        api_client.dart
      utils/
        formatters.dart
    features/
      dashboard/
        data/
          dashboard_api.dart
          dashboard_models.dart
        presentation/
          dashboard_screen.dart
      kassa/
        data/
          kassa_api.dart
          kassa_models.dart
        presentation/
          kassa_screen.dart
          cart_controller.dart
      nomenklatura/
        data/
          nomenklatura_api.dart
          nomenklatura_models.dart
        presentation/
          nomenklatura_screen.dart
      postupleniya/
        data/
          postupleniya_api.dart
          postupleniya_models.dart
        presentation/
          postupleniya_screen.dart
          postuplenie_detail_screen.dart
      otchety/
        data/
          otchety_api.dart
          otchety_models.dart
        presentation/
          otchety_screen.dart
      print_queue/
        data/
          print_queue_api.dart
          print_queue_models.dart
        presentation/
          print_queue_screen.dart
    widgets/
      app_scaffold.dart
      error_view.dart
      empty_view.dart
      loading_view.dart
```

## Экраны первой версии

### 1. Главная

Показывает:

- продажи за сегодня
- количество чеков за сегодня
- общее количество товаров
- количество поступлений
- количество позиций в очереди печати
- товары с малым остатком

Источник данных:

- `GET /api/dashboard`

### 2. Касса

Показывает:

- поиск товаров
- список товаров
- корзину
- итоговую сумму
- выбор типа продажи
- выбор способа оплаты
- оформление продажи

Источник данных:

- `GET /api/kassa/products`
- `POST /api/kassa/checkout`

### 3. Поступления

Показывает:

- список поступлений
- поиск по контрагенту
- открытие деталей поступления

Источник данных:

- `GET /api/postupleniya`
- `GET /api/postupleniya?uuid=...`

### 4. Номенклатура

Показывает:

- список товаров
- поиск
- остатки
- цены
- штрихкоды
- категории

Источник данных:

- `GET /api/nomenklatura`

### 5. Отчет

Показывает:

- фильтр по дате
- сводку продаж
- прибыль
- суммы по наличным, карте и долгам
- список товарных позиций в отчете

Источник данных:

- `GET /api/otchety`

### 6. Очередь печати

Показывает:

- список заданий
- группировку по job
- статус
- количество копий
- дату создания

Источник данных:

- `GET /api/barcode-print-queue`

## Навигация

Для первой версии лучше сделать нижнее меню на 5 пунктов:

- `Главная`
- `Касса`
- `Поступления`
- `Номенклатура`
- `Еще`

Во вкладке `Еще`:

- `Отчет`
- `Очередь печати`

Это удобнее для телефона, чем пытаться уместить все 6 разделов сразу в нижнюю панель.

## Слой API

Каждый раздел должен иметь свой маленький API-класс.

Пример:

- `DashboardApi`
- `KassaApi`
- `NomenklaturaApi`
- `PostupleniyaApi`
- `OtchetyApi`
- `PrintQueueApi`

Общий `ApiClient` должен:

- хранить базовый URL
- делать `GET` и `POST`
- проверять `ok`
- выбрасывать понятную ошибку

## Модели данных

Для первой версии не нужно делать слишком сложную архитектуру.

Достаточно:

- model classes для ответов API
- простые screen-level controllers
- локальное состояние внутри экранов

На старте можно обойтись без тяжелого state management.

Рекомендуемый подход:

- `ChangeNotifier` или `ValueNotifier`

Если потом проект вырастет, можно перейти на:

- `Riverpod`

## Что не делаем в первой версии

Чтобы не перегрузить запуск, пока не включаем:

- редактирование товаров
- создание поступлений с телефона
- офлайн-режим
- печать напрямую с Android
- полную замену desktop-кассы

Сначала делаем стабильный просмотр и базовую продажу.

## Порядок сборки

1. Создать Flutter-проект `android_app`
2. Настроить базовый роутинг
3. Подключить `ApiClient`
4. Собрать `Главную`
5. Собрать `Номенклатуру`
6. Собрать `Кассу`
7. Собрать `Поступления`
8. Собрать `Отчет`
9. Собрать `Очередь печати`
10. Протестировать на телефоне
11. Собрать `APK`

## Следующий практический шаг

Следующий шаг - создать каркас Flutter-проекта рядом с backend и подготовить внутри него:

- базовую структуру папок
- конфиг API
- стартовые экраны
- навигацию
