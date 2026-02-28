from __future__ import annotations

import json
import logging
import time

from workers.celery_app import app
from workers.utils import run_async

logger = logging.getLogger(__name__)

SCRIPT_TTL = 604800  # 7 days
AUDIO_TTL = 86400    # 24 hours


@app.task(
    bind=True,
    name="snap.generate_voice",
    max_retries=2,
    default_retry_delay=60,
)
def generate_voice(self, card_id: str, channel_label: str, card_name: str) -> str:
    """Generate voice audio from the approved script using ElevenLabs."""
    import redis as sync_redis

    from config.settings import get_pipeline_config, get_settings
    from services import elevenlabs, trello

    start = time.time()
    config = get_pipeline_config()
    channel = config.get_channel(channel_label)
    settings = get_settings()
    r = sync_redis.from_url(settings.redis_url)

    # Load script from Redis
    script = r.get(f"snap:script:{card_id}")
    if not script:
        raise ValueError(f"Script not found in Redis for card {card_id}")
    if isinstance(script, bytes):
        script = script.decode()

    async def _generate():
        # Add "Generating Voice" label
        await trello.add_label_by_name(card_id, config.label_generating_voice)

        # Generate voice with ElevenLabs
        result = await elevenlabs.generate_voice(
            text=script,
            voice_id=channel.elevenlabs_voice_id,
        )
        return result

    try:
        result = run_async(_generate())
        audio_bytes = result["audio"]
        gen_duration = result["duration_s"]

        # Store audio bytes in Redis (24h TTL)
        r.set(f"snap:audio:{card_id}", audio_bytes, ex=AUDIO_TTL)

        # Update pipeline stats
        stats_raw = r.get(f"snap:stats:{card_id}")
        stats = json.loads(stats_raw) if stats_raw else {}
        stats.update({
            "voice_duration": gen_duration,
            "audio_size_bytes": len(audio_bytes),
        })
        r.set(f"snap:stats:{card_id}", json.dumps(stats), ex=SCRIPT_TTL)

        duration = round(time.time() - start, 1)
        size_mb = len(audio_bytes) / (1024 * 1024)
        logger.info(
            "Voice generated: card=%s | %.1f MB | voice=%s | %.1fs",
            card_id, size_mb, channel.elevenlabs_voice_id, duration,
        )
        return "voice_generated"
    except Exception as exc:
        logger.error("Voice generation failed: %s", exc)
        raise self.retry(exc=exc)


@app.task(
    bind=True,
    name="snap.deliver",
    max_retries=3,
    default_retry_delay=30,
)
def deliver(self, card_id: str, channel_label: str, card_name: str) -> None:
    """Attach voice.mp3 + script.txt to card, move to 'video in edit', mark done."""
    import redis as sync_redis

    from config.settings import get_pipeline_config, get_settings
    from services import trello

    start = time.time()
    config = get_pipeline_config()
    settings = get_settings()
    r = sync_redis.from_url(settings.redis_url)

    # Retrieve audio bytes from Redis
    audio_bytes = r.get(f"snap:audio:{card_id}")
    if not audio_bytes:
        raise ValueError(f"Audio not found in Redis for card {card_id}")

    # Retrieve script from Redis
    script = r.get(f"snap:script:{card_id}")
    if isinstance(script, bytes):
        script = script.decode()

    # Load pipeline stats
    stats_raw = r.get(f"snap:stats:{card_id}")
    stats = json.loads(stats_raw) if stats_raw else {}

    async def _deliver():
        # 1. Attach voice.mp3
        await trello.attach_binary_file(card_id, "voice.mp3", audio_bytes, "audio/mpeg")

        # 2. Attach final script.txt
        if script:
            await trello.attach_text_file(card_id, "script.txt", script)

        # 3. Post completion comment
        audio_mb = len(audio_bytes) / (1024 * 1024)
        word_count = len(script.split()) if script else 0
        await trello.add_comment(
            card_id,
            f"**Snap Delivered**\n\n"
            f"Voice: {audio_mb:.1f} MB | Script: {word_count} words\n"
            f"Ready for video editing.",
        )

        # 4. Move card to "video in edit"
        await trello.move_card_to_list(card_id, config.ready_list)

        # 5. Clean up labels — remove intermediate, add Done
        await trello.remove_label_by_name(card_id, config.trigger_label)
        await trello.remove_label_by_name(card_id, config.label_review)
        await trello.remove_label_by_name(card_id, config.label_approved)
        await trello.remove_label_by_name(card_id, config.label_generating_voice)
        await trello.add_label_by_name(card_id, config.label_done)

    try:
        run_async(_deliver())
        duration = round(time.time() - start, 1)

        # Update final stats
        stats["step_deliver_duration"] = duration
        r.set(f"snap:stats:{card_id}", json.dumps(stats), ex=SCRIPT_TTL)

        # Clean up audio bytes from Redis (attached to card now)
        r.delete(f"snap:audio:{card_id}")

        logger.info("Snap delivered: card=%s | %.1fs", card_id, duration)
    except Exception as exc:
        logger.error("Delivery failed: %s", exc)
        raise self.retry(exc=exc)
