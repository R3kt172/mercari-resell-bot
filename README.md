# 🧵 ResellBot

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Playwright](https://img.shields.io/badge/playwright-stealth-45ba4b?logo=playwright&logoColor=white)](https://playwright.dev/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#)

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
| Конфигурация | `python-dotenv` — все секреты и настройки в `.env` |
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
python-dotenv
```

## Конфигурация

Все секреты и настройки читаются из переменных окружения через `python-dotenv` — в коде их нет.

```bash
cp .env.example .env
```

Заполните `.env`:

```env
BOT_TOKEN=your_telegram_bot_token
EBAY_CLIENT_ID=your_ebay_app_id
EBAY_CLIENT_SECRET=your_ebay_cert_id
EBAY_MARKETPLACE_ID=EBAY_US
USDJPY_RATE=150
USE_PROXY=false
PROXY=http://user:pass@host:port
REQUEST_TIMEOUT=25
MAX_RESULTS=15
```

| Переменная | Обязательна | Где взять / что это |
|---|---|---|
| `BOT_TOKEN` | ✅ | [@BotFather](https://t.me/BotFather) — без него бот не стартует |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | нет | [developer.ebay.com](https://developer.ebay.com) → Application Keys → **Production** keyset. Без них просто не будет работать поиск по eBay, остальные площадки не затронуты |
| `EBAY_MARKETPLACE_ID` | нет | регион/валюта выдачи eBay, по умолчанию `EBAY_US` |
| `USDJPY_RATE` | нет | курс йены; статическая константа, обновляйте вручную раз в неделю или подтяните через любой currency API |
| `USE_PROXY` / `PROXY` | нет | прокси для парсинга площадок |
| `REQUEST_TIMEOUT` / `MAX_RESULTS` | нет | таймаут запросов (сек) и лимит карточек в выдаче |

При отсутствии `BOT_TOKEN` бот сразу останавливается с понятной ошибкой в логах — так проще поймать забытый `.env` при деплое.

## Запуск

```bash
python searchbot.py
```

При старте поднимается один Chromium-инстанс на весь процесс, отдельные вкладки открываются под каждый поисковый запрос и закрываются после использования.

## Структура проекта

```
.
├── searchbot.py          # весь бот: БД, парсеры, FSM, хендлеры
├── .env.example           # шаблон переменных окружения
├── .env                    # ваши секреты (в .gitignore, не коммитится)
├── resell_bot.db           # SQLite, создаётся автоматически
├── requirements.txt
├── .gitignore
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

- [x] Вынести конфиг в `.env` / `python-dotenv`
- [ ] Разбить `searchbot.py` на модули
- [ ] Фоновый воркер для авто-обновления цен отслеживаемых лотов и алертов при падении цены
- [ ] Ротация прокси для браузерных парсеров
- [ ] Тесты на парсинг (фикстуры HTML вместо живых запросов)
- [ ] Docker-compose для деплоя

## Безопасность

Перед публикацией репозитория:

1. Убедитесь, что `BOT_TOKEN` и eBay-ключи заданы только в `.env`, а не в коде — в текущей версии `searchbot.py` они уже читаются из окружения.
2. Если старый токен бота когда-либо был закоммичен в историю git — **отзовите его через [@BotFather](https://t.me/BotFather)** (`/revoke`) и выпустите новый: скомпрометированный токен нужно считать утёкшим независимо от того, публичный репозиторий или приватный.
3. Проверьте, что `.env` действительно в `.gitignore` (он уже добавлен) — `git status` не должен его показывать.
4. Если репозиторий уже был запушен со старым `searchbot.py` с хардкодом — секреты остаются в истории коммитов даже после их удаления из последнего коммита. Либо перепишите историю (`git filter-repo` / BFG Repo-Cleaner), либо начните с чистого репозитория.
