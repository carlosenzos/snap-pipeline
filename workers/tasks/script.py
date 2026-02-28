from __future__ import annotations

import json
import logging
import time

from workers.celery_app import app
from workers.utils import run_async

logger = logging.getLogger(__name__)

SCRIPT_TTL = 604800  # 7 days


@app.task(
    bind=True,
    name="snap.write_script",
    max_retries=3,
    default_retry_delay=60,
)
def write_script(self, card_id: str, channel_label: str, card_name: str) -> str:
    """Research topic, generate script with Claude, attach to Trello card.

    After completion, pipeline pauses for human review. User adds 'Snap Approved'
    label to continue to voice generation.
    """
    from config.settings import get_pipeline_config
    from services import claude, trello
    from services.research import prepare_context

    start = time.time()
    config = get_pipeline_config()
    channel = config.get_channel(channel_label)

    # Guard: skip if card already has a script or is done (stale task in queue)
    skip_labels = {
        config.label_review.lower(),
        config.label_approved.lower(),
        config.label_generating_voice.lower(),
        config.label_done.lower(),
    }
    try:
        card_labels = run_async(trello.get_card_labels(card_id))
        current_labels = {l.get("name", "").lower() for l in card_labels}
        overlap = current_labels & skip_labels
        if overlap:
            logger.info(
                "Skipping stale write_script for card=%s — already has: %s",
                card_id, overlap,
            )
            return "skipped"
    except Exception as e:
        logger.warning("Could not check card labels (proceeding): %s", e)

    async def _write():
        # 1. Add "Writing Script" label
        await trello.add_label_by_name(card_id, config.label_writing)

        # 2. Fetch card description
        card = await trello.get_card(card_id)
        card_desc = card.get("desc", "")

        # 3. Parse description: extract instructions + fetch linked articles
        context = await prepare_context(card_desc)
        instructions = context["instructions"]
        articles = context["articles"]

        if instructions:
            logger.info("Card instructions: %s", instructions[:200])
        if articles:
            logger.info("Fetched %d article(s) from description links", len(articles))

        # 4. Fetch card attachments (images for Claude)
        attachments = await trello.get_card_attachments(card_id)
        image_attachments = []
        for att in attachments:
            mime = att.get("mimeType", "") or ""
            att_url = att.get("url", "")
            att_name = att.get("name", "")
            if mime.startswith("image/") and att_url:
                image_attachments.append({"name": att_name, "url": att_url})
                logger.info("Found image attachment: %s", att_name)

        # 5. Generate script with web search + research context + images
        system_prompt = channel.load_prompt()
        system_prompt = system_prompt.replace("INSERT TITLE", card_name)

        result = await claude.write_script(
            system_prompt,
            card_name,
            instructions=instructions,
            articles=articles,
            image_urls=image_attachments if image_attachments else None,
        )
        script = result["script"]
        claude_stats = result["stats"]
        research_log = result["research"]

        # 6. Attach script + research log to Trello card
        await trello.attach_text_file(card_id, "script.txt", script)
        await trello.attach_text_file(card_id, "research.txt", research_log)

        preview = script[:500] + ("..." if len(script) > 500 else "")
        sources = f" | {len(articles)} article(s) fetched" if articles else ""
        images = f" | {len(image_attachments)} image(s)" if image_attachments else ""

        from config.settings import get_settings as _get_settings
        _web_url = _get_settings().pipeline_web_url.rstrip("/")
        edit_link = f"\n\n[Edit script]({_web_url}/script/edit/{card_id})" if _web_url else ""

        await trello.add_comment(
            card_id,
            f"**Snap Script Generated** ({claude_stats['word_count']} words, "
            f"{claude_stats['char_count']} chars)\n"
            f"Cost: ${claude_stats['cost_usd']:.2f} | "
            f"Tokens: {claude_stats['input_tokens']} in / {claude_stats['output_tokens']} out | "
            f"Time: {claude_stats['duration_s']}s{sources}{images}\n\n"
            f"Review the script and add **Snap Approved** label when ready.{edit_link}\n\n{preview}",
        )

        # 7. Pause pipeline — remove Writing, add Script Ready
        await trello.remove_label_by_name(card_id, config.label_writing)
        await trello.add_label_by_name(card_id, config.label_review)

        return script, claude_stats

    try:
        script, claude_stats = run_async(_write())
        duration = round(time.time() - start, 1)

        # Store script in Redis (7-day TTL for review period)
        import redis as sync_redis
        from config.settings import get_settings
        settings = get_settings()
        r = sync_redis.from_url(settings.redis_url)
        r.set(f"snap:script:{card_id}", script, ex=SCRIPT_TTL)

        # Update pipeline stats
        stats_raw = r.get(f"snap:stats:{card_id}")
        stats = json.loads(stats_raw) if stats_raw else {}
        stats.update({
            "step_script_duration": duration,
            "script_word_count": claude_stats["word_count"],
            "script_char_count": claude_stats["char_count"],
            "script_input_tokens": claude_stats["input_tokens"],
            "script_output_tokens": claude_stats["output_tokens"],
            "script_cost_usd": claude_stats["cost_usd"],
            "claude_duration": claude_stats["duration_s"],
        })
        r.set(f"snap:stats:{card_id}", json.dumps(stats), ex=SCRIPT_TTL)

        logger.info(
            "Script done: card=%s | %d words, %d chars | $%.2f | %.1fs total (%.1fs Claude)",
            card_id, claude_stats["word_count"], claude_stats["char_count"],
            claude_stats["cost_usd"], duration, claude_stats["duration_s"],
        )
        return script
    except Exception as exc:
        logger.error("Script writing failed: %s", exc)
        raise self.retry(exc=exc)


