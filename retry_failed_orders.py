"""Retry Deliverect retail orders via GET /retry/{orderId}.

Select orders with a pasted ID list and/or filters (status, excluded status,
status history, channel, location, channel order display ID, time window),
then retry each through the retail retry endpoint.

Orders whose ``statusHistory`` contains an excluded status (default 123,
Manual Retry) are skipped so previously retried orders are not sent again.

Order listing uses a lean projection and sequential paging so large days do
not time out or OOM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests
from auth import getHeaders
from dotenv import load_dotenv
from utils import inventory_sync_created_range_for_date, london_now

API_BASE = "https://api.deliverect.io"
RETRY_BASE = f"{API_BASE}/retry"

# Bump when Cloud mounts look stale — Streamlit UI shows this version.
MODULE_VERSION = "2026-10-09-retry-filters"

# Deliverect failed-order statuses for retail retry.
FAILED_STATUSES = [120, 122]
DEFAULT_STATUSES = list(FAILED_STATUSES)
# Manual Retry — if this appears in statusHistory, do not retry again.
# Exported for the Streamlit UI (dry-run skip metric label).
SKIP_IF_HISTORY_STATUS = 123

PAGE_SIZE = 500
BATCH_SIZE = 1000
MAX_WORKERS = 10
SLEEP_SECONDS = 60
REQUEST_TIMEOUT = 120
MAX_ORDER_LOOKBACK_DAYS = 89
# Eve where-clauses go in the query string; keep each $in list small.
ID_QUERY_CHUNK = 80

_SPLIT_TOKENS = re.compile(r"[\s,;]+")
_IGNORED_ID_TOKENS = {
    "id",
    "_id",
    "orderid",
    "order_id",
    "channelorderdisplayid",
    "channel_order_display_id",
}

# Lean listing projection — need statusHistory to skip already-retried orders.
ORDER_LIST_PROJECTION = {
    "_id": 1,
    "status": 1,
    "statusHistory": 1,
}
ORDER_PREVIEW_PROJECTION = {
    "_id": 1,
    "_created": 1,
    "status": 1,
    "statusHistory": 1,
    "channelOrderDisplayId": 1,
    "location": 1,
    "channel": 1,
}

ProgressCallback = Optional[Callable[[str], None]]
CancelCheck = Optional[Callable[[], bool]]


def _http_ok(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def _api_response_detail(response: requests.Response) -> str:
    try:
        body = response.text
    except Exception:
        body = ""
    snippet = (body or "")[:400]
    return f"HTTP {response.status_code}: {snippet}"


def _get_with_retry(url: str, *, headers: dict, retries: int = 3) -> requests.Response:
    last: Optional[requests.Response] = None
    for attempt in range(retries):
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        last = response
        if response.status_code != 429:
            return response
        time.sleep(1.5 * (attempt + 1))
    return last  # type: ignore[return-value]


def window_for_london_date(day: date) -> tuple[str, str]:
    """UTC ISO bounds for a London calendar day (same convention as inventory sync)."""
    return inventory_sync_created_range_for_date(day)


def window_from_days(days: int) -> tuple[str, str]:
    """Rolling UTC window ending now, ``days`` long."""
    if days < 1:
        raise ValueError("days must be >= 1")
    if days > MAX_ORDER_LOOKBACK_DAYS:
        raise ValueError(
            f"days must be <= {MAX_ORDER_LOOKBACK_DAYS} (Deliverect order lookback limit)"
        )
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    return (
        since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        until.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
    )


def _retail_headers() -> dict:
    headers = dict(getHeaders())
    headers["Accept"] = "*/*"
    headers["X-Deliverect-Version"] = "retail"
    return headers


def parse_order_ids(raw: str) -> list[str]:
    """Split pasted order IDs on commas, semicolons, and whitespace.

    Drops blanks, duplicates, and a few common CSV header cells. Keeps order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in _SPLIT_TOKENS.split(raw or ""):
        value = token.strip().strip("'\"")
        if not value or value.lower() in _IGNORED_ID_TOKENS or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def parse_int_list(raw: str, *, label: str = "number") -> list[int]:
    """Split integers on commas, semicolons, and whitespace."""
    seen: set[int] = set()
    out: list[int] = []
    for token in _SPLIT_TOKENS.split(raw or ""):
        value = token.strip()
        if not value:
            continue
        if not re.fullmatch(r"-?\d+", value):
            raise ValueError(f"Invalid {label}: {value}")
        number = int(value)
        if number in seen:
            continue
        seen.add(number)
        out.append(number)
    return out


def order_history_statuses(order: dict) -> set[int]:
    """Status codes present in ``statusHistory``."""
    history = order.get("statusHistory") or []
    if not isinstance(history, list):
        return set()
    found: set[int] = set()
    for entry in history:
        if not isinstance(entry, dict):
            continue
        try:
            found.add(int(entry.get("status")))
        except (TypeError, ValueError):
            continue
    return found


