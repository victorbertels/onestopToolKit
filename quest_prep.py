"""Quest prep: clone JE / Deliveroo / Uber retail channel links onto a location.

Fetches the three retail channel links from a known-good template location,
clears partner store bindings (especially Uber ``storeId``), then creates
matching channel links on the destination location.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Optional

import requests
from auth import getHeaders

REQUEST_TIMEOUT = 60
API_BASE = "https://api.deliverect.io"

# OneStop retail channel backend IDs (not restaurant 2 / 7 / 9).
RETAIL_CHANNELS = {
    "Just Eat": 6009,
    "Deliveroo": 6002,
    "Uber Eats": 6007,
}

PARTNER_ORDER = ("Just Eat", "Deliveroo", "Uber Eats")


# Default OneStop template site for Quest prep (editable in the UI).
DEFAULT_QUEST_PREP_TEMPLATE_LOCATION_ID = "6a503e21a4416958f0241fd2"


def get_configured_account_id() -> str:
    return (os.getenv("ACCOUNT_ID") or "").strip()


def get_template_location_id() -> str:
    return (
        (os.getenv("QUEST_PREP_TEMPLATE_LOCATION_ID") or "").strip()
        or DEFAULT_QUEST_PREP_TEMPLATE_LOCATION_ID
    )


def _assert_location_on_allowed_account(
    location: dict,
    *,
    label: str,
) -> Optional[str]:
    """Return an error string if ``location`` is not on the configured ACCOUNT_ID."""
    expected = get_configured_account_id()
    if not expected:
        return "ACCOUNT_ID is not set in the environment or Streamlit secrets."
    account = (location.get("account") or "").strip()
    if account != expected:
        loc_id = location.get("_id") or "unknown"
        return (
            f"{label} `{loc_id}` belongs to account `{account or '—'}`, "
            f"but this toolkit is configured for `{expected}`."
        )
    return None


def get_channel_link(
    channel_link_id: str,
    headers: Optional[dict] = None,
) -> Optional[dict]:
    url = f"{API_BASE}/channelLinks/{channel_link_id}"
    response = requests.get(
        url, headers=headers or getHeaders(), timeout=REQUEST_TIMEOUT
    )
    if response.status_code != 200:
        return None
    return response.json()


def get_location(
    location_id: str,
    headers: Optional[dict] = None,
) -> Optional[dict]:
    url = f"{API_BASE}/locations/{location_id}"
    response = requests.get(
        url, headers=headers or getHeaders(), timeout=REQUEST_TIMEOUT
    )
    if response.status_code != 200:
        return None
    return response.json()


def _channel_link_ids_from_location(location: dict) -> list[str]:
    raw = location.get("channelLinks")
    if raw:
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("_id"):
                out.append(item["_id"])
        return out

    links = (
        ((location.get("_links") or {}).get("related") or {}).get("channelLinks") or []
    )
    ids: list[str] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        href = (item.get("href") or "").strip()
        if href:
            ids.append(href.rstrip("/").split("/")[-1])
    return ids


def _normalize_channel_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _drop_underscore_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_underscore_keys(nested)
            for key, nested in value.items()
            if not key.startswith("_")
        }
    if isinstance(value, list):
        return [_drop_underscore_keys(item) for item in value]
    return value


def pick_retail_templates(
    channel_links: list[dict],
) -> tuple[dict[str, dict], list[str]]:
    """
    Pick one retail channel link per partner from a template location's links.

    Returns ``({partner: channel_link}, missing_partner_names)``.
    """
    by_channel: dict[int, list[dict]] = {}
    for link in channel_links:
        channel_id = _normalize_channel_id(link.get("channel"))
        if channel_id is None:
            continue
        by_channel.setdefault(channel_id, []).append(link)

    selected: dict[str, dict] = {}
    missing: list[str] = []
    for partner in PARTNER_ORDER:
        channel_id = RETAIL_CHANNELS[partner]
        candidates = by_channel.get(channel_id) or []
        if not candidates:
            missing.append(partner)
            continue
        # Prefer a link whose name mentions the partner / retail.
        preferred = None
        partner_lower = partner.lower()
        for link in candidates:
            name = (link.get("name") or "").lower()
            if partner_lower in name or "retail" in name:
                preferred = link
                break
        selected[partner] = preferred or candidates[0]

    return selected, missing


def fetch_retail_templates_from_location(
    template_location_id: str,
    headers: Optional[dict] = None,
) -> tuple[Optional[dict[str, dict]], Optional[str], Optional[dict]]:
    """
    Load template location and its JE / Deliveroo / Uber retail channel links.

    Returns ``(templates_by_partner, error, template_location)``.
    """
    headers = headers or getHeaders()
    template_location = get_location(template_location_id, headers=headers)
    if template_location is None:
        return None, f"Could not load template location `{template_location_id}`.", None

    link_ids = _channel_link_ids_from_location(template_location)
    if not link_ids:
        return (
            None,
            f"Template location `{template_location_id}` has no channel links.",
            template_location,
        )

    channel_links: list[dict] = []
    for link_id in link_ids:
        link = get_channel_link(link_id, headers=headers)
        if isinstance(link, dict):
            channel_links.append(link)

    templates, missing = pick_retail_templates(channel_links)
    if missing:
        return (
            None,
            (
                f"Template location is missing retail channel(s): {', '.join(missing)}. "
                f"Need channel IDs {RETAIL_CHANNELS}."
            ),
            template_location,
        )

    return templates, None, template_location


def clear_partner_store_bindings(payload: dict, partner: str) -> dict:
    """Clear store-specific partner IDs so the new location is not bound to the old store."""
    result = copy.deepcopy(payload)
    channel_settings = result.get("channelSettings")
    if not isinstance(channel_settings, dict):
        return result

    # Always drop location-bound catalog / URL fields from the template.
    for key in ("storeUrl", "productLocation", "deliverooCatalogId"):
        channel_settings.pop(key, None)

    if partner == "Uber Eats":
        channel_settings["storeId"] = ""
    elif partner == "Just Eat":
        channel_settings["restaurantId"] = ""
        if "locReference" in channel_settings:
            channel_settings["locReference"] = ""

    return result


def build_channel_payload_from_template(
    source_channel_link: dict,
    destination_location: dict,
    *,
    partner: str,
    channel_name: Optional[str] = None,
) -> dict:
    """Build a POST /channelLinks payload from a template retail channel link."""
    payload = _drop_underscore_keys(copy.deepcopy(source_channel_link))
    payload.pop("posSettings", None)
    payload.pop("productLocation", None)
    payload.pop("brandId", None)

    payload["posSettings"] = copy.deepcopy(destination_location.get("posSettings") or {})
    payload["openingHours"] = copy.deepcopy(destination_location.get("openingHours") or [])
    payload["location"] = destination_location.get("_id")
    payload["account"] = destination_location.get("account")

    if channel_name is not None:
        payload["name"] = channel_name
    elif not payload.get("name"):
        payload["name"] = partner

    payload = clear_partner_store_bindings(payload, partner)

    # Always enable retail auto-accept on the new channel link (last write wins).
    pos_settings = payload.get("posSettings")
    if not isinstance(pos_settings, dict):
        pos_settings = {}
        payload["posSettings"] = pos_settings
    generic = pos_settings.get("generic")
    if not isinstance(generic, dict):
        generic = {}
        pos_settings["generic"] = generic
    generic["autoAcceptRetailOrder"] = True

    return payload


def create_channel(
    payload: dict,
    headers: Optional[dict] = None,
) -> tuple[Optional[str], Optional[str]]:
    """POST a channel link payload. Returns ``(channel_link_id, error_message)``."""
    url = f"{API_BASE}/channelLinks"
    response = requests.post(
        url,
        headers=headers or getHeaders(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if not (200 <= response.status_code < 300):
        return None, f"HTTP {response.status_code}: {response.text[:500]}"

    try:
        data = response.json()
    except ValueError:
        return None, "Channel created but response was not valid JSON."

    if not isinstance(data, dict):
        return None, "Channel created but response had unexpected format."

    channel_link_id = data.get("_id")
    if not channel_link_id:
        return None, "Channel created but response had no _id."
    return channel_link_id, None


def build_quest_prep_payloads(
    destination_location_id: str,
    template_location_id: str,
    headers: Optional[dict] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Build create payloads for JE / Deliveroo / Uber on the destination location.

    Returns ``(preview_dict, error)`` where preview_dict has:
      template_location, destination_location, payloads: {partner: payload},
      templates: {partner: {id, name, channel}}
    """
    headers = headers or getHeaders()
    templates, error, template_location = fetch_retail_templates_from_location(
        template_location_id, headers=headers
    )
    if error or templates is None or template_location is None:
        return None, error or "Failed to load retail templates."

    account_error = _assert_location_on_allowed_account(
        template_location, label="Template location"
    )
    if account_error:
        return None, account_error

    destination_location = get_location(destination_location_id, headers=headers)
    if destination_location is None:
        return None, f"Could not load destination location `{destination_location_id}`."

    account_error = _assert_location_on_allowed_account(
        destination_location, label="Destination location"
    )
    if account_error:
        return None, account_error

    payloads: dict[str, dict] = {}
    template_meta: dict[str, dict] = {}
    for partner in PARTNER_ORDER:
        source = templates[partner]
        payloads[partner] = build_channel_payload_from_template(
            source,
            destination_location,
            partner=partner,
        )
        template_meta[partner] = {
            "id": source.get("_id"),
            "name": source.get("name"),
            "channel": source.get("channel"),
            "sendToQuest": (source.get("channelSettings") or {}).get("sendToQuest"),
            "uberStoreIdCleared": partner == "Uber Eats",
        }

    return {
        "template_location": {
            "id": template_location.get("_id"),
            "name": template_location.get("name"),
        },
        "destination_location": {
            "id": destination_location.get("_id"),
            "name": destination_location.get("name"),
            "account": destination_location.get("account"),
        },
        "templates": template_meta,
        "payloads": payloads,
    }, None


