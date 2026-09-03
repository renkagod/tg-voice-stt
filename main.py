import os
import asyncio
import logging
import tempfile
import hashlib
import time
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from config import (
    TELEGRAM_TOKEN,
    ALLOWED_USERS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_FALLBACK_MODEL,
    GEMINI_SUMMARY_MODEL,
    get_user_model,
    set_user_model,
)
from transcriber import (
    transcribe_audio_stream,
    summarize_and_clean_text_stream,
    fetch_available_models,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Initialize Bot and Dispatcher
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()

# Whitelist Middleware
class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return
        if user.id not in ALLOWED_USERS:
            logger.warning(f"Unauthorized access attempt from user ID: {user.id} (username: {user.username})")
            return  # Silently ignore
        return await handler(event, data)

# Register Whitelist Middleware to both messages and callback queries
dp.message.outer_middleware(WhitelistMiddleware())
dp.callback_query.outer_middleware(WhitelistMiddleware())

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

from collections import OrderedDict
from typing import Union, Optional
from aiogram.methods.base import TelegramMethod

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
    """
    Attempts to stream the message using Telegram Bot API's native sendMessageDraft method.
    Falls back to classical editMessageText if not supported or fails.
    """
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

# Helper to build model selection inline keyboard
def get_models_keyboard(current_model: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    presets = [
        ("gemini-3.5-flash-lite", "⚡ Gemini 3.5 Flash Lite (500 RPD)"),
        ("gemini-3.5-transcribe", "🎙️ Gemini 3.5 Transcribe (+ фоллбек)"),
        ("gemini-2.5-flash", "🚀 Gemini 2.5 Flash"),
        ("gemini-3.1-flash-lite", "🔹 Gemini 3.1 Flash Lite"),
    ]
    for model_id, label in presets:
        prefix = "✅ " if model_id == current_model else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"set_model:{model_id}")
    builder.button(text="🔄 Список моделей из Google API", callback_data="fetch_api_models:0")
    builder.adjust(1)
    return builder.as_markup()

# Handler for /model and /settings commands
@dp.message(Command("start", "model", "settings", "help"))
async def handle_model_command(message: types.Message):
    user_model = get_user_model(message.from_user.id)
    text = (
        f"⚙️ *Настройки модели распознавания*\n\n"
        f"Текущая активная модель: `{user_model}`\n\n"
        f"Выберите модель для распознавания:\n"
        f"• *Gemini 3.5 Flash Lite:* универсальная и быстрая (лимит 500 запросов в день).\n"
        f"• *Gemini 3.5 Transcribe:* специализированная модель распознавания речи (25 RPD). При исчерпании лимитов автоматически переключится на `{GEMINI_FALLBACK_MODEL}`.\n\n"
        f"💡 Кнопка саммари и очистки текста всегда использует `{GEMINI_SUMMARY_MODEL}`."
    )
    await message.reply(text, reply_markup=get_models_keyboard(user_model), parse_mode="Markdown")

# Callback handler for setting model
@dp.callback_query(F.data.startswith("set_model:"))
async def handle_set_model(callback_query: types.CallbackQuery):
    model_id = callback_query.data.split(":", 1)[1]
    user_id = callback_query.from_user.id
    set_user_model(user_id, model_id)
    await callback_query.answer(f"Выбрана модель: {model_id}")
    
    text = (
        f"⚙️ *Настройки модели*\n\n"
        f"Текущая активная модель: `{model_id}`\n\n"
        f"💡 Если выбрана `gemini-3.5-transcribe`, при исчерпании лимитов (429) "
        f"бот автоматически выполнит фоллбек на `{GEMINI_FALLBACK_MODEL}`.\n"
        f"Кнопка саммари использует `{GEMINI_SUMMARY_MODEL}`."
    )
    try:
        await callback_query.message.edit_text(
            text,
            reply_markup=get_models_keyboard(model_id),
            parse_mode="Markdown"
        )
    except TelegramAPIError:
        pass

# Callback handler for back to presets
@dp.callback_query(F.data == "back_to_presets")
async def handle_back_to_presets(callback_query: types.CallbackQuery):
    await callback_query.answer()
    user_model = get_user_model(callback_query.from_user.id)
    text = (
        f"⚙️ *Настройки модели*\n\n"
        f"Текущая активная модель: `{user_model}`\n\n"
        f"💡 Если выбрана `gemini-3.5-transcribe`, при исчерпании лимитов (429) "
        f"бот автоматически выполнит фоллбек на `{GEMINI_FALLBACK_MODEL}`.\n"
        f"Кнопка саммари использует `{GEMINI_SUMMARY_MODEL}`."
    )
    try:
        await callback_query.message.edit_text(
            text,
            reply_markup=get_models_keyboard(user_model),
            parse_mode="Markdown"
        )
    except TelegramAPIError:
        pass

# Callback handler for fetching dynamic models from Google API
@dp.callback_query(F.data.startswith("fetch_api_models:"))
async def handle_fetch_api_models(callback_query: types.CallbackQuery):
    await callback_query.answer("Запрашиваю модели из Gemini API...")
    page = int(callback_query.data.split(":")[1])
    try:
        models = await fetch_available_models()
        user_model = get_user_model(callback_query.from_user.id)
        
        builder = InlineKeyboardBuilder()
        page_size = 5
        total_pages = (len(models) + page_size - 1) // page_size if models else 1
        page = max(0, min(page, total_pages - 1))
        
        start = page * page_size
        end = start + page_size
        current_page_models = models[start:end]
        
        for m in current_page_models:
            mid = m["id"]
            prefix = "✅ " if mid == user_model else ""
            name = m.get("displayName") or mid
            builder.button(text=f"{prefix}{name[:32]}", callback_data=f"set_model:{mid}")
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"fetch_api_models:{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"fetch_api_models:{page+1}"))
        
        builder.adjust(1)
        if nav_buttons:
            builder.row(*nav_buttons)
        builder.row(types.InlineKeyboardButton(text="🔙 К пресетам", callback_data="back_to_presets"))
        
        text = (
            f"📋 *Доступные модели в вашем Google AI Studio* (Стр. {page+1}/{total_pages}):\n\n"
            f"Текущая активная: `{user_model}`\n"
            f"Нажмите на модель, чтобы переключиться:"
        )
        try:
            await callback_query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        except TelegramAPIError:
            pass
    except Exception as e:
        logger.error(f"Error fetching models: {e}")
        await callback_query.message.edit_text(
            f"❌ Ошибка при запросе моделей из API: `{e}`",
            reply_markup=get_models_keyboard(get_user_model(callback_query.from_user.id)),
            parse_mode="Markdown"
        )

# Common streaming and reply logic
async def stream_and_reply_transcription(
    message: types.Message,
    status_msg: types.Message,
    audio_bytes: bytes,
    mime_type: str,
    user_model: str,
):
    full_text = ""
    current_message_text = ""
    fallback_msg = None
    last_sent_text = ""
    last_edit_time = 0
    edit_interval = 1.5
    status_deleted = False
    draft_state = {"supported": True}

    try:
        async for chunk in transcribe_audio_stream(audio_bytes, mime_type=mime_type, model_name=user_model):
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

            # If current chunk exceeds message limit, split it
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

            now = time.monotonic()
            if now - last_edit_time >= edit_interval and current_message_text.strip():
                if current_message_text != last_sent_text:
                    fallback_msg = await update_message_stream(
                        bot, message.chat.id, 1, current_message_text, fallback_msg, draft_state
                    )
                    last_sent_text = current_message_text
                    last_edit_time = now

        if not status_deleted:
            try:
                await status_msg.delete()
            except Exception:
                pass

        final_text = current_message_text.strip()
        if not full_text.strip():
            final_text = "[No speech detected]"
            full_text = final_text

        tg_msg = None
        if fallback_msg:
            tg_msg = fallback_msg
            if final_text and final_text != last_sent_text:
                try:
                    await tg_msg.edit_text(final_text)
                except TelegramAPIError as e:
                    if "message is not modified" not in str(e).lower():
                        logger.warning(f"Failed to edit final message: {e}")
                except Exception as e:
                    logger.warning(f"Failed to edit final message: {e}")
        else:
            if final_text:
                try:
                    tg_msg = await message.reply(final_text)
                except Exception as e:
                    logger.error(f"Failed to send final message: {e}")

        # Store full transcription in memory for summary button
        if full_text.strip() and full_text.strip() != "[No speech detected]":
            text_hash = get_text_hash(full_text)
            save_transcription(text_hash, full_text)

            if tg_msg:
                builder = InlineKeyboardBuilder()
                builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")
                try:
                    await tg_msg.edit_reply_markup(reply_markup=builder.as_markup())
                except Exception as e:
                    logger.warning(f"Could not attach reply markup: {e}")

    except Exception as e:
        logger.error(f"Error handling audio transcription: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ *Error processing audio.*", parse_mode="Markdown")
        except Exception:
            pass


# Handler for Voice Messages
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        logger.info("Ignoring voice message because it is a reply to the bot's own message.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    voice = message.voice
    logger.info(f"Received voice message from user {message.from_user.id}. File ID: {voice.file_id}")
    
    status_msg = await message.reply("⏳ *Downloading audio...*", parse_mode="Markdown")
    
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_ogg:
        temp_ogg_path = temp_ogg.name
    
    try:
        user_model = get_user_model(message.from_user.id)
        file_info = await bot.get_file(voice.file_id)
        await bot.download_file(file_info.file_path, temp_ogg_path)
        
        await status_msg.edit_text(f"🎙️ *Transcribing audio... ({user_model})*", parse_mode="Markdown")
        
        with open(temp_ogg_path, "rb") as f:
            audio_bytes = f.read()

        await stream_and_reply_transcription(
            message=message,
            status_msg=status_msg,
            audio_bytes=audio_bytes,
            mime_type="audio/ogg",
            user_model=user_model,
        )
    except Exception as e:
        logger.error(f"Error handling voice message: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ *Error processing voice message.*", parse_mode="Markdown")
        except Exception:
            pass
    finally:
        if os.path.exists(temp_ogg_path):
            try:
                os.remove(temp_ogg_path)
            except Exception as e:
                logger.error(f"Failed to remove temp file {temp_ogg_path}: {e}")


# Handler for Video Notes ("Circles"), Regular Videos, Audio files, and Video/Audio Documents
@dp.message(F.video_note | F.video | F.audio | F.document)
async def handle_video_or_audio_message(message: types.Message):
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        logger.info("Ignoring media message because it is a reply to the bot's own message.")
        return

    media_obj = message.video_note or message.video or message.audio or message.document
    if not media_obj:
        return

    doc_mime = ""
    doc_ext = ""
    if message.document:
        doc_mime = (message.document.mime_type or "").lower()
        doc_name = (message.document.file_name or "").lower()
        valid_exts = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")
        if not (doc_mime.startswith("video/") or doc_mime.startswith("audio/") or doc_name.endswith(valid_exts)):
            return  # Ignore non-video/audio documents
        doc_ext = os.path.splitext(doc_name)[1]

    # Check file size (Telegram Bot API download limit for standard bots is 20MB)
    if media_obj.file_size and media_obj.file_size > 20 * 1024 * 1024:
        await message.reply(
            "❌ *Файл слишком большой (> 20 МБ).* Telegram API не позволяет ботам скачивать файлы крупнее 20 МБ.\n\n"
            "💡 *Совет:* Сжмите видео (например, понизьте разрешение) или отправьте только аудиодорожку.",
            parse_mode="Markdown"
        )
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    logger.info(f"Received media from user {message.from_user.id}. File ID: {media_obj.file_id}")
    
    status_msg = await message.reply("⏳ *Downloading media...*", parse_mode="Markdown")

    is_video = bool(message.video_note or message.video or (message.document and doc_mime.startswith("video/")))
    input_ext = ".mp4" if is_video else (doc_ext or ".mp3")
    
    fd_in, temp_input_path = tempfile.mkstemp(suffix=input_ext)
    fd_wav, temp_wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd_in)
    os.close(fd_wav)
    
    try:
        user_model = get_user_model(message.from_user.id)
        file_info = await bot.get_file(media_obj.file_id)
        await bot.download_file(file_info.file_path, temp_input_path)
        
        await status_msg.edit_text(f"🎙️ *Extracting and transcribing audio... ({user_model})*", parse_mode="Markdown")
        
        conversion_success = await extract_audio_from_mp4(temp_input_path, temp_wav_path)
        if conversion_success:
            with open(temp_wav_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            with open(temp_input_path, "rb") as f:
                audio_bytes = f.read()
            if is_video:
                mime_type = "video/mp4"
            elif message.audio and message.audio.mime_type:
                mime_type = message.audio.mime_type
            elif doc_mime:
                mime_type = doc_mime
            else:
                mime_type = "audio/mpeg"

        await stream_and_reply_transcription(
            message=message,
            status_msg=status_msg,
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            user_model=user_model,
        )
    except Exception as e:
        logger.error(f"Error handling media message: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ *Error processing media.*", parse_mode="Markdown")
        except Exception:
            pass
    finally:
        for path in [temp_input_path, temp_wav_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove temp file {path}: {e}")


# Handler for "Summarize" callback button
@dp.callback_query(F.data.startswith("sum:"))
async def handle_summarize_callback(callback_query: types.CallbackQuery):
    await callback_query.answer("⏳ Processing text...", show_alert=False)
    
    text_hash = callback_query.data.split(":")[1]
    
    # Load full transcription from memory cache (or fallback to message text)
    original_text = load_transcription(text_hash)
    if not original_text:
        logger.warning(f"Saved transcription text for hash {text_hash} not found. Using fallback message text.")
        original_text = callback_query.message.text
        
    if not original_text or original_text == "[No speech detected]":
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        return
    
    # Remove button from the original message immediately
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    # Send typing action
    await callback_query.message.bot.send_chat_action(
        chat_id=callback_query.message.chat.id, 
        action="typing"
    )
    
    fallback_summary_msg = None
    draft_state = {"supported": True}
    
    try:
        full_summary = ""
        current_chunk_text = ""
        last_sent_text = ""
        last_edit_time = 0
        edit_interval = 1.5
        
        async for chunk in summarize_and_clean_text_stream(original_text):
            if not chunk:
                continue
            full_summary += chunk
            current_chunk_text += chunk
            
            # If chunk exceeds message limit, split it
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

            # Throttled stream update
            now = time.monotonic()
            if now - last_edit_time >= edit_interval and current_chunk_text.strip():
                if current_chunk_text != last_sent_text:
                    fallback_summary_msg = await update_message_stream(
                        bot, callback_query.message.chat.id, 2, current_chunk_text, fallback_summary_msg, draft_state
                    )
                    last_sent_text = current_chunk_text
                    last_edit_time = now
                
        # Final update to save the summary permanently in the chat
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
                
    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        if fallback_summary_msg:
            try:
                await fallback_summary_msg.edit_text("❌ *Error generating summary.*", parse_mode="Markdown")
            except Exception:
                pass
        else:
            try:
                await callback_query.message.reply("❌ *Error generating summary.*")
            except Exception:
                pass
        
        # Restore button if it failed
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Clean & Summarize (Error - retry)", callback_data=f"sum:{text_hash}")
        try:
            await callback_query.message.edit_reply_markup(reply_markup=builder.as_markup())
        except TelegramAPIError:
            pass

# Main function to start polling
async def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
        logger.critical("TELEGRAM_TOKEN is not set or has placeholder value. Exiting.")
        sys.exit(1)
        
    if not ALLOWED_USERS:
        logger.critical("ALLOWED_USERS is empty or not set. Exiting.")
        sys.exit(1)
        
    logger.info("Starting Telegram voice/video note transcription bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import sys
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
