# TravelLine Daily Occupancy Report → Telegram

Каждое утро в **8:00 по Москве** бот присылает в Telegram данные о загрузке отеля
за предыдущий день из **TravelLine Partner API** (PMS Analytics API):

- сколько номеров было занято (платно / бесплатно),
- сколько номеров не в эксплуатации (ремонт/консервация),
- % загрузки (если известно общее число номеров),
- количество заездов и гостей за день,
- выручка отеля за день (по номерам / по питанию / общая).

Работает бесплатно на GitHub Actions — свой сервер не нужен.

---

## Важно: TravelLine API требует официального подключения

В отличие от MOEX ISS API, TravelLine Partner API — это **не публичный** API:
доступ выдаётся только через официальное подключение к TravelLine. Есть два пути:

### Вариант A — вы сами являетесь отелем (или его сотрудником) в TravelLine

1. Зайдите в личный кабинет TravelLine.
2. Найдите раздел **«Подключения API»** (см. [инструкцию TravelLine](https://www.travelline.ru/support/knowledge-base/kak-sozdat-podklyuchenie-k-api/)).
3. Создайте новое API-подключение — система выдаст пару `client_id` / `client_secret`.
4. Узнайте `propertyId` вашего отеля — его можно увидеть в личном кабинете или
   получить через метод `GET /v1/properties` (Content API) с этими же учётными данными.

### Вариант B — вы не отель, а внешняя компания/интегратор

Нужно стать партнёром TravelLine и запросить тестовый/рабочий доступ:
`support@travelline.ru` или через раздел «Прямое подключение партнера»
в [документации для разработчиков](https://www.travelline.ru/dev-portal/docs/connect/partner).

**Без `client_id`, `client_secret` и `propertyId` бот не заработает** — это
не какое-то ограничение скрипта, а требование самого TravelLine API.

---

## Шаг 1. Telegram-бот (как и раньше)

1. Напишите **@BotFather** → `/newbot` → сохраните токен.
2. Напишите вашему боту любое сообщение (чтобы он мог писать вам).
3. Узнайте свой `chat_id` через **@userinfobot** или через
   `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`.

## Шаг 2. Разместить проект на GitHub

Создайте репозиторий и загрузите файлы, сохранив структуру:
```
travelline_daily_report.py
requirements.txt
.github/workflows/daily-report.yml
README.md
```

## Шаг 3. Добавить секреты

**Settings → Secrets and variables → Actions → New repository secret**

| Имя | Значение |
|---|---|
| `TL_CLIENT_ID` | client_id из личного кабинета TravelLine |
| `TL_CLIENT_SECRET` | client_secret из личного кабинета TravelLine |
| `TL_PROPERTY_ID` | ID вашего отеля в TravelLine |
| `TELEGRAM_BOT_TOKEN` | токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | ваш chat_id |

## Шаг 4. Проверить

Вкладка **Actions** → workflow **"TravelLine Daily Occupancy Report"** →
**Run workflow** → запуск вручную. Через 20–30 секунд должно прийти сообщение.

Если ошибка — смотрите лог шага "Run report script":
- `401 Unauthorized` при получении токена → неверные `TL_CLIENT_ID`/`TL_CLIENT_SECRET`.
- Пустой отчёт с предупреждением в тексте → проверьте `TL_PROPERTY_ID` и права
  API-подключения (нужен доступ к PMS Analytics API — уточните у TravelLine,
  что подключение включает нужную область доступа `api_accesses`).

После первого успешного запуска бот будет работать сам, каждый день в 8:00 по Москве.

---

## Технические детали (на случай, если что-то настраивать)

- **Авторизация**: OAuth 2.0, Client Credentials Flow.
  Эндпоинт: `https://partner.tlintegration.com/auth/token`.
  Токен живёт **15 минут**, refresh не поддерживается — скрипт получает новый
  токен при каждом запуске (это нормально для ежедневного запуска раз в сутки).
- **Данные о загрузке**: `GET https://partner.tlintegration.com/api/pms-analytics/v1/properties/{propertyId}/daily-occupancy`
  с параметрами `startStayDate` / `endStayDate` (максимум 31 день диапазон).
- **Лимиты**: 50 запросов/сек, 200/мин, 3000/час — для одного запуска в день
  это несущественно.
- **% загрузки**: скрипт пытается получить общее число номеров через
  `GET /v2/properties/{propertyId}/rooms`, чтобы посчитать процент. Если это
  не получится (например, метод недоступен для вашего подключения), отчёт всё
  равно придёт — просто без процента, с абсолютными цифрами.
- **Время отправки**: измените `cron: '0 5 * * *'` в workflow-файле (время всегда в UTC;
  МСК = UTC+3 круглый год).

Если понадобится добавить дополнительные метрики (например, ADR/RevPAR или разбивку
по категориям номеров), их можно получить из смежных методов TravelLine
Partner API — дайте знать, что именно нужно, и я дополню скрипт.
