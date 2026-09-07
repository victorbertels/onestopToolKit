"""Rename locations or channel links in Deliverect.

Load current names, edit in the UI or CSV, then PATCH only changed rows.
"""

from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from close_open_stores import channel_display_name
from utils import (
    get1Location,
    getAllChannelLinks,
    getAllLocations,
    getChannelLink,
    updateChannelLink,
    updateLocation,
)

LOCATION_CSV_COLUMNS = ("locationId", "locationName")
CHANNEL_LINK_CSV_COLUMNS = (
    "channelLinkId",
    "channelLinkName",
    "locationId",
    "locationName",
    "channel",
)


def _account_mismatch_error(expected: str, actual) -> Optional[str]:
    if not expected:
        return None
    if actual is None:
        return "Missing account on resource"
    if str(actual).strip() != expected.strip():
        return f"Account mismatch (resource account `{actual}`, expected `{expected}`)"
    return None


def _norm_name(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def to_csv_string(rows: list[dict], columns: tuple[str, ...]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def export_location_names(account: str, progress_callback=None) -> list[dict]:
    def on_locations(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("locations", page, page_items, total)

    locations = getAllLocations(account, progress_callback=on_locations)
    rows = []
    for loc in locations:
        loc_id = loc.get("_id") or loc.get("id")
        if not loc_id:
            continue
        rows.append(
            {
                "locationId": loc_id,
                "locationName": loc.get("name") or "",
            }
        )
    if progress_callback:
        progress_callback("building", 1, len(rows), len(rows))
    return rows


def export_channel_link_names(account: str, progress_callback=None) -> list[dict]:
    def on_channel_links(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("channelLinks", page, page_items, total)

    def on_locations(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("locations", page, page_items, total)

    channel_links = getAllChannelLinks(account, progress_callback=on_channel_links)
    locations = getAllLocations(account, progress_callback=on_locations)
    location_names = {loc.get("_id"): loc.get("name") or "" for loc in locations}

    rows = []
    for link in channel_links:
        link_id = link.get("_id") or link.get("id")
        if not link_id:
            continue
        location_id = link.get("location")
        rows.append(
            {
                "channelLinkId": link_id,
                "channelLinkName": link.get("name") or "",
                "locationId": location_id,
                "locationName": location_names.get(location_id, ""),
                "channel": channel_display_name(link.get("channel")),
            }
        )
    if progress_callback:
        progress_callback("building", 1, len(rows), len(rows))
    return rows


def changed_location_rows(original: list[dict], edited: list[dict]) -> list[dict]:
    current = {_norm_name(row.get("locationId")): _norm_name(row.get("locationName")) for row in original}
    changes = []
    for row in edited:
        loc_id = _norm_name(row.get("locationId"))
        if not loc_id:
            continue
        new_name = _norm_name(row.get("locationName"))
        if new_name != current.get(loc_id, ""):
            changes.append({"locationId": loc_id, "locationName": new_name})
    return changes


def changed_channel_link_rows(original: list[dict], edited: list[dict]) -> list[dict]:
    current = {
        _norm_name(row.get("channelLinkId")): _norm_name(row.get("channelLinkName"))
        for row in original
    }
    changes = []
    for row in edited:
        link_id = _norm_name(row.get("channelLinkId"))
        if not link_id:
            continue
        new_name = _norm_name(row.get("channelLinkName"))
        if new_name != current.get(link_id, ""):
            changes.append(
                {
                    "channelLinkId": link_id,
                    "channelLinkName": new_name,
                    "locationId": row.get("locationId"),
                    "locationName": row.get("locationName"),
                    "channel": row.get("channel"),
                }
            )
    return changes


def _update_location_name(row: dict, account_id: str = "") -> dict:
    loc_id = _norm_name(row.get("locationId"))
    new_name = _norm_name(row.get("locationName"))
    result = {
        "target_type": "Location",
        "target_id": loc_id,
        "previous_name": None,
        "new_name": new_name,
        "success": False,
        "error": None,
    }
    if not loc_id:
        result["error"] = "Missing location ID"
        return result
    if not new_name:
        result["error"] = "Name cannot be empty"
        return result

    info = get1Location(loc_id)
    if not info:
        result["error"] = "Failed to fetch location"
        return result

    result["previous_name"] = info.get("name") or ""
    mismatch = _account_mismatch_error(account_id, info.get("account"))
    if mismatch:
        result["error"] = mismatch
        return result

    if _norm_name(info.get("name")) == new_name:
        result["success"] = True
        return result

    etag = info.get("_etag")
    if not etag:
        result["error"] = "Missing etag on location"
        return result

    ok = bool(updateLocation(loc_id, {"name": new_name}, etag))
    result["success"] = ok
    result["error"] = None if ok else "PATCH location failed"
    return result


def _update_channel_link_name(row: dict, account_id: str = "") -> dict:
    link_id = _norm_name(row.get("channelLinkId"))
    new_name = _norm_name(row.get("channelLinkName"))
    result = {
        "target_type": "Channel link",
        "target_id": link_id,
        "previous_name": None,
        "new_name": new_name,
        "location_name": row.get("locationName"),
        "channel": row.get("channel"),
        "success": False,
        "error": None,
    }
    if not link_id:
        result["error"] = "Missing channel link ID"
        return result
    if not new_name:
        result["error"] = "Name cannot be empty"
        return result

    info = getChannelLink(link_id)
    if not info:
        result["error"] = "Failed to fetch channel link"
        return result

    result["previous_name"] = info.get("name") or ""
    result["channel"] = result.get("channel") or channel_display_name(info.get("channel"))
    mismatch = _account_mismatch_error(account_id, info.get("account"))
    if mismatch:
        result["error"] = mismatch
        return result

    if _norm_name(info.get("name")) == new_name:
        result["success"] = True
        return result

    etag = info.get("_etag")
    if not etag:
        result["error"] = "Missing etag on channel link"
        return result

    ok = bool(updateChannelLink(link_id, {"name": new_name}, etag))
    result["success"] = ok
    result["error"] = None if ok else "PATCH channel link failed"
    return result


def apply_location_name_updates(
    account: str,
    rows: list[dict],
    *,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    if not rows:
        return []
    results = []
    total = len(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_update_location_name, row, account) for row in rows]
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if on_progress:
                on_progress(completed, total, result)
    return results


def apply_channel_link_name_updates(
    account: str,
    rows: list[dict],
    *,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    if not rows:
        return []
    results = []
    total = len(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_update_channel_link_name, row, account) for row in rows]
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if on_progress:
                on_progress(completed, total, result)
    return results
