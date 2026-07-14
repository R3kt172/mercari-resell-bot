# Mercari JP Resell Bot 🤖

<div align="center">

**Telegram-бот для поиска выгодных товаров на японской площадке Mercari**

[![Python](https://img.shields.io/badge/Python-3.8+-blue?style=flat-square&logo=python)](https://www.python.org/downloads/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=flat-square&logo=telegram)](https://core.telegram.org/bots)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Mercari](https://img.shields.io/badge/Mercari-API-red?style=flat-square)](https://www.mercari.com/jp/)

</div>

---

## 📌 О проекте

Бот помогает реселлерам находить выгодные предложения на **Mercari Japan** — крупнейшей в Японии площадке для продажи товаров с рук. 

Он напрямую обращается к API Mercari, автоматически конвертирует цены из йен в доллары, рассчитывает потенциальную прибыль и позволяет отслеживать интересующие позиции.

---

## ✨ Возможности

| Функция | Описание |
|---------|----------|
| 🔍 **Поиск товаров** | По ключевым словам на английском или японском языке |
| 💰 **Расчёт прибыли** | Автоматический расчёт при указании розничной цены |
| 📊 **Отслеживание цен** | Сохранение истории изменения цены на выбранные товары |
| 🏆 **Топ по доходности** | Рейтинг товаров по уровню потенциальной прибыли |
| 🎯 **Фильтр по марже** | Ручная настройка минимального процента профита |
| 🖼️ **Изображения** | Фото товаров подгружаются прямо в чат |

---

## 🚀 Быстрый старт

### Требования
- Python 3.8 или выше
- Токен Telegram-бота (получить у [@BotFather](https://t.me/BotFather))

### Установка

```bash
# Клонируем репозиторий
git clone https://github.com/yourusername/mercari-resell-bot.git
cd mercari-resell-bot

# Создаём виртуальное окружение
python -m venv venv

# Активируем его
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# Устанавливаем зависимости
pip install -r requirements.txt
