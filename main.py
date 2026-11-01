import os
import re
import sys
import html
import asyncio
import logging
import tempfile
import hashlib
import time
from typing import Union, Optional, Callable, Awaitable, Dict, Any, List
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)

from config import (
    TELEGRAM_TOKEN,
    ADMIN_USERS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_SUMMARY_MODEL,
    GEMINI_API_KEY,
)
from key_pool import (
    init_pool,
    add_user_key,
    get_active_key,
    put_on_cooldown,
    revoke_key,
    revoke_user_key,
    restore_user_key,
    revoke_user_keys,
    has_access,
    get_pool_status_for_user,
)
from transcriber import (
    transcribe_audio_stream,
    summarize_and_clean_text_stream,
    split_clean_and_summary,
    GeminiAPIError,
    NO_SPEECH,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Initialize Bot and Dispatcher
BOT_TOKEN = TELEGRAM_TOKEN if TELEGRAM_TOKEN and TELEGRAM_TOKEN.strip() != "your_telegram_bot_token_here" else "123456:DUMMY_TOKEN_FOR_IMPORT"
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()

AI_STUDIO_URL = "https://aistudio.google.com/apikey"
MAX_FILE_SIZE = 20 * 1024 * 1024
PART_LIMIT = 3900          # leaves room for the view header and the "continued" note
STREAM_INTERVAL = 1.5      # seconds between stream updates (Telegram rate limits)
MAX_ATTEMPTS = 3
PENDING_TTL = 3600         # media that arrived before the key is kept this long

# Google API keys look like "AIza" + 35 characters
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")

# --- Copy -------------------------------------------------------------------

HELP_TEXT = (
    "<b>Пришлите голосовое, кружок или аудио до 20\u00a0МБ</b> — верну текст.\n\n"
    "Под текстом две кнопки: «Чистовик» убирает паразиты и паузы, «Суть» собирает главное списком. "
    "Переключаются в том же сообщении.\n\n"
    "Бот работает на бесплатных ключах Gemini, которые добавляют сами пользователи. Ваши ключи: /key"
)
NEED_KEY_TEXT = (
    "<b>Чтобы расшифровывать голосовые, нужен ключ Gemini.</b> Он бесплатный, занимает минуту.\n\n"
    "1. Откройте AI Studio и нажмите Create API key.\n"
    "2. Пришлите ключ сюда одним сообщением."
)
ASK_KEY_TEXT = "Пришлите ключ одним сообщением. Я проверю его и удалю из чата."
TOO_BIG_TEXT = "<b>Файл больше 20\u00a0МБ</b> — столько Telegram даёт скачать ботам. Обрежьте запись или пришлите её частями."
NO_SPEECH_TEXT = "<i>Речи не слышно.</i>"
DOWNLOAD_FAILED_TEXT = "<b>Не получилось скачать файл из Telegram.</b> Попробуйте ещё раз."
EXTRACT_FAILED_TEXT = "<b>Не получилось достать звук из кружка.</b> Попробуйте ещё раз; если не выйдет, файл, скорее всего, повреждён."
TRANSCRIBE_OUTCOME_TEXT = {
    "busy": "<b>Все ключи упёрлись в лимит Google.</b> Обычно он сбрасывается за минуту.",
    "empty": "<b>В общем пуле не осталось ключей.</b> Добавьте свой, и расшифровка заработает.",
    "failed": "<b>Не получилось расшифровать.</b> Google не ответил. Попробуйте ещё раз через минуту.",
}
CLEANUP_OUTCOME_TEXT = {
    "busy": "Не получилось: все ключи упёрлись в лимит Google. Нажмите ещё раз через минуту.",
    "empty": "Не получилось: в общем пуле не осталось ключей. Добавьте свой: /key",
    "failed": "Не получилось: Google не ответил. Текст выше цел, нажмите ещё раз.",
}
KEY_RESULT_TEXT = {
    "empty": "Это пустое сообщение, ключа в нём нет.",
    "mine": "<b>Этот ключ уже у вас</b> и работает.",
    "duplicate": "<b>Этот ключ уже в общем пуле.</b> Создайте свой в AI Studio.",
    "invalid": "<b>Google не принял ключ.</b> Проверьте, что скопировали его целиком, или создайте новый.",
    "check_failed": "<b>Google не ответил на проверку.</b> Пришлите ключ ещё раз через минуту.",
}

VIEW_LABELS = {"raw": "Как сказано", "clean": "Чистовик", "sum": "Суть"}
VIEW_HEADERS = {"clean": "Чистовик", "sum": "Суть"}
MEDIA_NOUNS = {"voice": "Голосовое", "video_note": "Кружок", "audio": "Запись"}

def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many

def esc(text: str) -> str:
    return html.escape(text, quote=False)

def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))

