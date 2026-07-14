# searchbot.py - ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ
import asyncio
import logging
import sqlite3
import json
import re
import aiohttp
import brotli
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
import urllib.parse

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
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from playwright.async_api import async_playwright, Browser, BrowserContext
from playwright_stealth import Stealth
# ==================== УЛУЧШЕННЫЕ НАСТРОЙКИ ====================
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

EBAY_COOKIES = {
    'ebay': 'nonsense',
    'dp1': 'buc/XXXXXXX&bu/XXXXXXXXX',
    'nonsense': '1'
}
# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8987441799:AAHUx5qh44H1-iXz0zy0YWjFzOm_OU9Q0jc"  # ЗАМЕНИ НА СВОЙ ТОКЕН!

# --- eBay Browse API (официальный, OAuth) ---
# Получить на https://developer.ebay.com -> Application Keys -> Production keyset
EBAY_CLIENT_ID = "ТВОЙ_APP_ID_СЮДА"
EBAY_CLIENT_SECRET = "ТВОЙ_CERT_ID_SECRET_СЮДА"
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
# marketplace id влияет на валюту и регион выдачи, EBAY_US / EBAY_GB / EBAY_DE и т.д.
EBAY_MARKETPLACE_ID = "EBAY_US"

REQUEST_TIMEOUT = 25
MAX_RESULTS = 15
USE_PROXY = False
PROXY = "http://proxy:port"

# ==================== БАЗА ДАННЫХ ====================
class Database:
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
        updates = []
        values = []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                values.append(value)
        
        if updates:
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
        self.cursor.execute(
            "DELETE FROM price_history WHERE item_id = ?",
            (item_id,)
        )
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

# ==================== БРАУЗЕР (Playwright, общий на весь бот) ====================
class BrowserManager:
    """
    Держит ОДИН процесс Chromium на всё время жизни бота.
    Запускать новый браузер на каждый поиск - слишком дорого по CPU/времени
    (старт браузера занимает 1-3 секунды сам по себе).
    Каждый поиск открывает новую страницу (tab) в общем браузере и закрывает её.

    К каждому новому контексту применяется playwright-stealth - патчит
    самые дешёвые автоматизационные сигналы (navigator.webdriver, плагины,
    languages и т.п.), которые антибот-системы проверяют первым делом.
    ВАЖНО: это снижает шанс капчи, но не гарантирует проход - Cloudflare
    дополнительно смотрит на IP-репутацию (датацентр vs обычный провайдер)
    и TLS-фингерпринт, на которые stealth не влияет.
    """
    def __init__(self):
        self._playwright = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._stealth = Stealth()  # один экземпляр, переиспользуем для всех контекстов

    async def start(self):
        if self.browser:
            return
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",  # скрывает navigator.webdriver=true
                "--no-sandbox",
            ],
        )
        logging.info("🌐 Playwright Chromium запущен (со stealth-патчами)")

    async def new_context(self) -> BrowserContext:
        """Новый изолированный контекст (свои cookies) под одну сессию поиска,
        со stealth-патчами от playwright-stealth."""
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
        logging.info("🌐 Playwright Chromium остановлен")


browser_manager = BrowserManager()


async def human_pause(min_ms: int = 400, max_ms: int = 1100):
    """
    Небольшая случайная пауза + имитация поведения человека (scroll).
    Антибот-системы смотрят не только на фингерпринт браузера, но и на
    темп действий - идеально мгновенные действия без пауз сами по себе
    подозрительны. Это не решает Cloudflare само по себе, но дополняет
    stealth-патчи.
    """
    import random
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def human_scroll(page):
    """Лёгкая случайная прокрутка страницы, как делает живой человек перед чтением списка."""
    import random
    try:
        await page.mouse.wheel(0, random.randint(200, 600))
        await human_pause(200, 500)
    except Exception:
        pass

