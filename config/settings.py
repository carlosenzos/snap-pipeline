from __future__ import annotations

import csv
import io
import logging
import time
from functools import lru_cache

import httpx
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Trello
    trello_api_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    trello_webhook_secret: str = ""

    # Claude (script writing)
    anthropic_api_key: str = ""

    # ElevenLabs (voice generation)
    elevenlabs_api_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Pipeline web URL (for edit links in Trello comments)
    pipeline_web_url: str = ""

    # Google Sheet for channel config (published as CSV)
    channels_sheet_id: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


class ChannelConfig:
    def __init__(
        self,
        name: str,
        prompt: str,
        elevenlabs_voice_id: str,
        category: str = "",
        discord_role_id: str = "",
    ):
        self.name = name
        self.prompt = prompt
        self.elevenlabs_voice_id = elevenlabs_voice_id
        self.category = category
        self.discord_role_id = discord_role_id

    def load_prompt(self) -> str:
        return self.prompt


class PipelineConfig:
    def __init__(self, channels: dict[str, ChannelConfig]):
        self._channels = channels

        self.trigger_label: str = "snap script"
        self.ready_list: str = "video in edit"

        self.label_writing: str = "Snap: Writing Script"
        self.label_review: str = "Snap: Script Ready"
        self.label_approved: str = "Snap Approved"
        self.label_generating_voice: str = "Snap: Generating Voice"
        self.label_done: str = "Snap: Done"
        self.label_error: str = "Snap: Error"

    def get_channel(self, label_name: str) -> ChannelConfig | None:
        return self._channels.get(label_name.lower())

    def get_snap_channel(self, label_names: set[str]) -> str | None:
        """Find a label ending with '(Snap)' that matches a registered show."""
        for name in label_names:
            if name.lower().endswith("(snap)") and self.get_channel(name):
                return name
        return None

    @property
    def channel_labels(self) -> set[str]:
        return {name for name in self._channels}


# --- Sheet fetching with cache ---

_sheet_cache: PipelineConfig | None = None
_sheet_cache_time: float = 0
SHEET_CACHE_TTL = 300  # 5 minutes


def _fetch_channels_from_sheet(sheet_id: str) -> dict[str, ChannelConfig]:
    """Fetch channel config from a published Google Sheet.

    Expected columns: Channel Name | Voice ID | Category | Discord Role ID | Prompt

    Only loads channels whose name ends with '(Snap)'.
    """
    if sheet_id.startswith("2PACX-"):
        url = f"https://docs.google.com/spreadsheets/d/e/{sheet_id}/pub?output=csv"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    resp = httpx.get(url, timeout=15, follow_redirects=True)
    resp.raise_for_status()

    channels: dict[str, ChannelConfig] = {}
    last_channel_key: str | None = None
    reader = csv.DictReader(io.StringIO(resp.text), skipinitialspace=True)

    for row in reader:
        row = {k.strip(): v for k, v in row.items() if k}

        name = (row.get("Channel Name") or "").strip()
        prompt_line = (row.get("Prompt") or "").strip()

        if not name:
            # Continuation row — append prompt text to the previous channel
            if last_channel_key and prompt_line:
                channels[last_channel_key].prompt += "\n" + prompt_line
            continue

        # Only load Snap channels
        if not name.lower().endswith("(snap)"):
            last_channel_key = None
            continue

        last_channel_key = name.lower()
        channels[last_channel_key] = ChannelConfig(
            name=name,
            elevenlabs_voice_id=(row.get("Voice ID") or "").strip(),
            category=(row.get("Category") or "").strip(),
            discord_role_id=(row.get("Discord Role ID") or "").strip(),
            prompt=prompt_line,
        )

    logger.info("Loaded %d Snap channels from Google Sheet", len(channels))
    for key, ch in channels.items():
        logger.info("Snap channel '%s': voice=%s, prompt=%d chars", ch.name, ch.elevenlabs_voice_id, len(ch.prompt))
    return channels


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_pipeline_config() -> PipelineConfig:
    """Get pipeline config, fetching from Google Sheet if configured.

    Caches for 5 minutes so sheet updates take effect without redeploying.
    """
    global _sheet_cache, _sheet_cache_time

    settings = get_settings()

    if not settings.channels_sheet_id:
        return PipelineConfig({})

    now = time.time()
    if _sheet_cache and (now - _sheet_cache_time) < SHEET_CACHE_TTL:
        return _sheet_cache

    try:
        channels = _fetch_channels_from_sheet(settings.channels_sheet_id)
        _sheet_cache = PipelineConfig(channels)
        _sheet_cache_time = now
        return _sheet_cache
    except Exception:
        logger.exception("Failed to fetch channels from Google Sheet")
        if _sheet_cache:
            return _sheet_cache
        return PipelineConfig({})