def media_kind(message: types.Message) -> Optional[str]:
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.audio or (message.document and (message.document.mime_type or "").startswith("audio/")):
        return "audio"
    return None

# --- Sending helpers: HTML first, plain text if Telegram rejects the markup ---

async def reply_html(message: types.Message, text: str, **kwargs) -> types.Message:
    # allow_sending_without_reply: the original may be gone (e.g. a deleted key message)
    kwargs.update(disable_web_page_preview=True, allow_sending_without_reply=True)
    try:
        return await message.reply(text, parse_mode="HTML", **kwargs)
    except TelegramAPIError as e:
        if "can't parse entities" not in str(e).lower():
            raise
        return await message.reply(strip_html(text), parse_mode=None, **kwargs)

async def send_html(chat_id: int, text: str, **kwargs) -> types.Message:
    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, **kwargs)
    except TelegramAPIError:
        return await bot.send_message(chat_id, strip_html(text), parse_mode=None, disable_web_page_preview=True, **kwargs)

async def edit_html(message: types.Message, text: str, **kwargs) -> types.Message:
    try:
        await message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, **kwargs)
    except TelegramAPIError as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return message
        if "can't parse entities" in err:
            try:
                await message.edit_text(strip_html(text), parse_mode=None, **kwargs)
            except TelegramAPIError:
                pass
        else:
            logger.debug(f"edit_text error: {e}")
    return message

async def set_reaction(message: types.Message, emoji: Optional[str]):
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [],
        )
    except Exception as e:
        logger.debug(f"Could not set reaction: {e}")

def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)

def ai_studio_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Открыть AI Studio", url=AI_STUDIO_URL)

# --- In-memory state ----------------------------------------------------------

def _remember(store: OrderedDict, key, value, limit: int):
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)

# Transcript cards: token -> {"raw", "full", "clean", "sum", "view", "busy", "overflow_sent"}
cards: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
# Media that arrived before the user had a key: user_id -> (message, time)
pending_media: "OrderedDict[int, tuple]" = OrderedDict()
# Media whose transcription failed, for the "try again" button: (chat_id, message_id) -> message
failed_media: "OrderedDict[tuple, types.Message]" = OrderedDict()
# The last keys card each user opened, refreshed when their keys change elsewhere
key_cards: "OrderedDict[int, types.Message]" = OrderedDict()

def card_token(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:20]

# --- Access middleware ----------------------------------------------------------

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Only process private messages and callback queries
        chat = getattr(event, "chat", None)
        if chat and chat.type != "private":
            return  # Silently ignore groups

        user = getattr(event, "from_user", None)
        if not user:
            return

        if isinstance(event, types.CallbackQuery):
            # Key management works without access, so a user can bring a key back
            if (event.data or "").startswith(("k:", "noop")) or has_access(user.id, ADMIN_USERS):
                return await handler(event, data)
            await event.answer("Расшифровка выключена: нужен ключ Gemini. Подробности — /key", show_alert=True)
            return

        if isinstance(event, types.Message):
            text = event.text or ""
            # Commands and anything that looks like a Gemini key get through
            if text.startswith("/") or KEY_RE.search(text):
                return await handler(event, data)

            if media_kind(event):
                if has_access(user.id, ADMIN_USERS):
                    return await handler(event, data)
                if event.reply_to_message and event.reply_to_message.from_user.id == bot.id:
                    return
                _remember(pending_media, user.id, (event, time.time()), limit=1000)
                noun = MEDIA_NOUNS[media_kind(event)]
                await reply_html(
                    event,
                    f"{NEED_KEY_TEXT}\n\n{noun} я запомнил и расшифрую сразу после проверки ключа.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ai_studio_button()]]),
                )
                return

            # Ignore any regular text/media (silent mode)
            return

        return await handler(event, data)

# Register Access Middleware
dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())