@app.task(
    bind=True,
    name="snap.revise_script",
    max_retries=2,
    default_retry_delay=30,
)
def revise_script(self, card_id: str, channel_label: str, card_name: str, comment_text: str) -> str:
    """Revise the current script based on a Trello comment (producer feedback)."""
    from config.settings import get_pipeline_config, get_settings
    from services import claude, trello

    start = time.time()
    config = get_pipeline_config()
    channel = config.get_channel(channel_label)

    async def _revise():
        import redis as sync_redis
        settings = get_settings()
        r = sync_redis.from_url(settings.redis_url)

        current_script = r.get(f"snap:script:{card_id}")
        if not current_script:
            raise ValueError("Script not found in Redis — may have expired")
        if isinstance(current_script, bytes):
            current_script = current_script.decode()

        system_prompt = channel.load_prompt()
        system_prompt = system_prompt.replace("INSERT TITLE", card_name)

        result = await claude.revise_script(
            system_prompt,
            current_script,
            comment_text,
        )
        revised_script = result["script"]
        stats = result["stats"]

        r.set(f"snap:script:{card_id}", revised_script, ex=SCRIPT_TTL)

        await trello.attach_text_file(card_id, "script.txt", revised_script)

        from config.settings import get_settings as _get_settings
        _web_url = _get_settings().pipeline_web_url.rstrip("/")
        edit_link = f"\n\n[Edit script]({_web_url}/script/edit/{card_id})" if _web_url else ""

        await trello.add_comment(
            card_id,
            f"**Snap Script Revised** ({stats['word_count']} words)\n"
            f"Cost: ${stats['cost_usd']:.4f} | Time: {stats['duration_s']}s\n\n"
            f"Review and add **Snap Approved** label when ready.{edit_link}",
        )

        return revised_script, stats

    try:
        revised_script, stats = run_async(_revise())
        duration = round(time.time() - start, 1)

        logger.info(
            "Revision done: card=%s | %d words | $%.4f | %.1fs",
            card_id, stats["word_count"], stats["cost_usd"], duration,
        )
        return revised_script
    except Exception as exc:
        logger.error("Revision failed: %s", exc)
        raise self.retry(exc=exc)