# ==================== КЛАССЫ ====================
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
        self.session = None
        self.ua = UserAgent()
        self.timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        # --- кэш OAuth токена eBay, чтобы не запрашивать новый на каждый поиск ---
        self._ebay_token = None
        self._ebay_token_expires_at = 0  # unix timestamp
    
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
            'Pragma': 'no-cache'
        }
        
        if headers:
            default_headers.update(headers)
        
        proxy = PROXY if USE_PROXY else None
        
        try:
            async with self.session.get(url, headers=default_headers, params=params,
                                      allow_redirects=True, proxy=proxy) as response:
                if response.status == 200:
                    content_encoding = response.headers.get('Content-Encoding', '')
                    
                    if 'br' in content_encoding.lower():
                        try:
                            compressed_data = await response.read()
                            decoded_data = brotli.decompress(compressed_data)
                            return decoded_data.decode('utf-8')
                        except Exception as e:
                            logging.error(f"Brotli decode error: {e}")
                            return await response.text()
                    else:
                        return await response.text()
                else:
                    logging.warning(f"HTTP {response.status} for {url}")
                    return ""
        except Exception as e:
            logging.error(f"Fetch error for {url}: {e}")
            return ""
    
    # ============ MERCARI JP (через mercapi - внутренний API) ============
    async def search_mercari_jp(self, query: str) -> List[Product]:
        """
        Использует библиотеку mercapi (https://github.com/take-kun/mercapi),
        которая обращается прямо к api.mercari.jp с криптографической
        DPoP-подписью запроса (как делает нативный веб-клиент Mercari),
        а не парсит HTML страницу - поэтому Cloudflare-капча с веб-сайта
        тут не встречается, и поиск быстрый (без браузера).

        Курс йены к доллару захардкожен (1 USD = 150 JPY) - для точности
        раз в день/неделю стоит подменять на реальный курс через
        бесплатный currency API, иначе профит будет чуть неточным при
        колебаниях курса.
        """
        products = []
        try:
            from mercapi import Mercapi
        except ImportError:
            logging.error("Mercari JP: библиотека 'mercapi' не установлена (pip install mercapi)")
            return products

        try:
            m = Mercapi()
            results = await m.search(query)

            for item in results.items[:MAX_RESULTS]:
                try:
                    if item.is_no_price:
                        continue

                    price_jpy = float(item.price)
                    name = item.name
                    link = f"https://jp.mercari.com/item/{item.id_}"
                    image_url = item.thumbnails[0] if item.thumbnails else ""

                    price_usd = round(price_jpy / 150, 2)  # приблизительный курс, см. docstring выше
                    shipping = 5.0 if price_jpy < 5000 else 0.0

                    products.append(Product(
                        name=name[:100],
                        price=price_usd,
                        url=link,
                        platform="Mercari JP",
                        image_url=image_url,
                        shipping_cost=shipping,
                        currency="JPY",
                    ))
                except Exception as e:
                    logging.error(f"Mercari JP item parse error: {e}")
                    continue

        except Exception as e:
            logging.error(f"Mercari JP (mercapi) error: {e}")

        return products
    # ============ GRAILED (Playwright) ============
    async def search_grailed(self, query: str) -> List[Product]:
        """
        Grailed - это React SPA, товары рендерятся через JS.
        Простой HTTP-запрос не увидит карточек товаров - поэтому открываем
        страницу настоящим (headless) браузером и ждём, пока React отрисует список.

        ВНИМАНИЕ: селекторы '[data-testid="listing-card"]' и др. подобраны по
        известной структуре Grailed на момент написания. Если структура сайта
        изменилась, нужно открыть страницу руками (или через
        page.screenshot()/page.content()) и поправить селекторы под актуальный DOM.
        """
        products = []
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.grailed.com/search?q={encoded_query}"

        context = await browser_manager.new_context()
        try:
            page = await context.new_page()
            await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
            await human_pause()
            await human_scroll(page)

            # ждём появления хотя бы одной карточки товара (или таймаут)
            try:
                await page.wait_for_selector(
                    'a[href*="/listings/"]', timeout=10000
                )
            except Exception:
                logging.warning("Grailed: карточки товаров не появились за 10с (капча/блок/изменилась структура)")

            cards = await page.query_selector_all('a[href*="/listings/"]')

            for card in cards[:MAX_RESULTS]:
                try:
                    href = await card.get_attribute('href')
                    if not href:
                        continue
                    link = href if href.startswith('http') else f"https://www.grailed.com{href}"

                    # Title и price обычно лежат внутри карточки в дочерних элементах.
                    # Берём весь текст карточки и достаём цену регуляркой - надёжнее
                    # чем привязываться к конкретному CSS-классу, который может смениться.
                    full_text = (await card.inner_text()) or ""
                    if not full_text.strip():
                        continue

                    price_match = re.search(r'\$\s?([\d,]+(?:\.\d{2})?)', full_text)
                    if not price_match:
                        continue
                    price = float(price_match.group(1).replace(',', ''))

                    # Название - первая непустая строка текста карточки, не являющаяся ценой
                    name = ""
                    for line in full_text.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('$'):
                            name = line
                            break
                    if not name:
                        continue

                    # Картинка - ищем img внутри карточки
                    image_url = ""
                    img_elem = await card.query_selector('img')
                    if img_elem:
                        image_url = (await img_elem.get_attribute('src')) or \
                                    (await img_elem.get_attribute('data-src')) or ""

                    products.append(Product(
                        name=name[:100],
                        price=price,
                        url=link,
                        platform="Grailed",
                        image_url=image_url,
                    ))
                except Exception as e:
                    logging.error(f"Grailed card parse error: {e}")
                    continue

            await page.close()
        except Exception as e:
            logging.error(f"Grailed Playwright error: {e}")
        finally:
            await context.close()

        return products
    
    # ============ EBAY OAUTH ============
    async def _get_ebay_token(self) -> Optional[str]:
        """
        Получает (и кэширует) OAuth Application Token для eBay Browse API.
        Application token живёт ~2 часа, поэтому кэшируем с запасом в 60 секунд.
        Документация: https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html
        """
        import time
        import base64

        now = time.time()
        if self._ebay_token and now < self._ebay_token_expires_at - 60:
            return self._ebay_token

        if EBAY_CLIENT_ID.startswith("ТВОЙ_") or EBAY_CLIENT_SECRET.startswith("ТВОЙ_"):
            logging.error(
                "eBay API ключи не настроены! Заполни EBAY_CLIENT_ID и "
                "EBAY_CLIENT_SECRET в конфигурации (получить на developer.ebay.com)"
            )
            return None

        credentials = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded_credentials}",
        }
        data = {
            "grant_type": "client_credentials",
            # scope для публичного поиска товаров (read-only)
            "scope": "https://api.ebay.com/oauth/api_scope",
        }

        try:
            async with self.session.post(EBAY_OAUTH_URL, headers=headers, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.error(f"eBay OAuth ошибка {resp.status}: {body[:300]}")
                    return None
                result = await resp.json()
                self._ebay_token = result["access_token"]
                # eBay возвращает expires_in в секундах (обычно 7200 = 2 часа)
                self._ebay_token_expires_at = now + int(result.get("expires_in", 7200))
                return self._ebay_token
        except Exception as e:
            logging.error(f"eBay OAuth запрос не удался: {e}")
            return None

    # ============ EBAY (Browse API) ============
    async def search_ebay(self, query: str) -> List[Product]:
        """
        Официальный eBay Browse API вместо парсинга HTML.
        Не блокируется антибот-защитой, отдаёт название/цену/ссылку/картинку
        напрямую в JSON. Нужны ключи EBAY_CLIENT_ID/EBAY_CLIENT_SECRET.
        """
        products = []

        token = await self._get_ebay_token()
        if not token:
            return products

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
            "Content-Type": "application/json",
        }
        params = {
            "q": query,
            "limit": str(MAX_RESULTS),
            "sort": "price",  # сортировка по возрастанию цены, удобно для поиска выгодных лотов
        }

        try:
            async with self.session.get(
                EBAY_BROWSE_SEARCH_URL, headers=headers, params=params
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.error(f"eBay Browse API ошибка {resp.status}: {body[:300]}")
                    return products

                data = await resp.json()
                items = data.get("itemSummaries", [])

                for item in items:
                    try:
                        name = item.get("title", "")
                        price_info = item.get("price", {})
                        price = float(price_info.get("value", 0))
                        currency = price_info.get("currency", "USD")
                        link = item.get("itemWebUrl", "")

                        # картинка - основная, плюс есть additionalImages если нужно больше
                        image_url = item.get("image", {}).get("imageUrl", "")

                        # доставка (берём первую опцию, если есть)
                        shipping_cost = 0.0
                        shipping_options = item.get("shippingOptions", [])
                        if shipping_options:
                            ship_cost_info = shipping_options[0].get("shippingCost", {})
                            shipping_cost = float(ship_cost_info.get("value", 0))

                        condition = item.get("condition", "")

                        if name and price > 0 and link:
                            products.append(Product(
                                name=name[:100],
                                price=price,
                                url=link,
                                platform="eBay",
                                image_url=image_url,
                                shipping_cost=shipping_cost,
                                currency=currency,
                                condition=condition,
                            ))
                    except Exception as e:
                        logging.error(f"eBay item parse error: {e}")
                        continue

        except Exception as e:
            logging.error(f"eBay Browse API запрос не удался: {e}")

        return products
    
    # ============ STOCKX (Playwright) ============
    async def search_stockx(self, query: str) -> List[Product]:
        """
        StockX закрыл старый паттерн window.__INITIAL_STATE__ и стоит за
        жёстким Cloudflare. Headless-браузер обходит JS-проверку, но
        Cloudflare иногда всё равно показывает капчу датацентровским IP -
        в этом случае products будет пустым, и это не баг кода.
        """
        products = []
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://stockx.com/search?s={encoded_query}"

        context = await browser_manager.new_context()
        try:
            page = await context.new_page()
            await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
            await human_pause()
            await human_scroll(page)

            # StockX рендерит карточки товаров с ссылками на /<slug>
            try:
                await page.wait_for_selector('a[href^="/"][data-testid]', timeout=10000)
            except Exception:
                # фоллбэк-селектор, если data-testid сменился
                try:
                    await page.wait_for_selector('div[class*="product"] a', timeout=5000)
                except Exception:
                    logging.warning("StockX: товары не появились (вероятно капча Cloudflare)")

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

                    name = ""
                    for line in full_text.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('$') and 'Last Sale' not in line:
                            name = line
                            break
                    if not name:
                        continue

                    image_url = ""
                    img_elem = await card.query_selector('img')
                    if img_elem:
                        image_url = (await img_elem.get_attribute('src')) or ""

                    products.append(Product(
                        name=name[:100],
                        price=price,
                        url=link,
                        platform="StockX",
                        image_url=image_url,
                    ))
                except Exception as e:
                    logging.error(f"StockX card parse error: {e}")
                    continue

            await page.close()
        except Exception as e:
            logging.error(f"StockX Playwright error: {e}")
        finally:
            await context.close()

        return products
    
    # ============ DEPOP (Playwright) ============
    async def search_depop(self, query: str) -> List[Product]:
        """
        Depop тоже SPA за Cloudflare. Структура карточек у Depop менялась
        несколько раз за последние годы - если селекторы не находят карточек,
        открой search_url в обычном Chrome, через DevTools посмотри реальный
        класс/data-атрибут карточки товара и обнови селектор ниже.
        """
        products = []
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.depop.com/search/?q={encoded_query}"

        context = await browser_manager.new_context()
        try:
            page = await context.new_page()
            await page.goto(search_url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
            await human_pause()
            await human_scroll(page)

            try:
                await page.wait_for_selector('a[href*="/products/"]', timeout=10000)
            except Exception:
                logging.warning("Depop: товары не появились (вероятно капча Cloudflare/изменилась структура)")

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

                    name = ""
                    for line in full_text.split('\n'):
                        line = line.strip()
                        if line and not re.match(r'^[\$£€]', line):
                            name = line
                            break
                    if not name:
                        name = "Depop item"  # Depop часто не даёт текстовый title в карточке

                    image_url = ""
                    img_elem = await card.query_selector('img')
                    if img_elem:
                        image_url = (await img_elem.get_attribute('src')) or \
                                    (await img_elem.get_attribute('data-src')) or ""

                    products.append(Product(
                        name=name[:100],
                        price=price,
                        url=link,
                        platform="Depop",
                        image_url=image_url,
                    ))
                except Exception as e:
                    logging.error(f"Depop card parse error: {e}")
                    continue

            await page.close()
        except Exception as e:
            logging.error(f"Depop Playwright error: {e}")
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
        [KeyboardButton(text="❓ Помощь")]
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
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ==================== БОТ ====================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    db.add_user(user_id, username)
    
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот для поиска выгодных вещей для реселла.\n\n"
        "🔹 Ищу на Grailed, eBay, StockX, Depop, Mercari JP\n"
        "🔹 Сравниваю цены и считаю профит\n"
        "🔹 Mercari JP - японский рынок с уникальными вещами!\n\n"
        "Используй кнопки ниже:",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "🇯🇵 Mercari JP")
async def mercari_search(message: Message, state: FSMContext):
    await message.answer(
        "🇯🇵 <b>Mercari Japan</b>\n\n"
        "💰 Цены в йенах (JPY), конвертируются в USD\n"
        "📦 Учтите доставку из Японии (~$5-15)\n"
        "✨ Часто можно найти редкие вещи по отличным ценам\n\n"
        "Введите название товара:",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(SearchState.waiting_for_query)
    await state.update_data(force_platform="mercari_jp")

@dp.message(F.text == "🔍 Поиск товара")
async def search_item(message: Message, state: FSMContext):
    await message.answer(
        "Введите название товара:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(SearchState.waiting_for_query)

@dp.message(SearchState.waiting_for_query)
async def process_search_query(message: Message, state: FSMContext):
    query = message.text
    await state.update_data(query=query)
    
    data = await state.get_data()
    force_platform = data.get('force_platform')
    
    if force_platform:
        await search_on_platform(message, state, force_platform, query)
    else:
        await message.answer(
            "Выберите площадку:",
            reply_markup=platform_keyboard()
        )

@dp.callback_query(F.data.startswith("platform_"))
async def select_platform(callback: CallbackQuery, state: FSMContext):
    platform = callback.data.replace("platform_", "")
    data = await state.get_data()
    query = data.get('query', '')
    
    await search_on_platform(callback.message, state, platform, query)

async def search_on_platform(message: Message, state: FSMContext, platform: str, query: str):
    platform_names = {
        "grailed": "Grailed",
        "ebay": "eBay", 
        "stockx": "StockX",
        "depop": "Depop",
        "mercari_jp": "Mercari JP",
        "all": "всех площадках"
    }
    
    platform_display = platform_names.get(platform, platform)
    
    await message.edit_text(
        f"🔍 Ищу '{query}' на {platform_display}...\n⏱️ Подождите немного"
    )
    
    products = []
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
            grailed = await parser.search_grailed(query)
            ebay = await parser.search_ebay(query)
            stockx = await parser.search_stockx(query)
            depop = await parser.search_depop(query)
            mercari = await parser.search_mercari_jp(query)
            products = grailed + ebay + stockx + depop + mercari
    
    if not products:
        await message.edit_text(
            "❌ Ничего не найдено. Попробуйте другой запрос.\n\n"
            "💡 Советы:\n"
            "• Используйте английские названия\n"
            "• Для Mercari попробуйте японские названия\n"
            "• Проверьте правильность написания",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")
                ]]
            )
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
    
    text = (
        f"🛍️ <b>{product.name}</b>\n\n"
        f"💰 Цена: <b>${product.price:.2f}</b>"
    )
    
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
    keyboard = []
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"prev_{page}"))
    nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="none"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"next_{page}"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="✅ Отслеживать", callback_data=f"track_{page}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    # Если у товара есть картинка - удаляем старое сообщение и шлём фото с подписью,
    # т.к. Telegram не даёт превратить текстовое сообщение в фото через edit_text.
    if product.image_url:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer_photo(
            photo=product.image_url,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    else:
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            # на случай если предыдущее сообщение было фото-сообщением (edit_text не сработает)
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
    
    if callback.data.startswith("next_"):
        page = current_page + 1
    else:
        page = current_page - 1
    
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
        f"(в долларах США, например: 150)\n\n"
        f"<i>Текущая цена: ${product.price:.2f}</i>"
    )
    prompt_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_main")]
    ])

    # Карточка товара могла быть photo-сообщением (если у товара есть картинка),
    # а edit_text на фото-сообщении вызовет ошибку Telegram API - поэтому
    # удаляем и отправляем новое текстовое сообщение вместо правки на месте.
    try:
        await callback.message.edit_text(
            prompt_text, parse_mode="HTML", reply_markup=prompt_keyboard
        )
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            prompt_text, parse_mode="HTML", reply_markup=prompt_keyboard
        )

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
        await message.answer("❌ Ошибка, попробуйте начать заново")
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
        'shipping_cost': product.shipping_cost
    }
    
    user_id = message.from_user.id
    db.add_tracked_item(user_id, item_data)
    
    settings = db.get_user_settings(user_id)
    min_profit = settings['min_profit']
    
    text = (
        f"✅ Товар добавлен!\n\n"
        f"🛍️ <b>{product.name}</b>\n"
        f"💰 Розница: ${retail_price:.2f}\n"
        f"💵 Цена: ${product.price:.2f}"
    )
    
    if product.shipping_cost > 0:
        text += f"\n📦 Доставка: ${product.shipping_cost:.2f}"
        text += f"\n💵 Итого: ${product.price + product.shipping_cost:.2f}"
    
    text += f"\n📈 Профит: <b>{profit}%</b>\n"
    text += f"🏷️ {product.platform}\n"
    text += f"🔗 <a href='{product.url}'>Ссылка</a>\n\n"
    
    if profit >= min_profit:
        text += f"✅ <b>Проходит по фильтру ({min_profit}%)!</b>"
    else:
        text += f"⚠️ Не проходит по фильтру ({min_profit}%)"
    
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    await state.clear()