# Asynchronous helper to extract audio from MP4 using ffmpeg
async def extract_audio_from_mp4(mp4_path: str, wav_path: str) -> bool:
    logger.info(f"Extracting audio from MP4: {mp4_path} -> WAV: {wav_path}")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", mp4_path, "-vn", "-ar", "16000", "-ac", "1", wav_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()
    success = process.returncode == 0
    logger.info(f"Audio extraction {'succeeded' if success else 'failed'}")
    return success

class SendMessageDraft(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "sendMessageDraft"

    chat_id: Union[int, str]
    draft_id: int
    text: str
    parse_mode: Optional[str] = None

def split_point(text: str, limit: int) -> int:
    """Where to cut an over-long text: a paragraph break, else a space, else hard at the limit."""
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    return cut if cut > 0 else limit

class TranscriptStream:
    """
    Streams a growing transcript into the chat: native drafts when available, message edits otherwise.
    Text over the message limit is closed off as separate messages; the last part stays open for the card.
    """
    def __init__(self, message: types.Message, draft_state: dict):
        self.message = message
        self.draft_state = draft_state
        self.live: Optional[types.Message] = None
        self.text = ""
        self.shown = ""
        self.last_update = 0.0

    async def feed(self, chunk: str):
        self.text += chunk
        while len(self.text) > PART_LIMIT:
            cut = split_point(self.text, PART_LIMIT)
            part, self.text = self.text[:cut].rstrip(), self.text[cut:].lstrip()
            await self._close_part(part)
        now = time.monotonic()
        if now - self.last_update >= STREAM_INTERVAL and self.text.strip() and self.text != self.shown:
            await self._show(self.text)
            self.last_update = now

    async def _show(self, text: str):
        if self.draft_state.get("supported", True):
            try:
                await bot(SendMessageDraft(chat_id=self.message.chat.id, draft_id=self.message.message_id, text=text))
                self.shown = text
                return
            except Exception as e:
                logger.info(f"sendMessageDraft unavailable ({e}). Using edit_text fallback.")
                self.draft_state["supported"] = False
        try:
            if self.live:
                await self.live.edit_text(text)
            else:
                self.live = await self.message.reply(text)
        except TelegramAPIError as e:
            if "message is not modified" not in str(e).lower():
                logger.debug(f"Stream update error: {e}")
        self.shown = text

    async def _close_part(self, part: str):
        if self.live:
            try:
                await self.live.edit_text(part)
            except TelegramAPIError:
                pass
            self.live = None
        else:
            await self.message.reply(part)
        self.shown = ""
        self.last_update = 0.0

    async def finish(self, text: str, markup: Optional[InlineKeyboardMarkup]) -> Optional[types.Message]:
        """Puts the final (last) part in place with its keyboard."""
        if self.live:
            return await edit_html(self.live, text, reply_markup=markup)
        return await reply_html(self.message, text, reply_markup=markup)

# --- Key failover -------------------------------------------------------------------

async def retire_key(api_key: str, status_code: int):
    """Revokes a key Google rejected and tells its owner what happened."""
    owner_id = revoke_key(api_key, reason=f"Gemini API {status_code}")
    if not owner_id:
        return
    await refresh_keys_card(owner_id)
    still_works = has_access(owner_id, ADMIN_USERS)
    text = (
        f"<b>Ключ <code>…{esc(api_key[-4:])}</code> перестал работать</b>, Google его отклонил. "
        f"Обычно это значит, что ключ удалён в AI Studio или проект заблокирован.\n\n"
        + ("Пока есть другой ключ, расшифровка работает." if still_works
           else "Расшифровка выключена, пока вы не добавите новый ключ.")
    )
    try:
        await send_html(owner_id, text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[button("Добавить новый ключ", "k:add")]]))
    except Exception as e:
        logger.warning(f"Could not notify key owner {owner_id}: {e}")

