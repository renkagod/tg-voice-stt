import os
import asyncio
import logging
import tempfile
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from config import TELEGRAM_TOKEN, ALLOWED_USERS
from transcriber import transcribe_audio, summarize_and_clean_text

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Initialize Bot and Dispatcher
# We use DefaultBotProperties for modern aiogram configuration
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

# Handler for Voice Messages
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    # Send a typing/recording action to show responsiveness
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    voice = message.voice
    logger.info(f"Received voice message from user {message.from_user.id}. File ID: {voice.file_id}")
    
    # Download the voice file (OGG)
    file_info = await bot.get_file(voice.file_id)
    
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_ogg:
        temp_ogg_path = temp_ogg.name
    
    try:
        await bot.download_file(file_info.file_path, temp_ogg_path)
        
        # Read the audio bytes
        with open(temp_ogg_path, "rb") as f:
            audio_bytes = f.read()
        
        # Transcribe using Gemini API
        transcription = await transcribe_audio(audio_bytes, mime_type="audio/ogg")
        
        if not transcription:
            transcription = "[Речь отсутствует]"
            
        # Build reply keyboard
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Очистить и саммаризовать", callback_data="summarize")
        
        await message.reply(
            transcription,
            reply_markup=builder.as_markup()
        )
        
    except Exception as e:
        logger.error(f"Error handling voice message: {e}", exc_info=True)
        # In case of failure, don't crash the bot.
    finally:
        # Clean up temp file
        if os.path.exists(temp_ogg_path):
            try:
                os.remove(temp_ogg_path)
            except Exception as e:
                logger.error(f"Failed to remove temp file {temp_ogg_path}: {e}")

# Handler for Video Notes ("Circles")
@dp.message(F.video_note)
async def handle_video_note_message(message: types.Message):
    # Send typing status
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    video_note = message.video_note
    logger.info(f"Received video note from user {message.from_user.id}. File ID: {video_note.file_id}")
    
    file_info = await bot.get_file(video_note.file_id)
    
    # Setup temp paths
    fd_mp4, temp_mp4_path = tempfile.mkstemp(suffix=".mp4")
    fd_wav, temp_wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd_mp4)
    os.close(fd_wav)
    
    try:
        # Download MP4
        await bot.download_file(file_info.file_path, temp_mp4_path)
        
        # Extract audio using ffmpeg
        conversion_success = await extract_audio_from_mp4(temp_mp4_path, temp_wav_path)
        
        if conversion_success:
            # Read extracted WAV bytes
            with open(temp_wav_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            # Fallback to direct MP4 upload if conversion fails
            logger.warning("Ffmpeg extraction failed, falling back to direct MP4 send")
            with open(temp_mp4_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "video/mp4"
            
        # Transcribe using Gemini API
        transcription = await transcribe_audio(audio_bytes, mime_type=mime_type)
        
        if not transcription:
            transcription = "[Речь отсутствует]"
            
        # Build reply keyboard
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Очистить и саммаризовать", callback_data="summarize")
        
        await message.reply(
            transcription,
            reply_markup=builder.as_markup()
        )
        
    except Exception as e:
        logger.error(f"Error handling video note message: {e}", exc_info=True)
    finally:
        # Clean up temp files
        for path in [temp_mp4_path, temp_wav_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove temp file {path}: {e}")

# Handler for "Summarize" callback button
@dp.callback_query(F.data == "summarize")
async def handle_summarize_callback(callback_query: types.CallbackQuery):
    # Prevent double clicks by showing loading state in Telegram UI
    await callback_query.answer("⏳ Обработка текста...", show_alert=False)
    
    original_text = callback_query.message.text
    if not original_text or original_text == "[Речь отсутствует]":
        await callback_query.message.edit_reply_markup(reply_markup=None)
        return
    
    # Temporarily remove button to show loading
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    # Send typing action to the chat
    await callback_query.message.bot.send_chat_action(
        chat_id=callback_query.message.chat.id, 
        action="typing"
    )
    
    try:
        # Generate summary and clean text via Gemini
        summary = await summarize_and_clean_text(original_text)
        
        # Combine the original verbatim text and the summary
        new_text = f"{original_text}\n\n---\n{summary}"
        
        # Telegram limit check
        if len(new_text) <= 4096:
            try:
                await callback_query.message.edit_text(new_text, parse_mode="Markdown")
            except TelegramAPIError as e:
                logger.warning(f"Failed to write markdown summary: {e}. Falling back to plain text.")
                await callback_query.message.edit_text(new_text, parse_mode=None)
        else:
            # If combined is too long, reply to the message with the summary instead of editing
            try:
                await callback_query.message.reply(summary, parse_mode="Markdown")
            except TelegramAPIError:
                await callback_query.message.reply(summary, parse_mode=None)
                
    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        # Restore button if it failed, so user can try again
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Очистить и саммаризовать (Ошибка - попробовать еще раз)", callback_data="summarize")
        try:
            await callback_query.message.edit_reply_markup(reply_markup=builder.as_markup())
        except TelegramAPIError:
            pass

# Main function to start polling
async def main():
    # Make sure we have configuration set
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
        logger.critical("TELEGRAM_TOKEN is not set or has placeholder value. Exiting.")
        sys.exit(1)
        
    if not ALLOWED_USERS:
        logger.critical("ALLOWED_USERS is empty or not set. Exiting.")
        sys.exit(1)
        
    logger.info("Starting Telegram voice/video note transcription bot...")
    
    # Skip pending updates when starting to prevent backlogs
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import sys
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
