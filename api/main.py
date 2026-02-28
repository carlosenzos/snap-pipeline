from __future__ import annotations

import logging

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response

from api.script_editor import router as script_router
from api.trello_auth import verify_trello_signature
from config.settings import get_pipeline_config, get_settings

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Snap Pipeline")
app.include_router(script_router)

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        s = get_settings()
        _redis = aioredis.from_url(s.redis_url, decode_responses=True)
    return _redis


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/reset-card/{card_id}")
async def reset_card(card_id: str):
    """Clear idempotency keys so a card can be re-processed."""
    r = await get_redis()
    d1 = await r.delete(f"snap:script:{card_id}")
    d2 = await r.delete(f"snap:voice:{card_id}")
    d3 = await r.delete(f"snap:audio:{card_id}")
    return {"status": "cleared", "card_id": card_id, "keys_deleted": d1 + d2 + d3}


@app.head("/webhooks/trello")
async def trello_validation():
    """Trello requires a 200 on HEAD to validate the webhook callback URL."""
    return Response(status_code=200)


@app.post("/webhooks/trello")
async def trello_webhook(request: Request):
    body = await request.body()

    # 1. Verify HMAC-SHA1 signature
    signature = request.headers.get("x-trello-webhook", "")
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    callback_url = f"{proto}://{host}{request.url.path}"
    logger.info("Webhook callback_url=%s signature=%s", callback_url, signature[:20] if signature else "NONE")
    if not verify_trello_signature(body, signature, callback_url):
        logger.warning("Invalid Trello webhook signature, callback_url=%s", callback_url)
        return Response(status_code=401)

    payload = await request.json()
    action = payload.get("action", {})
    action_type = action.get("type", "")

    # 2. Only process relevant action types
    relevant_types = {"addLabelToCard", "commentCard"}
    logger.info("Webhook action_type=%s", action_type)
    if action_type not in relevant_types:
        return {"status": "ignored", "reason": f"action_type={action_type}"}

    # 3. Extract card info
    card_data = action.get("data", {}).get("card", {})
    card_id = card_data.get("id")
    card_name = card_data.get("name", "")
    if not card_id:
        return {"status": "ignored", "reason": "no card_id"}

    # 4. Fetch current labels from Trello API
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.trello.com/1/cards/{card_id}",
            params={
                "key": settings.trello_api_key,
                "token": settings.trello_token,
                "fields": "labels,name",
            },
        )
        resp.raise_for_status()
        full_card = resp.json()

    card_labels_raw = full_card.get("labels", [])
    card_name = full_card.get("name", card_name)
    label_names = {l.get("name", "").lower() for l in card_labels_raw if l.get("name")}
    logger.info("Card=%s labels=%s", card_name, label_names)

    config = get_pipeline_config()

    # --- ROUTE A: Comment on card in review → Script revision ---
    if action_type == "commentCard":
        comment_text = action.get("data", {}).get("text", "")

        # Skip bot comments (our bot always uses **bold** prefix)
        if comment_text.startswith("**"):
            return {"status": "ignored", "reason": "bot comment"}

        # Only revise if card is in review state
        if config.label_review.lower() not in label_names:
            return {"status": "ignored", "reason": "card not in review"}

        channel_label = config.get_snap_channel(label_names)
        if not channel_label:
            return {"status": "ignored", "reason": "no snap channel label"}

        channel_config = config.get_channel(channel_label)
        logger.info("Revision requested for card=%s: %s", card_id, comment_text[:100])

        from workers.tasks.script import revise_script
        revise_script.delay(card_id, channel_config.name, card_name, comment_text)
        return {"status": "revision_queued", "card_id": card_id}

    # --- ROUTE B: "Snap Approved" label → Start voice pipeline ---
    added_label = action.get("data", {}).get("label", {}).get("name", "")
    if added_label.lower() == config.label_approved.lower():
        channel_label = config.get_snap_channel(label_names)
        if not channel_label:
            return {"status": "ignored", "reason": "no snap channel label"}

        r = await get_redis()
        already = await r.set(f"snap:voice:{card_id}", "1", nx=True, ex=86400)
        if not already:
            return {"status": "ignored", "reason": "voice already processing"}

        channel_config = config.get_channel(channel_label)
        logger.info("Snap approved, starting voice pipeline for card=%s", card_id)

        from workers.tasks.pipeline import start_voice_pipeline
        start_voice_pipeline(card_id, channel_config.name, card_name)
        return {"status": "voice_enqueued", "card_id": card_id}

    # --- ROUTE C: Trigger label + (Snap) channel → Start script pipeline ---
    trigger = config.trigger_label.lower()
    if trigger not in label_names:
        logger.info("No trigger label '%s' in %s", trigger, label_names)
        return {"status": "ignored", "reason": "no trigger label"}

    channel_label = config.get_snap_channel(label_names)
    if not channel_label:
        logger.info("No (Snap) channel label found in %s", label_names)
        return {"status": "ignored", "reason": "no snap channel label"}

    # Idempotency check
    r = await get_redis()
    already = await r.set(f"snap:script:{card_id}", "1", nx=True, ex=86400)
    if not already:
        return {"status": "ignored", "reason": "already processing"}

    channel_config = config.get_channel(channel_label)
    logger.info(
        "Starting snap script pipeline for card=%s channel=%s topic=%s",
        card_id, channel_config.name, card_name,
    )

    from workers.tasks.pipeline import start_script_pipeline
    start_script_pipeline(card_id, channel_config.name, card_name)
    return {"status": "script_enqueued", "card_id": card_id, "channel": channel_config.name}