async def run_with_key_failover(work: Callable[[str], Awaitable[None]]) -> str:
    """
    Runs work(api_key) on pool keys, rotating on rate limits and dead keys.
    Returns "ok", "busy" (every key is rate-limited), "empty" (no keys at all) or "failed".
    """
    for attempt in range(MAX_ATTEMPTS):
        api_key = get_active_key()
        if not api_key:
            return "busy" if get_pool_status_for_user(0)["total_active"] else "empty"
        try:
            await work(api_key)
            return "ok"
        except GeminiAPIError as e:
            logger.warning(f"Attempt {attempt+1} failed with code {e.status_code}: {e}")
            if e.status_code == 429:
                put_on_cooldown(api_key, seconds=60)
                continue
            if e.status_code in (400, 403):
                await retire_key(api_key, e.status_code)
                continue
            logger.error(f"Non-retriable Gemini error: {e}")
            return "failed"
        except Exception as e:
            logger.error(f"Unexpected error during Gemini call: {e}", exc_info=True)
            return "failed"
    return "failed"

# --- Transcript cards -------------------------------------------------------------

def render_view(card: Dict[str, Any], view: str) -> str:
    if view == "raw":
        body = card["raw"]
    elif view == "clean":
        body = card["clean"]
    else:
        body = "\n".join(f"— {p}" for p in card["sum"])
    if len(body) > PART_LIMIT:
        body = body[:split_point(body, PART_LIMIT)].rstrip() + "\n\n…продолжение ниже."
    text = esc(body)
    if view in VIEW_HEADERS:
        text = f"<b>{VIEW_HEADERS[view]}</b>\n{text}"
    return text

def overflow_parts(card: Dict[str, Any], view: str) -> List[str]:
    """The rest of a cleaned text that did not fit into the card, in message-sized parts."""
    body = card["clean"] if view == "clean" else "\n".join(f"— {p}" for p in card["sum"])
    parts = []
    if len(body) <= PART_LIMIT:
        return parts
    body = body[split_point(body, PART_LIMIT):].lstrip()
    while body:
        cut = split_point(body, PART_LIMIT) if len(body) > PART_LIMIT else len(body)
        parts.append(body[:cut].rstrip())
        body = body[cut:].lstrip()
    return parts

def view_keyboard(token: str, current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        button(label, f"v:{view}:{token}") for view, label in VIEW_LABELS.items() if view != current
    ]])

BUSY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[button("Готовлю…", "noop")]])

async def stream_transcription(message: types.Message, audio_bytes: bytes, mime_type: str) -> str:
    draft_state = {"supported": True}

    async def work(api_key: str):
        stream = TranscriptStream(message, draft_state)
        full_text = ""
        async for chunk in transcribe_audio_stream(
            audio_bytes, mime_type=mime_type, model_name=DEFAULT_GEMINI_MODEL, api_key=api_key
        ):
            if chunk:
                full_text += chunk
                await stream.feed(chunk)

        full_text = full_text.strip()
        if not full_text or full_text == NO_SPEECH:
            await stream.finish(NO_SPEECH_TEXT, None)
            return

        token = card_token(full_text)
        card = {"raw": stream.text.strip(), "full": full_text, "clean": None, "sum": None,
                "view": "raw", "busy": False, "overflow_sent": set()}
        _remember(cards, token, card, limit=500)
        await stream.finish(render_view(card, "raw"), view_keyboard(token, "raw"))

    return await run_with_key_failover(work)