def order_has_history_status(
    order: dict,
    status: int = SKIP_IF_HISTORY_STATUS,
) -> bool:
    """True if ``statusHistory`` contains an entry with the given status code."""
    return status in order_history_statuses(order)


def _dedupe_strs(values: Optional[list[str]]) -> list[str]:
    return parse_order_ids("\n".join(str(v) for v in (values or []) if str(v).strip()))


def _chunks(values: list, size: int):
    step = max(1, size)
    for start in range(0, len(values), step):
        yield values[start : start + step]


def _mongo_eq_or_in(values: list) -> Any:
    if len(values) == 1:
        return values[0]
    return {"$in": values}


def _order_status(order: dict) -> Optional[int]:
    try:
        return int(order.get("status"))
    except (TypeError, ValueError):
        return None


def _skip_reason(
    order: dict,
    *,
    include_statuses: set[int],
    exclude_statuses: set[int],
    exclude_history: set[int],
    require_history: set[int],
) -> Optional[str]:
    """Why this order is not retried, or None to keep it.

    ``include_statuses`` empty means current status is not restricted.
    """
    status = _order_status(order)
    if include_statuses and status not in include_statuses:
        return "status"
    if exclude_statuses and status in exclude_statuses:
        return "status"
    history = order_history_statuses(order)
    if exclude_history and history.intersection(exclude_history):
        return "history"
    if require_history and not history.intersection(require_history):
        return "require_history"
    return None


def _build_order_where(
    account: str,
    *,
    since: str,
    until: str,
    statuses: list[int],
    exclude_statuses: list[int],
    channels: list[int],
    location_ids: list[str],
    channel_order_display_ids: list[str],
    order_ids: list[str],
    apply_status: bool,
) -> dict[str, Any]:
    where: dict[str, Any] = {"account": account}
    if since or until:
        created: dict[str, str] = {}
        if since:
            created["$gt"] = since
        if until:
            created["$lt"] = until
        where["_created"] = created

    if apply_status:
        exclude_set = set(exclude_statuses)
        include = [s for s in statuses if s not in exclude_set]
        if statuses and exclude_statuses and not include:
            raise ValueError("Include and exclude statuses cancel each other out.")
        if include:
            where["status"] = {"$in": include}
        elif exclude_statuses:
            where["status"] = {"$nin": exclude_statuses}

    if channels:
        where["channel"] = _mongo_eq_or_in(channels)
    if location_ids:
        where["location"] = _mongo_eq_or_in(location_ids)
    if channel_order_display_ids:
        where["channelOrderDisplayId"] = _mongo_eq_or_in(channel_order_display_ids)
    if order_ids:
        where["_id"] = _mongo_eq_or_in(order_ids)
    return where


def _large_id_list(
    order_ids: list[str],
    location_ids: list[str],
    channel_order_display_ids: list[str],
) -> Optional[tuple[str, list[str]]]:
    candidates: list[tuple[str, list[str]]] = []
    if len(order_ids) > ID_QUERY_CHUNK:
        candidates.append(("_id", order_ids))
    if len(location_ids) > ID_QUERY_CHUNK:
        candidates.append(("location", location_ids))
    if len(channel_order_display_ids) > ID_QUERY_CHUNK:
        candidates.append(("channelOrderDisplayId", channel_order_display_ids))
    if len(candidates) > 1:
        names = ", ".join(name for name, _ in candidates)
        raise ValueError(
            f"Only one long ID list is supported at a time "
            f"(over {ID_QUERY_CHUNK}): {names}."
        )
    return candidates[0] if candidates else None


def _wheres_for_scan(
    account: str,
    *,
    since: str,
    until: str,
    statuses: list[int],
    exclude_statuses: list[int],
    channels: list[int],
    location_ids: list[str],
    channel_order_display_ids: list[str],
    order_ids: list[str],
    apply_status: bool,
) -> list[dict[str, Any]]:
    large = _large_id_list(order_ids, location_ids, channel_order_display_ids)

    def build(
        oids: list[str], locs: list[str], display_ids: list[str]
    ) -> dict[str, Any]:
        return _build_order_where(
            account,
            since=since,
            until=until,
            statuses=statuses,
            exclude_statuses=exclude_statuses,
            channels=channels,
            location_ids=locs,
            channel_order_display_ids=display_ids,
            order_ids=oids,
            apply_status=apply_status,
        )

    if not large:
        return [
            build(order_ids, location_ids, channel_order_display_ids)
        ]
    field, values = large
    wheres: list[dict[str, Any]] = []
    for part in _chunks(values, ID_QUERY_CHUNK):
        wheres.append(
            build(
                part if field == "_id" else order_ids,
                part if field == "location" else location_ids,
                part if field == "channelOrderDisplayId" else channel_order_display_ids,
            )
        )
    return wheres


