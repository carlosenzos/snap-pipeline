"""ElevenLabs TTS API client for voice generation."""
from __future__ import annotations

import logging
import time

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elevenlabs.io/v1"
GENERATE_TIMEOUT = 300  # 5 minutes — long scripts can take a while


async def generate_voice(text: str, voice_id: str) -> dict:
    """Generate speech audio from text using ElevenLabs TTS.

    Args:
        text: The script text to convert to speech.
        voice_id: The ElevenLabs voice ID to use.

    Returns:
        Dict with "audio" (raw MP3 bytes) and "duration_s" (generation time).
    """
    s = get_settings()
    start = time.time()

    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": s.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "output_format": "mp3_44100_128",
            },
        )
        resp.raise_for_status()

    elapsed = round(time.time() - start, 1)
    audio_bytes = resp.content
    size_mb = len(audio_bytes) / (1024 * 1024)

    logger.info(
        "Voice generated: %d bytes (%.1f MB) | voice=%s | %.1fs",
        len(audio_bytes), size_mb, voice_id, elapsed,
    )

    return {"audio": audio_bytes, "duration_s": elapsed}
