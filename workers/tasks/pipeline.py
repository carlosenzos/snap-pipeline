from __future__ import annotations

import logging

from celery import chain

from workers.celery_app import app
from workers.tasks.deliver import deliver, generate_voice
from workers.tasks.script import write_script
from workers.utils import run_async

logger = logging.getLogger(__name__)


def start_script_pipeline(card_id: str, channel_label: str, card_name: str) -> None:
    """Write script, then pause for review."""
    pipeline = chain(
        write_script.si(card_id, channel_label, card_name),
    )
    pipeline.apply_async(
        link_error=on_pipeline_error.si(card_id, channel_label, card_name),
    )
    logger.info("Script pipeline dispatched for card=%s", card_id)


def start_voice_pipeline(card_id: str, channel_label: str, card_name: str) -> None:
    """Generate voice + deliver. Triggered by 'Snap Approved' label."""
    pipeline = chain(
        generate_voice.si(card_id, channel_label, card_name),
        deliver.si(card_id, channel_label, card_name),
    )
    pipeline.apply_async(
        link_error=on_pipeline_error.si(card_id, channel_label, card_name),
    )
    logger.info("Voice pipeline dispatched for card=%s", card_id)


@app.task(bind=True, name="snap.on_error")
def on_pipeline_error(self, card_id: str, channel_label: str, card_name: str) -> None:
    """Error callback — fires if any task in the chain fails after retries."""
    from config.settings import get_pipeline_config

    logger.error("Snap pipeline failed for card=%s (%s)", card_id, card_name)

    config = get_pipeline_config()

    parent_id = self.request.parent_id
    error_msg = f"Snap pipeline failed (task: {parent_id}). Check worker logs for details."

    async def _handle_error():
        from services import trello

        await trello.add_label_by_name(card_id, config.label_error)
        await trello.add_comment(
            card_id,
            f"**Snap Pipeline Error**\n\n{error_msg}",
        )

    try:
        run_async(_handle_error())
    except Exception:
        logger.exception("Failed to handle pipeline error for card=%s", card_id)
