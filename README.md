# 🧵 ResellBot

Telegram-бот для поиска выгодных вещей под перепродажу: агрегирует лоты с нескольких площадок, сравнивает цены и считает потенциальный профит.

> Grailed · eBay · StockX · Depop · Mercari JP

---

## Содержание

- [О проекте](#о-проекте)
- [Возможности](#возможности)
- [Стек](#стек)
- [Архитектура](#архитектура)
- [Установка](#установка)
- [Конфигурация](#конфигурация)
- [Запуск](#запуск)
- [Структура проекта](#структура-проекта)
- [Как считается профит](#как-считается-профит)
- [Известные ограничения](#известные-ограничения)
- [Roadmap](#roadmap)
- [Безопасность](#безопасность)

---

## О проекте

ResellBot — телеграм-бот для тех, кто занимается ресейлом одежды и обуви. По запросу пользователя бот ищет товар сразу на нескольких площадках, показывает карточки с ценой и фото, а также умеет отслеживать лоты и считать профит относительно розничной цены.

Часть площадок (Grailed, StockX, Depop) — это SPA с активной защитой от ботов, поэтому парсинг идёт через headless-браузер с элементами "очеловечивания" поведения. eBay подключён через официальный Browse API, а Mercari Japan — через отдельную библиотеку, работающую с внутренним API площадки.

## Возможности

- 🔍 **Поиск по площадкам** — по отдельности или сразу по всем
- 📈 **Сравнение цен** — лучшая цена на каждой площадке в одной таблице
- 💰 **Расчёт профита** — `(розница − (цена + доставка)) / (цена + доставка) × 100%`
- 📊 **Трекинг товаров** — сохранение лотов и история цен в SQLite
- ⚙️ **Персональные настройки** — минимальный порог профита, избранные бренды
- 🇯🇵 **Mercari JP** — отдельный режим с конвертацией йены в доллары
- 🖼️ **Карточки товаров** — пагинация, фото, ссылка на оригинал

## Стек

| Слой | Технологии |
|---|---|
| Bot framework | [aiogram 3](https://docs.aiogram.dev/) (FSM, inline-клавиатуры) |
| Браузерный парсинг | Playwright + [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) |
| HTTP-клиент | aiohttp, brotli (для сжатых ответов) |
| Официальные API | eBay Browse API (OAuth client credentials) |
| Внешние библиотеки | [mercapi](https://github.com/lifailon/mercapi) для Mercari JP |
| Хранилище | SQLite (`users`, `tracked_items`, `price_history`) |
| Прочее | fake-useragent, dataclasses |

## Архитектура

```
Telegram ⇄ aiogram Dispatcher (FSM: поиск → выбор площадки → карточки → трекинг)
                        │
                        ▼
                 MarketParser
        ┌───────────────┼────────────────┐
        │               │                │
  aiohttp + API     Playwright         mercapi
  (eBay Browse)   (Grailed/StockX/    (Mercari JP,
                      Depop)          internal API)
                        │
                        ▼
                 SQLite (resell_bot.db)
```

- **`BrowserManager`** держит один экземпляр Chromium на всё время жизни бота — под каждый поиск открывается свежий контекст с рандомным User-Agent и stealth-патчами, а не новый браузер.
- **`MarketParser`** — единая точка входа для всех источников: у каждой площадки свой метод `search_*`, но все возвращают список `Product`.
- **`Database`** — тонкая обёртка над `sqlite3` без ORM: для объёма данных бота этого достаточно.

## Установка

```bash
git clone https://github.com/<your-username>/resell-bot.git
cd resell-bot

python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

**Зависимости** (`requirements.txt`):

```
aiogram>=3.0
aiohttp
brotli
fake-useragent
playwright
playwright-stealth
mercapi
```

## Конфигурация

Секреты **не должны лежать в коде** — сейчас в `searchbot.py` они захардкожены прямо в константах, это стоит вынести в переменные окружения перед пушем в публичный репозиторий.

Создайте `.env`:

```env
BOT_TOKEN=your_telegram_bot_token
EBAY_CLIENT_ID=your_ebay_app_id
EBAY_CLIENT_SECRET=your_ebay_cert_id
EBAY_MARKETPLACE_ID=EBAY_US
USE_PROXY=false
PROXY=http://user:pass@host:port
```

И подгружайте их через `python-dotenv` / `os.environ` вместо констант в начале файла.

| Переменная | Где взять |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | [developer.ebay.com](https://developer.ebay.com) → Application Keys → **Production** keyset |
| `USDJPY_RATE` | курс йены; можно раз в неделю подтягивать через любой currency API вместо константы |

## Запуск

```bash
python searchbot.py
```

При старте поднимается один Chromium-инстанс на весь процесс, отдельные вкладки открываются под каждый поисковый запрос и закрываются после использования.

## Структура проекта

```
.
├── searchbot.py          # весь бот: БД, парсеры, FSM, хендлеры
├── resell_bot.db          # SQLite, создаётся автоматически
├── requirements.txt
└── README.md
```

> Файл сейчас монолитный — при росте функциональности стоит разнести на модули: `db.py`, `parsers/`, `handlers/`, `keyboards.py`, `config.py`.

### Схема БД

```
users            (user_id, username, min_profit, preferred_brands, preferred_platforms)
tracked_items    (id, user_id, item_url, item_name, retail_price, current_price,
                   min_price, max_price, profit_percent, platform, last_checked, shipping_cost)
price_history    (id, item_id, price, checked_at)
```

## Как считается профит

```
итоговая цена = цена лота + стоимость доставки
профит, %     = (розничная цена − итоговая цена) / итоговая цена × 100
```

Показывается зелёным/красным индикатором и сравнивается с персональным порогом пользователя из настроек (по умолчанию 10%).

## Известные ограничения

- **Cloudflare / антибот-защита.** Stealth-патчи скрывают базовые признаки автоматизации (`navigator.webdriver` и т.п.), но не спасают от блокировки по IP- и TLS-фингерпринту — StockX особенно капризен в этом плане.
- **Хрупкость селекторов.** Grailed, StockX и Depop — SPA без публичного API, парсинг идёт по CSS-селекторам разметки, которая может измениться в любой момент.
- **eBay возвращает сырые цены без учёта скидок/купонов** — сортировка идёт по `price`, без учёта итоговой стоимости с доставкой на уровне API-запроса.
- **Курс йены — статическая константа**, требует ручного/периодического обновления.

## Roadmap

- [ ] Вынести конфиг в `.env` / `pydantic-settings`
- [ ] Разбить `searchbot.py` на модули
- [ ] Фоновый воркер для авто-обновления цен отслеживаемых лотов и алертов при падении цены
- [ ] Ротация прокси для браузерных парсеров
- [ ] Тесты на парсинг (фикстуры HTML вместо живых запросов)
- [ ] Docker-compose для деплоя

## Безопасность

Перед публикацией репозитория:

1. Замените захардкоженный `BOT_TOKEN` и eBay-ключи на переменные окружения.
2. Если токен бота уже засветился в истории коммитов — **отзовите его через [@BotFather](https://t.me/BotFather)** (`/revoke`) и выпустите новый: старый нужно считать скомпрометированным.
3. Добавьте `.env` в `.gitignore`.
