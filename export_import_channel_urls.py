"""Export and import channel-link storeUrl/menuUrl values via CSV.

Adapted from RetailTools/exportImportChannelUrl for OnestopToolkit's utils
(raw API docs with `_id`, getChannelLink + updateChannelLink + etag).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from utils import getAllChannelLinks, getAllLocations, getChannelLink, updateChannelLink

CSV_COLUMNS = ("locationName", "locationId", "channelLinkId", "channelLinkName", "storeUrl")
DEFAULT_CSV_PATH = "channel_urls.csv"
CHANNEL_LINK_STATUS_TESTING = 2
_QUOTE_CHARS = frozenset("\"'“”‘’«»„‟")


def normalize_channel_url(value):
    """Turn quote-only / blank cells into a real empty URL.

    Excel and Word often convert typed \"\" into curly quotes (“”), which
    must not be written to storeUrl/menuUrl.
    """
    if value is None:
        return None
    text = str(value).strip()
    while len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()
    if not text or all(ch in _QUOTE_CHARS or ch.isspace() for ch in text):
        return ""
    return text


def _is_test_channel_link(link: dict) -> bool:
    try:
        return int(link.get("status")) == CHANNEL_LINK_STATUS_TESTING
    except (TypeError, ValueError):
        return False


def _account_mismatch_error(expected: str, actual) -> Optional[str]:
    if not expected:
        return None
    if actual is None:
        return "Missing account on resource"
    if str(actual).strip() != expected.strip():
        return f"Account mismatch (resource account `{actual}`, expected `{expected}`)"
    return None


def export_channel_urls(account: str, progress_callback=None) -> list[dict]:
    """Fetch non-testing channel links and return export-format row dicts."""

    def on_channel_links(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("channelLinks", page, page_items, total)

    def on_locations(page: int, page_items: int, total: int) -> None:
        if progress_callback:
            progress_callback("locations", page, page_items, total)

    channel_links = getAllChannelLinks(account, progress_callback=on_channel_links)
    locations = getAllLocations(account, progress_callback=on_locations)
    location_names = {loc.get("_id"): loc.get("name", "") for loc in locations}

    rows = []
    for link in channel_links:
        if _is_test_channel_link(link):
            continue
        location_id = link.get("location")
        settings = link.get("channelSettings") or {}
        rows.append(
            {
                "locationName": location_names.get(location_id, ""),
                "locationId": location_id,
                "channelLinkId": link.get("_id"),
                "channelLinkName": link.get("name"),
                "storeUrl": normalize_channel_url(settings.get("storeUrl", "")) or "",
            }
        )

    if progress_callback:
        progress_callback("building", 1, len(rows), len(rows))
    return rows


def to_csv_string(rows: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def write_csv(rows: list[dict], path: str = DEFAULT_CSV_PATH) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        file.write(to_csv_string(rows))


def read_csv(path: str = DEFAULT_CSV_PATH) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def row_has_url_update(row: dict) -> bool:
    channel_link_id = (row.get("channelLinkId") or "").strip()
    if not channel_link_id:
        return False
    if "storeUrl" not in row:
        return False
    return normalize_channel_url(row.get("storeUrl")) is not None


def _update_row(row: dict, account_id: str = "") -> dict:
    channel_link_id = (row.get("channelLinkId") or "").strip()
    store_url = normalize_channel_url(row.get("storeUrl"))
    result = {**row, "storeUrl": store_url}

    if not channel_link_id or store_url is None:
        return {**result, "success": False, "error": "Missing channel link ID or URL"}

    info = getChannelLink(channel_link_id)
    if not info:
        return {**result, "success": False, "error": "Failed to fetch channel link"}

    mismatch = _account_mismatch_error(account_id, info.get("account"))
    if mismatch:
        return {**result, "success": False, "error": mismatch}

    settings = info.get("channelSettings") or {}
    current_store = normalize_channel_url(settings.get("storeUrl")) or ""
    current_menu = normalize_channel_url(info.get("menuUrl")) or ""
    if current_store == store_url and current_menu == store_url:
        return {**result, "success": True, "error": None}

    etag = info.get("_etag")
    if not etag:
        return {**result, "success": False, "error": "Missing etag on channel link"}

    ok = bool(
        updateChannelLink(
            channel_link_id,
            {"channelSettings": {"storeUrl": store_url}, "menuUrl": store_url},
            etag,
        )
    )
    return {
        **result,
        "success": ok,
        "error": None if ok else "PATCH channel link failed",
    }


def import_channel_urls(
    account: str,
    rows: Optional[list[dict]] = None,
    path: Optional[str] = None,
    max_workers: int = 10,
    on_progress: Optional[Callable[[int, int, dict], None]] = None,
) -> list[dict]:
    if rows is None:
        rows = read_csv(path or DEFAULT_CSV_PATH)

    rows = [row for row in rows if row_has_url_update(row)]
    if not rows:
        return []

    results = []
    total = len(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_update_row, row, account) for row in rows]
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if on_progress:
                on_progress(completed, total, result)
    return results


def _resolve_account_id(cli_account: str) -> str:
    account = (cli_account or os.getenv("ACCOUNT_ID") or "").strip()
    if not account:
        print(
            "Account ID required: set ACCOUNT_ID in the environment or pass --account",
            file=sys.stderr,
        )
        sys.exit(1)
    return account


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or import channel link store URLs")
    parser.add_argument("mode", choices=["export", "import"], help="export to CSV or import from CSV")
    parser.add_argument("--account", default="", help="Deliverect account ID (default: ACCOUNT_ID env var)")
    parser.add_argument("--file", default=DEFAULT_CSV_PATH, help="CSV file path")
    parser.add_argument("--workers", type=int, default=10, help="parallel workers for import")
    args = parser.parse_args()
    account = _resolve_account_id(args.account)

    if args.mode == "export":
        rows = export_channel_urls(account)
        write_csv(rows, args.file)
        print(f"Exported {len(rows)} channel links to {args.file}")
        return

    results = import_channel_urls(account, path=args.file, max_workers=args.workers)
    ok = sum(1 for row in results if row.get("success"))
    print(f"Done: {ok}/{len(results)} updated (workers={args.workers})")


if __name__ == "__main__":
    main()
