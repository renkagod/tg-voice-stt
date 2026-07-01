import os
import asyncio
import logging
import tempfile
import hashlib
import time
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from config import TELEGRAM_TOKEN, ALLOWED_USERS
from transcriber import transcribe_audio_stream, summarize_and_clean_text_stream

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

# Helper functions for handling state
def get_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def save_transcription(text_hash: str, text: str):
    path = os.path.join(tempfile.gettempdir(), f"trans_{text_hash}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def load_transcription(text_hash: str) -> str:
    path = os.path.join(tempfile.gettempdir(), f"trans_{text_hash}.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

from typing import Union, Optional
from aiogram.methods.base import TelegramMethod

class SendMessageDraft(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "sendMessageDraft"
    
    chat_id: Union[int, str]
    draft_id: int
    text: str
    parse_mode: Optional[str] = None

# Helper for native streaming with editMessageText fallback
async def update_message_stream(bot: Bot, chat_id: int, draft_id: int, text: str, fallback_msg=None):
    """
    Attempts to stream the message using the new native sendMessageDraft method.
    Falls back to classical editMessageText if not supported or fails.
    """
    try:
        # Call the new Bot API method using aiogram's standard call mechanism
        await bot(SendMessageDraft(chat_id=chat_id, draft_id=draft_id, text=text))
        return None  # Native streaming active, no fallback message needed yet
    except Exception as e:
        logger.warning(f"sendMessageDraft failed: {e}. Falling back to editMessageText.")
        if fallback_msg:
            try:
                await fallback_msg.edit_text(text)
                return fallback_msg
            except Exception:
                return fallback_msg
        else:
            try:
                msg = await bot.send_message(chat_id=chat_id, text=text)
                return msg
            except Exception:
                return None

# Handler for Voice Messages
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        logger.info(f"Ignoring voice message because it is a reply to the bot's own message.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    voice = message.voice
    logger.info(f"Received voice message from user {message.from_user.id}. File ID: {voice.file_id}")
    
    # Send immediate loading status message
    status_msg = await message.reply("⏳ *Downloading audio...*", parse_mode="Markdown")
    
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_ogg:
        temp_ogg_path = temp_ogg.name
    
    try:
        # Download
        file_info = await bot.get_file(voice.file_id)
        await bot.download_file(file_info.file_path, temp_ogg_path)
        
        # Update status
        await status_msg.edit_text("🎙️ *Transcribing audio... (Gemini)*", parse_mode="Markdown")
        
        # Read the audio bytes
        with open(temp_ogg_path, "rb") as f:
            audio_bytes = f.read()
        
        # Stream transcription to Telegram
        full_text = ""
        current_message_text = ""
        fallback_msg = None
        last_edit_time = 0
        edit_interval = 1.5  # Rate limit safety buffer
        status_deleted = False
        
        async for chunk in transcribe_audio_stream(audio_bytes, mime_type="audio/ogg"):
            if not chunk:
                continue
                
            # Delete loading status on first text chunk
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
                if fallback_msg:
                    try:
                        await fallback_msg.edit_text(current_message_text[:4000])
                    except Exception:
                        pass
                else:
                    await message.reply(current_message_text[:4000])
                    
                current_message_text = current_message_text[4000:]
                fallback_msg = None
                last_edit_time = 0
                
            # Throttled edit
            now = time.monotonic()
            current_interval = 1.5
            if now - last_edit_time >= current_interval:
                if current_message_text.strip():
                    fallback_msg = await update_message_stream(
                        bot, message.chat.id, 1, current_message_text, fallback_msg
                    )
                    last_edit_time = now
        
        # Cleanup status if it wasn't deleted (e.g., empty or silent audio)
        if not status_deleted:
            try:
                await status_msg.delete()
            except Exception:
                pass
                
        # Send the final, persistent message (which clears/replaces the draft)
        if current_message_text.strip():
            if fallback_msg:
                try:
                    tg_msg = fallback_msg
                    await tg_msg.edit_text(current_message_text)
                except Exception:
                    tg_msg = await message.reply(current_message_text)
            else:
                tg_msg = await message.reply(current_message_text)
        else:
            tg_msg = fallback_msg
            
        # Fallback if text is empty
        if not full_text.strip():
            full_text = "[No speech detected]"
            if fallback_msg:
                try:
                    await fallback_msg.edit_text(full_text)
                    tg_msg = fallback_msg
                except Exception:
                    tg_msg = await message.reply(full_text)
            else:
                tg_msg = await message.reply(full_text)
                
        # Store full transcription for summary button
        text_hash = get_text_hash(full_text)
        save_transcription(text_hash, full_text)
        
        # Attach button to the last message chunk
        if tg_msg and full_text.strip() != "[No speech detected]":
            builder = InlineKeyboardBuilder()
            builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")
            try:
                await tg_msg.edit_reply_markup(reply_markup=builder.as_markup())
            except Exception as e:
                logger.warning(f"Could not attach reply markup: {e}")
        
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

# Handler for Video Notes ("Circles")
@dp.message(F.video_note)
async def handle_video_note_message(message: types.Message):
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        logger.info(f"Ignoring video note because it is a reply to the bot's own message.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    video_note = message.video_note
    logger.info(f"Received video note from user {message.from_user.id}. File ID: {video_note.file_id}")
    
    # Send immediate loading status message
    status_msg = await message.reply("⏳ *Downloading video note...*", parse_mode="Markdown")
    
    fd_mp4, temp_mp4_path = tempfile.mkstemp(suffix=".mp4")
    fd_wav, temp_wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd_mp4)
    os.close(fd_wav)
    
    try:
        # Download
        file_info = await bot.get_file(video_note.file_id)
        await bot.download_file(file_info.file_path, temp_mp4_path)
        
        # Update status
        await status_msg.edit_text("🎙️ *Extracting and transcribing audio... (Gemini)*", parse_mode="Markdown")
        
        # Extract audio using ffmpeg
        conversion_success = await extract_audio_from_mp4(temp_mp4_path, temp_wav_path)
        
        if conversion_success:
            with open(temp_wav_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            logger.warning("Ffmpeg extraction failed, falling back to direct MP4 send")
            with open(temp_mp4_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "video/mp4"
            
        # Stream transcription to Telegram
        full_text = ""
        current_message_text = ""
        fallback_msg = None
        last_edit_time = 0
        edit_interval = 1.5
        status_deleted = False
        
        async for chunk in transcribe_audio_stream(audio_bytes, mime_type=mime_type):
            if not chunk:
                continue
                
            # Delete loading status on first text chunk
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
                if fallback_msg:
                    try:
                        await fallback_msg.edit_text(current_message_text[:4000])
                    except Exception:
                        pass
                else:
                    await message.reply(current_message_text[:4000])
                    
                current_message_text = current_message_text[4000:]
                fallback_msg = None
                last_edit_time = 0
                
            # Throttled edit
            now = time.monotonic()
            current_interval = 1.5
            if now - last_edit_time >= current_interval:
                if current_message_text.strip():
                    fallback_msg = await update_message_stream(
                        bot, message.chat.id, 1, current_message_text, fallback_msg
                    )
                    last_edit_time = now
        
        # Cleanup status if it wasn't deleted
        if not status_deleted:
            try:
                await status_msg.delete()
            except Exception:
                pass
                
        # Send final message
        if current_message_text.strip():
            if fallback_msg:
                try:
                    tg_msg = fallback_msg
                    await tg_msg.edit_text(current_message_text)
                except Exception:
                    tg_msg = await message.reply(current_message_text)
            else:
                tg_msg = await message.reply(current_message_text)
        else:
            tg_msg = fallback_msg
            
        # Fallback if text is empty
        if not full_text.strip():
            full_text = "[No speech detected]"
            if fallback_msg:
                try:
                    await fallback_msg.edit_text(full_text)
                    tg_msg = fallback_msg
                except Exception:
                    tg_msg = await message.reply(full_text)
            else:
                tg_msg = await message.reply(full_text)
                
        # Store full transcription for summary button
        text_hash = get_text_hash(full_text)
        save_transcription(text_hash, full_text)
        
        # Attach button to the last message chunk
        if tg_msg and full_text.strip() != "[No speech detected]":
            builder = InlineKeyboardBuilder()
            builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")
            try:
                await tg_msg.edit_reply_markup(reply_markup=builder.as_markup())
            except Exception as e:
                logger.warning(f"Could not attach reply markup: {e}")
        
    except Exception as e:
        logger.error(f"Error handling video note message: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ *Error processing video note.*", parse_mode="Markdown")
        except Exception:
            pass
    finally:
        for path in [temp_mp4_path, temp_wav_path]:
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
    
    # Load full transcription
    original_text = load_transcription(text_hash)
    if not original_text:
        logger.warning(f"Saved transcription text file for hash {text_hash} not found. Using fallback message text.")
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
    
    try:
        full_summary = ""
        last_edit_time = 0
        edit_interval = 1.5
        
        async for chunk in summarize_and_clean_text_stream(original_text):
            if not chunk:
                continue
            full_summary += chunk
            
            # Throttled stream update
            now = time.monotonic()
            current_interval = 1.5
            if now - last_edit_time >= current_interval:
                fallback_summary_msg = await update_message_stream(
                    bot, callback_query.message.chat.id, 2, full_summary, fallback_summary_msg
                )
                last_edit_time = now
                
        # Final update to save the summary permanently in the chat
        if fallback_summary_msg:
            try:
                await fallback_summary_msg.edit_text(full_summary, parse_mode="Markdown")
            except TelegramAPIError:
                try:
                    await fallback_summary_msg.edit_text(full_summary, parse_mode=None)
                except TelegramAPIError:
                    pass
        else:
            try:
                await callback_query.message.reply(full_summary, parse_mode="Markdown")
            except TelegramAPIError:
                await callback_query.message.reply(full_summary, parse_mode=None)
                
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