def create_quest_channels(
    payloads_by_partner: dict[str, dict],
    headers: Optional[dict] = None,
) -> list[dict]:
    """Create channel links for each partner. Returns result rows."""
    headers = headers or getHeaders()
    results: list[dict] = []
    for partner in PARTNER_ORDER:
        payload = payloads_by_partner.get(partner)
        if not payload:
            results.append(
                {
                    "partner": partner,
                    "success": False,
                    "channel_link_id": None,
                    "error": "No payload prepared.",
                }
            )
            continue
        channel_link_id, error = create_channel(payload, headers=headers)
        results.append(
            {
                "partner": partner,
                "success": bool(channel_link_id),
                "channel_link_id": channel_link_id,
                "name": payload.get("name"),
                "error": error,
            }
        )
    return results


def prep_location_for_quest(
    destination_location_id: str,
    template_location_id: str,
    headers: Optional[dict] = None,
) -> tuple[Optional[list[dict]], Optional[str], Optional[dict]]:
    """
    End-to-end: build payloads from template site and create the three retail links.

    Returns ``(results, error, preview)``.
    """
    preview, error = build_quest_prep_payloads(
        destination_location_id,
        template_location_id,
        headers=headers,
    )
    if error or preview is None:
        return None, error, None

    results = create_quest_channels(preview["payloads"], headers=headers)
    return results, None, preview
