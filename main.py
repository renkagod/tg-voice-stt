import os
import sys
import asyncio
import logging
import tempfile
import hashlib
import time
from typing import Union, Optional
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.methods.base import TelegramMethod

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
    revoke_user_keys,
    has_access,
    get_pool_status_for_user,
    mask_key,
)
from transcriber import (
    transcribe_audio_stream,
    summarize_and_clean_text_stream,
    GeminiAPIError,
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

# Whitelist / Access Middleware
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Only process private messages and callback queries
        chat = getattr(event, "chat", None)
        if chat and chat.type != "private":
            return  # Silently ignore groups

        user = getattr(event, "from_user", None)
        if not user:
            return

        # If it's a callback query
        if isinstance(event, types.CallbackQuery):
            if not has_access(user.id, ADMIN_USERS):
                await event.answer("⚠️ У вас нет активного ключа в казне. Добавьте ключ через /key", show_alert=True)
                return
            return await handler(event, data)

        # If it's a message
        if isinstance(event, types.Message):
            # Commands are always allowed through
            if event.text and event.text.startswith("/"):
                return await handler(event, data)

            # Check voice / video notes
            if event.voice or event.video_note:
                if not has_access(user.id, ADMIN_USERS):
                    text = (
                        "👋 Чтобы пользоваться ботом, внесите свой ключ Google Gemini API в общую казну.\n\n"
                        "Команда для добавления:\n"
                        "/key <ваш_ключ>\n\n"
                        "Получить бесплатный ключ можно тут: https://aistudio.google.com/"
                    )
                    await event.reply(text, parse_mode="Markdown", disable_web_page_preview=True)
                    return
                return await handler(event, data)

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

# Helper functions for handling state in memory (avoids tmpfs memory leaks)
MAX_SAVED_TRANSCRIPTIONS = 100
transcriptions_cache: OrderedDict[str, str] = OrderedDict()

def get_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def save_transcription(text_hash: str, text: str):
    if len(transcriptions_cache) >= MAX_SAVED_TRANSCRIPTIONS:
        transcriptions_cache.popitem(last=False)
    transcriptions_cache[text_hash] = text

def load_transcription(text_hash: str) -> str:
    return transcriptions_cache.get(text_hash, "")

class SendMessageDraft(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "sendMessageDraft"
    
    chat_id: Union[int, str]
    draft_id: int
    text: str
    parse_mode: Optional[str] = None

# Helper for native streaming with editMessageText fallback
async def update_message_stream(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    fallback_msg=None,
    draft_state: Optional[dict] = None
):
    if draft_state is None:
        draft_state = {"supported": True}

    if draft_state.get("supported", True):
        try:
            await bot(SendMessageDraft(chat_id=chat_id, draft_id=draft_id, text=text))
            return None
        except Exception as e:
            logger.info(f"sendMessageDraft unavailable ({e}). Using edit_text fallback.")
            draft_state["supported"] = False

    if fallback_msg:
        try:
            await fallback_msg.edit_text(text)
            return fallback_msg
        except TelegramAPIError as e:
            if "message is not modified" in str(e).lower():
                return fallback_msg
            logger.debug(f"edit_text error: {e}")
            return fallback_msg
        except Exception:
            return fallback_msg
    else:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=text)
            return msg
        except Exception as e:
            logger.warning(f"Failed to send fallback message: {e}")
            return None

# Command handlers
@dp.message(Command("start", "help"))
async def handle_start_command(message: types.Message):
    text = (
        "🎙️ *Telegram Voice STT & Summary Bot*\n\n"
        "Бот для мгновенной расшифровки голосовых сообщений и кружочков с помощью Google Gemini.\n\n"
        "🏛 *Общая казна ключей:*\n"
        "Бот работает по принципу общего пула. Каждый пользователь вносит свой бесплатный API-ключ Gemini, "
        "и все ключи распределяют нагрузку между собой.\n\n"
        "📌 *Команды:*\n"
        "• /key <ваш_ключ> — добавить ключ в казну и активировать доступ\n"
        "• /key — проверить состояние казны и своих ключей\n"
        "• /revoke — отозвать все свои ключи и закрыть доступ\n\n"
        "🔗 Получить бесплатный ключ Google Gemini API: https://aistudio.google.com/"
    )
    await message.reply(text, parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(Command("key"))
async def handle_key_command(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        raw_key = parts[1].strip()
        status_msg = await message.reply("⏳ Проверяю ключ в Google AI Studio...")
        success, res_msg = await add_user_key(message.from_user.id, raw_key)
        await status_msg.edit_text(res_msg)
    else:
        status = get_pool_status_for_user(message.from_user.id)
        user_keys_text = ""
        if status["user_keys"]:
            for i, k in enumerate(status["user_keys"], 1):
                user_keys_text += f"{i}. `{k['masked']}` — {k['status']}\n"
        else:
            user_keys_text = "У вас пока нет привязанных ключей.\n"

        text = (
            f"🏛 *Общая казна ключей:*\n"
            f"• Всего активных ключей: {status['total_active']}\n"
            f"• Доступно прямо сейчас: {status['available']}\n"
            f"• В кулдауне (429): {status['on_cooldown']}\n\n"
            f"🔑 *Ваши ключи в казне:*\n"
            f"{user_keys_text}\n"
            f"💡 Чтобы добавить еще ключ: /key <ваш_ключ>\n"
            f"💡 Чтобы отозвать свои ключи: /revoke"
        )
        await message.reply(text, parse_mode="Markdown")

@dp.message(Command("revoke"))
async def handle_revoke_command(message: types.Message):
    count = revoke_user_keys(message.from_user.id)
    if count > 0:
        await message.reply(f"✅ Ваши ключи ({count} шт.) отозваны из казны. Доступ к боту приостановлен.")
    else:
        await message.reply("У вас нет активных ключей в казне.")

async def _do_stream_transcription(
    message: types.Message,
    status_msg: types.Message,
    audio_bytes: bytes,
    mime_type: str,
    api_key: str,
) -> bool:
    full_text = ""
    current_message_text = ""
    fallback_msg = None
    last_sent_text = ""
    last_edit_time = 0
    edit_interval = 1.5
    status_deleted = False
    draft_state = {"supported": True}

    async for chunk in transcribe_audio_stream(
        audio_bytes, mime_type=mime_type, model_name=DEFAULT_GEMINI_MODEL, api_key=api_key
    ):
        if not chunk:
            continue

        if not status_deleted:
            try:
                await status_msg.delete()
            except Exception:
                pass
            status_deleted = True

        full_text += chunk
        current_message_text += chunk

        # Split if chunk exceeds message limit
        if len(current_message_text) > 4000:
            part_to_send = current_message_text[:4000]
            if fallback_msg:
                try:
                    if part_to_send != last_sent_text:
                        await fallback_msg.edit_text(part_to_send)
                except Exception:
                    pass
            else:
                await message.reply(part_to_send)

            current_message_text = current_message_text[4000:]
            fallback_msg = None
            last_sent_text = ""
            last_edit_time = 0

        # Throttled stream update
        now = time.monotonic()
        if now - last_edit_time >= edit_interval and current_message_text.strip():
            if current_message_text != last_sent_text:
                fallback_msg = await update_message_stream(
                    bot, message.chat.id, 1, current_message_text, fallback_msg, draft_state
                )
                last_sent_text = current_message_text
                last_edit_time = now

    final_text = current_message_text.strip()
    if not final_text:
        final_text = "[No speech detected]"

    # Cache full transcription
    text_hash = get_text_hash(full_text.strip())
    save_transcription(text_hash, full_text.strip())

    builder = InlineKeyboardBuilder()
    builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")

    if fallback_msg:
        try:
            await fallback_msg.edit_text(final_text, reply_markup=builder.as_markup())
        except TelegramAPIError as e:
            if "message is not modified" not in str(e).lower():
                try:
                    await fallback_msg.edit_text(final_text)
                except TelegramAPIError:
                    pass
    else:
        try:
            if not status_deleted:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            await message.reply(final_text, reply_markup=builder.as_markup())
        except TelegramAPIError:
            await message.reply(final_text)

    return True

async def execute_transcription_with_failover(
    message: types.Message,
    status_msg: types.Message,
    audio_bytes: bytes,
    mime_type: str,
):
    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        api_key = get_active_key()
        if not api_key:
            err_text = (
                "⚠️ Все ключи в казне временно исчерпали лимиты (429). "
                "Подождите 1–2 минуты или добавьте рабочий ключ через /key <ключ>."
            )
            try:
                await status_msg.edit_text(err_text, parse_mode="Markdown")
            except Exception:
                await message.reply(err_text, parse_mode="Markdown")
            return

        try:
            success = await _do_stream_transcription(
                message=message,
                status_msg=status_msg,
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                api_key=api_key,
            )
            if success:
                return
        except GeminiAPIError as e:
            logger.warning(f"Transcription attempt {attempt+1} failed with code {e.status_code}: {e}")
            if e.status_code == 429:
                put_on_cooldown(api_key, seconds=60)
                continue
            elif e.status_code in (400, 403):
                owner_id = revoke_key(api_key, reason=f"Gemini API {e.status_code}")
                if owner_id and owner_id != 0:
                    try:
                        masked = mask_key(api_key)
                        await bot.send_message(
                            chat_id=owner_id,
                            text=(
                                f"⚠️ Ваш API-ключ Gemini (`{masked}`) перестал работать (код {e.status_code}) "
                                f"и был отозван из казны.\n\n"
                                f"Чтобы сохранить доступ к боту, привяжите новый ключ через команду:\n"
                                f"/key <новый_ключ>"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception as notify_err:
                        logger.warning(f"Could not notify key owner {owner_id}: {notify_err}")
                continue
            else:
                logger.error(f"Non-retriable Gemini error: {e}")
                break
        except Exception as e:
            logger.error(f"Unexpected error during transcription: {e}", exc_info=True)
            break

    # If all attempts failed
    try:
        await status_msg.edit_text("❌ Не удалось расшифровать аудио. Попробуйте позже.")
    except Exception:
        pass

# Handler for voice and video note messages
@dp.message(F.voice | F.video_note)
async def handle_voice_message(message: types.Message):
    # Ignore replies to bot messages
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        logger.info("Ignoring voice/video note sent in reply to bot message.")
        return

    is_video_note = bool(message.video_note)
    file_id = message.video_note.file_id if is_video_note else message.voice.file_id

    # Send initial status indicator
    status_msg = await message.reply("⏳ Listening...")

    temp_input_path = ""
    temp_wav_path = ""
    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            await status_msg.edit_text("❌ Failed to download audio file.")
            return

        ext = ".mp4" if is_video_note else ".ogg"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as temp_in:
            temp_input_path = temp_in.name

        await bot.download_file(file.file_path, destination=temp_input_path)

        if is_video_note:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_out:
                temp_wav_path = temp_out.name
            
            conversion_success = await extract_audio_from_mp4(temp_input_path, temp_wav_path)
            if not conversion_success:
                await status_msg.edit_text("❌ Failed to extract audio from video note.")
                return

            with open(temp_wav_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            with open(temp_input_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/ogg"

        await execute_transcription_with_failover(
            message=message,
            status_msg=status_msg,
            audio_bytes=audio_bytes,
            mime_type=mime_type,
        )

    except Exception as e:
        logger.error(f"Error handling voice/video note: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ Error processing audio.")
        except Exception:
            pass
    finally:
        for path in [temp_input_path, temp_wav_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove temp file {path}: {e}")

# Handler for "Clean & Summarize" callback button
@dp.callback_query(F.data.startswith("sum:"))
async def handle_summarize_callback(callback_query: types.CallbackQuery):
    await callback_query.answer("⏳ Processing text...", show_alert=False)
    
    text_hash = callback_query.data.split(":")[1]
    original_text = load_transcription(text_hash)
    if not original_text:
        original_text = callback_query.message.text
        
    if not original_text or original_text == "[No speech detected]":
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        return
    
    # Remove button immediately
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    await callback_query.message.bot.send_chat_action(
        chat_id=callback_query.message.chat.id, 
        action="typing"
    )
    
    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        api_key = get_active_key()
        if not api_key:
            await callback_query.message.reply(
                "⚠️ Все ключи в казне временно исчерпали лимиты (429). Повторите попытку позже."
            )
            return

        fallback_summary_msg = None
        draft_state = {"supported": True}
        full_summary = ""
        current_chunk_text = ""
        last_sent_text = ""
        last_edit_time = 0
        edit_interval = 1.5

        try:
            async for chunk in summarize_and_clean_text_stream(
                original_text, model_name=GEMINI_SUMMARY_MODEL, api_key=api_key
            ):
                if not chunk:
                    continue
                full_summary += chunk
                current_chunk_text += chunk
                
                if len(current_chunk_text) > 4000:
                    part_to_send = current_chunk_text[:4000]
                    if fallback_summary_msg:
                        try:
                            if part_to_send != last_sent_text:
                                await fallback_summary_msg.edit_text(part_to_send)
                        except Exception:
                            pass
                    else:
                        await callback_query.message.reply(part_to_send)

                    current_chunk_text = current_chunk_text[4000:]
                    fallback_summary_msg = None
                    last_sent_text = ""
                    last_edit_time = 0

                now = time.monotonic()
                if now - last_edit_time >= edit_interval and current_chunk_text.strip():
                    if current_chunk_text != last_sent_text:
                        fallback_summary_msg = await update_message_stream(
                            bot, callback_query.message.chat.id, 2, current_chunk_text, fallback_summary_msg, draft_state
                        )
                        last_sent_text = current_chunk_text
                        last_edit_time = now
                    
            final_summary = current_chunk_text.strip()
            if fallback_summary_msg:
                if final_summary and final_summary != last_sent_text:
                    try:
                        await fallback_summary_msg.edit_text(final_summary, parse_mode="Markdown")
                    except TelegramAPIError as e:
                        if "message is not modified" not in str(e).lower():
                            try:
                                await fallback_summary_msg.edit_text(final_summary, parse_mode=None)
                            except TelegramAPIError:
                                pass
            else:
                if final_summary:
                    try:
                        await callback_query.message.reply(final_summary, parse_mode="Markdown")
                    except TelegramAPIError:
                        await callback_query.message.reply(final_summary, parse_mode=None)
            return

        except GeminiAPIError as e:
            logger.warning(f"Summary attempt {attempt+1} failed with code {e.status_code}: {e}")
            if e.status_code == 429:
                put_on_cooldown(api_key, seconds=60)
                continue
            elif e.status_code in (400, 403):
                owner_id = revoke_key(api_key, reason=f"Gemini API {e.status_code}")
                if owner_id and owner_id != 0:
                    try:
                        masked = mask_key(api_key)
                        await bot.send_message(
                            chat_id=owner_id,
                            text=(
                                f"⚠️ Ваш API-ключ Gemini (`{masked}`) перестал работать (код {e.status_code}) "
                                f"и был отозван из казны.\n\n"
                                f"Чтобы сохранить доступ к боту, привяжите новый ключ через команду:\n"
                                f"/key <новый_ключ>"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                continue
            else:
                break
        except Exception as e:
            logger.error(f"Error generating summary: {e}", exc_info=True)
            break

    # Restore button on failure
    builder = InlineKeyboardBuilder()
    builder.button(text="✨ Clean & Summarize (Retry)", callback_data=f"sum:{text_hash}")
    try:
        await callback_query.message.edit_reply_markup(reply_markup=builder.as_markup())
    except TelegramAPIError:
        pass

async def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
        logger.critical("TELEGRAM_TOKEN is not set or has placeholder value. Exiting.")
        sys.exit(1)
        
    logger.info("Initializing key pool database...")
    init_pool(default_env_key=GEMINI_API_KEY)
        
    logger.info("Starting Telegram voice/video note transcription bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
