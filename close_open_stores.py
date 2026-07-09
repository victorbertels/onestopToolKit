"""Close / open stores via Deliverect channel-link busy mode.

Adapted from RetailTools/closeAllStores for OnestopToolkit's utils shape
(raw API docs with `_id`, ungrouped channel links).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import requests
from auth import getHeaders
from utils import getAllChannelLinks, getAllLocations

CLOSE_DELAY = 999
OPEN_DELAY = 0

# Known Deliverect channel backend IDs → display names (OneStop retail).
KNOWN_CHANNEL_NAMES = {
    2: "Deliveroo",
    7: "Uber Eats",
    9: "Just Eat",
    6002: "Deliveroo",
    6007: "Uber Eats",
    6009: "Just Eat",
}


def delay_for_action(close_stores: bool) -> int:
    return CLOSE_DELAY if close_stores else OPEN_DELAY


def normalize_locations(raw_locations: list[dict]) -> list[dict]:
    """Map raw location docs to {id, name, tags}."""
    out = []
    for loc in raw_locations:
        loc_id = loc.get("_id") or loc.get("id")
        if not loc_id:
            continue
        out.append(
            {
                "id": loc_id,
                "name": loc.get("name") or loc_id,
                "tags": loc.get("tags") or [],
            }
        )
    return out


def normalize_flat_channel_links(raw_links: list[dict]) -> list[dict]:
    """Map raw channel link docs to {id, name, location, channel}."""
    out = []
    for link in raw_links:
        link_id = link.get("_id") or link.get("id")
        if not link_id:
            continue
        out.append(
            {
                "id": link_id,
                "name": link.get("name") or link_id,
                "location": link.get("location"),
                "channel": link.get("channel"),
            }
        )
    return out


def channel_display_name(channel_id) -> str:
    normalized = _normalize_channel_id(channel_id)
    if normalized is None:
        return "—"
    return KNOWN_CHANNEL_NAMES.get(normalized, str(normalized))


def get_channel_groups(flat_links: list[dict]) -> list[dict]:
    """Build [{channelId, channel, channelLinksIds}] from flat normalized links."""
    by_channel: dict[int, list[str]] = {}
    for link in flat_links:
        channel_id = _normalize_channel_id(link.get("channel"))
        if channel_id is None:
            continue
        by_channel.setdefault(channel_id, []).append(link["id"])

    return [
        {
            "channelId": channel_id,
            "channel": channel_display_name(channel_id),
            "channelLinksIds": link_ids,
        }
        for channel_id, link_ids in sorted(by_channel.items(), key=lambda item: item[0])
    ]


def load_account_busy_mode_data(account: str) -> dict:
    """Fetch and normalize locations + channel links for the busy-mode UI."""
    locations = normalize_locations(getAllLocations(account))
    flat_links = normalize_flat_channel_links(
        getAllChannelLinks(account, group_by_channel=False)
    )
    channel_groups = get_channel_groups(flat_links)
    return {
        "locations": locations,
        "flat_channel_links": flat_links,
        "channel_groups": channel_groups,
    }


def get_unique_location_tags(locations: list[dict]) -> list[str]:
    tags = set()
    for loc in locations:
        for tag in loc.get("tags") or []:
            if tag:
                tags.add(tag)
    return sorted(tags)


def filter_locations_by_tags(locations: list[dict], selected_tags: list[str]) -> list[dict]:
    if not selected_tags:
        return []
    selected = set(selected_tags)
    return [loc for loc in locations if selected & set(loc.get("tags") or [])]


def resolve_location_ids(
    locations: list[dict],
    location_mode: str,
    selected_location_ids: list[str],
    selected_tags: list[str],
) -> set[str]:
    if location_mode == "All locations":
        return {loc["id"] for loc in locations if loc.get("id")}
    if location_mode == "Location groups (tags)":
        return {
            loc["id"]
            for loc in filter_locations_by_tags(locations, selected_tags)
            if loc.get("id")
        }
    return set(selected_location_ids)


def resolve_channel_ids(
    channel_groups: list[dict],
    channel_mode: str,
    selected_channel_ids: list,
) -> set[int]:
    if channel_mode == "All channels":
        return {
            group["channelId"]
            for group in channel_groups
            if group.get("channelId") is not None
        }
    return {int(channel_id) for channel_id in selected_channel_ids}


def _normalize_location_id(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_channel_id(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def filter_channel_links(
    flat_links: list[dict],
    location_ids: Optional[set[str]] = None,
    channel_ids: Optional[set[int]] = None,
) -> list[dict]:
    result = flat_links
    if location_ids is not None:
        loc_set = {_normalize_location_id(loc_id) for loc_id in location_ids}
        loc_set.discard(None)
        result = [
            link
            for link in result
            if _normalize_location_id(link.get("location")) in loc_set
        ]
    if channel_ids is not None:
        ch_set = {_normalize_channel_id(channel_id) for channel_id in channel_ids}
        ch_set.discard(None)
        result = [
            link
            for link in result
            if _normalize_channel_id(link.get("channel")) in ch_set
        ]
    return [link for link in result if link.get("id")]


def build_channel_name_lookup(channel_groups: list[dict]) -> dict[int, str]:
    lookup = {}
    for group in channel_groups:
        channel_id = group.get("channelId")
        if channel_id is not None:
            lookup[channel_id] = group.get("channel") or str(channel_id)
    return lookup


def _location_lookup(locations: list[dict]) -> dict[str, dict]:
    return {loc["id"]: loc for loc in locations if loc.get("id")}


def enrich_channel_links(
    flat_links: list[dict],
    locations: list[dict],
    channel_groups: list[dict],
) -> list[dict]:
    location_lookup = _location_lookup(locations)
    channel_names = build_channel_name_lookup(channel_groups)
    enriched = []
    for link in flat_links:
        loc = location_lookup.get(link.get("location"), {})
        channel_id = link.get("channel")
        enriched.append(
            {
                **link,
                "location_name": loc.get("name") or link.get("location") or "—",
                "channel_name": channel_names.get(
                    _normalize_channel_id(channel_id),
                    channel_display_name(channel_id),
                ),
                "tags": ", ".join(loc.get("tags") or []) or "—",
            }
        )
    return enriched


def select_channel_links(
    flat_links: list[dict],
    locations: list[dict],
    channel_groups: list[dict],
    location_mode: str,
    selected_location_ids: list[str],
    selected_tags: list[str],
    channel_mode: str,
    selected_channel_ids: list,
) -> list[dict]:
    if location_mode == "All locations":
        location_ids = None
    else:
        location_ids = resolve_location_ids(
            locations, location_mode, selected_location_ids, selected_tags
        )

    if channel_mode == "All channels":
        channel_ids = None
    else:
        channel_ids = resolve_channel_ids(
            channel_groups, channel_mode, selected_channel_ids
        )

    filtered = filter_channel_links(flat_links, location_ids, channel_ids)
    return enrich_channel_links(filtered, locations, channel_groups)


def set_channel_link_busy_mode(
    channel_link_id: str,
    preparation_time_delay: int,
    headers: Optional[dict] = None,
) -> dict:
    auth_headers = headers or getHeaders()
    url = f"https://api.deliverect.io/channellink/{channel_link_id}/busymode"
    payload = {
        "channelLinkId": channel_link_id,
        "preparationTimeDelay": preparation_time_delay,
    }
    try:
        response = requests.post(url, headers=auth_headers, json=payload, timeout=60)
        body = response.json() if response.text else {}
        success = response.status_code == 200
        return {
            "target_type": "Channel link",
            "target_id": channel_link_id,
            "preparation_time_delay": preparation_time_delay,
            "success": success,
            "status_code": response.status_code,
            "error": None
            if success
            else (body.get("message") or response.text[:300] or "Request failed"),
        }
    except Exception as exc:
        return {
            "target_type": "Channel link",
            "target_id": channel_link_id,
            "preparation_time_delay": preparation_time_delay,
            "success": False,
            "status_code": None,
            "error": str(exc),
        }


def run_busy_mode_for_channel_links(
    channel_links: list,
    close_stores: bool,
    headers: Optional[dict] = None,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    delay = delay_for_action(close_stores)
    results = []
    total = len(channel_links)

    def work(link) -> dict:
        if isinstance(link, str):
            link_id = link
            meta = {}
        else:
            link_id = link.get("id") or link
            meta = link
        result = set_channel_link_busy_mode(link_id, delay, headers=headers)
        result["target_name"] = meta.get("name") or link_id
        result["location_name"] = meta.get("location_name") or "—"
        result["channel_name"] = meta.get("channel_name") or "—"
        result["tags"] = meta.get("tags") or "—"
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(work, link) for link in channel_links]
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            results.append(row)
            if on_progress:
                on_progress(completed, total, row)
    return results
