import base64
import json
import logging
import aiohttp
from typing import Optional, List, Dict, Any
from config import (
    GEMINI_API_KEY,
    DEFAULT_GEMINI_MODEL,
    GEMINI_FALLBACK_MODEL,
    GEMINI_SUMMARY_MODEL,
)

logger = logging.getLogger(__name__)

async def fetch_available_models() -> List[Dict[str, Any]]:
    """
    Fetches the list of available models from Google Gemini API.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini API error (Status {response.status}): {error_text}")
            data = await response.json()
            models = []
            for item in data.get("models", []):
                name = item.get("name", "").replace("models/", "")
                display_name = item.get("displayName", name)
                methods = item.get("supportedGenerationMethods", [])
                if "generateContent" in methods or "transcribe" in name.lower() or "interactions" in methods:
                    models.append({
                        "id": name,
                        "displayName": display_name,
                        "description": item.get("description", ""),
                        "inputTokenLimit": item.get("inputTokenLimit", 0),
                        "outputTokenLimit": item.get("outputTokenLimit", 0),
                        "methods": methods
                    })
            return models

async def _stream_gemini_content(model: str, audio_bytes: bytes, mime_type: str):
    """
    Streams audio transcription tokens from Gemini streamGenerateContent endpoint.
    """
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}&alt=sse"
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
                        "Transcribe exactly what is spoken in the original language.\n\n"
                        "CRITICAL: Structure the transcription into continuous, readable text. Use standard single spaces "
                        "between sentences within a paragraph. Insert a double newline '\\n\\n' ONLY between major, "
                        "substantial paragraphs (typically 4-6 sentences long). NEVER insert double newlines '\\n\\n' "
                        "after short single sentences, phrases, or conversational pauses.\n\n"
                        "STT DETAILS & NON-VERBALS:\n"
                        "- If the speaker makes a long pause or hesitates, indicate it with an ellipsis '...'.\n"
                        "- Include non-verbal sounds such as sighs, deep breaths, laughter, etc., "
                        "representing them clearly in square brackets (e.g. [sighs], [sigh], [laughs], [giggles], [gasps], [snorts]).\n\n"
                        "Ensure proper punctuation and capitalization throughout. Do not add comments, headers, or metadata. "
                        "If the audio is silent or contains no speech, respond with '[No speech detected]'."
                    )
                }
            ]
        }
    }
    
    logger.info(f"Initiating transcription stream from Gemini model: {model}...")
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
                        candidates = data.get("candidates", [])
                        if candidates and "content" in candidates[0]:
                            parts = candidates[0]["content"].get("parts", [])
                            for part in parts:
                                if "text" in part:
                                    yield part["text"]
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue

async def _transcribe_interactions(model: str, audio_bytes: bytes, mime_type: str) -> str:
    """
    Calls Interactions API for specialized transcribe models.
    """
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": [
            {
                "type": "audio",
                "data": audio_b64,
                "mime_type": mime_type
            }
        ]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Gemini Interactions API error (Status {response.status}): {error_text}")
            data = await response.json()
            if "output_text" in data:
                return data["output_text"].strip()
            if "steps" in data:
                for step in data["steps"]:
                    if step.get("type") == "model_output" and "text" in step:
                        return step["text"].strip()
                    if "model_output" in step:
                        out = step["model_output"]
                        if isinstance(out, str):
                            return out.strip()
                        if isinstance(out, dict) and "text" in out:
                            return out["text"].strip()
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return str(data)

async def transcribe_audio(audio_bytes: bytes, mime_type: str, model_name: Optional[str] = None) -> str:
    """
    Sends audio bytes to Gemini for verbatim transcription (one-shot).
    """
    model = (model_name or DEFAULT_GEMINI_MODEL).strip()
    full_text = ""
    async for chunk in transcribe_audio_stream(audio_bytes, mime_type, model_name=model):
        full_text += chunk
    return full_text.strip()

async def transcribe_audio_stream(audio_bytes: bytes, mime_type: str, model_name: Optional[str] = None):
    """
    Yields chunks of transcription text from Gemini in real-time.
    If the primary model fails (e.g. rate limit 429 or quota exceeded),
    automatically falls back to GEMINI_FALLBACK_MODEL.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    primary_model = (model_name or DEFAULT_GEMINI_MODEL).strip()
    fallback_model = GEMINI_FALLBACK_MODEL.strip()

    try:
        if "transcribe" in primary_model.lower() and "flash" not in primary_model.lower():
            try:
                text = await _transcribe_interactions(primary_model, audio_bytes, mime_type)
                yield text
                return
            except Exception as e:
                logger.warning(f"Interactions API with {primary_model} failed: {e}. Trying streamGenerateContent...")
                async for chunk in _stream_gemini_content(primary_model, audio_bytes, mime_type):
                    yield chunk
                return
        else:
            async for chunk in _stream_gemini_content(primary_model, audio_bytes, mime_type):
                yield chunk
            return
    except Exception as e:
        if primary_model != fallback_model:
            logger.warning(
                f"Primary model '{primary_model}' transcription failed: {e}. "
                f"Falling back to '{fallback_model}'..."
            )
            async for chunk in _stream_gemini_content(fallback_model, audio_bytes, mime_type):
                yield chunk
        else:
            raise e

