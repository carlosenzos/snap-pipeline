from __future__ import annotations

import logging

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/script", tags=["script-editor"])

SCRIPT_TTL = 604800  # 7 days


async def _get_redis() -> aioredis.Redis:
    s = get_settings()
    return aioredis.from_url(s.redis_url, decode_responses=True)


async def _get_card_name(card_id: str) -> str:
    """Fetch card name from Trello API."""
    settings = get_settings()
    if not settings.trello_api_key or not settings.trello_token:
        return "Unknown Card"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.trello.com/1/cards/{card_id}",
                params={
                    "key": settings.trello_api_key,
                    "token": settings.trello_token,
                    "fields": "name",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("name", "Unknown Card")
    except Exception as e:
        logger.warning("Failed to fetch card name for %s: %s", card_id, e)
        return "Unknown Card"


EDITOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edit Snap Script</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; padding: 24px; }
  .container { max-width: 800px; margin: 0 auto; }
  h1 { font-size: 18px; color: #999; margin-bottom: 4px; }
  .card-title { font-size: 24px; font-weight: 600; margin-bottom: 20px; color: #fff; }
  textarea { width: 100%; min-height: 500px; background: #1a1a1a; color: #e0e0e0; border: 1px solid #333; border-radius: 8px; padding: 16px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 14px; line-height: 1.7; resize: vertical; outline: none; }
  textarea:focus { border-color: #7c3aed; }
  .bar { display: flex; justify-content: space-between; align-items: center; margin-top: 14px; }
  .word-count { color: #666; font-size: 13px; }
  .btn { background: #7c3aed; color: #fff; border: none; padding: 10px 28px; border-radius: 6px; font-size: 14px; font-weight: 500; cursor: pointer; }
  .btn:hover { background: #6d28d9; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; border-radius: 6px; font-size: 14px; display: none; z-index: 100; }
  .toast.ok { background: #065f46; color: #6ee7b7; }
  .toast.err { background: #7f1d1d; color: #fca5a5; }
  .msg { text-align: center; padding: 80px 20px; color: #999; font-size: 16px; }
</style>
</head>
<body>
<div class="container">
  {{BODY}}
</div>
<script>
{{SCRIPT}}
</script>
</body>
</html>"""


EDITOR_BODY = """
<h1>Edit Snap Script</h1>
<div class="card-title">{{CARD_TITLE}}</div>
<textarea id="editor">{{SCRIPT_TEXT}}</textarea>
<div class="bar">
  <span class="word-count" id="wc"></span>
  <button class="btn" id="save" onclick="saveScript()">Save</button>
</div>
<div class="toast" id="toast"></div>
"""

EDITOR_JS = """
const ta = document.getElementById('editor');
const wc = document.getElementById('wc');
const cardId = '{{CARD_ID}}';

function updateWc() {
  const words = ta.value.trim().split(/\\s+/).filter(w => w.length > 0).length;
  wc.textContent = words + ' words';
}
ta.addEventListener('input', updateWc);
updateWc();

function flash(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err');
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

async function saveScript() {
  const btn = document.getElementById('save');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const resp = await fetch('/script/edit/' + cardId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script: ta.value }),
    });
    const data = await resp.json();
    if (resp.ok) {
      flash('Saved (' + data.word_count + ' words)', true);
    } else {
      flash(data.detail || 'Save failed', false);
    }
  } catch (e) {
    flash('Network error', false);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

// Ctrl/Cmd+S to save
document.addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault();
    saveScript();
  }
});
"""


def _render_editor(card_id: str, card_title: str, script_text: str) -> str:
    body = EDITOR_BODY.replace("{{CARD_TITLE}}", card_title).replace(
        "{{SCRIPT_TEXT}}", script_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    js = EDITOR_JS.replace("{{CARD_ID}}", card_id)
    return EDITOR_HTML.replace("{{BODY}}", body).replace("{{SCRIPT}}", js)


def _render_message(text: str) -> str:
    body = f'<div class="msg">{text}</div>'
    return EDITOR_HTML.replace("{{BODY}}", body).replace("{{SCRIPT}}", "")


@router.get("/edit/{card_id}", response_class=HTMLResponse)
async def edit_script_page(card_id: str):
    r = await _get_redis()
    script = await r.get(f"snap:script:{card_id}")

    if script is None:
        return HTMLResponse(_render_message("Script not found &mdash; it may have expired."))

    if script == "1":
        return HTMLResponse(_render_message("Script is being generated&hellip; refresh in a minute."))

    card_title = await _get_card_name(card_id)
    return HTMLResponse(_render_editor(card_id, card_title, script))


@router.post("/edit/{card_id}")
async def save_script(card_id: str, body: dict):
    script = body.get("script", "")
    if not script.strip():
        return JSONResponse({"detail": "Script cannot be empty"}, status_code=400)

    r = await _get_redis()
    existing = await r.get(f"snap:script:{card_id}")
    if existing is None:
        return JSONResponse({"detail": "Script not found in Redis"}, status_code=404)

    await r.set(f"snap:script:{card_id}", script, ex=SCRIPT_TTL)

    word_count = len(script.split())

    # Re-attach script.txt to Trello card + post comment
    settings = get_settings()
    if settings.trello_api_key and settings.trello_token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.trello.com/1/cards/{card_id}/attachments",
                    params={
                        "key": settings.trello_api_key,
                        "token": settings.trello_token,
                    },
                    files={"file": ("script.txt", script.encode(), "text/plain")},
                    timeout=15,
                )
                await client.post(
                    f"https://api.trello.com/1/cards/{card_id}/actions/comments",
                    params={
                        "key": settings.trello_api_key,
                        "token": settings.trello_token,
                        "text": f"**Script Manually Edited** ({word_count} words)",
                    },
                    timeout=15,
                )
        except Exception as e:
            logger.warning("Failed to update Trello for card %s: %s", card_id, e)

    logger.info("Script edited via web: card=%s, %d words", card_id, word_count)
    return {"status": "saved", "word_count": word_count}