async def show_view(callback_query: types.CallbackQuery, token: str, view: str):
    card_msg = callback_query.message
    card = cards.get(token)
    if card is None:
        # After a restart the cache is empty; a card still showing the transcript can be rebuilt from itself
        text = card_msg.text or ""
        if text.split("\n", 1)[0] in VIEW_HEADERS.values():
            await callback_query.answer(
                "Бот перезапускался и не помнит исходник этого сообщения. Перешлите голосовое ещё раз.",
                show_alert=True,
            )
            return
        card = {"raw": text, "full": text, "clean": None, "sum": None,
                "view": "raw", "busy": False, "overflow_sent": set()}
        _remember(cards, token, card, limit=500)

    if card["busy"]:
        await callback_query.answer("Уже готовлю")
        return

    if view == "raw" or card[view] is not None:
        await callback_query.answer()
        card["view"] = view
        await edit_html(card_msg, render_view(card, view), reply_markup=view_keyboard(token, view))
        await send_overflow(card_msg, card, view)
        return

    await callback_query.answer("Готовлю чистовик" if view == "clean" else "Готовлю суть")
    card["busy"] = True
    await edit_html(card_msg, render_view(card, card["view"]), reply_markup=BUSY_KEYBOARD)
    await bot.send_chat_action(chat_id=card_msg.chat.id, action="typing")

    async def work(api_key: str):
        response = ""
        shown = ""
        last_update = 0.0
        async for chunk in summarize_and_clean_text_stream(
            card["full"], model_name=GEMINI_SUMMARY_MODEL, api_key=api_key
        ):
            if not chunk:
                continue
            response += chunk
            now = time.monotonic()
            if now - last_update < STREAM_INTERVAL:
                continue
            clean, points = split_clean_and_summary(response)
            preview = clean if view == "clean" else "\n".join(f"— {p}" for p in points)
            if preview and preview != shown:
                text = f"{VIEW_HEADERS[view]}\n{preview}"
                if len(text) > PART_LIMIT:
                    text = text[:PART_LIMIT] + "…"
                try:
                    await card_msg.edit_text(text, reply_markup=BUSY_KEYBOARD)
                except TelegramAPIError:
                    pass
                shown = preview
                last_update = now
        clean, points = split_clean_and_summary(response)
        if not clean:
            raise RuntimeError("Empty cleanup response")
        card["clean"], card["sum"] = clean, points or [clean]

    outcome = await run_with_key_failover(work)
    card["busy"] = False
    if outcome == "ok":
        card["view"] = view
        await edit_html(card_msg, render_view(card, view), reply_markup=view_keyboard(token, view))
        await send_overflow(card_msg, card, view)
    else:
        note = f"\n\n<i>{CLEANUP_OUTCOME_TEXT[outcome]}</i>"
        await edit_html(card_msg, render_view(card, card["view"]) + note,
                        reply_markup=view_keyboard(token, card["view"]))

async def send_overflow(card_msg: types.Message, card: Dict[str, Any], view: str):
    if view == "raw" or view in card["overflow_sent"]:
        return
    card["overflow_sent"].add(view)
    for part in overflow_parts(card, view):
        await card_msg.reply(part)

# --- Keys ---------------------------------------------------------------------------

def key_line(key: Dict[str, Any]) -> str:
    name = f"<code>…{esc(key['suffix'])}</code>"
    if key["state"] == "active":
        return f"{name} работает"
    if key["state"] == "cooldown":
        return f"{name} отдыхает ещё {key['cooldown_left']}\u00a0с: Google ограничил частоту"
    if key["revoked_by"] == "user":
        return f"{name} отозван"
    return f"{name} отклонён Google"

def keys_card(user_id: int):
    status = get_pool_status_for_user(user_id)
    keys = status["user_keys"][-10:]
    active = [k for k in keys if k["is_active"]]
    restorable = [k for k in keys if not k["is_active"] and k["revoked_by"] == "user"]
    is_admin = user_id in ADMIN_USERS
    works = is_admin or bool(active)

    lines = ["<b>Расшифровка работает</b>" if works else "<b>Расшифровка выключена</b>"]
    if keys:
        lines.append("\nВаши ключи\n" + "\n".join(key_line(k) for k in keys))
        if not works:
            lines.append("\nВерните ключ или добавьте новый." if restorable else "\nДобавьте новый ключ.")
    elif is_admin:
        lines.append("\nУ вас доступ администратора, свой ключ не нужен.")
    else:
        lines.append("\nКлючей пока нет. Нужен бесплатный ключ Gemini из AI Studio — пришлите его сюда.")

    total, free = status["total_active"], status["available"]
    if total:
        if free == total:
            free_text = "он свободен" if total == 1 else "все свободны"
        elif not free:
            free_text = "сейчас свободных нет"
        else:
            free_text = f"сейчас {plural(free, 'свободен', 'свободно', 'свободно')}\u00a0{free}"
        lines.append(f"\nВ общем пуле {total}\u00a0{plural(total, 'ключ', 'ключа', 'ключей')}, {free_text}.")
    else:
        lines.append("\nОбщий пул пуст.")

    rows, row = [], []
    for k in active:
        row.append(button(f"Отозвать …{k['suffix']}", f"k:rv:{k['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if len(active) > 1:
        rows.append([button("Отозвать все", "k:rva")])
    for k in restorable[-4:]:
        rows.append([button(f"Вернуть …{k['suffix']}", f"k:rs:{k['id']}")])
    rows.append([button("Добавить ключ", "k:add")])
    if not keys and not is_admin:
        rows.append([ai_studio_button()])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

async def send_keys_card(message: types.Message):
    text, markup = keys_card(message.from_user.id)
    card_msg = await reply_html(message, text, reply_markup=markup)
    _remember(key_cards, message.from_user.id, card_msg, limit=1000)

async def refresh_keys_card(user_id: int):
    """Keeps the last keys card a user opened in step with changes made elsewhere."""
    card_msg = key_cards.get(user_id)
    if card_msg:
        text, markup = keys_card(user_id)
        await edit_html(card_msg, text, reply_markup=markup)

async def accept_key(message: types.Message, raw_key: str):
    # The key is a secret: take it out of the chat history before anything else
    try:
        await message.delete()
        deleted = True
    except TelegramAPIError:
        deleted = False

    status_msg = await message.answer("Проверяю ключ…")
    success, code = await add_user_key(message.from_user.id, raw_key)
    if not success:
        markup = None
        if code in ("invalid", "duplicate"):
            markup = InlineKeyboardMarkup(inline_keyboard=[[ai_studio_button()]])
        await edit_html(status_msg, KEY_RESULT_TEXT[code], reply_markup=markup)
        return

    text = "<b>Ключ снова работает.</b>" if code == "restored" else "<b>Ключ работает.</b>"
    text += " Сообщение с ключом удалил из чата." if deleted else " Удалите сообщение с ключом, чтобы его никто не увидел."
    await edit_html(status_msg, text)
    await refresh_keys_card(message.from_user.id)

    pending = pending_media.pop(message.from_user.id, None)
    if pending and time.time() - pending[1] < PENDING_TTL:
        await handle_media_message(pending[0])

# --- Command handlers ---------------------------------------------------------------

@dp.message(Command("start", "help"))
async def handle_start_command(message: types.Message):
    if has_access(message.from_user.id, ADMIN_USERS):
        await reply_html(message, HELP_TEXT)
    else:
        await reply_html(message, NEED_KEY_TEXT, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[ai_studio_button()]]))

