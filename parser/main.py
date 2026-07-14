    # searchbot.py
    # бот для поиска шмоток на разных площадках + сравнение цен + трекинг профита
    # grailed/stockx/depop - через playwright (SPA, без браузера не спарсить)
    # ebay - через официальный Browse API (ключи от developer.ebay.com)
    # mercari jp - через либу mercapi (дергает их внутренний api напрямую)

    import asyncio
    import logging
    import sqlite3
    import re
    import random
    import time
    import base64
    import urllib.parse
    from datetime import datetime
    from typing import Dict, List, Optional
    from dataclasses import dataclass

    import aiohttp
    import brotli

    from aiogram import Bot, Dispatcher, types, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import (
        InlineKeyboardMarkup, InlineKeyboardButton,
        ReplyKeyboardMarkup, KeyboardButton,
        CallbackQuery, Message
    )
    from fake_useragent import UserAgent
    from playwright.async_api import async_playwright, Browser, BrowserContext
    from playwright_stealth import Stealth

    # ==================== КОНФИГ ====================
    BOT_TOKEN = "8987441799:AAHUx5qh44H1-iXz0zy0YWjFzOm_OU9Q0jc"  # свой токен сюда

    # eBay Browse API - developer.ebay.com -> Application Keys -> Production keyset
    EBAY_CLIENT_ID = "ТВОЙ_APP_ID_СЮДА"
    EBAY_CLIENT_SECRET = "ТВОЙ_CERT_ID_SECRET_СЮДА"
    EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    EBAY_BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    EBAY_MARKETPLACE_ID = "EBAY_US"  # влияет на валюту/регион выдачи

    REQUEST_TIMEOUT = 25
    MAX_RESULTS = 15
    USE_PROXY = False
    PROXY = "http://proxy:port"

    USDJPY_RATE = 150  # курс йены, раз в неделю можно подтягивать актуальный через любой currency api

    logging.basicConfig(level=logging.INFO)


    # ==================== БД ====================
    class Database:
        """простая обертка над sqlite, без ORM - для такого объема данных хватает с головой"""

        def __init__(self, db_name="resell_bot.db"):
            self.conn = sqlite3.connect(db_name, check_same_thread=False)
            self.cursor = self.conn.cursor()
            self._init_tables()

        def _init_tables(self):
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    min_profit INTEGER DEFAULT 10,
                    preferred_brands TEXT,
                    preferred_platforms TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS tracked_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    item_url TEXT,
                    item_name TEXT,
                    retail_price REAL,
                    current_price REAL,
                    min_price REAL,
                    max_price REAL,
                    profit_percent INTEGER,
                    platform TEXT,
                    last_checked TIMESTAMP,
                    notification_sent BOOLEAN DEFAULT 0,
                    shipping_cost REAL DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    price REAL,
                    checked_at TIMESTAMP,
                    FOREIGN KEY(item_id) REFERENCES tracked_items(id)
                )
            ''')
            self.conn.commit()

        def add_user(self, user_id: int, username: str = ""):
            self.cursor.execute(
                "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            self.conn.commit()

        def update_user_settings(self, user_id: int, **kwargs):
            updates, values = [], []
            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    values.append(value)

            if not updates:
                return

            values.append(user_id)
            query = f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?"
            self.cursor.execute(query, values)
            self.conn.commit()

        def get_user_settings(self, user_id: int) -> Dict:
            self.cursor.execute(
                "SELECT min_profit, preferred_brands, preferred_platforms FROM users WHERE user_id = ?",
                (user_id,)
            )
            result = self.cursor.fetchone()
            return {
                "min_profit": result[0] if result else 10,
                "preferred_brands": result[1] if result else "",
                "preferred_platforms": result[2] if result else "all"
            }

        def add_tracked_item(self, user_id: int, item_data: Dict) -> int:
            self.cursor.execute('''
                INSERT INTO tracked_items 
                (user_id, item_url, item_name, retail_price, current_price, 
                min_price, max_price, profit_percent, platform, last_checked, shipping_cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                item_data['url'],
                item_data['name'],
                item_data['retail_price'],
                item_data['current_price'],
                item_data['current_price'],
                item_data['current_price'],
                item_data['profit_percent'],
                item_data['platform'],
                datetime.now(),
                item_data.get('shipping_cost', 0)
            ))
            item_id = self.cursor.lastrowid

            self.cursor.execute(
                "INSERT INTO price_history (item_id, price) VALUES (?, ?)",
                (item_id, item_data['current_price'])
            )
            self.conn.commit()
            return item_id

        def get_tracked_items(self, user_id: int) -> List[Dict]:
            self.cursor.execute('''
                SELECT id, item_url, item_name, retail_price, current_price, 
                    min_price, max_price, profit_percent, platform, last_checked, shipping_cost
                FROM tracked_items
                WHERE user_id = ?
                ORDER BY profit_percent DESC
            ''', (user_id,))
            columns = ['id', 'url', 'name', 'retail_price', 'current_price',
                    'min_price', 'max_price', 'profit_percent', 'platform', 'last_checked', 'shipping_cost']
            return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

        def delete_item(self, item_id: int, user_id: int):
            self.cursor.execute(
                "DELETE FROM tracked_items WHERE id = ? AND user_id = ?",
                (item_id, user_id)
            )
            self.cursor.execute("DELETE FROM price_history WHERE item_id = ?", (item_id,))
            self.conn.commit()

        def get_items_by_profit(self, user_id: int, min_profit: int) -> List[Dict]:
            self.cursor.execute('''
                SELECT id, item_url, item_name, retail_price, current_price, 
                    profit_percent, platform, shipping_cost
                FROM tracked_items
                WHERE user_id = ? AND profit_percent >= ?
                ORDER BY profit_percent DESC
            ''', (user_id, min_profit))
            columns = ['id', 'url', 'name', 'retail_price', 'current_price',
                    'profit_percent', 'platform', 'shipping_cost']
            return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

        def close(self):
            self.conn.close()


    db = Database()


    # ==================== БРАУЗЕР ====================
    class BrowserManager:
        """
        один Chromium на весь бот, чтобы не поднимать процесс на каждый запрос
        (старт браузера сам по себе 1-3 сек). под каждый поиск - новая вкладка/контекст.
        stealth патчит базовые признаки автоматизации (navigator.webdriver и тд),
        но от Cloudflare по IP/TLS фингерпринту это не спасает стопроцентно.
        """

        def __init__(self):
            self._playwright = None
            self.browser: Optional[Browser] = None
            self._stealth = Stealth()

        async def start(self):
            if self.browser:
                return
            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            logging.info("Chromium запущен")

        async def new_context(self) -> BrowserContext:
            if not self.browser:
                await self.start()
            ua = UserAgent()
            context = await self.browser.new_context(
                user_agent=ua.random,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            await self._stealth.apply_stealth_async(context)
            return context

        async def stop(self):
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logging.info("Chromium остановлен")


    browser_manager = BrowserManager()


    async def human_pause(min_ms: int = 400, max_ms: int = 1100):
        """рандомная пауза, чтобы не дергать страницу мгновенно как бот"""
        await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


    async def human_scroll(page):
        try:
            await page.mouse.wheel(0, random.randint(200, 600))
            await human_pause(200, 500)
        except Exception:
            pass


    # ==================== МОДЕЛЬ ====================
    @dataclass
    class Product:
        name: str
        price: float
        url: str
        platform: str
        image_url: str = ""
        retail_price: float = 0
        profit_percent: int = 0
        shipping_cost: float = 0
        currency: str = "USD"
        condition: str = ""

        def calculate_profit(self):
            if self.retail_price > 0:
                total_cost = self.price + self.shipping_cost
                self.profit_percent = int((self.retail_price - total_cost) / total_cost * 100)
            return self.profit_percent


    # ==================== ПАРСЕРЫ ====================
    class MarketParser:
        def __init__(self):
            self.session: Optional[aiohttp.ClientSession] = None
            self.ua = UserAgent()
            self.timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._ebay_token = None
            self._ebay_token_expires_at = 0

        async def __aenter__(self):
            self.session = aiohttp.ClientSession(timeout=self.timeout)
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if self.session:
                await self.session.close()

        async def fetch(self, url: str, params: dict = None, headers: dict = None) -> str:
            default_headers = {
                'User-Agent': self.ua.random,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Cache-Control': 'no-cache',
            }
            if headers:
                default_headers.update(headers)

            proxy = PROXY if USE_PROXY else None

            try:
                async with self.session.get(url, headers=default_headers, params=params,
                                            allow_redirects=True, proxy=proxy) as response:
                    if response.status != 200:
                        logging.warning(f"HTTP {response.status} для {url}")
                        return ""

                    if 'br' in response.headers.get('Content-Encoding', '').lower():
                        try:
                            raw = await response.read()
                            return brotli.decompress(raw).decode('utf-8')
                        except Exception as e:
                            logging.error(f"brotli decode error: {e}")
                            return await response.text()

                    return await response.text()
            except Exception as e:
                logging.error(f"fetch error {url}: {e}")
                return ""

        # ---------- Mercari JP (через mercapi, без браузера) ----------
        async def search_mercari_jp(self, query: str) -> List[Product]:
            products = []
            try:
                from mercapi import Mercapi
            except ImportError:
                logging.error("Mercari JP: нет либы mercapi (pip install mercapi)")
                return products

            try:
                m = Mercapi()
                results = await m.search(query)

                for item in results.items[:MAX_RESULTS]:
                    try:
                        if item.is_no_price:
                            continue

                        price_jpy = float(item.price)
                        price_usd = round(price_jpy / USDJPY_RATE, 2)
                        shipping = 5.0 if price_jpy < 5000 else 0.0

                        products.append(Product(
                            name=item.name[:100],
                            price=price_usd,
                            url=f"https://jp.mercari.com/item/{item.id_}",
                            platform="Mercari JP",
                            image_url=item.thumbnails[0] if item.thumbnails else "",
                            shipping_cost=shipping,
                            currency="JPY",
                        ))
                    except Exception as e:
                        logging.error(f"mercari item parse error: {e}")
                        continue
            except Exception as e:
                logging.error(f"mercari search error: {e}")

            return products

        # ---------- Grailed (SPA, нужен браузер) ----------
        async def search_grailed(self, query: str) -> List[Product]:
            products = []
            search_url = f"https://www.grailed.com/search?q={urllib.parse.quote(query)}"

            context = await browser_manager.new_context()
            try:
                page = await context.new_page()
                await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
                await human_pause()
                await human_scroll(page)

                try:
                    await page.wait_for_selector('a[href*="/listings/"]', timeout=10000)
                except Exception:
                    logging.warning("grailed: карточки не подгрузились (капча/поменялась верстка)")

                cards = await page.query_selector_all('a[href*="/listings/"]')

                for card in cards[:MAX_RESULTS]:
                    try:
                        href = await card.get_attribute('href')
                        if not href:
                            continue
                        link = href if href.startswith('http') else f"https://www.grailed.com{href}"

                        full_text = (await card.inner_text()) or ""
                        if not full_text.strip():
                            continue

                        price_match = re.search(r'\$\s?([\d,]+(?:\.\d{2})?)', full_text)
                        if not price_match:
                            continue
                        price = float(price_match.group(1).replace(',', ''))

                        name = next((l.strip() for l in full_text.split('\n')
                                    if l.strip() and not l.strip().startswith('$')), "")
                        if not name:
                            continue

                        img_elem = await card.query_selector('img')
                        image_url = ""
                        if img_elem:
                            image_url = (await img_elem.get_attribute('src')) or \
                                        (await img_elem.get_attribute('data-src')) or ""

                        products.append(Product(
                            name=name[:100], price=price, url=link,
                            platform="Grailed", image_url=image_url,
                        ))
                    except Exception as e:
                        logging.error(f"grailed card parse error: {e}")
                        continue

                await page.close()
            except Exception as e:
                logging.error(f"grailed error: {e}")
            finally:
                await context.close()

            return products

        # ---------- eBay OAuth token (кэшируется на ~2 часа) ----------
        async def _get_ebay_token(self) -> Optional[str]:
            now = time.time()
            if self._ebay_token and now < self._ebay_token_expires_at - 60:
                return self._ebay_token

            if EBAY_CLIENT_ID.startswith("ТВОЙ_") or EBAY_CLIENT_SECRET.startswith("ТВОЙ_"):
                logging.error("eBay ключи не заполнены (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
                return None

            creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            }
            data = {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            }

            try:
                async with self.session.post(EBAY_OAUTH_URL, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        logging.error(f"ebay oauth {resp.status}: {(await resp.text())[:300]}")
                        return None
                    result = await resp.json()
                    self._ebay_token = result["access_token"]
                    self._ebay_token_expires_at = now + int(result.get("expires_in", 7200))
                    return self._ebay_token
            except Exception as e:
                logging.error(f"ebay oauth request failed: {e}")
                return None

        # ---------- eBay (официальный Browse API) ----------
        async def search_ebay(self, query: str) -> List[Product]:
            products = []
            token = await self._get_ebay_token()
            if not token:
                return products

            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
                "Content-Type": "application/json",
            }
            params = {"q": query, "limit": str(MAX_RESULTS), "sort": "price"}

            try:
                async with self.session.get(EBAY_BROWSE_SEARCH_URL, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        logging.error(f"ebay browse api {resp.status}: {(await resp.text())[:300]}")
                        return products

                    data = await resp.json()
                    for item in data.get("itemSummaries", []):
                        try:
                            name = item.get("title", "")
                            price_info = item.get("price", {})
                            price = float(price_info.get("value", 0))
                            link = item.get("itemWebUrl", "")
                            if not (name and price > 0 and link):
                                continue

                            shipping_cost = 0.0
                            shipping_options = item.get("shippingOptions", [])
                            if shipping_options:
                                shipping_cost = float(shipping_options[0].get("shippingCost", {}).get("value", 0))

                            products.append(Product(
                                name=name[:100],
                                price=price,
                                url=link,
                                platform="eBay",
                                image_url=item.get("image", {}).get("imageUrl", ""),
                                shipping_cost=shipping_cost,
                                currency=price_info.get("currency", "USD"),
                                condition=item.get("condition", ""),
                            ))
                        except Exception as e:
                            logging.error(f"ebay item parse error: {e}")
                            continue
            except Exception as e:
                logging.error(f"ebay browse api request failed: {e}")

            return products

        # ---------- StockX (SPA + жесткий cloudflare) ----------
        async def search_stockx(self, query: str) -> List[Product]:
            products = []
            search_url = f"https://stockx.com/search?s={urllib.parse.quote(query)}"

            context = await browser_manager.new_context()
            try:
                page = await context.new_page()
                await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
                await human_pause()
                await human_scroll(page)

                try:
                    await page.wait_for_selector('a[href^="/"][data-testid]', timeout=10000)
                except Exception:
                    try:
                        await page.wait_for_selector('div[class*="product"] a', timeout=5000)
                    except Exception:
                        logging.warning("stockx: пусто (скорее всего капча)")

                cards = await page.query_selector_all('a[href^="/"][data-testid]')
                if not cards:
                    cards = await page.query_selector_all('div[class*="product"] a[href]')

                for card in cards[:MAX_RESULTS]:
                    try:
                        href = await card.get_attribute('href')
                        if not href or href in ('/', '/search'):
                            continue
                        link = href if href.startswith('http') else f"https://stockx.com{href}"

                        full_text = (await card.inner_text()) or ""
                        if not full_text.strip():
                            continue

                        price_match = re.search(r'\$\s?([\d,]+(?:\.\d{2})?)', full_text)
                        if not price_match:
                            continue
                        price = float(price_match.group(1).replace(',', ''))

                        name = next((l.strip() for l in full_text.split('\n')
                                    if l.strip() and not l.strip().startswith('$') and 'Last Sale' not in l), "")
                        if not name:
                            continue

                        img_elem = await card.query_selector('img')
                        image_url = (await img_elem.get_attribute('src')) if img_elem else ""

                        products.append(Product(
                            name=name[:100], price=price, url=link,
                            platform="StockX", image_url=image_url or "",
                        ))
                    except Exception as e:
                        logging.error(f"stockx card parse error: {e}")
                        continue

                await page.close()
            except Exception as e:
                logging.error(f"stockx error: {e}")
            finally:
                await context.close()

            return products

        # ---------- Depop (SPA) ----------
        async def search_depop(self, query: str) -> List[Product]:
            products = []
            search_url = f"https://www.depop.com/search/?q={urllib.parse.quote(query)}"

            context = await browser_manager.new_context()
            try:
                page = await context.new_page()
                await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
                await human_pause()
                await human_scroll(page)

                try:
                    await page.wait_for_selector('a[href*="/products/"]', timeout=10000)
                except Exception:
                    logging.warning("depop: карточки не появились")

                cards = await page.query_selector_all('a[href*="/products/"]')

                for card in cards[:MAX_RESULTS]:
                    try:
                        href = await card.get_attribute('href')
                        if not href:
                            continue
                        link = href if href.startswith('http') else f"https://www.depop.com{href}"

                        full_text = (await card.inner_text()) or ""
                        if not full_text.strip():
                            continue

                        price_match = re.search(r'[\$£€]\s?([\d,]+(?:\.\d{2})?)', full_text)
                        if not price_match:
                            continue
                        price = float(price_match.group(1).replace(',', ''))

                        name = next((l.strip() for l in full_text.split('\n')
                                    if l.strip() and not re.match(r'^[\$£€]', l.strip())), "") or "Depop item"

                        img_elem = await card.query_selector('img')
                        image_url = ""
                        if img_elem:
                            image_url = (await img_elem.get_attribute('src')) or \
                                        (await img_elem.get_attribute('data-src')) or ""

                        products.append(Product(
                            name=name[:100], price=price, url=link,
                            platform="Depop", image_url=image_url,
                        ))
                    except Exception as e:
                        logging.error(f"depop card parse error: {e}")
                        continue

                await page.close()
            except Exception as e:
                logging.error(f"depop error: {e}")
            finally:
                await context.close()

            return products


    # ==================== СОСТОЯНИЯ ====================
    class SearchState(StatesGroup):
        waiting_for_query = State()
        waiting_for_retail_price = State()
        waiting_for_price_check = State()


    class SettingsState(StatesGroup):
        changing_min_profit = State()
        changing_brands = State()


    # ==================== КЛАВИАТУРЫ ====================
    def main_menu_keyboard():
        keyboard = [
            [KeyboardButton(text="🔍 Поиск товара")],
            [KeyboardButton(text="📊 Мои отслеживаемые")],
            [KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="💰 Топ по профиту")],
            [KeyboardButton(text="📈 Сравнение цен")],
            [KeyboardButton(text="🇯🇵 Mercari JP")],
            [KeyboardButton(text="❓ Помощь")],
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


    def platform_keyboard():
        keyboard = [
            [InlineKeyboardButton(text="👟 Grailed", callback_data="platform_grailed"),
            InlineKeyboardButton(text="🛍️ eBay", callback_data="platform_ebay")],
            [InlineKeyboardButton(text="📦 StockX", callback_data="platform_stockx"),
            InlineKeyboardButton(text="👗 Depop", callback_data="platform_depop")],
            [InlineKeyboardButton(text="🇯🇵 Mercari JP", callback_data="platform_mercari_jp")],
            [InlineKeyboardButton(text="🌐 Все площадки", callback_data="platform_all")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)


    # ==================== БОТ ====================
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    PLATFORM_NAMES = {
        "grailed": "Grailed",
        "ebay": "eBay",
        "stockx": "StockX",
        "depop": "Depop",
        "mercari_jp": "Mercari JP",
        "all": "всех площадках",
    }


    @dp.message(Command("start"))
    async def start_command(message: Message, state: FSMContext):
        db.add_user(message.from_user.id, message.from_user.username or "")
        await state.clear()
        await message.answer(
            "👋 Привет! Ищу выгодные вещи для реселла.\n\n"
            "🔹 Grailed, eBay, StockX, Depop, Mercari JP\n"
            "🔹 Сравниваю цены и считаю профит\n"
            "🔹 Mercari JP - японский рынок, часто редкие вещи дешево\n\n"
            "Жми на кнопки ниже:",
            reply_markup=main_menu_keyboard()
        )


    @dp.message(F.text == "🇯🇵 Mercari JP")
    async def mercari_search(message: Message, state: FSMContext):
        await message.answer(
            "🇯🇵 <b>Mercari Japan</b>\n\n"
            "💰 Цены в йенах, конвертирую в USD\n"
            "📦 Доставка из Японии ~$5-15\n\n"
            "Введите название товара:",
            parse_mode="HTML",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(SearchState.waiting_for_query)
        await state.update_data(force_platform="mercari_jp")


    @dp.message(F.text == "🔍 Поиск товара")
    async def search_item(message: Message, state: FSMContext):
        await message.answer("Введите название товара:", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(SearchState.waiting_for_query)


    @dp.message(SearchState.waiting_for_query)
    async def process_search_query(message: Message, state: FSMContext):
        await state.update_data(query=message.text)
        data = await state.get_data()
        force_platform = data.get('force_platform')

        if force_platform:
            await search_on_platform(message, state, force_platform, message.text)
        else:
            await message.answer("Выберите площадку:", reply_markup=platform_keyboard())


    @dp.callback_query(F.data.startswith("platform_"))
    async def select_platform(callback: CallbackQuery, state: FSMContext):
        platform = callback.data.replace("platform_", "")
        data = await state.get_data()
        await search_on_platform(callback.message, state, platform, data.get('query', ''))


    async def search_on_platform(message: Message, state: FSMContext, platform: str, query: str):
        await message.edit_text(f"🔍 Ищу '{query}' на {PLATFORM_NAMES.get(platform, platform)}...\n⏱️ Подождите")

        products: List[Product] = []
        async with MarketParser() as parser:
            if platform == "grailed":
                products = await parser.search_grailed(query)
            elif platform == "ebay":
                products = await parser.search_ebay(query)
            elif platform == "stockx":
                products = await parser.search_stockx(query)
            elif platform == "depop":
                products = await parser.search_depop(query)
            elif platform == "mercari_jp":
                products = await parser.search_mercari_jp(query)
            elif platform == "all":
                products = (
                    await parser.search_grailed(query)
                    + await parser.search_ebay(query)
                    + await parser.search_stockx(query)
                    + await parser.search_depop(query)
                    + await parser.search_mercari_jp(query)
                )

        if not products:
            await message.edit_text(
                "❌ Ничего не найдено. Попробуйте другой запрос.\n\n"
                "💡 Советы:\n"
                "• Используйте английские названия\n"
                "• Для Mercari можно японские\n"
                "• Проверьте написание",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")
                ]])
            )
            await state.update_data(force_platform=None)
            return

        await state.update_data(products=products, force_platform=None)
        await show_products(message, products, 0, state)


    async def show_products(message: Message, products: List[Product], page: int, state: FSMContext):
        if not products or page >= len(products):
            await message.edit_text("❌ Товары не найдены")
            return

        product = products[page]
        await state.update_data(current_product=product, page=page, products=products)

        text = f"🛍️ <b>{product.name}</b>\n\n💰 Цена: <b>${product.price:.2f}</b>"

        if product.shipping_cost > 0:
            text += f"\n📦 Доставка: <b>${product.shipping_cost:.2f}</b>"
            text += f"\n💵 Итого: <b>${product.price + product.shipping_cost:.2f}</b>"

        text += f"\n🏷️ Площадка: {product.platform}"
        if product.condition:
            text += f"\n🏷️ Состояние: {product.condition}"
        if product.currency != "USD":
            text += f"\n💱 Валюта: {product.currency}"
        text += f"\n🔗 <a href='{product.url}'>Ссылка на товар</a>\n\n"
        text += "<i>Введите розничную цену для расчета профита</i>"

        total_pages = len(products)
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"prev_{page}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="none"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"next_{page}"))

        keyboard = [
            nav_row,
            [InlineKeyboardButton(text="✅ Отслеживать", callback_data=f"track_{page}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

        # если есть картинка - шлем фото, edit_text из текста в фото телега не дает
        if product.image_url:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer_photo(
                photo=product.image_url, caption=text, parse_mode="HTML", reply_markup=reply_markup
            )
        else:
            try:
                await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            except Exception:
                # прошлое сообщение могло быть фото-карточкой - edit_text на нем упадет
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


    @dp.callback_query(F.data.startswith("next_") | F.data.startswith("prev_"))
    async def paginate_products(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        products = data.get('products', [])
        current_page = data.get('page', 0)

        page = current_page + 1 if callback.data.startswith("next_") else current_page - 1

        if page < 0 or page >= len(products):
            await callback.answer("❌ Нет больше товаров")
            return

        await state.update_data(page=page)
        await show_products(callback.message, products, page, state)


    @dp.callback_query(F.data.startswith("track_"))
    async def track_product(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        products = data.get('products', [])
        page = data.get('page', 0)

        if page >= len(products):
            await callback.answer("❌ Товар не найден")
            return

        product = products[page]
        await state.update_data(tracking_product=product)

        prompt_text = (
            f"📝 Введите розничную цену для '{product.name[:50]}...':\n"
            f"(в долларах, например: 150)\n\n"
            f"<i>Текущая цена: ${product.price:.2f}</i>"
        )
        prompt_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_main")]
        ])

        try:
            await callback.message.edit_text(prompt_text, parse_mode="HTML", reply_markup=prompt_keyboard)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(prompt_text, parse_mode="HTML", reply_markup=prompt_keyboard)

        await state.set_state(SearchState.waiting_for_retail_price)


    @dp.message(SearchState.waiting_for_retail_price)
    async def process_retail_price(message: Message, state: FSMContext):
        try:
            retail_price = float(message.text.replace('$', '').replace(',', '').strip())
        except ValueError:
            await message.answer("❌ Введите корректную цену (например: 150)")
            return

        data = await state.get_data()
        product = data.get('tracking_product')

        if not product:
            await message.answer("❌ Ошибка, начните заново")
            await state.clear()
            return

        product.retail_price = retail_price
        profit = product.calculate_profit()

        item_data = {
            'url': product.url,
            'name': product.name,
            'retail_price': retail_price,
            'current_price': product.price,
            'profit_percent': profit,
            'platform': product.platform,
            'shipping_cost': product.shipping_cost,
        }

        user_id = message.from_user.id
        db.add_tracked_item(user_id, item_data)
        min_profit = db.get_user_settings(user_id)['min_profit']

        text = (
            f"✅ Товар добавлен!\n\n"
            f"🛍️ <b>{product.name}</b>\n"
            f"💰 Розница: ${retail_price:.2f}\n"
            f"💵 Цена: ${product.price:.2f}"
        )
        if product.shipping_cost > 0:
            text += f"\n📦 Доставка: ${product.shipping_cost:.2f}"
            text += f"\n💵 Итого: ${product.price + product.shipping_cost:.2f}"
        text += f"\n📈 Профит: <b>{profit}%</b>\n🏷️ {product.platform}\n🔗 <a href='{product.url}'>Ссылка</a>\n\n"
        text += f"✅ <b>Проходит по фильтру ({min_profit}%)!</b>" if profit >= min_profit \
            else f"⚠️ Не проходит по фильтру ({min_profit}%)"

        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
        await state.clear()


    @dp.message(F.text == "📊 Мои отслеживаемые")
    async def show_tracked(message: Message):
        items = db.get_tracked_items(message.from_user.id)
        if not items:
            await message.answer("📭 Нет отслеживаемых товаров.", reply_markup=main_menu_keyboard())
            return

        text = "📊 <b>Отслеживаемые товары:</b>\n\n"
        for i, item in enumerate(items[:10], 1):
            emoji = "🟢" if item['profit_percent'] > 0 else "🔴"
            text += (
                f"{i}. {emoji} <b>{item['name'][:40]}...</b>\n"
                f"   💰 ${item['current_price']:.2f} | 📈 {item['profit_percent']}%\n"
                f"   🏷️ {item['platform']}\n"
                f"   🔗 <a href='{item['url']}'>Ссылка</a>\n\n"
            )
        if len(items) > 10:
            text += f"<i>...и еще {len(items) - 10} товаров</i>"

        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


    @dp.message(F.text == "💰 Топ по профиту")
    async def show_top_profit(message: Message):
        user_id = message.from_user.id
        min_profit = db.get_user_settings(user_id)['min_profit']
        items = db.get_items_by_profit(user_id, min_profit)

        if not items:
            await message.answer(f"❌ Нет товаров с профитом >= {min_profit}%", reply_markup=main_menu_keyboard())
            return

        text = f"💰 <b>Топ товаров ({min_profit}%+):</b>\n\n"
        for i, item in enumerate(items[:15], 1):
            text += (
                f"{i}. 🏆 <b>{item['name'][:40]}...</b>\n"
                f"   📈 Профит: <b>{item['profit_percent']}%</b>\n"
                f"   💰 ${item['current_price']:.2f} (ритейл: ${item['retail_price']:.2f})\n"
                f"   🏷️ {item['platform']}\n"
                f"   🔗 <a href='{item['url']}'>Ссылка</a>\n\n"
            )

        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


    @dp.message(F.text == "📈 Сравнение цен")
    async def compare_prices(message: Message, state: FSMContext):
        await message.answer("🔍 Введите название товара для сравнения:", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(SearchState.waiting_for_price_check)


    @dp.message(SearchState.waiting_for_price_check)
    async def process_price_comparison(message: Message, state: FSMContext):
        query = message.text
        await message.answer(f"🔄 Ищу '{query}' на всех площадках...\n⏱️ Может занять до 30 сек")

        async with MarketParser() as parser:
            all_products = (
                await parser.search_grailed(query)
                + await parser.search_ebay(query)
                + await parser.search_stockx(query)
                + await parser.search_depop(query)
                + await parser.search_mercari_jp(query)
            )

        if not all_products:
            await message.answer("❌ Ничего не найдено.", reply_markup=main_menu_keyboard())
            await state.clear()
            return

        best_by_platform: Dict[str, Product] = {}
        for product in all_products:
            current_best = best_by_platform.get(product.platform)
            if current_best is None or product.price < current_best.price:
                best_by_platform[product.platform] = product

        sorted_products = sorted(best_by_platform.values(), key=lambda p: p.price)

        text = f"📊 <b>Сравнение цен для '{query}'</b>\n\n"
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, product in enumerate(sorted_products, 1):
            text += f"{medals.get(i, f'{i}.')} <b>{product.platform}</b>\n"
            text += f"   💰 ${product.price:.2f}"
            if product.shipping_cost > 0:
                text += f" (+${product.shipping_cost:.2f} доставка)"
                text += f"\n   💵 Итого: ${product.price + product.shipping_cost:.2f}"
            text += f"\n   🔗 <a href='{product.url}'>Ссылка</a>\n\n"

        builder = InlineKeyboardBuilder()
        for product in sorted_products[:5]:
            builder.button(text=f"Открыть на {product.platform}", url=product.url)
        builder.button(text="🔄 Обновить", callback_data="compare_refresh")
        builder.button(text="🔙 Назад", callback_data="back_main")
        builder.adjust(1)

        await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await state.clear()


    @dp.message(F.text == "⚙️ Настройки")
    async def settings_menu(message: Message):
        settings = db.get_user_settings(message.from_user.id)
        text = (
            "⚙️ <b>Настройки</b>\n\n"
            f"📈 Минимальный профит: <b>{settings['min_profit']}%</b>\n"
            f"🏷️ Бренды: <b>{settings['preferred_brands'] or 'Не указаны'}</b>\n\n"
            "Что изменить?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📈 Изменить профит", callback_data="settings_profit")],
            [InlineKeyboardButton(text="🏷️ Изменить бренды", callback_data="settings_brands")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
        ])
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


    @dp.callback_query(F.data == "settings_profit")
    async def change_profit(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text(
            "📈 Введите минимальный процент профита (0-1000):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="settings_back")]
            ])
        )
        await state.set_state(SettingsState.changing_min_profit)


    @dp.message(SettingsState.changing_min_profit)
    async def process_min_profit(message: Message, state: FSMContext):
        try:
            min_profit = max(0, min(1000, int(message.text)))
        except ValueError:
            await message.answer("❌ Введите число")
            return

        db.update_user_settings(message.from_user.id, min_profit=min_profit)
        await message.answer(f"✅ Минимальный профит: <b>{min_profit}%</b>", parse_mode="HTML",
                            reply_markup=main_menu_keyboard())
        await state.clear()


    @dp.callback_query(F.data == "settings_brands")
    async def change_brands(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text(
            "🏷️ Введите бренды через запятую:\nНапример: Nike, Supreme, Adidas",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="settings_back")]
            ])
        )
        await state.set_state(SettingsState.changing_brands)


    @dp.message(SettingsState.changing_brands)
    async def process_brands(message: Message, state: FSMContext):
        db.update_user_settings(message.from_user.id, preferred_brands=message.text)
        await message.answer(f"✅ Бренды: <b>{message.text}</b>", parse_mode="HTML",
                            reply_markup=main_menu_keyboard())
        await state.clear()


    @dp.callback_query(F.data == "settings_back")
    async def settings_back(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await settings_menu(callback.message)


    @dp.callback_query(F.data == "back_main")
    async def back_to_main(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.delete()
        await callback.message.answer("👋 Главное меню", reply_markup=main_menu_keyboard())


    @dp.message(F.text == "❓ Помощь")
    async def help_command(message: Message):
        text = (
            "❓ <b>Помощь</b>\n\n"
            "🔍 <b>Поиск</b> - товары на всех площадках\n"
            "🇯🇵 <b>Mercari JP</b> - японский рынок\n"
            "💰 <b>Профит</b> = (ритейл - цена) / цена * 100%\n"
            "📊 <b>Отслеживание</b> - сохраняй товары, смотри историю\n"
            "⚙️ <b>Настройки</b> - минимальный профит\n\n"
            "💡 Для Mercari можно вводить японские названия"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


    # ==================== ЗАПУСК ====================
    async def main():
        logging.info("бот запущен")
        await browser_manager.start()  # один Chromium на все время работы, вкладки - под каждый поиск
        try:
            await dp.start_polling(bot, skip_updates=True)
        finally:
            await browser_manager.stop()
            db.close()


    if __name__ == "__main__":
        asyncio.run(main())
