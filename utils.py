from pathlib import Path
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import copy
import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, time as dt_time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from auth import getHeaders

# All Onestop Toolkit tools are limited to this Deliverect account.
ONESTOP_ALLOWED_ACCOUNT_ID = "6963884edc8e7760066fa547"


def is_onestop_account(account_id: str) -> bool:
    return (account_id or "").strip() == ONESTOP_ALLOWED_ACCOUNT_ID


def post_opening_hours_and_update_channels(
    locations: list,
    *,
    raise_on_error: bool = True,
    debug: bool = False,
):
    """POST opening hours for one or more locations and trigger channel updates."""
    url = "https://api.deliverect.io/locations/channels/updateOpeningHours"
    payload = {"locations": locations, "enhanceOpeningHours": True}
    if debug:
        print(f"\n--- POST updateOpeningHours ---")
        print(f"Request: POST {url}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
    response = requests.post(url, headers=getHeaders(), json=payload)
    if debug:
        print(f"Response: {_api_response_detail(response)}")
    if not _http_ok(response):
        if raise_on_error:
            raise RuntimeError(
                f"POST locations/channels/updateOpeningHours: {_api_response_detail(response)}"
            )
        return False
    if not (response.content or b"").strip():
        return True
    try:
        return response.json()
    except ValueError:
        return True


def postOpeningHoursAndUpdateChannels(locationId: str, channelLinkId: str, openingHours: list):
    """Update opening hours for a single channel link at a location."""
    return post_opening_hours_and_update_channels(
        [
            {
                "id": locationId,
                "channels": [{"id": channelLinkId, "openingHours": openingHours}],
                "triggerUpdate": True,
            }
        ]
    )



def activateUberEats(channelLinkId: str):
    url =f"https://api.deliverect.io/v2/channelLinks/{channelLinkId}/activate"
    response = requests.post(url, headers=getHeaders())
    if not _http_ok(response):
        return False
    return response.json()


def _http_ok(response: requests.Response) -> bool:
    """True if HTTP status is 2xx (e.g. 200 or 201)."""
    return 200 <= response.status_code < 300


def _paginated_account_url(resource: str, account: str, page: int, max_results: int) -> str:
    """Stable sort by _id prevents duplicate items across paginated API pages."""
    return (
        f"https://api.deliverect.io/{resource}"
        f'?where={{"account":"{account}"}}'
        f"&page={page}&max_results={max_results}&sort=_id"
    )


def _assert_unique_ids(items: list, resource: str) -> None:
    seen = set()
    duplicates = []
    for item in items:
        item_id = item.get("_id")
        if not item_id or item_id in seen:
            if item_id:
                duplicates.append(item_id)
            continue
        seen.add(item_id)
    if duplicates:
        sample = ", ".join(duplicates[:5])
        raise ValueError(
            f"Paginated {resource} response returned duplicate _id(s): {sample} "
            f"({len(duplicates)} total). Each record must have a unique _id — "
            "ensure sort=_id is on the GET query."
        )


def _api_response_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = (response.text or "").strip()
    body_text = str(body)
    if len(body_text) > 500:
        body_text = body_text[:500] + "..."
    return f"HTTP {response.status_code} — {body_text}"


def _channel_link_ids_from_location(location: dict) -> list:
    """
    Location documents may list links as `channelLinks` (ids) or under
    `_links.related.channelLinks` as HATEOAS `{ "href": "channelLinks/<id>" }`.
    """
    raw = location.get("channelLinks")
    if raw:
        out = []
        for x in raw:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict) and x.get("_id"):
                out.append(x["_id"])
        return out

    links = (
        ((location.get("_links") or {}).get("related") or {}).get("channelLinks")
        or []
    )
    ids = []
    for item in links:
        if not isinstance(item, dict):
            continue
        href = (item.get("href") or "").strip()
        if not href:
            continue
        # "channelLinks/691cbd..." or URL ending with same
        ids.append(href.rstrip("/").split("/")[-1])
    return ids


