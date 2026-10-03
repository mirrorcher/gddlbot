"""
Inline-бот для Telegram: поиск демонов на GDDL (https://gdladder.com).

Использование в любом чате:  @gddemonladder Cataclysm
Бот показывает список подходящих уровней; после выбора отправляется карточка
с местом, enjoyment, ID, сложностью (tier) и ссылкой на уровень в GDDL.
"""

import html
import logging
import os
import time

import httpx
from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("gddl-bot")

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Если API у GDDL окажется другим, меняется только здесь (через переменные окружения).
GDDL_SITE = os.getenv("GDDL_SITE", "https://gdladder.com")
GDDL_API_BASE = os.getenv("GDDL_API_BASE", f"{GDDL_SITE}/api")
SEARCH_PATH = os.getenv("GDDL_SEARCH_PATH", "/level/search")
SEARCH_PARAM = os.getenv("GDDL_SEARCH_PARAM", "name")
LEVEL_URL_TEMPLATE = os.getenv("GDDL_LEVEL_URL", f"{GDDL_SITE}/level/{{id}}")

MAX_RESULTS = 10          # Telegram показывает в инлайне до 50, но 10 удобнее
MIN_QUERY_LEN = 2         # чтобы не слать запросы на каждую первую букву
CACHE_TTL = 300           # секунд

_cache: dict[str, tuple[float, list[dict]]] = {}


# ------------------------------------------------------------- работа с GDDL
def _first(d: dict, *keys):
    """Первое непустое значение из словаря по списку возможных ключей."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _fmt_num(v) -> str | None:
    if v is None:
        return None
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def normalize(raw: dict) -> dict | None:
    """Приводит уровень из ответа сайта к единому виду."""
    meta = raw.get("Meta") or raw.get("meta") or {}
    level_id = _first(raw, "ID", "id", "LevelID", "levelId") or _first(meta, "ID", "id")
    name = _first(meta, "Name", "name") or _first(raw, "Name", "name")
    if level_id is None or not name:
        return None
    return {
        "id": level_id,
        "name": str(name),
        "tier": _fmt_num(_first(raw, "Rating", "rating", "Tier", "tier")),
        "enjoyment": _fmt_num(_first(raw, "Enjoyment", "enjoyment")),
        "rank": _first(raw, "Rank", "rank", "Position", "position", "Place"),
        "difficulty": _first(meta, "Difficulty", "difficulty")
        or _first(raw, "Difficulty", "difficulty"),
        "url": LEVEL_URL_TEMPLATE.format(id=level_id),
    }


async def search_levels(client: httpx.AsyncClient, query: str) -> list[dict]:
    key = query.casefold()
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    resp = await client.get(
        GDDL_API_BASE + SEARCH_PATH,
        params={SEARCH_PARAM: query, "limit": MAX_RESULTS},
    )
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict):
        items = _first(data, "levels", "Levels", "results", "data", "items") or []
    else:
        items = data

    levels = []
    for raw in items:
        if isinstance(raw, dict):
            lvl = normalize(raw)
            if lvl:
                levels.append(lvl)
    levels = levels[:MAX_RESULTS]

    _cache[key] = (now, levels)
    if len(_cache) > 500:  # простая защита от разрастания кэша
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return levels


# ------------------------------------------------------------ оформление
def card_text(lvl: dict) -> str:
    lines = [f"<b>{html.escape(lvl['name'])}</b>"]
    if lvl["rank"] is not None:
        lines.append(f"🏆 Место в топе: <b>#{html.escape(str(lvl['rank']))}</b>")
    if lvl["tier"]:
        lines.append(f"📊 Сложность (tier): <b>{html.escape(lvl['tier'])}</b>")
    if lvl["difficulty"]:
        lines.append(f"😈 Демон: {html.escape(str(lvl['difficulty']))}")
    if lvl["enjoyment"]:
        lines.append(f"🎉 Enjoyment: <b>{html.escape(lvl['enjoyment'])}</b>")
    lines.append(f"🆔 ID: <code>{html.escape(str(lvl['id']))}</code>")
    lines.append(f'🔗 <a href="{html.escape(lvl["url"], quote=True)}">Открыть в GDDL</a>')
    return "\n".join(lines)


def short_description(lvl: dict) -> str:
    parts = []
    if lvl["rank"] is not None:
        parts.append(f"#{lvl['rank']}")
    if lvl["tier"]:
        parts.append(f"Tier {lvl['tier']}")
    if lvl["enjoyment"]:
        parts.append(f"Enj {lvl['enjoyment']}")
    parts.append(f"ID {lvl['id']}")
    return " · ".join(parts)


# -------------------------------------------------------------- хендлеры
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    query = (iq.query or "").strip()

    if len(query) < MIN_QUERY_LEN:
        await iq.answer([], cache_time=1)
        return

    client: httpx.AsyncClient = context.application.bot_data["http"]
    try:
        levels = await search_levels(client, query)
    except Exception:
        log.exception("GDDL search failed for %r", query)
        await iq.answer(
            [
                InlineQueryResultArticle(
                    id="error",
                    title="GDDL сейчас недоступен",
                    description="Попробуй ещё раз через минуту",
                    input_message_content=InputTextMessageContent(
                        "Не удалось получить данные с GDDL, попробуй позже."
                    ),
                )
            ],
            cache_time=5,
        )
        return

    if not levels:
        await iq.answer(
            [
                InlineQueryResultArticle(
                    id="empty",
                    title="Ничего не найдено",
                    description=f"По запросу «{query}» уровней нет",
                    input_message_content=InputTextMessageContent(
                        f"На GDDL не нашлось уровней по запросу «{query}»."
                    ),
                )
            ],
            cache_time=30,
        )
        return

    results = [
        InlineQueryResultArticle(
            id=str(lvl["id"]),
            title=lvl["name"],
            description=short_description(lvl),
            input_message_content=InputTextMessageContent(
                card_text(lvl),
                parse_mode=ParseMode.HTML,
                link_preview_options=None,
            ),
        )
        for lvl in levels
    ]
    await iq.answer(results, cache_time=60, is_personal=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    me = await context.bot.get_me()
    await update.message.reply_text(
        "Я ищу демонов на GDDL.\n\n"
        f"В любом чате напиши @{me.username} и название уровня, например:\n"
        f"@{me.username} Cataclysm\n\n"
        "Выбери нужный уровень из списка — отправлю место, enjoyment, ID, "
        "сложность и ссылку."
    )


async def on_startup(app: Application) -> None:
    app.bot_data["http"] = httpx.AsyncClient(
        timeout=10.0,
        headers={"User-Agent": "gddl-telegram-bot/1.0"},
        follow_redirects=True,
    )


async def on_shutdown(app: Application) -> None:
    await app.bot_data["http"].aclose()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(InlineQueryHandler(inline_query))
    log.info("Bot started")
    app.run_polling(allowed_updates=[Update.INLINE_QUERY, Update.MESSAGE])


if __name__ == "__main__":
    main()