@dp.message(F.text == "📊 Мои отслеживаемые")
async def show_tracked(message: Message):
    user_id = message.from_user.id
    items = db.get_tracked_items(user_id)
    
    if not items:
        await message.answer("📭 Нет отслеживаемых товаров.", reply_markup=main_menu_keyboard())
        return
    
    text = "📊 <b>Отслеживаемые товары:</b>\n\n"
    for i, item in enumerate(items[:10], 1):
        profit_emoji = "🟢" if item['profit_percent'] > 0 else "🔴"
        text += (
            f"{i}. {profit_emoji} <b>{item['name'][:40]}...</b>\n"
            f"   💰 ${item['current_price']:.2f} | 📈 {item['profit_percent']}%\n"
            f"   🏷️ {item['platform']}\n"
            f"   🔗 <a href='{item['url']}'>Ссылка</a>\n\n"
        )
    
    if len(items) > 10:
        text += f"<i>...и еще {len(items)-10} товаров</i>"
    
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

@dp.message(F.text == "💰 Топ по профиту")
async def show_top_profit(message: Message):
    user_id = message.from_user.id
    settings = db.get_user_settings(user_id)
    min_profit = settings['min_profit']
    
    items = db.get_items_by_profit(user_id, min_profit)
    
    if not items:
        await message.answer(
            f"❌ Нет товаров с профитом >= {min_profit}%",
            reply_markup=main_menu_keyboard()
        )
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
    await message.answer(
        "🔍 Введите название товара для сравнения:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(SearchState.waiting_for_price_check)

@dp.message(SearchState.waiting_for_price_check)
async def process_price_comparison(message: Message, state: FSMContext):
    query = message.text
    
    await message.answer(f"🔄 Ищу '{query}' на всех площадках...\n⏱️ Подождите до 30 секунд")
    
    all_products = []
    async with MarketParser() as parser:
        grailed = await parser.search_grailed(query)
        ebay = await parser.search_ebay(query)
        stockx = await parser.search_stockx(query)
        depop = await parser.search_depop(query)
        mercari = await parser.search_mercari_jp(query)
        all_products = grailed + ebay + stockx + depop + mercari
    
    if not all_products:
        await message.answer("❌ Ничего не найдено.", reply_markup=main_menu_keyboard())
        await state.clear()
        return
    
    best_by_platform = {}
    for product in all_products:
        if product.platform not in best_by_platform or product.price < best_by_platform[product.platform].price:
            best_by_platform[product.platform] = product
    
    sorted_products = sorted(best_by_platform.values(), key=lambda x: x.price)
    
    text = f"📊 <b>Сравнение цен для '{query}'</b>\n\n"
    
    for i, product in enumerate(sorted_products, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        
        text += f"{medal} <b>{product.platform}</b>\n"
        text += f"   💰 ${product.price:.2f}"
        
        if product.shipping_cost > 0:
            text += f" (+${product.shipping_cost:.2f} доставка)"
            text += f"\n   💵 Итого: ${product.price + product.shipping_cost:.2f}"
        
        text += f"\n   🔗 <a href='{product.url}'>Ссылка</a>\n\n"
    
    builder = InlineKeyboardBuilder()
    for product in sorted_products[:5]:
        builder.button(
            text=f"Открыть на {product.platform}",
            url=product.url
        )
    builder.button(text="🔄 Обновить", callback_data="compare_refresh")
    builder.button(text="🔙 Назад", callback_data="back_main")
    builder.adjust(1)
    
    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await state.clear()

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message):
    user_id = message.from_user.id
    settings = db.get_user_settings(user_id)
    
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"📈 Минимальный профит: <b>{settings['min_profit']}%</b>\n"
        f"🏷️ Бренды: <b>{settings['preferred_brands'] or 'Не указаны'}</b>\n\n"
        "Выберите что изменить:"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Изменить профит", callback_data="settings_profit")],
        [InlineKeyboardButton(text="🏷️ Изменить бренды", callback_data="settings_brands")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data == "settings_profit")
async def change_profit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📈 Введите минимальный процент профита (от 10 до 1000):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="settings_back")]
        ])
    )
    await state.set_state(SettingsState.changing_min_profit)

