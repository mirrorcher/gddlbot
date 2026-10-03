"""
Inline-бот для Telegram: поиск демонов на GDDL (https://gdladder.com).

Использование в любом чате:  @gdladderbot Cataclysm
Бот показывает список подходящих уровней; после выбора отправляется карточка
с местом, сложностью (tier), enjoyment, ID, типом демона и ссылкой на GDDL.
Под карточкой кнопки «Шоукейс» и «Музыка».
"""

import asyncio
import html
import logging
import os
import re
import time
from urllib.parse import quote_plus

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
DETAIL_PATH = os.getenv("GDDL_DETAIL_PATH", "/level/{id}")  # для докачки шоукейса/музыки
LEVEL_URL_TEMPLATE = os.getenv("GDDL_LEVEL_URL", f"{GDDL_SITE}/level/{{id}}")

MAX_RESULTS = 10
MIN_QUERY_LEN = 2
CACHE_TTL = 300  # секунд

_cache: dict[str, tuple[float, list[dict]]] = {}
_detail_cache: dict[str, tuple[float, dict]] = {}

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ------------------------------------------------------------- работа с GDDL
def _first(d: dict, *keys):
    """Первое непустое значение из словаря по списку возможных ключей."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] not in (None, "", 0):
            return d[k]
    return None


def _fmt_num(v) -> str | None:
    if v is None:
        return None
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _demon_label(v) -> str | None:
    """Приводит сложность демона к виду «Extreme Demon»."""
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    if "demon" not in v.casefold():
        v += " Demon"
    return v


def _showcase_url(v) -> str | None:
    if not v:
        return None
    v = str(v).strip()
    if v.startswith("http"):
        return v
    if YT_ID_RE.match(v):
        return f"https://www.youtube.com/watch?v={v}"
    return None


def _music_url(raw: dict, meta: dict) -> tuple[str | None, str | None]:
    """Возвращает (ссылка, название песни)."""
    song = _first(raw, "Song", "song") or _first(meta, "Song", "song")
    song_name = None
    song_id = None
    if isinstance(song, dict):
        song_name = _first(song, "Name", "name", "Title", "title")
        song_id = _first(song, "ID", "id", "SongID")
    elif isinstance(song, str):
        song_name = song
    song_id = song_id or _first(raw, "SongID", "songId") or _first(meta, "SongID", "songId")

    url = _first(raw, "SongURL", "songUrl") or _first(meta, "SongURL", "songUrl")
    if not url and song_id:
        try:
            if int(song_id) > 0:
                url = f"https://www.newgrounds.com/audio/listen/{int(song_id)}"
        except (TypeError, ValueError):
            pass
    return url, (str(song_name) if song_name else None)


def normalize(raw: dict) -> dict | None:
    """Приводит уровень из ответа сайта к единому виду."""
    meta = raw.get("Meta") or raw.get("meta") or {}
    level_id = _first(raw, "ID", "id", "LevelID", "levelId") or _first(meta, "ID", "id")
    name = _first(meta, "Name", "name") or _first(raw, "Name", "name")
    if level_id is None or not name:
        return None

    music_url, song_name = _music_url(raw, meta)
    return {
        "id": level_id,
        "name": str(name),
        "tier": _fmt_num(_first(raw, "Rating", "rating", "Tier", "tier")),
        "enjoyment": _fmt_num(_first(raw, "Enjoyment", "enjoyment")),
        "rank": _first(raw, "Rank", "rank", "Position", "position", "Place"),
        "demon": _demon_label(
            _first(meta, "Difficulty", "difficulty") or _first(raw, "Difficulty", "difficulty")
        ),
        "url": LEVEL_URL_TEMPLATE.format(id=level_id),
        "showcase": _showcase_url(
            _first(raw, "Showcase", "showcase", "ShowcaseVideoID", "ShowcaseURL")
            or _first(meta, "Showcase", "showcase", "ShowcaseVideoID", "ShowcaseURL")
        ),
        "music": music_url,
        "song_name": song_name,
    }


def _extract_items(data):
    if isinstance(data, dict):
        return _first(data, "levels", "Levels", "results", "data", "items") or []
    return data


async def _enrich(client: httpx.AsyncClient, lvl: dict) -> None:
    """Если в результатах поиска нет шоукейса/музыки, пробует взять их со страницы уровня."""
    if lvl["showcase"] and lvl["music"]:
        return
    key = str(lvl["id"])
    now = time.time()
    hit = _detail_cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        detail = hit[1]
    else:
        try:
            resp = await client.get(
                GDDL_API_BASE + DETAIL_PATH.format(id=lvl["id"]), timeout=6.0
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data[0] if isinstance(data, list) and data else data
            detail = normalize(raw) if isinstance(raw, dict) else None
        except Exception:
            log.info("detail fetch failed for %s", lvl["id"])
            detail = None
        _detail_cache[key] = (now, detail or {})
    if detail:
        lvl["showcase"] = lvl["showcase"] or detail.get("showcase")
        lvl["music"] = lvl["music"] or detail.get("music")
        lvl["song_name"] = lvl["song_name"] or detail.get("song_name")


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
    items = _extract_items(resp.json())

    levels = []
    for raw in items:
        if isinstance(raw, dict):
            lvl = normalize(raw)
            if lvl:
                levels.append(lvl)
    levels = levels[:MAX_RESULTS]

    await asyncio.gather(*(_enrich(client, lvl) for lvl in levels))

    _cache[key] = (now, levels)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    if len(_detail_cache) > 2000:
        _detail_cache.clear()
    return levels


# ------------------------------------------------------------ оформление
def card_text(lvl: dict) -> str:
    lines = [f"<b>{html.escape(lvl['name'])}</b>"]
    if lvl["rank"] is not None:
        lines.append(f"🏆 Место в топе: <b>#{html.escape(str(lvl['rank']))}</b>")
    if lvl["tier"]:
        lines.append(f"📊 Сложность (tier): <b>{html.escape(lvl['tier'])}</b>")
    if lvl["demon"]:
        lines.append(f"😈 Демон: <b>{html.escape(lvl['demon'])}</b>")
    if lvl["enjoyment"]:
        lines.append(f"🎉 Enjoyment: <b>{html.escape(lvl['enjoyment'])}</b>")
    lines.append(f"🆔 ID: <code>{html.escape(str(lvl['id']))}</code>")
    if lvl["song_name"]:
        lines.append(f"🎵 Музыка: {html.escape(lvl['song_name'])}")
    lines.append(f'🔗 <a href="{html.escape(lvl["url"], quote=True)}">Открыть в GDDL</a>')
    return "\n".join(lines)


def keyboard(lvl: dict) -> InlineKeyboardMarkup:
    # Если точной ссылки на шоукейс нет, кнопка ведёт на поиск в YouTube.
    showcase = lvl["showcase"] or (
        "https://www.youtube.com/results?search_query="
        + quote_plus(f"{lvl['name']} geometry dash showcase")
    )
    row = [InlineKeyboardButton("🎬 Шоукейс", url=showcase)]
    if lvl["music"]:
        row.append(InlineKeyboardButton("🎵 Музыка", url=lvl["music"]))
    return InlineKeyboardMarkup([row])


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
            ),
            reply_markup=keyboard(lvl),
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
        "Выбери нужный уровень из списка: отправлю место, сложность, enjoyment, "
        "ID и ссылку, а ниже будут кнопки «Шоукейс» и «Музыка»."
    )


async def on_startup(app: Application) -> None:
    app.bot_data["http"] = httpx.AsyncClient(
        timeout=10.0,
        headers={"User-Agent": "gddl-telegram-bot/1.1"},
        follow_redirects=True,
    )


async def on_shutdown(app: Application) -> None:
    await app.bot_data["http"].aclose()

# --------------------------------- МИНИ ВЕБ-СЕРВЕР FLASK ДЛЯ RENDER
import threading
from flask import Flask

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Бот стабильно работает и не спит!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# ----------------------------------------------------------- ЗАПУСК БОТА
def main() -> None:
    # Запускаем Flask-сервер в фоновом потоке, чтобы он не блокировал getUpdates
    threading.Thread(target=run_flask, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(InlineQueryHandler(inline_query))
    
    log.info("Бот успешно запущен вместе с веб-сервером")
    app.run_polling(allowed_updates=[Update.INLINE_QUERY, Update.MESSAGE])


if __name__ == "__main__":
    main()
