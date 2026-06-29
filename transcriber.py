import base64
import json
import logging
import aiohttp
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

async def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """
    Sends audio bytes to Gemini for verbatim transcription (one-shot).
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
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
                        "You are a professional speech-to-text transcriber. Your task is to transcribe the provided "
                        "audio verbatim, word-for-word, without any editing, summarizing, or omissions. "
                        "Transcribe exactly what is spoken in the original language. "
                        "For readability, format the output with proper punctuation, capitalization, and logical paragraph breaks. "
                        "Do not add comments, headers, or metadata. If the audio is silent or contains no speech, "
                        "respond with '[No speech detected]'."
                    )
                }
            ]
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
            result = await response.json()
            try:
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                raise RuntimeError("Failed to parse transcription from Gemini response.")

async def transcribe_audio_stream(audio_bytes: bytes, mime_type: str):
    """
    Yields chunks of transcription text from Gemini in real-time.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    # Use streamGenerateContent with alt=sse for streaming
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?key={GEMINI_API_KEY}&alt=sse"
    headers = {"Content-Type": "application/json"}
    
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
                        "You are a professional speech-to-text transcriber. Your task is to transcribe the provided "
                        "audio verbatim, word-for-word, without any editing, summarizing, or omissions. "
                        "Transcribe exactly what is spoken in the original language. "
                        "For readability, format the output with proper punctuation, capitalization, and logical paragraph breaks. "
                        "Do not add comments, headers, or metadata. If the audio is silent or contains no speech, "
                        "respond with '[No speech detected]'."
                    )
                }
            ]
        }
    }
    
    logger.info(f"Initiating transcription stream from Gemini model: {GEMINI_MODEL}...")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
                
            async for line in response.content:
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data:"):
                    data_json = line_str[len("data:"):].strip()
                    if not data_json:
                        continue
                    try:
                        data = json.loads(data_json)
                        chunk_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        yield chunk_text
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue

async def summarize_and_clean_text(verbatim_text: str) -> str:
    """
    Cleans up transcription text and generates a structured summary using Gemini (one-shot).
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    if not verbatim_text or verbatim_text == "[No speech detected]":
        return "Nothing to clean and summarize."

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
                        "You are a professional editor. You are provided with a verbatim transcript of a voice message "
                        "(which may contain typos, repetitions, and filler words like 'uh', 'um', 'like', 'well').\n\n"
                        "Perform the following tasks:\n"
                        "1. Clean up the text by removing filler words, repetitions, stuttering, and fluff. Make it "
                        "grammatically correct, coherent, and easy to read, while preserving the author's language, "
                        "style, and key details.\n"
                        "2. Provide a concise summary of the key points as a structured bulleted list.\n\n"
                        "Ensure that your response is written in the same language as the provided transcript.\n\n"
                        "Format your response exactly as follows:\n"
                        "✍️ **Cleaned Text:**\n"
                        "[cleaned text]\n\n"
                        "📌 **Key Summary Points:**\n"
                        "- [point 1]\n"
                        "- [point 2]\n\n"
                        "Do not add any greetings, preambles, or extra comments."
                    )
                }
            ]
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
            result = await response.json()
            try:
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                raise RuntimeError("Failed to parse summary from Gemini response.")

async def summarize_and_clean_text_stream(verbatim_text: str):
    """
    Yields chunks of cleaned text and summary from Gemini in real-time.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    if not verbatim_text or verbatim_text == "[No speech detected]":
        yield "Nothing to clean and summarize."
        return

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?key={GEMINI_API_KEY}&alt=sse"
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
                        "You are a professional editor. You are provided with a verbatim transcript of a voice message "
                        "(which may contain typos, repetitions, and filler words like 'uh', 'um', 'like', 'well').\n\n"
                        "Perform the following tasks:\n"
                        "1. Clean up the text by removing filler words, repetitions, stuttering, and fluff. Make it "
                        "grammatically correct, coherent, and easy to read, while preserving the author's language, "
                        "style, and key details.\n"
                        "2. Provide a concise summary of the key points as a structured bulleted list.\n\n"
                        "Ensure that your response is written in the same language as the provided transcript.\n\n"
                        "Format your response exactly as follows:\n"
                        "✍️ **Cleaned Text:**\n"
                        "[cleaned text]\n\n"
                        "📌 **Key Summary Points:**\n"
                        "- [point 1]\n"
                        "- [point 2]\n\n"
                        "Do not add any greetings, preambles, or extra comments."
                    )
                }
            ]
        }
    }
    
    logger.info(f"Initiating summary stream from Gemini model: {GEMINI_MODEL}...")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
                
            async for line in response.content:
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data:"):
                    data_json = line_str[len("data:"):].strip()
                    if not data_json:
                        continue
                    try:
                        data = json.loads(data_json)
                        chunk_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        yield chunk_text
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