@dp.message(Command("key"))
async def handle_key_command(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        await accept_key(message, parts[1].strip())
        return
    await send_keys_card(message)

@dp.message(Command("revoke"))
async def handle_revoke_command(message: types.Message):
    await send_keys_card(message)

@dp.message(F.text.func(lambda text: bool(KEY_RE.search(text))))
async def handle_key_text(message: types.Message):
    await accept_key(message, KEY_RE.search(message.text).group(0))

# --- Media ------------------------------------------------------------------------------

# Handler for voice, video note, and audio media messages
@dp.message(F.voice | F.video_note | F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def handle_media_message(message: types.Message):
    # Ignore replies to bot messages
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        logger.info("Ignoring media sent in reply to bot message.")
        return

    media = message.voice or message.video_note or message.audio or message.document
    if not media:
        return
    if (media.file_size or 0) > MAX_FILE_SIZE:
        await reply_html(message, TOO_BIG_TEXT)
        return

    await set_reaction(message, "👀")
    temp_input_path = ""
    temp_wav_path = ""
    try:
        if message.video_note:
            ext = ".mp4"
        elif message.voice:
            ext = ".ogg"
        else:
            ext = os.path.splitext(getattr(media, "file_name", None) or ".mp3")[1] or ".mp3"

        file = await bot.get_file(media.file_id)
        if not file.file_path:
            await reply_with_retry(message, DOWNLOAD_FAILED_TEXT)
            return

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as temp_in:
            temp_input_path = temp_in.name

        await bot.download_file(file.file_path, destination=temp_input_path)

        if message.video_note:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_out:
                temp_wav_path = temp_out.name

            if not await extract_audio_from_mp4(temp_input_path, temp_wav_path):
                await reply_with_retry(message, EXTRACT_FAILED_TEXT)
                return

            with open(temp_wav_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        elif message.voice:
            with open(temp_input_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/ogg"
        else:
            with open(temp_input_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/mpeg"
            if media.mime_type:
                mime_type = media.mime_type
            elif ext.lower() == ".wav":
                mime_type = "audio/wav"
            elif ext.lower() in (".m4a", ".mp4"):
                mime_type = "audio/mp4"
            elif ext.lower() == ".ogg":
                mime_type = "audio/ogg"

        outcome = await stream_transcription(message, audio_bytes, mime_type)
        if outcome != "ok":
            await reply_with_retry(message, TRANSCRIBE_OUTCOME_TEXT[outcome], add_key=outcome == "empty")

    except Exception as e:
        logger.error(f"Error handling media: {e}", exc_info=True)
        await reply_with_retry(message, TRANSCRIBE_OUTCOME_TEXT["failed"])
    finally:
        await set_reaction(message, None)
        for path in [temp_input_path, temp_wav_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove temp file {path}: {e}")

async def reply_with_retry(message: types.Message, text: str, add_key: bool = False):
    _remember(failed_media, (message.chat.id, message.message_id), message, limit=200)
    action = button("Добавить ключ", "k:add") if add_key else button("Попробовать снова", f"re:{message.message_id}")
    try:
        await reply_html(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[action]]))
    except TelegramAPIError as e:
        logger.warning(f"Could not report failure: {e}")

# --- Callbacks ------------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("v:"))
async def handle_view_callback(callback_query: types.CallbackQuery):
    _, view, token = callback_query.data.split(":", 2)
    if view not in VIEW_LABELS:
        await callback_query.answer()
        return
    await show_view(callback_query, token, view)

@dp.callback_query(F.data.startswith("re:"))
async def handle_retry_callback(callback_query: types.CallbackQuery):
    message_id = int(callback_query.data.split(":", 1)[1])
    original = failed_media.pop((callback_query.message.chat.id, message_id), None)
    if not original:
        await callback_query.answer("Бот перезапускался и не помнит это сообщение. Перешлите его ещё раз.", show_alert=True)
        return
    await callback_query.answer("Пробую снова")
    try:
        await callback_query.message.delete()
    except TelegramAPIError:
        pass
    await handle_media_message(original)

@dp.callback_query(F.data == "noop")
async def handle_noop_callback(callback_query: types.CallbackQuery):
    await callback_query.answer("Уже готовлю")

@dp.callback_query(F.data.startswith("k:"))
async def handle_key_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    action = parts[1]

    if action == "add":
        await callback_query.answer()
        await callback_query.message.answer(
            ASK_KEY_TEXT, reply_markup=ForceReply(input_field_placeholder="Ключ Gemini, начинается с AIza")
        )
        return

    if action == "rva":
        count = revoke_user_keys(user_id)
        await callback_query.answer(f"Отозвано: {count} {plural(count, 'ключ', 'ключа', 'ключей')}")
    elif action in ("rv", "rs") and len(parts) == 3 and parts[2].isdigit():
        key_id = int(parts[2])
        keys = {k["id"]: k for k in get_pool_status_for_user(user_id)["user_keys"]}
        suffix = keys[key_id]["suffix"] if key_id in keys else ""
        if action == "rv" and revoke_user_key(user_id, key_id):
            await callback_query.answer(f"Ключ …{suffix} отозван")
        elif action == "rs" and restore_user_key(user_id, key_id):
            await callback_query.answer(f"Ключ …{suffix} снова в пуле")
        else:
            await callback_query.answer("Этот ключ уже изменился")
    else:
        await callback_query.answer()
        return

    text, markup = keys_card(user_id)
    await edit_html(callback_query.message, text, reply_markup=markup)
    _remember(key_cards, user_id, callback_query.message, limit=1000)

# --- Startup -------------------------------------------------------------------------------

async def register_bot_profile():
    """Fills the "/" menu and the texts people see before the first message."""
    try:
        await bot.set_my_commands([
            BotCommand(command="key", description="Ключи и статус"),
            BotCommand(command="help", description="Как пользоваться"),
        ])
        await bot.set_my_short_description("Голосовые и кружки в текст. Чистовик и суть по кнопке.")
        await bot.set_my_description(
            "Пришлите голосовое, кружок или аудио до 20\u00a0МБ — верну текст. "
            "Под расшифровкой кнопки «Чистовик» без слов-паразитов и «Суть» с главным списком."
        )
    except Exception as e:
        logger.warning(f"Could not register bot commands and description: {e}")

async def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
        logger.critical("TELEGRAM_TOKEN is not set or has placeholder value. Exiting.")
        sys.exit(1)

    logger.info("Initializing key pool database...")
    init_pool(default_env_key=GEMINI_API_KEY)

    logger.info("Starting Telegram voice/video note transcription bot...")
    await register_bot_profile()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
