"""Clear channelSettings.isMenuPushRestricted for pasted channel link IDs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from close_open_stores import channel_display_name
from suspend_stores import parse_ids
from utils import getChannelLink, updateChannelLink

__all__ = [
    "parse_ids",
    "clear_menu_push_restriction",
    "run_clear_menu_push_restrictions",
]


def _account_mismatch_error(expected: str, actual) -> Optional[str]:
    if not expected:
        return None
    if actual is None:
        return "Missing account on resource"
    if str(actual).strip() != expected.strip():
        return f"Account mismatch (resource account `{actual}`, expected `{expected}`)"
    return None


def clear_menu_push_restriction(
    channel_link_id: str,
    *,
    account_id: Optional[str] = None,
) -> dict:
    """Fetch one channel link, optionally verify account, then set isMenuPushRestricted=False."""
    link_id = (channel_link_id or "").strip()
    if not link_id:
        return {
            "target_type": "Channel link",
            "target_id": channel_link_id,
            "target_name": "—",
            "location_name": "—",
            "channel_name": "—",
            "previous_value": None,
            "success": False,
            "error": "Missing channel link ID",
        }

    info = getChannelLink(link_id)
    if not info:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": "—",
            "location_name": "—",
            "channel_name": "—",
            "previous_value": None,
            "success": False,
            "error": "Failed to fetch channel link",
        }

    name = info.get("name") or link_id
    location_name = info.get("location") or "—"
    channel_name = channel_display_name(info.get("channel"))
    previous = (info.get("channelSettings") or {}).get("isMenuPushRestricted")

    mismatch = _account_mismatch_error(account_id or "", info.get("account"))
    if mismatch:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": name,
            "location_name": location_name,
            "channel_name": channel_name,
            "previous_value": previous,
            "success": False,
            "error": mismatch,
        }

    etag = info.get("_etag")
    if not etag:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": name,
            "location_name": location_name,
            "channel_name": channel_name,
            "previous_value": previous,
            "success": False,
            "error": "Missing etag on channel link",
        }

    if previous is False:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": name,
            "location_name": location_name,
            "channel_name": channel_name,
            "previous_value": previous,
            "success": True,
            "error": None,
        }

    ok = bool(
        updateChannelLink(
            link_id,
            {"channelSettings": {"isMenuPushRestricted": False}},
            etag,
        )
    )
    return {
        "target_type": "Channel link",
        "target_id": link_id,
        "target_name": name,
        "location_name": location_name,
        "channel_name": channel_name,
        "previous_value": previous,
        "success": ok,
        "error": None if ok else "PATCH channel link failed",
    }


def run_clear_menu_push_restrictions(
    channel_link_ids: list,
    *,
    account_id: Optional[str] = None,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    """Set isMenuPushRestricted=False on each pasted channel link ID."""
    results: list[dict] = []
    total = len(channel_link_ids)

    def work(link_id) -> dict:
        if isinstance(link_id, dict):
            link_id = link_id.get("id") or link_id.get("_id") or link_id
        return clear_menu_push_restriction(str(link_id), account_id=account_id)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(work, link_id) for link_id in channel_link_ids]
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            results.append(row)
            if on_progress:
                on_progress(completed, total, row)
    return results