def _fetch_orders_page(
    where: dict,
    *,
    page: int,
    max_results: int = PAGE_SIZE,
    projection: Optional[dict] = None,
) -> dict:
    proj = projection if projection is not None else ORDER_LIST_PROJECTION
    params = (
        f"where={quote(json.dumps(where, separators=(',', ':')))}"
        f"&projection={quote(json.dumps(proj, separators=(',', ':')))}"
        f"&max_results={max_results}&page={page}&sort=_id"
    )
    url = f"{API_BASE}/orders?{params}"
    response = _get_with_retry(url, headers=getHeaders())
    if not _http_ok(response):
        raise RuntimeError(
            f"GET orders page={page}: {_api_response_detail(response)}"
        )
    return response.json()


def scan_failed_orders(
    account: str,
    *,
    since: str = "",
    until: str = "",
    statuses: Optional[list[int]] = None,
    exclude_statuses: Optional[list[int]] = None,
    exclude_history_statuses: Optional[list[int]] = None,
    require_history_statuses: Optional[list[int]] = None,
    order_ids: Optional[list[str]] = None,
    location_ids: Optional[list[str]] = None,
    channels: Optional[list[int]] = None,
    channel_order_display_ids: Optional[list[str]] = None,
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    """Scan orders and return retry IDs plus skip counts (no /retry calls).

    ``statuses=None`` keeps the default failed statuses (120, 122). Pass ``[]``
    to leave current status unrestricted. ``exclude_history_statuses=None``
    skips Manual Retry (123). Pass ``[]`` to retry orders that were already
    manually retried.

    Pasted ``order_ids`` are matched on this account, then status filters run
    in-process so skip reasons stay accurate. Other searches push status
    include/exclude into the API query.
    """
    since = (since or "").strip()
    until = (until or "").strip()
    include_statuses = list(DEFAULT_STATUSES if statuses is None else statuses)
    excluded_statuses = [int(s) for s in (exclude_statuses or [])]
    if exclude_history_statuses is None:
        excluded_history = [SKIP_IF_HISTORY_STATUS]
    else:
        excluded_history = [int(s) for s in exclude_history_statuses]
    required_history = [int(s) for s in (require_history_statuses or [])]
    wanted_ids = _dedupe_strs(order_ids)
    locations = _dedupe_strs(location_ids)
    display_ids = _dedupe_strs(channel_order_display_ids)
    channel_list = [int(c) for c in (channels or [])]

    if not (
        include_statuses
        or excluded_statuses
        or wanted_ids
        or locations
        or display_ids
        or channel_list
    ):
        raise ValueError(
            "Add an include status, an exclude status, order IDs, a location, "
            "a channel, or a channel order display ID."
        )
    if (not since or not until) and not (wanted_ids or display_ids):
        raise ValueError(
            "A time window is required unless you pass order IDs or channel order display IDs."
        )
    if bool(since) != bool(until):
        raise ValueError("Provide both since and until, or neither.")

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    # ID lookups apply status client-side so "excluded status" is countable.
    apply_status = not wanted_ids
    wheres = _wheres_for_scan(
        account,
        since=since,
        until=until,
        statuses=include_statuses,
        exclude_statuses=excluded_statuses,
        channels=channel_list,
        location_ids=locations,
        channel_order_display_ids=display_ids,
        order_ids=wanted_ids,
        apply_status=apply_status,
    )
    log(
        f"Scanning {len(wheres)} query chunk(s) — "
        f"include status {include_statuses or 'any'}, "
        f"exclude status {excluded_statuses or 'none'}, "
        f"exclude history {excluded_history or 'none'}"
    )

    include_set = set(include_statuses)
    exclude_set = set(excluded_statuses)
    exclude_history_set = set(excluded_history)
    require_history_set = set(required_history)

    ids: list[str] = []
    seen: set[str] = set()
    skipped_status = 0
    skipped_history = 0
    skipped_require = 0
    api_total = 0

    def _take(items: list) -> None:
        nonlocal skipped_status, skipped_history, skipped_require
        for item in items:
            if not isinstance(item, dict):
                continue
            oid = item.get("_id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            reason = _skip_reason(
                item,
                include_statuses=include_set,
                exclude_statuses=exclude_set,
                exclude_history=exclude_history_set,
                require_history=require_history_set,
            )
            if reason == "status":
                skipped_status += 1
                continue
            if reason == "history":
                skipped_history += 1
                continue
            if reason == "require_history":
                skipped_require += 1
                continue
            ids.append(str(oid))

    for index, where in enumerate(wheres, start=1):
        page1 = _fetch_orders_page(where, page=1, projection=ORDER_LIST_PROJECTION)
        meta = page1.get("_meta") or {}
        total = int(meta.get("total") or 0)
        max_results = int(meta.get("max_results") or PAGE_SIZE)
        pages = max(1, (total + max_results - 1) // max_results) if total else 0
        api_total += total
        if len(wheres) == 1:
            log(f"API matched {total:,} order(s) ({pages} page(s))")
        else:
            log(
                f"Chunk {index}/{len(wheres)}: API matched {total:,} "
                f"({pages} page(s))"
            )
        if pages == 0:
            continue
        _take(page1.get("_items") or [])
        for page in range(2, pages + 1):
            data = _fetch_orders_page(
                where,
                page=page,
                max_results=max_results,
                projection=ORDER_LIST_PROJECTION,
            )
            _take(data.get("_items") or [])
            if page % 10 == 0 or page == pages:
                log(
                    f"Fetched page {page}/{pages} "
                    f"({len(ids):,} to retry, {skipped_history:,} skipped on history, "
                    f"{skipped_status:,} skipped on status)"
                )

    not_returned = [oid for oid in wanted_ids if oid not in seen] if wanted_ids else []
    plan = {
        "accountId": account,
        "since": since,
        "until": until,
        "statuses": include_statuses,
        "excludeStatuses": excluded_statuses,
        "excludeHistoryStatuses": excluded_history,
        "requireHistoryStatuses": required_history,
        "channels": channel_list,
        "locationIds": locations,
        "channelOrderDisplayIds": display_ids,
        "requestedOrderIds": len(wanted_ids),
        "matchedTotal": api_total,
        "uniqueMatched": len(seen),
        "toRetry": len(ids),
        "skippedStatus": skipped_status,
        "skippedHistory": skipped_history,
        "skippedRequireHistory": skipped_require,
        "skippedManualRetry": skipped_history,
        "skipHistoryStatus": (
            excluded_history[0] if len(excluded_history) == 1 else None
        ),
        "notReturned": len(not_returned),
        "notReturnedSample": not_returned[:50],
        "orderIds": ids,
    }
    log(
        f"Dry-run plan: {plan['toRetry']:,} would retry, "
        f"{skipped_history:,} skipped on history, "
        f"{skipped_status:,} skipped on status, "
        f"{skipped_require:,} missing required history, "
        f"{plan['notReturned']:,} not returned, "
        f"{plan['uniqueMatched']:,} unique of {plan['matchedTotal']:,} reported"
    )
    return plan


def fetch_failed_order_ids(
    account: str,
    *,
    since: str,
    until: str,
    statuses: Optional[list[int]] = None,
    progress_callback: ProgressCallback = None,
) -> list[str]:
    """Fetch failed order IDs, skipping any with Manual Retry (123) in history."""
    plan = scan_failed_orders(
        account,
        since=since,
        until=until,
        statuses=statuses,
        progress_callback=progress_callback,
    )
    return list(plan["orderIds"])


def fetch_failed_orders(
    account: str,
    *,
    since: str,
    until: str,
    statuses: Optional[list[int]] = None,
    preview: bool = False,
    progress_callback: ProgressCallback = None,
) -> list[dict]:
    """Fetch failed orders.

    Default: lean projection + skip history-123 (wraps ``fetch_failed_order_ids``).
    ``preview=True``: small field set for UI tables (still sequential pages).
    """
    if not preview:
        return [
            {"_id": oid}
            for oid in fetch_failed_order_ids(
                account,
                since=since,
                until=until,
                statuses=statuses,
                progress_callback=progress_callback,
            )
        ]

    statuses = list(statuses or DEFAULT_STATUSES)

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    where = _build_order_where(
        account,
        since=since,
        until=until,
        statuses=statuses,
        exclude_statuses=[],
        channels=[],
        location_ids=[],
        channel_order_display_ids=[],
        order_ids=[],
        apply_status=True,
    )
    page1 = _fetch_orders_page(where, page=1, projection=ORDER_PREVIEW_PROJECTION)
    meta = page1.get("_meta") or {}
    total = int(meta.get("total") or 0)
    max_results = int(meta.get("max_results") or PAGE_SIZE)
    pages = max(1, (total + max_results - 1) // max_results) if total else 1
    log(f"Found {total:,} failed order(s) — preview fields ({pages} page(s))")

    by_id: dict[str, dict] = {}
    skipped_manual_retry = 0
    for item in page1.get("_items") or []:
        oid = item.get("_id")
        if not oid:
            continue
        if order_has_history_status(item, SKIP_IF_HISTORY_STATUS):
            skipped_manual_retry += 1
            continue
        by_id[oid] = item

    for page in range(2, pages + 1):
        data = _fetch_orders_page(
            where,
            page=page,
            max_results=max_results,
            projection=ORDER_PREVIEW_PROJECTION,
        )
        for item in data.get("_items") or []:
            oid = item.get("_id")
            if not oid:
                continue
            if order_has_history_status(item, SKIP_IF_HISTORY_STATUS):
                skipped_manual_retry += 1
                continue
            by_id[oid] = item
        if page % 10 == 0 or page == pages:
            log(
                f"Fetched preview page {page}/{pages} "
                f"({len(by_id):,} unique, {skipped_manual_retry:,} skipped)"
            )

    return list(by_id.values())


def _order_id(order: Any) -> str:
    if isinstance(order, str):
        return order.strip()
    if isinstance(order, dict):
        return str(order.get("_id") or "").strip()
    return ""


def retry_order(order_id: str, *, headers: Optional[dict] = None) -> dict:
    """Call retail retry for one order. Returns a slim result dict."""
    oid = (order_id or "").strip()
    if not oid:
        return {
            "orderId": order_id,
            "success": False,
            "statusCode": None,
            "error": "Missing order ID",
        }

    url = f"{RETRY_BASE}/{oid}"
    try:
        response = requests.get(
            url, headers=headers or _retail_headers(), timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        return {
            "orderId": oid,
            "success": False,
            "statusCode": None,
            "error": str(exc),
        }

    ok = _http_ok(response)
    return {
        "orderId": oid,
        "success": ok,
        "statusCode": response.status_code,
        # Only keep error text — storing every response body OOMs on large runs.
        "error": None if ok else _api_response_detail(response),
    }


def _process_retry_batch(
    order_ids: list[str],
    *,
    headers: dict,
    max_workers: int,
    batch_number: int,
    total_batches: int,
    completed_before: int = 0,
    grand_total: int = 0,
    progress_callback: ProgressCallback = None,
) -> list[dict]:
    """Retry one batch of order IDs in parallel."""

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    batch_total = len(order_ids)
    overall = grand_total or (completed_before + batch_total)
    log(
        f"Batch {batch_number}/{total_batches}: starting {batch_total} order(s) "
        f"with {max_workers} workers "
        f"(overall {completed_before}/{overall})"
    )
    results: list[dict] = []
    done = 0
    ok = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(retry_order, oid, headers=headers): oid for oid in order_ids
        }
        for future in as_completed(futures):
            oid = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "orderId": oid,
                    "success": False,
                    "statusCode": None,
                    "error": str(exc),
                }
            result["dryRun"] = False
            results.append(result)
            done += 1
            if result.get("success"):
                ok += 1
            else:
                fail += 1

            # Live counter so long batches don't look stuck.
            if done == 1 or done == batch_total or done % 10 == 0:
                overall_done = completed_before + done
                log(
                    f"Batch {batch_number}/{total_batches}: "
                    f"{done}/{batch_total} "
                    f"(ok={ok} fail={fail}) · "
                    f"overall {overall_done}/{overall}"
                )

    log(
        f"Batch {batch_number}/{total_batches} done — "
        f"ok={ok} fail={fail}"
    )
    return results


def _sleep_interruptible(
    seconds: int,
    *,
    should_cancel: CancelCheck = None,
    progress_callback: ProgressCallback = None,
) -> bool:
    """Sleep up to ``seconds``. Returns True if cancelled early."""
    if seconds <= 0:
        return bool(should_cancel and should_cancel())

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    log(f"Sleeping {seconds}s before next batch … (Cancel to stop)")
    end = time.time() + seconds
    last_announce = time.time()
    while True:
        if should_cancel and should_cancel():
            log("Cancel requested — stopping before next batch")
            return True
        remaining = end - time.time()
        if remaining <= 0:
            break
        # Refresh countdown about once a second so the UI stays alive.
        now = time.time()
        if now - last_announce >= 1.0:
            log(f"Sleeping {int(remaining)}s before next batch … (Cancel to stop)")
            last_announce = now
        time.sleep(min(0.5, remaining))
    return bool(should_cancel and should_cancel())


def retry_orders(
    orders: list,
    *,
    dry_run: bool = False,
    max_workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    sleep_seconds: int = SLEEP_SECONDS,
    progress_callback: ProgressCallback = None,
    should_cancel: CancelCheck = None,
) -> list[dict]:
    """Retry each order in batches; dry_run only lists what would be retried.

    ``orders`` may be dicts with ``_id`` or plain ID strings.
    Between batches (when more remain), sleeps ``sleep_seconds``.

    If ``should_cancel`` returns True before a batch or during the inter-batch
    sleep, returns results completed so far (partial run).
    """

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    order_ids = [_order_id(o) for o in orders]
    order_ids = [oid for oid in order_ids if oid]
    seen: set[str] = set()
    unique_ids: list[str] = []
    for oid in order_ids:
        if oid in seen:
            continue
        seen.add(oid)
        unique_ids.append(oid)

    batch_size = max(1, int(batch_size))
    sleep_seconds = max(0, int(sleep_seconds))
    total = len(unique_ids)
    total_batches = max(1, (total + batch_size - 1) // batch_size) if total else 0

    if dry_run:
        results = [
            {
                "orderId": oid,
                "success": True,
                "statusCode": None,
                "error": None,
                "dryRun": True,
            }
            for oid in unique_ids
        ]
        log(
            f"Dry run — would retry {len(results)} order(s) "
            f"in batches of {batch_size}"
        )
        return results

    if not unique_ids:
        log("No orders to retry")
        return []

    log(f"Retrying {total} failed order(s) in batches of {batch_size}")
    headers = _retail_headers()
    results: list[dict] = []
    completed_before = 0

    for batch_number, start in enumerate(range(0, total, batch_size), start=1):
        if should_cancel and should_cancel():
            log(
                f"Cancelled — completed {completed_before}/{total} "
                f"(stopped before batch {batch_number}/{total_batches})"
            )
            return results

        batch = unique_ids[start : start + batch_size]
        batch_results = _process_retry_batch(
            batch,
            headers=headers,
            max_workers=max_workers,
            batch_number=batch_number,
            total_batches=total_batches,
            completed_before=completed_before,
            grand_total=total,
            progress_callback=progress_callback,
        )
        results.extend(batch_results)
        completed_before += len(batch)

        if start + batch_size < total:
            if _sleep_interruptible(
                sleep_seconds,
                should_cancel=should_cancel,
                progress_callback=progress_callback,
            ):
                log(
                    f"Cancelled — completed {completed_before}/{total} "
                    f"(stopped after batch {batch_number}/{total_batches})"
                )
                return results

    log(f"Done — {completed_before}/{total} retried")
    return results


def _result_from_plan(
    plan: dict,
    *,
    dry_run: bool,
    order_ids: list[str],
    results: list,
    cancelled: bool,
) -> dict:
    ok = sum(1 for r in results if r.get("success"))
    return {
        "accountId": plan["accountId"],
        "since": plan["since"],
        "until": plan["until"],
        "statuses": plan["statuses"],
        "excludeStatuses": plan.get("excludeStatuses") or [],
        "excludeHistoryStatuses": plan.get("excludeHistoryStatuses") or [],
        "requireHistoryStatuses": plan.get("requireHistoryStatuses") or [],
        "dryRun": dry_run,
        "orderCount": len(order_ids),
        "matchedTotal": plan["matchedTotal"],
        "uniqueMatched": plan["uniqueMatched"],
        "toRetry": plan["toRetry"],
        "skippedStatus": plan.get("skippedStatus", 0),
        "skippedHistory": plan.get("skippedHistory", 0),
        "skippedRequireHistory": plan.get("skippedRequireHistory", 0),
        "skippedManualRetry": plan.get("skippedManualRetry", 0),
        "skipHistoryStatus": plan.get("skipHistoryStatus"),
        "notReturned": plan.get("notReturned", 0),
        "successCount": ok,
        "failureCount": len(results) - ok,
        "orderIds": order_ids,
        "results": results,
        "cancelled": cancelled,
    }


def run_retry_failed_orders(
    account: str,
    *,
    since: str = "",
    until: str = "",
    statuses: Optional[list[int]] = None,
    exclude_statuses: Optional[list[int]] = None,
    exclude_history_statuses: Optional[list[int]] = None,
    require_history_statuses: Optional[list[int]] = None,
    order_ids: Optional[list[str]] = None,
    location_ids: Optional[list[str]] = None,
    channels: Optional[list[int]] = None,
    channel_order_display_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    max_workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    sleep_seconds: int = SLEEP_SECONDS,
    progress_callback: ProgressCallback = None,
    should_cancel: CancelCheck = None,
) -> dict:
    """Scan orders (with skip stats) and optionally retry them."""
    plan = scan_failed_orders(
        account,
        since=since,
        until=until,
        statuses=statuses,
        exclude_statuses=exclude_statuses,
        exclude_history_statuses=exclude_history_statuses,
        require_history_statuses=require_history_statuses,
        order_ids=order_ids,
        location_ids=location_ids,
        channels=channels,
        channel_order_display_ids=channel_order_display_ids,
        progress_callback=progress_callback,
    )
    matched_ids = list(plan["orderIds"])
    if should_cancel and should_cancel():
        if progress_callback:
            progress_callback("Cancelled before retries started")
        return _result_from_plan(
            plan,
            dry_run=dry_run,
            order_ids=matched_ids,
            results=[],
            cancelled=True,
        )
    results = retry_orders(
        matched_ids,
        dry_run=dry_run,
        max_workers=max_workers,
        batch_size=batch_size,
        sleep_seconds=sleep_seconds,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
    )
    cancelled = bool(should_cancel and should_cancel())
    return _result_from_plan(
        plan,
        dry_run=dry_run,
        order_ids=matched_ids,
        results=results,
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# Background jobs for Streamlit cancel button (thread-safe progress store)
# ---------------------------------------------------------------------------

_RETRY_JOBS: dict[str, dict[str, Any]] = {}
_RETRY_JOBS_LOCK = threading.Lock()


def start_retry_orders_job(
    orders: list,
    *,
    max_workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    sleep_seconds: int = SLEEP_SECONDS,
) -> str:
    """Start ``retry_orders`` in a background thread. Returns job id."""
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    job: dict[str, Any] = {
        "id": job_id,
        "cancel": cancel_event,
        "message": "Starting…",
        "done": False,
        "cancelled": False,
        "error": None,
        "results": None,
        "total": len(orders),
    }
    with _RETRY_JOBS_LOCK:
        _RETRY_JOBS[job_id] = job

    def worker() -> None:
        def on_progress(msg: str) -> None:
            job["message"] = msg

        try:
            results = retry_orders(
                orders,
                dry_run=False,
                max_workers=max_workers,
                batch_size=batch_size,
                sleep_seconds=sleep_seconds,
                progress_callback=on_progress,
                should_cancel=cancel_event.is_set,
            )
            job["results"] = results
            job["cancelled"] = cancel_event.is_set()
            if job["cancelled"]:
                job["message"] = (
                    f"Cancelled — completed {len(results)}/{len(orders)}"
                )
            else:
                job["message"] = f"Done — {len(results)}/{len(orders)} retried"
        except Exception as exc:
            job["error"] = str(exc)
            job["message"] = f"Failed: {exc}"
        finally:
            job["done"] = True

    threading.Thread(
        target=worker, daemon=True, name=f"retry-job-{job_id[:8]}"
    ).start()
    return job_id


def get_retry_job(job_id: str) -> Optional[dict[str, Any]]:
    with _RETRY_JOBS_LOCK:
        job = _RETRY_JOBS.get(job_id)
    if not job:
        return None
    return {
        "id": job["id"],
        "message": job.get("message") or "",
        "done": bool(job.get("done")),
        "cancelled": bool(job.get("cancelled")),
        "error": job.get("error"),
        "results": job.get("results"),
        "total": job.get("total") or 0,
    }


def cancel_retry_job(job_id: str) -> bool:
    """Request cancel; takes effect between batches / during inter-batch sleep."""
    with _RETRY_JOBS_LOCK:
        job = _RETRY_JOBS.get(job_id)
    if not job:
        return False
    job["cancel"].set()
    job["message"] = "Cancel requested — will stop after the current batch…"
    return True


def clear_retry_job(job_id: str) -> None:
    with _RETRY_JOBS_LOCK:
        _RETRY_JOBS.pop(job_id, None)


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
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=(
            "Find retail orders and retry them via GET /retry/{orderId} "
            "with X-Deliverect-Version: retail."
        )
    )
    parser.add_argument(
        "--account",
        default="",
        help="Deliverect account ID (default: ACCOUNT_ID env var)",
    )
    parser.add_argument(
        "--date",
        default="",
        help="London calendar day YYYY-MM-DD (uses toolkit day window). Default: today London, unless order IDs are passed.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Rolling UTC lookback in days (overrides --date)",
    )
    parser.add_argument(
        "--since",
        default="",
        help="UTC ISO lower bound (use with --until; overrides --date/--days)",
    )
    parser.add_argument(
        "--until",
        default="",
        help="UTC ISO upper bound (use with --since)",
    )
    parser.add_argument(
        "--order-ids",
        default="",
        help="Order IDs separated by commas, spaces, or newlines",
    )
    parser.add_argument(
        "--order-id",
        action="append",
        default=[],
        help="Order ID (repeatable)",
    )
    parser.add_argument(
        "--status",
        type=int,
        action="append",
        dest="statuses",
        help=(
            f"Include current status (repeatable; default {DEFAULT_STATUSES} "
            "when no order IDs are passed)"
        ),
    )
    parser.add_argument(
        "--exclude-status",
        type=int,
        action="append",
        dest="exclude_statuses",
        help="Drop orders whose current status is this code (repeatable)",
    )
    parser.add_argument(
        "--exclude-history-status",
        type=int,
        action="append",
        dest="exclude_history_statuses",
        help=(
            "Skip orders whose statusHistory contains this code "
            f"(repeatable; default {SKIP_IF_HISTORY_STATUS})"
        ),
    )
    parser.add_argument(
        "--include-retried",
        action="store_true",
        help=f"Do not skip history status {SKIP_IF_HISTORY_STATUS} (Manual Retry)",
    )
    parser.add_argument(
        "--require-history-status",
        type=int,
        action="append",
        dest="require_history_statuses",
        help="Keep only orders whose history contains this code (repeatable, any match)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        action="append",
        dest="channels",
        help="Channel id (repeatable)",
    )
    parser.add_argument(
        "--location",
        action="append",
        default=[],
        dest="locations",
        help="Location ID (repeatable, or comma-separated)",
    )
    parser.add_argument(
        "--channel-order-id",
        action="append",
        default=[],
        dest="channel_order_ids",
        help="channelOrderDisplayId (repeatable, or comma-separated)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching orders without calling /retry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually call /retry (required unless --dry-run)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Parallel retry workers per batch (default: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Orders per batch before sleeping (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=SLEEP_SECONDS,
        help=f"Seconds to sleep between batches (default: {SLEEP_SECONDS})",
    )
    args = parser.parse_args()

    account = _resolve_account_id(args.account)
    since = (args.since or "").strip()
    until = (args.until or "").strip()
    order_ids = parse_order_ids(args.order_ids)
    order_ids.extend(parse_order_ids("\n".join(args.order_id or [])))
    order_ids = parse_order_ids("\n".join(order_ids))
    location_ids = parse_order_ids("\n".join(args.locations or []))
    display_ids = parse_order_ids("\n".join(args.channel_order_ids or []))

    if (since and not until) or (until and not since):
        print("Provide both --since and --until, or neither.", file=sys.stderr)
        sys.exit(1)

    explicit_window = bool(since or until or args.days or (args.date or "").strip())
    specific_ids = bool(order_ids or display_ids)
    if specific_ids and not explicit_window:
        since, until = "", ""
    elif since and until:
        pass
    elif args.days:
        try:
            since, until = window_from_days(args.days)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        day = (
            datetime.strptime(args.date, "%Y-%m-%d").date()
            if args.date.strip()
            else london_now().date()
        )
        since, until = window_for_london_date(day)

    if args.statuses is not None:
        statuses = args.statuses
    elif specific_ids:
        statuses = []
    else:
        statuses = None

    if args.include_retried:
        exclude_history = list(args.exclude_history_statuses or [])
    elif args.exclude_history_statuses:
        exclude_history = list(args.exclude_history_statuses)
    else:
        exclude_history = None

    dry_run = bool(args.dry_run) or not args.execute
    if not args.execute and not args.dry_run:
        print(
            "No --execute flag — running dry-run. Pass --execute to retry.",
            file=sys.stderr,
        )

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        result = run_retry_failed_orders(
            account,
            since=since,
            until=until,
            statuses=statuses,
            exclude_statuses=args.exclude_statuses,
            exclude_history_statuses=exclude_history,
            require_history_statuses=args.require_history_statuses,
            order_ids=order_ids,
            location_ids=location_ids,
            channels=args.channels,
            channel_order_display_ids=display_ids,
            dry_run=dry_run,
            max_workers=args.workers,
            batch_size=args.batch_size,
            sleep_seconds=args.sleep,
            progress_callback=progress,
        )
    except Exception as exc:
        print(f"Retry failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Keep stdout lean: full per-order dump OOMs / floods on large days.
    failures = [r for r in result["results"] if not r.get("success")]
    summary = {
        "accountId": result["accountId"],
        "since": result["since"],
        "until": result["until"],
        "statuses": result["statuses"],
        "dryRun": result["dryRun"],
        "matchedTotal": result.get("matchedTotal"),
        "uniqueMatched": result.get("uniqueMatched"),
        "toRetry": result.get("toRetry", result["orderCount"]),
        "skippedStatus": result.get("skippedStatus"),
        "skippedHistory": result.get("skippedHistory"),
        "skippedRequireHistory": result.get("skippedRequireHistory"),
        "skippedManualRetry": result.get("skippedManualRetry"),
        "notReturned": result.get("notReturned"),
        "excludeStatuses": result.get("excludeStatuses"),
        "excludeHistoryStatuses": result.get("excludeHistoryStatuses"),
        "skipHistoryStatus": result.get("skipHistoryStatus"),
        "orderCount": result["orderCount"],
        "successCount": result["successCount"],
        "failureCount": result["failureCount"],
        "failures": [
            {
                "orderId": r["orderId"],
                "statusCode": r.get("statusCode"),
                "error": r.get("error"),
            }
            for r in failures[:200]
        ],
        "failuresTruncated": max(0, len(failures) - 200),
    }
    if dry_run:
        summary["sampleOrderIds"] = result.get("orderIds", [])[:20]
    print(json.dumps(summary, indent=2))
    if result["failureCount"] and not dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