def getAllLocations(account: str, progress_callback=None):
    """
    Get all locations for an account.
    
    Args:
        account: Account ID
        return_format: "list" returns list of dicts with name/id, 
                      "ids" returns just location IDs,
                      "raw" returns raw API response locations array
                      
    Returns:
        List of locations in requested format
    """
    try:
        location_list = []
        page = 1
        max_results = 500
        while True:
            url = _paginated_account_url("locations", account, page, max_results)
            response = requests.get(url, headers=getHeaders())
            data = response.json()
            if not _http_ok(response):
                return []

            items = data.get("_items", [])
            if not items:
                print("No more location to be found.")
                break
            location_list.extend(items)
            if progress_callback:
                progress_callback(page, len(items), len(location_list))
            page += 1
            print("Fetching locations on page", page)
        _assert_unique_ids(location_list, "locations")
        return location_list
    except Exception as e:
        print(f"Error getting locations: {e}")
        return []


def getLocation(location_id, all_locations: list):
    for location in all_locations:
        if location.get("_id") == location_id:
            return location
    return None


def createRetailChannel(location: dict ,channelPayload: dict):
    locationId = location.get("_id")
    accountId = location.get("account")
    locationPosSettings = location.get("posSettings")
    payload = copy.deepcopy(channelPayload)
    payload['posSettings'] = locationPosSettings
    payload['location'] = locationId
    payload['account'] = accountId
    url = f"https://api.deliverect.io/channelLinks"
    response = requests.post(url, headers=getHeaders(), json=payload)
    if not _http_ok(response):
        return False
    return response.json().get("_id")


def updateLocation(locationId: str, locationPayload: dict, _etag,):
    url = f"https://api.deliverect.io/locations/{locationId}"
    headers = getHeaders()
    headers['If-Match'] = _etag
    response = requests.patch(url, headers=headers, json=locationPayload)
    if not _http_ok(response):
        return False
    # 204 No Content or empty body = success; don't treat missing `_id` as failure
    if response.status_code == 204 or not (response.content or b"").strip():
        return locationId
    try:
        data = response.json()
    except ValueError:
        return locationId
    if isinstance(data, dict):
        return data.get("_id") or locationId
    return locationId


def checkIfRetailOrderAutoAcceptEnabled(location: str):
    posSettings = location.get("posSettings")
    generic = posSettings.get("generic")
    return generic.get("autoAcceptRetailOrder")

# Keys produced by create_retail_channels (location → channels) → human-readable group titles
CHANNEL_GROUP_LABELS = {
    "justEatRetail": "Just Eat",
    "deliverooRetail": "Deliveroo",
    "uberEatsRetail": "Uber Eats",
}


def groupResultsByChannel(results_by_location: dict) -> dict:
    """
    { locationName: { channelKey: linkId } } → { "Just Eat": { locationName: linkId }, ... }
    """
    out = {}
    for location_name, channels in results_by_location.items():
        for channel_key, link_id in channels.items():
            label = CHANNEL_GROUP_LABELS.get(channel_key)
            if label is None:
                continue
            out.setdefault(label, {})[location_name] = link_id
    return out


def getChannelLink(channelLinkId: str):
    url = f"https://api.deliverect.io/channelLinks/{channelLinkId}"
    response = requests.get(url, headers=getHeaders())
    if not _http_ok(response):
        return False
    return response.json()

def checkApplication(channelLink: str):
    channelSettings = channelLink.get("channelSettings")
    application = channelSettings.get("application")
    return application

