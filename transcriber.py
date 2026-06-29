import base64
import json
import logging
import aiohttp
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

async def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """
    Sends audio bytes to Gemini for verbatim transcription.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    # Base64-encode the audio bytes
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    # We pass the transcription instruction as a system instruction to make sure 
    # the model strictly returns the verbatim words of the speaker without edits.
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": audio_b64
                        }
                    }
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Ты — профессиональный инструмент для распознавания речи. Твоя единственная задача — "
                        "расшифровать предоставленный аудиофайл дословно, слово в слово, без каких-либо сокращений, "
                        "обобщений, форматирования, удаления слов-паразитов или исправлений ошибок. Запиши ровно те "
                        "слова, которые были произнесены, на том языке, на котором они звучат. Не добавляй никаких "
                        "комментариев от себя, не придумывай заголовки, не вводи разметку. Если в аудиозаписи "
                        "тишина или нет речи, ответь просто '[Речь отсутствует]'."
                    )
                }
            ]
        }
    }
    
    logger.info(f"Sending transcription request to Gemini model: {GEMINI_MODEL}...")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Gemini API returned status {response.status}: {error_text}")
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
            
            result = await response.json()
            try:
                text_content = result["candidates"][0]["content"]["parts"][0]["text"]
                return text_content.strip()
            except (KeyError, IndexError) as e:
                logger.error(f"Unexpected response payload structure: {result}. Error: {e}")
                raise RuntimeError("Failed to parse transcription from Gemini response.")

async def summarize_and_clean_text(verbatim_text: str) -> str:
    """
    Cleans up transcription text and generates a structured summary using Gemini.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    if not verbatim_text or verbatim_text == "[Речь отсутствует]":
        return "Нечего очищать и саммаризовать."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": verbatim_text
                    }
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Ты — профессиональный редактор. Тебе предоставлен дословный текст расшифровки "
                        "аудиосообщения (возможно, с опечатками, повторами и словами-паразитами вроде 'эээ', "
                        "'ну', 'как бы', 'так сказать').\n\n"
                        "Сделай следующее:\n"
                        "1. Очисти текст от слов-паразитов, повторов, заиканий и лишней воды. Сделай его "
                        "грамматически правильным, связным, структурированным и легкочитаемым, сохранив "
                        "при этом стиль автора и все важные детали.\n"
                        "2. Ниже напиши краткую выжимку (саммари) с главными мыслями в виде структурированного "
                        "маркированного списка.\n\n"
                        "Формат ответа:\n"
                        "✍️ **Очищенный текст:**\n"
                        "[чистый текст]\n\n"
                        "📌 **Главные мысли (Саммари):**\n"
                        "- [мысль 1]\n"
                        "- [мысль 2]\n\n"
                        "Отвечай строго в указанном формате на русском языке, не добавляй никаких лишних приветствий "
                        "или мета-комментариев от себя."
                    )
                }
            ]
        }
    }
    
    logger.info(f"Sending cleanup/summary request to Gemini model: {GEMINI_MODEL}...")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Gemini API returned status {response.status}: {error_text}")
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
            
            result = await response.json()
            try:
                text_content = result["candidates"][0]["content"]["parts"][0]["text"]
                return text_content.strip()
            except (KeyError, IndexError) as e:
                logger.error(f"Unexpected response payload structure: {result}. Error: {e}")
                raise RuntimeError("Failed to parse summary from Gemini response.")