async def summarize_and_clean_text(verbatim_text: str, model_name: Optional[str] = None) -> str:
    """
    Cleans up transcription text and generates a structured summary using Gemini (one-shot).
    """
    full_summary = ""
    async for chunk in summarize_and_clean_text_stream(verbatim_text, model_name=model_name):
        full_summary += chunk
    return full_summary.strip()

async def summarize_and_clean_text_stream(verbatim_text: str, model_name: Optional[str] = None):
    """
    Yields chunks of cleaned text and summary from Gemini in real-time.
    Uses GEMINI_SUMMARY_MODEL (text model) to ensure proper summarization and cleanup.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")

    if not verbatim_text or verbatim_text == "[No speech detected]":
        yield "Nothing to clean and summarize."
        return

    model = (model_name or GEMINI_SUMMARY_MODEL).strip()
    if "transcribe" in model.lower() and "flash" not in model.lower():
        model = GEMINI_SUMMARY_MODEL.strip()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}&alt=sse"
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
                        "You are a professional editor. Your task is to clean up a verbatim transcript of a voice message. "
                        "The transcript contains filler words, non-verbal annotations, hesitations, repetitions, and typos.\n\n"
                        "CRITICAL RULES FOR CLEANING TEXT:\n"
                        "1. DO NOT REPHRASE OR REWRITE: Keep the original phrasing, vocabulary, slang, tone, and sentence structure. "
                        "Do not try to make the text sound formal, literary, or like a book. Preserve all specific terms, slang, "
                        "and casual expressions (e.g., keep 'low attention span', 'СДВГ-кид', 'не ебу что ли', 'похуй', 'кид', etc. exactly as they are spoken).\n"
                        "2. WHAT TO REMOVE: Only remove filler words (слова-паразиты: 'типа', 'ну', 'короче', 'условно', 'как бы', 'вот', 'блин', 'собственно'), "
                        "verbal hesitations (e.g. 'а-а-ай', 'э-э'), non-verbal annotations in brackets (e.g. '[вздох]', '[смех]', '[звук]'), and repetitions "
                        "(e.g. change 'одно, одно и то же' to 'одно и то же').\n"
                        "3. PUNCTUATION & TYPOS: Correct minor typos and adjust punctuation so the text flows naturally, without altering the actual spoken words.\n\n"
                        "TASKS:\n"
                        "1. Output the cleaned text following the CRITICAL RULES above under the header '✍️ **Cleaned Text:**'.\n"
                        "2. Provide a concise summary of the key points as a structured bulleted list under the header '📌 **Key Summary Points:**'.\n\n"
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
    
    logger.info(f"Initiating summary stream from Gemini model: {model}...")
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

