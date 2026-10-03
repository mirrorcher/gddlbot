"""
Inline-бот для Telegram: поиск демонов на GDDL (https://gdladder.com).

Использование в любом чате:  @gdladderbot Cataclysm
Бот показывает список подходящих уровней; после выбора отправляется карточка
с местом в топе, сложностью (tier), enjoyment, ID, типом демона и ссылкой на GDDL.
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
# httpx пишет в INFO полный URL запроса, а для Telegram в нём лежит токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gddl-bot")

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.environ["BOT_TOKEN"]

GDDL_SITE = os.getenv("GDDL_SITE", "https://gdladder.com")
GDDL_API_BASE = os.getenv("GDDL_API_BASE", f"{GDDL_SITE}/api")
SEARCH_PATH = os.getenv("GDDL_SEARCH_PATH", "/levels")
SEARCH_PARAM = os.getenv("GDDL_SEARCH_PARAM", "name")
DETAIL_PATH = os.getenv("GDDL_DETAIL_PATH", "/levels/{id}")
LEVEL_URL_TEMPLATE = os.getenv("GDDL_LEVEL_URL", f"{GDDL_SITE}/level/{{id}}")

# Global Demonlist (demonlist.org): позиция в топе для экстрим-демонов.
DEMONLIST_API = os.getenv("DEMONLIST_API", "https://api.demonlist.org")
DEMONLIST_PATH = os.getenv("DEMONLIST_PATH", "/level/classic/list")
DEMONLIST_LIMIT = 50

MAX_RESULTS = 10
MIN_QUERY_LEN = 2
CACHE_TTL = 300  # секунд

_cache: dict[str, tuple[float, list[dict]]] = {}
_detail_cache: dict[int, tuple[float, dict]] = {}
_demonlist_cache: dict[str, tuple[float, dict[int, int]]] = {}

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ----------------------------------------------------------------- утилиты
def _fmt_num(v) -> str | None:
    if v is None:
        return None
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return None


def _demon_label(v) -> str | None:
    """«Extreme» -> «Extreme Demon»."""
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    return v if "demon" in v.casefold() else f"{v} Demon"


def _showcase_url(v) -> str | None:
    if not v:
        return None
    v = str(v).strip()
    if v.startswith("http"):
        return v
    if YT_ID_RE.match(v):
        return f"https://www.youtube.com/watch?v={v}"
    return None


def _song_url(song_id, song_name) -> str | None:
    """Newgrounds, если известен ID песни; иначе поиск на YouTube по названию."""
    try:
        if song_id and int(song_id) > 0:
            return f"https://www.newgrounds.com/audio/listen/{int(song_id)}"
    except (TypeError, ValueError):
        pass
    if song_name:
        return "https://www.youtube.com/results?search_query=" + quote_plus(
            f"{song_name} Geometry Dash"
        )
    return None


# ------------------------------------------------------------- работа с GDDL
def from_search(raw: dict) -> dict | None:
    """Элемент из /levels?name=... (плоский формат, ключи в нижнем регистре)."""
    level_id = raw.get("id")
    name = raw.get("name")
    if level_id is None or not name:
        return None
    return {
        "id": level_id,
        "name": str(name),
        "tier": _fmt_num(raw.get("rating")),
        "enjoyment": _fmt_num(raw.get("enjoyment")),
        "demon": _demon_label(raw.get("difficulty")),
        "showcase": _showcase_url(raw.get("showcase")),
        "song_name": raw.get("songName") or None,
        "song_id": None,
        "rank": None,
        "rank_source": None,  # "demonlist" или "gddl"
        "url": LEVEL_URL_TEMPLATE.format(id=level_id),
    }


def merge_detail(lvl: dict, d: dict) -> None:
    """Дополняет уровень данными из /levels/{id} (ключи с большой буквы, есть Meta)."""
    meta = d.get("Meta") or {}
    song = meta.get("Song") or {}

    rank = d.get("DifficultyIndex")
    if rank:
        lvl["rank"] = rank
        lvl["rank_source"] = "gddl"
    lvl["tier"] = _fmt_num(d.get("Rating")) or lvl["tier"]
    lvl["enjoyment"] = _fmt_num(d.get("Enjoyment")) or lvl["enjoyment"]
    lvl["demon"] = _demon_label(meta.get("Difficulty")) or lvl["demon"]
    lvl["showcase"] = _showcase_url(d.get("Showcase")) or lvl["showcase"]
    lvl["song_name"] = song.get("Name") or lvl["song_name"]
    lvl["song_id"] = song.get("ID") or meta.get("SongID") or lvl["song_id"]


async def _enrich(client: httpx.AsyncClient, lvl: dict) -> None:
    lid = lvl["id"]
    now = time.time()
    hit = _detail_cache.get(lid)
    if hit and now - hit[0] < CACHE_TTL:
        detail = hit[1]
    else:
        try:
            resp = await client.get(
                GDDL_API_BASE + DETAIL_PATH.format(id=lid), timeout=6.0
            )
            resp.raise_for_status()
            detail = resp.json()
            if not isinstance(detail, dict):
                detail = {}
        except Exception as e:
            log.info("detail fetch failed for %s: %s", lid, type(e).__name__)
            detail = {}
        _detail_cache[lid] = (now, detail)
    if detail:
        merge_detail(lvl, detail)


async def demonlist_positions(client: httpx.AsyncClient, term: str) -> dict[int, int]:
    """{ID уровня в игре: позиция в Global Demonlist} для уровней, найденных по запросу."""
    key = term.casefold()
    now = time.time()
    hit = _demonlist_cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    positions: dict[int, int] = {}
    try:
        resp = await client.get(
            DEMONLIST_API + DEMONLIST_PATH,
            params={"search": term, "limit": DEMONLIST_LIMIT},
            timeout=6.0,
        )
        resp.raise_for_status()
        levels = (resp.json().get("data") or {}).get("levels") or []
        for item in levels:
            ingame_id, placement = item.get("ingame_id"), item.get("placement")
            if ingame_id and placement:
                positions[int(ingame_id)] = int(placement)
    except Exception as e:
        log.info("demonlist lookup failed for %r: %s", term, type(e).__name__)
        return {}  # при сбое не кэшируем, чтобы повторить при следующем запросе

    _demonlist_cache[key] = (now, positions)
    if len(_demonlist_cache) > 500:
        _demonlist_cache.clear()
    return positions


async def apply_demonlist(client: httpx.AsyncClient, query: str, levels: list[dict]) -> None:
    """Для уровней из demonlist позиция берётся только оттуда."""
    positions = dict(await demonlist_positions(client, query))

    # Если запрос широкий и нужный экстрим не попал в первую выдачу, ищем его по точному названию.
    missing = [l for l in levels if l["demon"] == "Extreme Demon" and l["id"] not in positions]
    if missing:
        extra = await asyncio.gather(
            *(demonlist_positions(client, l["name"]) for l in missing)
        )
        for found in extra:
            positions.update(found)

    for lvl in levels:
        placement = positions.get(lvl["id"])
        if placement:
            lvl["rank"] = placement
            lvl["rank_source"] = "demonlist"


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
    items = data.get("data", []) if isinstance(data, dict) else data

    levels = []
    for raw in items:
        if isinstance(raw, dict):
            lvl = from_search(raw)
            if lvl:
                levels.append(lvl)
    levels = levels[:MAX_RESULTS]

    await asyncio.gather(*(_enrich(client, lvl) for lvl in levels))
    await apply_demonlist(client, query, levels)

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
        label = "Место в топе" if lvl["rank_source"] == "demonlist" else "Место в GDDL"
        lines.append(f"🏆 {label}: <b>#{html.escape(str(lvl['rank']))}</b>")
    if lvl["tier"]:
        lines.append(f"📊 Сложность (tier): <b>{html.escape(lvl['tier'])}</b>")
    if lvl["demon"]:
        lines.append(f"😈 Демон: <b>{html.escape(lvl['demon'])}</b>")
    if lvl["enjoyment"]:
        lines.append(f"🎉 Enjoyment: <b>{html.escape(lvl['enjoyment'])}</b>")
    lines.append(f"🆔 ID: <code>{html.escape(str(lvl['id']))}</code>")
    if lvl["song_name"]:
        lines.append(f"🎵 Музыка: {html.escape(str(lvl['song_name']))}")
    lines.append(f'🔗 <a href="{html.escape(lvl["url"], quote=True)}">Открыть в GDDL</a>')
    return "\n".join(lines)


def keyboard(lvl: dict) -> InlineKeyboardMarkup:
    # Если точной ссылки на шоукейс нет, кнопка ведёт на поиск в YouTube.
    showcase = lvl["showcase"] or (
        "https://www.youtube.com/results?search_query="
        + quote_plus(f"{lvl['name']} geometry dash showcase")
    )
    row = [InlineKeyboardButton("🎬 Шоукейс", url=showcase)]
    music = _song_url(lvl["song_id"], lvl["song_name"])
    if music:
        row.append(InlineKeyboardButton("🎵 Музыка", url=music))
    return InlineKeyboardMarkup([row])


def short_description(lvl: dict) -> str:
    parts = []
    if lvl["demon"]:
        parts.append(lvl["demon"])
    if lvl["tier"]:
        parts.append(f"Tier {lvl['tier']}")
    if lvl["rank"] is not None:
        parts.append(f"#{lvl['rank']}")
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
        headers={"User-Agent": "gddl-telegram-bot/1.2"},
        follow_redirects=True,
    )


async def on_shutdown(app: Application) -> None:
    await app.bot_data["http"].aclose()


# --------------------------------- МИНИ ВЕБ-СЕРВЕР FLASK ДЛЯ RENDER
import threading
from flask import Flask

# Пинги каждые пару минут не должны засорять лог.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

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
