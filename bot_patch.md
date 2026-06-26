# bot_patch.md — что добавить в bj_bot_fixed.py

Всего 5 изменений, ничего не трогаем в логике бота.

────────────────────────────────────────────────────────────────────
## 1. Добавить импорт WebAppInfo

Найди строку с импортами из aiogram.types (где KB, IKB и т.д.)
Добавь WebAppInfo в тот же блок:

    from aiogram.types import (
        ReplyKeyboardMarkup,
        KeyboardButton,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        WebAppInfo,            # ← добавить эту строку
    )

────────────────────────────────────────────────────────────────────
## 2. Добавить константу WEBAPP_URL

После строки с BOT_TOKEN (примерно строка 28) добавь:

    WEBAPP_URL = os.getenv("WEBAPP_URL", "https://СЮДА_ВСТАВЬ_NGROK_URL")

────────────────────────────────────────────────────────────────────
## 3. Добавить TX строки — в TX['en'] (после 'btn_no':)

    'btn_webapp': '🎮 Blackjack Online',

────────────────────────────────────────────────────────────────────
## 4. Добавить TX строки — в TX['ru'] (после 'btn_no':)

    'btn_webapp': '🎮 Блекджек Онлайн',

────────────────────────────────────────────────────────────────────
## 5. Изменить reply_kb (строка ~831) — добавить первую строку с кнопкой

    async def reply_kb(uid):
        """PM reply keyboard — NO play button (play is group-only)."""
        lang = await get_lang(uid)
        kb = [
            [KB(text=t(lang,"btn_webapp"), web_app=WebAppInfo(url=WEBAPP_URL))],  # ← новое
            [KB(text=t(lang,"btn_profile")), KB(text=t(lang,"btn_bonus"))],
            [KB(text=t(lang,"btn_shop")),    KB(text=t(lang,"btn_top"))],
            [KB(text=t(lang,"btn_ref")),     KB(text=t(lang,"btn_upgrade"))],
            [KB(text=t(lang,"btn_settings"))],
        ]
        return RKM(keyboard=kb, resize_keyboard=True, is_persistent=True)

════════════════════════════════════════════════════════════════════
## Как запустить

### Терминал 1 — бот (как обычно):
    python bj_bot_fixed.py

### Терминал 2 — сервер мини-аппки:
    export BOT_TOKEN="твой_токен"
    export DB_DSN="postgresql://localhost/bjbot"
    python webapp_server.py

### Терминал 3 — ngrok:
    ngrok http 8080

    Копируй Forwarding URL вида https://xxxx.ngrok-free.app
    Вставь в переменную окружения перед запуском сервера:
    export WEBAPP_URL="https://xxxx.ngrok-free.app"
    или в бот_патче строку 2 прямо в код.

### Порядок запуска:
    1. pg_ctl start  (если не запущен)
    2. python bj_bot_fixed.py
    3. python webapp_server.py
    4. ngrok http 8080
    5. Вставить ngrok URL → перезапустить webapp_server.py и бота

### Проверка:
    Открой ЛС с ботом → появится кнопка «🎮 Blackjack Online» / «🎮 Блекджек Онлайн»
    Нажми → откроется мини-аппка

════════════════════════════════════════════════════════════════════
## Зависимости (если не установлены)

    pip install aiohttp asyncpg --break-system-packages