def updateChannelLink(
    channelLinkId: str,
    payload: dict,
    _etag,
    *,
    raise_on_error: bool = False,
    debug: bool = False,
):
    url = f"https://api.deliverect.io/channelLinks/{channelLinkId}"
    headers = getHeaders()
    headers["If-Match"] = _etag
    if debug:
        print(f"\n--- PATCH {channelLinkId} ---")
        print(f"Request: PATCH {url}")
        print(f"If-Match (etag): {_etag}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
    response = requests.patch(url, headers=headers, json=payload)
    if debug:
        print(f"Response: {_api_response_detail(response)}")
    if not _http_ok(response):
        if raise_on_error:
            raise RuntimeError(
                f"PATCH channelLinks/{channelLinkId}: {_api_response_detail(response)}"
            )
        return False
    return True


def getAllChannelLinks(account: str, group_by_channel: bool = True, progress_callback=None):
    """
    Get all channel links for an account, optionally grouped by channel.
    
    Args:
        account: Account ID
        group_by_channel: If True, returns list grouped by channel.
                        If False, returns flat list of all channel links.
                        
    Returns:
        If group_by_channel=True: [{"channel": channelName, "channelLinksIds": [...]}, ...]
        If group_by_channel=False: [{"name": ..., "id": ..., "channel": ...}, ...]
    """
    all_channelLinks = []
    page = 1
    max_results = 500

    while True:
        url = _paginated_account_url("channelLinks", account, page, max_results)
        response = requests.get(url, headers=getHeaders())

        if response.status_code != 200:
            break

        items = response.json().get("_items", [])
        if not items:
            print("No more channelLinks to be found")
            break

        all_channelLinks.extend(items)
        if progress_callback:
            progress_callback(page, len(items), len(all_channelLinks))
        page += 1
        print("Fetching channel links on page", page)

    _assert_unique_ids(all_channelLinks, "channelLinks")
    return all_channelLinks


def extractOpeningHours(channelLink: dict):
    openingHours = channelLink.get("openingHours")
    return openingHours

def extractOpeningHoursPerDay(opening_hours: list) -> str:
    """Convert openingHours list to CSV (dayOfWeek,startTime,endTime)."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["dayOfWeek", "startTime", "endTime"],
        extrasaction="ignore",
    )
    writer.writeheader()
    if opening_hours:
        writer.writerows(opening_hours)
    return output.getvalue()


OPENING_HOURS_DAY_COLUMNS = ("Mon", "Tues", "Wed", "Thurs", "Fri", "Sat", "Sun")
OPENING_HOURS_CSV_COLUMNS = [
    "locationName",
    "locationId",
    "channelLinkName",
    "channelLinkId",
    *OPENING_HOURS_DAY_COLUMNS,
]


def _to_time_str(val):
    if val is None or (isinstance(val, str) and not str(val).strip()):
        return None
    if isinstance(val, dt_time):
        return val.strftime("%H:%M")
    if isinstance(val, datetime):
        return val.strftime("%H:%M")
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in ("closed", "n/a", "-", "—"):
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
        if m:
            return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
        return None
    try:
        h = int(val * 24)
        m = int(round((val * 24 % 1) * 60))
        if m == 60:
            h += 1
            m = 0
        return f"{h:02d}:{m:02d}"
    except (TypeError, ValueError):
        return None


def parse_opening_hours_day_cell(cell) -> tuple:
    if cell is None:
        return None, None
    s = str(cell).strip()
    if not s or s.lower() in ("closed", "n/a", "-", "—"):
        return None, None
    m = re.match(
        r"^(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(\d{1,2}:\d{2}(?::\d{2})?)$",
        s,
    )
    if not m:
        return None, None
    start = _to_time_str(m.group(1))
    end = _to_time_str(m.group(2))
    if not start or not end:
        return None, None
    return start, end


def opening_hours_from_csv_row(row: dict) -> list:
    out = []
    for day_index, col in enumerate(OPENING_HOURS_DAY_COLUMNS, start=1):
        start, end = parse_opening_hours_day_cell(row.get(col))
        if start is None or end is None:
            continue
        out.append({"dayOfWeek": day_index, "startTime": start, "endTime": end})
    return out


def inspect_opening_hours_row(row: dict) -> dict:
    """Classify a CSV row for import preview and payload building."""
    channel_link_id = (row.get("channelLinkId") or "").strip()
    location_id = (row.get("locationId") or "").strip()
    hours = opening_hours_from_csv_row(row)
    missing_days = []
    invalid_days = []

    for col in OPENING_HOURS_DAY_COLUMNS:
        cell = row.get(col)
        s = str(cell).strip() if cell is not None else ""
        if not s or s.lower() in ("closed", "n/a", "-", "—"):
            missing_days.append(col)
            continue
        start, end = parse_opening_hours_day_cell(cell)
        if start is None or end is None:
            invalid_days.append(col)

    day_count = len(hours)
    if not channel_link_id:
        status = "skipped_no_id"
    elif not location_id:
        status = "skipped_no_location"
    elif invalid_days:
        status = "skipped_invalid"
    elif day_count == 0:
        status = "skipped_no_hours"
    elif day_count < 7:
        status = "partial"
    else:
        status = "full"

    return {
        "locationId": location_id,
        "channelLinkId": channel_link_id,
        "hours": hours,
        "day_count": day_count,
        "missing_days": missing_days,
        "invalid_days": invalid_days,
        "status": status,
        "importable": status in ("partial", "full"),
    }


def _validate_opening_hours_csv_columns(rows: list) -> None:
    if not rows:
        raise ValueError("CSV is empty")
    missing = [c for c in OPENING_HOURS_CSV_COLUMNS if c not in rows[0].keys()]
    if missing:
        raise ValueError(f"Missing columns: {missing}")


def load_opening_hours_import_payloads_from_rows(rows: list) -> list:
    _validate_opening_hours_csv_columns(rows)

    payloads = []
    for row in rows:
        info = inspect_opening_hours_row(row)
        if not info["importable"]:
            continue
        payloads.append(
            {
                "locationId": info["locationId"],
                "channelLinkId": info["channelLinkId"],
                "openingHours": info["hours"],
            }
        )
    return payloads


def load_opening_hours_import_payloads(filepath: str) -> list:
    with open(filepath, newline="") as f:
        rows = list(csv.DictReader(f))
    return load_opening_hours_import_payloads_from_rows(rows)


def load_opening_hours_import_payloads_from_text(text: str) -> list:
    rows = list(csv.DictReader(io.StringIO(text)))
    return load_opening_hours_import_payloads_from_rows(rows)


def analyze_opening_hours_csv_rows(rows: list) -> dict:
    """Summarize how many CSV rows can be imported vs skipped."""
    _validate_opening_hours_csv_columns(rows)

    importable = 0
    full_week = 0
    partial = 0
    skipped_no_id = 0
    skipped_no_location = 0
    skipped_no_hours = 0
    skipped_invalid = 0
    row_details = []

    for row in rows:
        info = inspect_opening_hours_row(row)
        row_details.append(
            {
                **info,
                "locationName": row.get("locationName", ""),
                "channelLinkName": row.get("channelLinkName", ""),
            }
        )
        status = info["status"]
        if status == "skipped_no_id":
            skipped_no_id += 1
        elif status == "skipped_no_location":
            skipped_no_location += 1
        elif status == "skipped_invalid":
            skipped_invalid += 1
        elif status == "skipped_no_hours":
            skipped_no_hours += 1
        elif status == "partial":
            partial += 1
            importable += 1
        elif status == "full":
            full_week += 1
            importable += 1

    return {
        "total_rows": len(rows),
        "importable": importable,
        "full_week": full_week,
        "partial": partial,
        "skipped_no_id": skipped_no_id,
        "skipped_no_location": skipped_no_location,
        "skipped_no_hours": skipped_no_hours,
        "skipped_invalid": skipped_invalid,
        "row_details": row_details,
    }


def fetch_opening_hours_csv_rows(account: str, progress_callback=None) -> list:
    """Fetch channel links + locations and return export-format row dicts."""

    def on_channel_links(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("channelLinks", page, page_items, total)

    def on_locations(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("locations", page, page_items, total)

    channel_links = getAllChannelLinks(account, progress_callback=on_channel_links)
    locations = getAllLocations(account, progress_callback=on_locations)
    location_names = {loc.get("_id"): loc.get("name", "") for loc in locations}

    if progress_callback:
        progress_callback("building", 1, len(channel_links), len(channel_links))

    return [_opening_hours_wide_row(link, location_names) for link in channel_links]


def opening_hours_csv_text(rows: list) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=OPENING_HOURS_CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _format_day_hours(day: dict) -> str:
    start = day.get("startTime", "")
    end = day.get("endTime", "")
    if start and end:
        return f"{start}-{end}"
    return ""


def _opening_hours_wide_row(link: dict, location_names: dict) -> dict:
    location_id = link.get("location", "")
    row = {
        "locationName": location_names.get(location_id, ""),
        "locationId": location_id,
        "channelLinkName": link.get("name", ""),
        "channelLinkId": link.get("_id", ""),
    }
    hours_by_day = {
        day.get("dayOfWeek"): day
        for day in (link.get("openingHours") or [])
        if day.get("dayOfWeek") is not None
    }
    for day_num, day_name in enumerate(OPENING_HOURS_DAY_COLUMNS, start=1):
        row[day_name] = _format_day_hours(hours_by_day.get(day_num, {}))
    return row


def exportChannelLinksOpeningHoursCsv(account: str, filepath: str) -> None:
    """Fetch channel links + locations and write opening hours to CSV."""
    rows = fetch_opening_hours_csv_rows(account)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OPENING_HOURS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def group_opening_hours_payloads_by_location(payloads: list) -> list[dict]:
    """Group channel opening-hour payloads by location for bulk API calls."""
    by_location: dict[str, list] = {}
    for payload in payloads:
        location_id = payload["locationId"]
        by_location.setdefault(location_id, []).append(
            {
                "id": payload["channelLinkId"],
                "openingHours": payload["openingHours"],
            }
        )
    return [
        {"id": location_id, "channels": channels, "triggerUpdate": True}
        for location_id, channels in by_location.items()
    ]


def _post_location_opening_hours(
    location_payload: dict,
    *,
    debug: bool = False,
) -> None:
    post_opening_hours_and_update_channels(
        [location_payload],
        raise_on_error=True,
        debug=debug,
    )


def import_opening_hours_payloads(
    account: str,
    payloads: list,
    workers: int = 20,
    *,
    debug: bool = False,
    progress_callback=None,
) -> tuple:
    """POST opening hours via locations/channels/updateOpeningHours from CSV payloads."""
    if debug and workers > 1:
        print("Debug mode: using 1 worker for readable output")
        workers = 1

    location_payloads = group_opening_hours_payloads_by_location(payloads)

    ok = 0
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _post_location_opening_hours,
                location_payload,
                debug=debug,
            ): location_payload
            for location_payload in location_payloads
        }
        for future in as_completed(futures):
            location_payload = futures[future]
            channel_ids = [ch["id"] for ch in location_payload.get("channels") or []]
            try:
                future.result()
                ok += len(channel_ids)
                if progress_callback:
                    for channel_link_id in channel_ids:
                        progress_callback("ok", channel_link_id, None)
            except Exception as exc:
                msg = str(exc)
                for channel_link_id in channel_ids:
                    failures.append({"channelLinkId": channel_link_id, "error": msg})
                    print(f"FAIL {channel_link_id}: {exc}")
                    if progress_callback:
                        progress_callback("fail", channel_link_id, msg)

    return ok, len(payloads), failures


def importChannelLinksOpeningHoursCsv(
    account: str,
    filepath: str,
    workers: int = 20,
    *,
    debug: bool = False,
) -> tuple:
    """Read export-format CSV and POST opening hours via updateOpeningHours API."""
    payloads = load_opening_hours_import_payloads(filepath)

    if debug:
        print(f"Loaded {len(payloads)} payloads from {filepath}")

    ok, total, _failures = import_opening_hours_payloads(
        account,
        payloads,
        workers=workers,
        debug=debug,
    )
    return ok, total


# Deliverect status values — locations use strings; channel links use ints.
CHANNEL_LINK_STATUS_INACTIVE = 0
CHANNEL_LINK_STATUS_SUSPENDED = 1
CHANNEL_LINK_STATUS_TESTING = 2
CHANNEL_LINK_STATUS_SUBSCRIBED = 3
CHANNEL_LINK_STATUS_ONBOARDING = 4

LOCATION_STATUS_SUSPENDED = "SUSPENDED"
LOCATION_STATUS_SUBSCRIBED = "SUBSCRIBED"
LOCATION_STATUS_TESTING = "TESTING"

CHANNEL_LINK_STATUS_OPTIONS = (
    (CHANNEL_LINK_STATUS_INACTIVE, "Inactive (0)"),
    (CHANNEL_LINK_STATUS_SUSPENDED, "Suspended (1)"),
    (CHANNEL_LINK_STATUS_TESTING, "Testing (2)"),
    (CHANNEL_LINK_STATUS_SUBSCRIBED, "Subscribed (3)"),
    (CHANNEL_LINK_STATUS_ONBOARDING, "Onboarding (4)"),
)

LOCATION_STATUS_OPTIONS = (
    LOCATION_STATUS_SUSPENDED,
    LOCATION_STATUS_SUBSCRIBED,
    LOCATION_STATUS_TESTING,
)

# When updating a location, also set its channel links to the matching int status.
LOCATION_TO_CHANNEL_LINK_STATUS = {
    LOCATION_STATUS_SUSPENDED: CHANNEL_LINK_STATUS_SUSPENDED,
    LOCATION_STATUS_SUBSCRIBED: CHANNEL_LINK_STATUS_SUBSCRIBED,
    LOCATION_STATUS_TESTING: CHANNEL_LINK_STATUS_TESTING,
}


def set_channel_link_status(channel_link_id: str, status: int) -> dict:
    """
    PATCH a channel link ``status`` (integer enum).
    Returns a result dict with success / error details.
    """
    link_id = (channel_link_id or "").strip()
    if not link_id:
        return {
            "target_type": "Channel link",
            "target_id": channel_link_id,
            "success": False,
            "error": "Missing channel link ID",
            "applied_status": status,
        }

    info = getChannelLink(link_id)
    if not info:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "success": False,
            "error": "Failed to fetch channel link",
            "applied_status": status,
        }

    etag = info.get("_etag")
    if not etag:
        return {
            "target_type": "Channel link",
            "target_id": link_id,
            "success": False,
            "error": "Missing etag on channel link",
            "applied_status": status,
        }

    ok = bool(updateChannelLink(link_id, {"status": int(status)}, etag))
    return {
        "target_type": "Channel link",
        "target_id": link_id,
        "target_name": info.get("name") or link_id,
        "location_id": info.get("location"),
        "success": ok,
        "error": None if ok else "PATCH channel link failed",
        "applied_status": status,
    }


def set_location_status(location_id: str, status: str) -> dict:
    """
    PATCH a location ``status`` (string enum). Refreshes etag first.
    Returns a result dict with success / error details.
    """
    loc_id = (location_id or "").strip()
    if not loc_id:
        return {
            "target_type": "Location",
            "target_id": location_id,
            "success": False,
            "error": "Missing location ID",
            "applied_status": status,
        }

    # Channel link PATCHes can bump the location's etag; always re-fetch before PATCH.
    fresh = get1Location(loc_id)
    if not fresh:
        return {
            "target_type": "Location",
            "target_id": loc_id,
            "success": False,
            "error": "Failed to fetch location",
            "applied_status": status,
        }

    loc_etag = fresh.get("_etag")
    if not loc_etag:
        return {
            "target_type": "Location",
            "target_id": loc_id,
            "success": False,
            "error": "Missing etag on location",
            "applied_status": status,
        }

    ok = bool(updateLocation(loc_id, {"status": status}, loc_etag))
    return {
        "target_type": "Location",
        "target_id": loc_id,
        "target_name": fresh.get("name") or loc_id,
        "success": ok,
        "error": None if ok else "PATCH location failed",
        "applied_status": status,
    }


def suspend_channel_link(channel_link_id: str) -> dict:
    """PATCH a channel link to suspended status (status=1)."""
    return set_channel_link_status(channel_link_id, CHANNEL_LINK_STATUS_SUSPENDED)


def suspend_location(location_id: str) -> dict:
    """PATCH a location to status SUSPENDED."""
    return set_location_status(location_id, LOCATION_STATUS_SUSPENDED)


def markLocationAndChannelLinksAsSuspended(locationObject: dict) -> bool:
    """
    PATCH each channel link to suspended, then PATCH the location to SUSPENDED.
    Returns True only if every channel PATCH succeeded and the location PATCH succeeded.
    """
    if not locationObject:
        return False

    loc_id = locationObject.get("_id")
    if not loc_id:
        return False

    channel_updates_ok = True
    for link_id in _channel_link_ids_from_location(locationObject):
        if not suspend_channel_link(link_id).get("success"):
            channel_updates_ok = False

    location_patch_ok = bool(suspend_location(loc_id).get("success"))
    return channel_updates_ok and location_patch_ok


def getAllOperationReports(
    account: str,
    *,
    operation_types: list,
    channels: list,
    created_after: str,
    created_before: str,
    max_results: int = 500,
) -> list:
    """Fetch operation reports matching filters (paginated).

    Same pattern as RetailTools/snoozeHistory/snoozeHistoryPerPlu.py:
    GET https://api.deliverect.com/operationReports?where=<json>&max_results=...
    """
    where = {
        "operationType": {"$in": operation_types},
        "channel": {"$in": channels},
        "account": {"$in": [account]},
        "_created": {"$gt": created_after, "$lt": created_before},
    }
    where_str = json.dumps(where)
    all_reports = []
    page = 1

    while True:
        url = (
            "https://api.deliverect.com/operationReports"
            f"?where={where_str}&max_results={max_results}&page={page}&sort=-_created"
        )
        response = requests.get(url, headers=getHeaders())
        if not _http_ok(response):
            raise RuntimeError(
                f"GET operationReports page {page}: {_api_response_detail(response)}"
            )

        items = response.json().get("_items", [])
        if not items:
            break

        all_reports.extend(items)
        if len(items) < max_results:
            break
        page += 1
        print(f"Fetching operation reports on page {page}")

    _assert_unique_ids(all_reports, "operationReports")
    return all_reports


INVENTORY_SYNC_RATE_LIMIT_MESSAGE = "Listings update rate-limited (429)"
INVENTORY_SYNC_SUCCESS_STATUS = 90
INVENTORY_SYNC_TYPE_SNOOZE_FALLBACK = "Snooze fallback"
INVENTORY_SYNC_TYPE_LISTING_UPDATE = "Listing update"
INVENTORY_SYNC_DEFAULT_OPERATION_TYPES = [1101]
INVENTORY_SYNC_DEFAULT_CHANNELS = [6002, 2]
OPERATION_REPORT_BASE_URL = "https://retail.deliverect.com/operationreports"


def get_london_tz() -> ZoneInfo:
    """Lazy load — avoids import failure when tzdata is missing at module init."""
    return ZoneInfo("Europe/London")


def inventory_sync_created_range_for_date(day) -> tuple[str, str]:
    """UTC window for a calendar day: prev day 23:00 → date 22:59:59."""
    if isinstance(day, str):
        day = datetime.strptime(day, "%Y-%m-%d").date()
    day_before = day - timedelta(days=1)
    created_after = f"{day_before.isoformat()}T23:00:00.000Z"
    created_before = f"{day.isoformat()}T22:59:59.999Z"
    return created_after, created_before


def parse_utc_iso(iso_str: str) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def london_now() -> datetime:
    return datetime.now(get_london_tz())


def london_date_time_to_utc_iso(
    day: date,
    clock: dt_time,
    *,
    end_of_second: bool = False,
) -> str:
    dt = datetime.combine(day, clock, tzinfo=get_london_tz())
    utc = dt.astimezone(timezone.utc)
    suffix = ".999Z" if end_of_second else ".000Z"
    return utc.strftime(f"%Y-%m-%dT%H:%M:%S{suffix}")


def inventory_sync_range_london(
    report_date: date,
    from_time: dt_time,
    to_time: dt_time,
) -> tuple[str, str, str]:
    """UTC API window from a London report date and time range."""
    to_day = report_date
    if to_time <= from_time:
        to_day = report_date + timedelta(days=1)
    created_after = london_date_time_to_utc_iso(report_date, from_time)
    created_before = london_date_time_to_utc_iso(
        to_day, to_time, end_of_second=True
    )
    range_label = (
        f"{report_date.isoformat()} "
        f"{from_time.strftime('%H:%M')}–{to_time.strftime('%H:%M')} London"
    )
    return created_after, created_before, range_label


def format_operation_created(iso_str: str) -> str:
    created = parse_utc_iso(iso_str)
    if not created:
        return iso_str or "—"
    london = created.astimezone(get_london_tz())
    tz_label = london.tzname() or "London"
    return london.strftime(f"%H:%M:%S {tz_label}, %d %b %Y")


def build_inventory_sync_table_rows(
    operation_reports: list,
    location_names: dict,
) -> list[dict]:
    """One row per operation report for UI tables."""
    rows = []
    for op in operation_reports:
        op_id = op.get("_id") or ""
        location_id = op.get("location") or ""
        status = op.get("operationStatus")
        created = op.get("_created") or ""
        messages = [(entry.get("message") or "") for entry in (op.get("log") or [])]
        is_snooze_fallback = any(
            INVENTORY_SYNC_RATE_LIMIT_MESSAGE in message for message in messages
        )
        location_name = location_names.get(location_id) or location_id or "—"

        rows.append(
            {
                "Time": format_operation_created(created),
                "Location": location_name,
                "Type": INVENTORY_SYNC_TYPE_SNOOZE_FALLBACK
                if is_snooze_fallback
                else INVENTORY_SYNC_TYPE_LISTING_UPDATE,
                "Result": "Success"
                if status == INVENTORY_SYNC_SUCCESS_STATUS
                else "Failed",
                "Report URL": f"{OPERATION_REPORT_BASE_URL}/{op_id}" if op_id else "",
                "_created": created,
            }
        )

    rows.sort(key=lambda row: row["_created"], reverse=True)
    for row in rows:
        row.pop("_created", None)
    return rows


def analyse_inventory_sync_reports(
    operation_reports: list,
    *,
    rate_limit_message: str = INVENTORY_SYNC_RATE_LIMIT_MESSAGE,
    success_status: int = INVENTORY_SYNC_SUCCESS_STATUS,
) -> dict:
    snooze_backup = set()
    snooze_backup_location_ids = set()
    normal_listings = set()
    success_count = 0
    fail_count = 0
    status_counts = {}
    rate_limited_details = []

    for op in operation_reports:
        status = op.get("operationStatus")
        status_counts[status] = status_counts.get(status, 0) + 1

        if status == success_status:
            success_count += 1
        else:
            fail_count += 1

        messages = [(entry.get("message") or "") for entry in (op.get("log") or [])]
        # 429 in log → Deliverect fell back to snooze; otherwise it was a normal listing update.
        has_rate_limit = any(rate_limit_message in message for message in messages)
        op_id = op.get("_id")

        if has_rate_limit:
            snooze_backup.add(op_id)
            location_id = op.get("location")
            if location_id:
                snooze_backup_location_ids.add(location_id)
            sample_message = next(
                (message for message in messages if rate_limit_message in message),
                "",
            )
            rate_limited_details.append(
                {
                    "_id": op_id,
                    "location": location_id,
                    "message": sample_message,
                }
            )
        else:
            normal_listings.add(op_id)

    total = len(operation_reports)
    success_rate = (success_count / total * 100) if total else 0

    return {
        "total": total,
        "success_count": success_count,
        "fail_count": fail_count,
        "success_rate": success_rate,
        "status_counts": status_counts,
        "snooze_backup": snooze_backup,
        "snooze_backup_location_ids": snooze_backup_location_ids,
        "normal_listings": normal_listings,
        "rate_limited_details": rate_limited_details,
    }


def get1Location(locationId: str):
    url = f"https://api.deliverect.io/locations/{locationId}"
    response = requests.get(url, headers=getHeaders())
    if not _http_ok(response):
        return False
    return response.json()

