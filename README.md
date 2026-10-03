# GDDL Telegram bot

Инлайн-бот: `@gddemonladder Cataclysm` → список уровней → карточка с местом,
enjoyment, ID, tier и ссылкой на GDDL.

## 1. Настройка в BotFather
1. `/newbot` (или открой существующего) → получи токен.
2. `/setinline` → выбери бота → напиши подсказку, например `Название демона`.
3. (по желанию) `/setinlinefeedback` не нужен.

## 2. Проверка локально (необязательно)
```
pip install -r requirements.txt
BOT_TOKEN=123:abc python bot.py
```

## 3. Запуск без твоего компьютера
Боту нужен любой сервер, который работает постоянно. Подходят Railway, Fly.io,
Koyeb, Render (Background Worker), любой VPS. Везде одно и то же:
- загрузить эту папку (через GitHub или Dockerfile),
- команда запуска: `python bot.py`,
- переменная окружения `BOT_TOKEN` = токен из BotFather.

Бот работает через long polling, поэтому домен и открытые порты не нужны.

## Если GDDL отвечает не так, как ожидалось
Всё настраивается переменными окружения, код менять не нужно:
- `GDDL_API_BASE` (по умолчанию `https://gdladder.com/api`)
- `GDDL_SEARCH_PATH` (по умолчанию `/level/search`)
- `GDDL_SEARCH_PARAM` (по умолчанию `name`)
- `GDDL_LEVEL_URL` (по умолчанию `https://gdladder.com/level/{id}`)

Реальный адрес запроса видно в DevTools браузера (вкладка Network), когда
пользуешься поиском на gdladder.com. Названия полей в ответе читаются
с запасом (`ID/id`, `Rating/Tier`, `Enjoyment`, `Rank/Position` и т.д.);
если поля нет, строка в карточке просто пропускается.
