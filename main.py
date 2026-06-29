import os
import asyncio
import logging
import tempfile
import hashlib
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

# Helper functions for handling long text and state
def chunk_text(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, limit)
        if split_idx == -1:
            split_idx = limit
            
        chunks.append(text[:split_idx])
        text = text[split_idx:].lstrip()
    if text:
        chunks.append(text)
    return chunks

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

# Handler for Voice Messages
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
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
        
        # Transcribe using Gemini API
        transcription = await transcribe_audio(audio_bytes, mime_type="audio/ogg")
        
        if not transcription:
            transcription = "[No speech detected]"
            
        # Clean up status message before sending results
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        # Store transcription for summary
        text_hash = get_text_hash(transcription)
        save_transcription(text_hash, transcription)
        
        # Chunk text if too long
        chunks = chunk_text(transcription)
        
        # Attach button to the last chunk
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")
        
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await message.reply(chunk, reply_markup=builder.as_markup())
            else:
                await message.reply(chunk)
        
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
            
        # Transcribe using Gemini API
        transcription = await transcribe_audio(audio_bytes, mime_type=mime_type)
        
        if not transcription:
            transcription = "[No speech detected]"
            
        # Clean up status message before sending results
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        # Store transcription for summary
        text_hash = get_text_hash(transcription)
        save_transcription(text_hash, transcription)
        
        # Chunk text if too long
        chunks = chunk_text(transcription)
        
        # Attach button to the last chunk
        builder = InlineKeyboardBuilder()
        builder.button(text="✨ Clean & Summarize", callback_data=f"sum:{text_hash}")
        
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await message.reply(chunk, reply_markup=builder.as_markup())
            else:
                await message.reply(chunk)
        
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
    
    # Remove button from the original message immediately to show it has been processed
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    # Send typing action
    await callback_query.message.bot.send_chat_action(
        chat_id=callback_query.message.chat.id, 
        action="typing"
    )
    
    try:
        # Generate summary and clean text via Gemini
        summary = await summarize_and_clean_text(original_text)
        
        # Send summary as a separate message replying to the transcription
        try:
            await callback_query.message.reply(summary, parse_mode="Markdown")
        except TelegramAPIError as e:
            logger.warning(f"Failed to send summary in Markdown: {e}. Falling back to plain text.")
            await callback_query.message.reply(summary, parse_mode=None)
                
    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        # Restore button if it failed, so user can try again
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
