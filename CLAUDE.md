# Snap Pipeline

## Working Relationship

**You are the CTO.** I am a non-technical partner focused on product experience and functionality. Your job is to:
- Own all technical decisions and architecture
- Push back on ideas that are technically problematic
- Find the best long-term solutions, not quick hacks
- Think through potential technical issues before implementing

### 1. Understand Before Acting
- First think through the problem, read the codebase for relevant files
- Never speculate about code you haven't opened
- If a file is referenced, **READ IT FIRST** before answering

### 2. Check In Before Major Changes
- Before making any major changes, check in with me to verify the plan

### 3. Communicate Clearly
- Every step of the way, provide a high-level explanation of what changes were made

### 4. Simplicity Above All
- Make every task and code change as simple as possible
- Every change should impact as little code as possible

### 5. Keep This File Updated
- Whenever changes are made to the project, update this CLAUDE.md file

---

## Overview

Automated Snapchat script writing + voice generation pipeline. Turns a Trello card (topic + Snap show label) into a script + voice.mp3, ready for video editing. Separate project from the YouTube pipeline but same Trello board.

## Architecture

```
Trello Board (webhook) → FastAPI Web Service → Celery Worker (Redis broker)
                                                  │
                                                  ├─ write_script() → Claude generates script
                                                  │          → Pipeline PAUSES for review
                                                  │
                                                  ├─ [Comment on card → Claude revises script]
                                                  │  (repeat as many times as needed)
                                                  │
                                                  ├─ "Snap Approved" label added
                                                  ├─ generate_voice() → ElevenLabs TTS
                                                  └─ deliver() → Attach files, move card
```

**Railway services:**
- `snap-web` — FastAPI webhook receiver (`SERVICE_TYPE=web`)
- `snap-worker` — Celery worker (`SERVICE_TYPE=worker`)
- `snap-redis` — Managed Redis plugin

## Pipeline Flow

```
Card has "Show Name (Snap)" label + "snap script" label
  → "Snap: Writing Script" label
  → Read card title + description
  → Extract instructions (non-URL text from description)
  → Fetch links from description (X/Twitter, articles, etc.)
  → Claude writes script (show prompt + web research)
  → Attach script.txt + research.txt
  → "Snap: Script Ready" — PAUSE for review
  → [Revisions via comments / manual edits via /script/edit/{card_id}]
  → "Snap Approved" label added
  → ElevenLabs generates voice.mp3
  → Attach voice.mp3 + script.txt, move to "video in edit"
  → "Snap: Done"
```

## Environment Variables

```
TRELLO_API_KEY=
TRELLO_TOKEN=
TRELLO_BOARD_ID=
TRELLO_WEBHOOK_SECRET=
ANTHROPIC_API_KEY=
ELEVENLABS_API_KEY=
REDIS_URL=              # auto-set by Railway Redis
CHANNELS_SHEET_ID=      # published Google Sheet
PIPELINE_WEB_URL=       # for /script/edit links
```

## Project Structure

```
snap-pipeline/
├── api/
│   ├── main.py              # FastAPI: Trello webhook + /health + /admin
│   ├── trello_auth.py       # HMAC-SHA1 signature verification
│   └── script_editor.py     # Web editor for script review/revision
├── workers/
│   ├── celery_app.py        # Celery config (Redis broker)
│   ├── utils.py             # run_async() helper
│   └── tasks/
│       ├── pipeline.py      # Chain orchestrator + error callback
│       ├── script.py        # Claude script generation + revisions
│       └── deliver.py       # ElevenLabs voice generation + card delivery
├── services/
│   ├── trello.py            # Trello API client (includes attach_binary_file)
│   ├── claude.py            # Claude Opus script writer + Sonnet revisions
│   ├── research.py          # URL fetching + description parsing
│   └── elevenlabs.py        # ElevenLabs TTS API
├── config/
│   └── settings.py          # pydantic-settings + Google Sheet loader
├── main.py                  # Railway entry point (SERVICE_TYPE dispatcher)
├── Procfile                 # web: python main.py
└── requirements.txt
```

## Label Lifecycle

| Label | Set by | Purpose |
|---|---|---|
| `Show Name (Snap)` | User | Identifies the show |
| `snap script` | User | Trigger |
| `Snap: Writing Script` | Script task | In progress |
| `Snap: Script Ready` | Script task | Awaiting review |
| `Snap Approved` | User | Triggers voice generation |
| `Snap: Generating Voice` | Voice task | ElevenLabs in progress |
| `Snap: Done` | Deliver task | Complete |
| `Snap: Error` | Error handler | Failed |

## Redis Keys

| Key | TTL | Purpose |
|---|---|---|
| `snap:script:{card_id}` | 7 days | Script text + idempotency |
| `snap:voice:{card_id}` | 24h | Voice pipeline idempotency |
| `snap:audio:{card_id}` | 24h | Raw MP3 bytes |
| `snap:stats:{card_id}` | 7 days | Pipeline stats |

## Google Sheet Config

Same sheet as YouTube pipeline. Snap shows identified by `(Snap)` suffix in Channel Name.

| Channel Name | Voice ID | Category | Discord Role ID | Prompt |
|---|---|---|---|---|
| Show Name (Snap) | voice_abc123 | | | Snap show prompt... |

## Key Design Decisions

- **Same patterns as YouTube pipeline** — FastAPI + Celery + Redis, same Trello board
- **No Discord notifications** — Snap pipeline is simpler, no designer pings needed
- **ElevenLabs TTS** — Voice generated directly (not via video tool)
- **`attach_binary_file()`** — New Trello method for uploading MP3 files
- **`get_snap_channel()`** — Finds label ending with `(Snap)` matching a registered show
- **Streaming Claude API** — Required for extended thinking + 40k max tokens
- **Split pipeline with human review** — Script writes automatically, then pauses for review/revisions before voice generation
- **Google Sheet as primary config** — No redeployment needed to add shows

## Claude Script Writing Settings

| Setting | Value |
|---------|-------|
| Model | `claude-opus-4-6` |
| `max_tokens` | `40000` |
| `budget_tokens` | `16000` |
| Web Search | `max_uses: 5` |
| Streaming | Required |
| Revision Model | `claude-sonnet-4-5-20250929` |

## ElevenLabs Settings

| Setting | Value |
|---------|-------|
| Model | `eleven_multilingual_v2` |
| Format | `mp3_44100_128` |
| Timeout | 300s |

## Adding a New Snap Show

1. Add a row in the Google Sheet with channel name ending in `(Snap)`, voice ID, and prompt
2. Create matching label on the Trello board
3. Done — config refreshes every 5 minutes

## Trello Webhook Registration

After deploying and setting env vars:
```bash
curl -X POST "https://api.trello.com/1/webhooks/" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "<TRELLO_API_KEY>",
    "callbackURL": "https://<SNAP_WEB_URL>/webhooks/trello",
    "idModel": "<BOARD_ID>",
    "description": "Snap Pipeline",
    "token": "<TRELLO_TOKEN>"
  }'
```

## Deployment

```bash
railway up --service snap-web
railway up --service snap-worker
```
