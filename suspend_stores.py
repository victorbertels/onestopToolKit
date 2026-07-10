"""Set location / channel-link status by pasted IDs (no full-account load).

Locations use string statuses (e.g. SUSPENDED / SUBSCRIBED / TESTING).
Channel links use integer statuses (0–4).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from close_open_stores import channel_display_name
from utils import (
    CHANNEL_LINK_STATUS_OPTIONS,
    CHANNEL_LINK_STATUS_SUSPENDED,
    LOCATION_STATUS_OPTIONS,
    LOCATION_STATUS_SUSPENDED,
    ONESTOP_ALLOWED_ACCOUNT_ID,
    _channel_link_ids_from_location as channel_link_ids_from_location,
    get1Location,
    getChannelLink,
    set_channel_link_status,
    set_location_status,
)

# Re-export for callers that imported the old name.
SUSPEND_ALLOWED_ACCOUNT_ID = ONESTOP_ALLOWED_ACCOUNT_ID

CHANNEL_LINK_STATUS_LABELS = {value: label for value, label in CHANNEL_LINK_STATUS_OPTIONS}


def parse_ids(raw: str) -> list[str]:
    """Split pasted IDs (one per line); drop blanks and duplicates, keep order."""
    seen = set()
    out = []
    for line in (raw or "").splitlines():
        value = line.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _account_mismatch_error(expected: str, actual) -> Optional[str]:
    if not expected:
        return None
    if actual is None:
        return "Missing account on resource"
    if str(actual).strip() != expected.strip():
        return f"Account mismatch (resource account `{actual}`, expected `{expected}`)"
    return None


def set_channel_link_status_by_id(
    channel_link_id: str,
    *,
    status: int = CHANNEL_LINK_STATUS_SUSPENDED,
    account_id: Optional[str] = None,
) -> dict:
    """Fetch one channel link, optionally verify account, then set its int status."""
    link_id = (channel_link_id or "").strip()
    if not link_id:
        return {
            "target_type": "Channel link",
            "target_id": channel_link_id,
            "target_name": "—",
            "location_name": "—",
            "channel_name": "—",
            "success": False,
            "error": "Missing channel link ID",
            "applied_status": status,
        }

    info = getChannelLink(link_id)
    if not info:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": "—",
            "location_name": "—",
            "channel_name": "—",
            "success": False,
            "error": "Failed to fetch channel link",
            "applied_status": status,
        }

    mismatch = _account_mismatch_error(account_id or "", info.get("account"))
    if mismatch:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "target_name": info.get("name") or link_id,
            "location_name": info.get("location") or "—",
            "channel_name": channel_display_name(info.get("channel")),
            "success": False,
            "error": mismatch,
            "applied_status": status,
        }

    result = set_channel_link_status(link_id, status)
    result["target_name"] = info.get("name") or result.get("target_name") or link_id
    result["location_name"] = info.get("location") or "—"
    result["channel_name"] = channel_display_name(info.get("channel"))
    return result


def set_location_status_by_id(
    location_id: str,
    *,
    location_status: str = LOCATION_STATUS_SUSPENDED,
    channel_link_status: int = CHANNEL_LINK_STATUS_SUSPENDED,
    account_id: Optional[str] = None,
    also_update_channel_links: bool = True,
    max_workers: int = 10,
) -> list[dict]:
    """
    Fetch one location, optionally verify account, set status on its channel links
    (from the location document), then set the location status.
    """
    loc_id = (location_id or "").strip()
    if not loc_id:
        return [
            {
                "target_type": "Location",
                "target_id": location_id,
                "target_name": "—",
                "location_name": "—",
                "channel_name": "—",
                "success": False,
                "error": "Missing location ID",
                "applied_status": location_status,
            }
        ]

    location = get1Location(loc_id)
    if not location:
        return [
            {
                "target_type": "Location",
                "target_id": loc_id,
                "target_name": "—",
                "location_name": "—",
                "channel_name": "—",
                "success": False,
                "error": "Failed to fetch location",
                "applied_status": location_status,
            }
        ]

    loc_name = location.get("name") or loc_id
    mismatch = _account_mismatch_error(account_id or "", location.get("account"))
    if mismatch:
        return [
            {
                "target_type": "Location",
                "target_id": loc_id,
                "target_name": loc_name,
                "location_name": loc_name,
                "channel_name": "—",
                "success": False,
                "error": mismatch,
                "applied_status": location_status,
            }
        ]

    results: list[dict] = []

    if also_update_channel_links:
        link_ids = channel_link_ids_from_location(location)
        if link_ids:
            link_results = run_set_channel_link_statuses(
                link_ids,
                status=channel_link_status,
                account_id=account_id,
                max_workers=max_workers,
            )
            for row in link_results:
                row["location_name"] = loc_name
                results.append(row)

    loc_result = set_location_status(loc_id, location_status)
    loc_result["target_name"] = loc_name
    loc_result["location_name"] = loc_name
    loc_result["channel_name"] = "—"
    results.append(loc_result)
    return results


def run_set_channel_link_statuses(
    channel_link_ids: list,
    *,
    status: int = CHANNEL_LINK_STATUS_SUSPENDED,
    account_id: Optional[str] = None,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    """Set each pasted channel link ID to the given int status. Does not touch locations."""
    results: list[dict] = []
    total = len(channel_link_ids)

    def work(link_id) -> dict:
        if isinstance(link_id, dict):
            link_id = link_id.get("id") or link_id.get("_id") or link_id
        return set_channel_link_status_by_id(
            str(link_id), status=status, account_id=account_id
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(work, link_id) for link_id in channel_link_ids]
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            results.append(row)
            if on_progress:
                on_progress(completed, total, row)
    return results


def run_set_statuses_by_ids(
    *,
    location_ids: list[str],
    channel_link_ids: list[str],
    location_status: str = LOCATION_STATUS_SUSPENDED,
    channel_link_status: int = CHANNEL_LINK_STATUS_SUSPENDED,
    account_id: Optional[str] = None,
    also_update_location_channel_links: bool = True,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    """
    Set status on pasted location IDs and/or channel link IDs.

    Location work runs first (optional links on those locations + location), then any
    extra channel link IDs. Progress is over the combined target count
    (locations + standalone channel links).
    """
    results: list[dict] = []
    total = len(location_ids) + len(channel_link_ids)
    completed = 0

    def bump(row: dict) -> None:
        nonlocal completed
        completed += 1
        if on_progress:
            on_progress(completed, total, row)

    for loc_id in location_ids:
        batch = set_location_status_by_id(
            loc_id,
            location_status=location_status,
            channel_link_status=channel_link_status,
            account_id=account_id,
            also_update_channel_links=also_update_location_channel_links,
            max_workers=max_workers,
        )
        results.extend(batch)
        last = batch[-1] if batch else {
            "target_type": "Location",
            "target_id": loc_id,
            "target_name": loc_id,
        }
        bump(last)

    if channel_link_ids:
        link_results = run_set_channel_link_statuses(
            channel_link_ids,
            status=channel_link_status,
            account_id=account_id,
            max_workers=max_workers,
            on_progress=lambda _c, _t, row: bump(row),
        )
        results.extend(link_results)

    return results


# Backward-compatible aliases used by older call sites / imports.
def suspend_channel_link_by_id(channel_link_id: str, *, account_id: Optional[str] = None) -> dict:
    return set_channel_link_status_by_id(
        channel_link_id,
        status=CHANNEL_LINK_STATUS_SUSPENDED,
        account_id=account_id,
    )


def suspend_location_by_id(
    location_id: str,
    *,
    account_id: Optional[str] = None,
    also_suspend_channel_links: bool = True,
    max_workers: int = 10,
) -> list[dict]:
    return set_location_status_by_id(
        location_id,
        location_status=LOCATION_STATUS_SUSPENDED,
        channel_link_status=CHANNEL_LINK_STATUS_SUSPENDED,
        account_id=account_id,
        also_update_channel_links=also_suspend_channel_links,
        max_workers=max_workers,
    )


def run_suspend_channel_links(
    channel_link_ids: list,
    *,
    account_id: Optional[str] = None,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    return run_set_channel_link_statuses(
        channel_link_ids,
        status=CHANNEL_LINK_STATUS_SUSPENDED,
        account_id=account_id,
        max_workers=max_workers,
        on_progress=on_progress,
    )


def run_suspend_by_ids(
    *,
    location_ids: list[str],
    channel_link_ids: list[str],
    account_id: Optional[str] = None,
    also_suspend_location_channel_links: bool = True,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
    location_status: str = LOCATION_STATUS_SUSPENDED,
    channel_link_status: int = CHANNEL_LINK_STATUS_SUSPENDED,
) -> list[dict]:
    return run_set_statuses_by_ids(
        location_ids=location_ids,
        channel_link_ids=channel_link_ids,
        location_status=location_status,
        channel_link_status=channel_link_status,
        account_id=account_id,
        also_update_location_channel_links=also_suspend_location_channel_links,
        max_workers=max_workers,
        on_progress=on_progress,
    )
