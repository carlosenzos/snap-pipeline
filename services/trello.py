from __future__ import annotations

import io
import logging

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.trello.com/1"


def _auth_params() -> dict:
    s = get_settings()
    return {"key": s.trello_api_key, "token": s.trello_token}


async def get_card(card_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/cards/{card_id}",
            params=_auth_params(),
        )
        resp.raise_for_status()
        return resp.json()


async def get_card_labels(card_id: str) -> list[dict]:
    card = await get_card(card_id)
    return card.get("labels", [])


async def get_card_attachments(card_id: str) -> list[dict]:
    """Get all attachments on a card. Returns list of {name, url, mimeType}."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/cards/{card_id}/attachments",
            params=_auth_params(),
        )
        resp.raise_for_status()
        return resp.json()


async def add_label_by_name(card_id: str, label_name: str) -> None:
    """Add a label to a card by finding/creating the label by name."""
    s = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/boards/{s.trello_board_id}/labels",
            params=_auth_params(),
        )
        resp.raise_for_status()
        labels = resp.json()

        label_id = None
        for label in labels:
            if label.get("name", "").lower() == label_name.lower():
                label_id = label["id"]
                break

        if not label_id:
            resp = await client.post(
                f"{BASE_URL}/boards/{s.trello_board_id}/labels",
                params={**_auth_params(), "name": label_name, "color": "sky"},
            )
            resp.raise_for_status()
            label_id = resp.json()["id"]

        resp = await client.post(
            f"{BASE_URL}/cards/{card_id}/idLabels",
            params=_auth_params(),
            json={"value": label_id},
        )
        if resp.status_code != 409:
            resp.raise_for_status()


async def remove_label_by_name(card_id: str, label_name: str) -> None:
    """Remove a label from a card by name."""
    async with httpx.AsyncClient() as client:
        card = await get_card(card_id)
        for label in card.get("labels", []):
            if label.get("name", "").lower() == label_name.lower():
                resp = await client.delete(
                    f"{BASE_URL}/cards/{card_id}/idLabels/{label['id']}",
                    params=_auth_params(),
                )
                resp.raise_for_status()
                return


async def add_comment(card_id: str, text: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/cards/{card_id}/actions/comments",
            params={**_auth_params(), "text": text},
        )
        resp.raise_for_status()


async def attach_text_file(card_id: str, filename: str, content: str) -> None:
    """Attach a text file to a Trello card."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/cards/{card_id}/attachments",
            params=_auth_params(),
            files={"file": (filename, io.BytesIO(content.encode()), "text/plain")},
        )
        resp.raise_for_status()


async def attach_binary_file(card_id: str, filename: str, data: bytes, mime_type: str) -> None:
    """Attach a binary file (e.g. MP3) to a Trello card."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{BASE_URL}/cards/{card_id}/attachments",
            params=_auth_params(),
            files={"file": (filename, io.BytesIO(data), mime_type)},
        )
        resp.raise_for_status()


async def move_card_to_list(card_id: str, list_name: str) -> None:
    """Move a card to a named list on the board."""
    s = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/boards/{s.trello_board_id}/lists",
            params=_auth_params(),
        )
        resp.raise_for_status()
        lists = resp.json()

        target_list_id = None
        for lst in lists:
            if lst["name"].lower() == list_name.lower():
                target_list_id = lst["id"]
                break

        if not target_list_id:
            raise ValueError(f"List '{list_name}' not found on board")

        resp = await client.put(
            f"{BASE_URL}/cards/{card_id}",
            params={**_auth_params(), "idList": target_list_id},
        )
        resp.raise_for_status()