@dp.message(SettingsState.changing_min_profit)
async def process_min_profit(message: Message, state: FSMContext):
    try:
        min_profit = int(message.text)
        if min_profit < 0:
            min_profit = 0
        if min_profit > 1000:
            min_profit = 1000
    except ValueError:
        await message.answer("❌ Введите число")
        return
    
    user_id = message.from_user.id
    db.update_user_settings(user_id, min_profit=min_profit)
    
    await message.answer(
        f"✅ Минимальный профит: <b>{min_profit}%</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )
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
    brands = message.text
    user_id = message.from_user.id
    db.update_user_settings(user_id, preferred_brands=brands)
    
    await message.answer(
        f"✅ Бренды: <b>{brands}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await settings_menu(callback.message)

@dp.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "👋 Главное меню",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "❓ Помощь")
async def help_command(message: Message):
    text = (
        "❓ <b>Помощь</b>\n\n"
        "🔍 <b>Поиск</b> - найди товары на всех площадках\n"
        "🇯🇵 <b>Mercari JP</b> - японский рынок\n"
        "💰 <b>Профит</b> = (ритейл - цена) / цена * 100%\n"
        "📊 <b>Отслеживание</b> - сохраняй товары и смотри историю\n"
        "⚙️ <b>Настройки</b> - измени минимальный профит\n\n"
        "💡 Для Mercari используй английские или японские названия"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

# ==================== ЗАПУСК ====================
async def main():
    logging.info("🚀 Бот запущен!")
    # Запускаем ОДИН Chromium-процесс на всё время работы бота.
    # Каждый поиск Grailed/StockX/Depop/Mercari открывает свою вкладку
    # в этом же браузере, а не создаёт новый процесс - это сильно быстрее.
    await browser_manager.start()
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await browser_manager.stop()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())