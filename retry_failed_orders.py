"""Retry failed Deliverect retail orders via GET /retry/{orderId}.

Finds orders in failed statuses (120, 122) for an account + time window, then
retries each through the retail retry endpoint.

Order listing uses a minimal ``_id`` projection and sequential paging so large
days do not time out or OOM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

# Deliverect failed-order statuses (same set as ops retry scripts).
FAILED_STATUSES = [120, 122]
DEFAULT_STATUSES = list(FAILED_STATUSES)

PAGE_SIZE = 500
BATCH_SIZE = 1000
MAX_WORKERS = 10
SLEEP_SECONDS = 60
REQUEST_TIMEOUT = 120
MAX_ORDER_LOOKBACK_DAYS = 89

# Keep listing payloads tiny — full order docs OOM / time out on large days.
ORDER_ID_PROJECTION = {"_id": 1}
ORDER_PREVIEW_PROJECTION = {
    "_id": 1,
    "_created": 1,
    "status": 1,
    "channelOrderDisplayId": 1,
    "location": 1,
    "channel": 1,
}

ProgressCallback = Optional[Callable[[str], None]]


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


def _fetch_orders_page(
    account: str,
    *,
    since: str,
    until: str,
    statuses: list[int],
    page: int,
    max_results: int = PAGE_SIZE,
    projection: Optional[dict] = None,
) -> dict:
    where: dict[str, Any] = {
        "account": account,
        "status": {"$in": statuses},
        "_created": {"$gt": since, "$lt": until},
    }
    proj = projection if projection is not None else ORDER_ID_PROJECTION
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


def fetch_failed_order_ids(
    account: str,
    *,
    since: str,
    until: str,
    statuses: Optional[list[int]] = None,
    progress_callback: ProgressCallback = None,
) -> list[str]:
    """Fetch failed order ``_id`` values only (minimal projection, sequential pages)."""
    statuses = list(statuses or DEFAULT_STATUSES)

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    page1 = _fetch_orders_page(
        account,
        since=since,
        until=until,
        statuses=statuses,
        page=1,
        projection=ORDER_ID_PROJECTION,
    )
    meta = page1.get("_meta") or {}
    total = int(meta.get("total") or 0)
    max_results = int(meta.get("max_results") or PAGE_SIZE)
    pages = max(1, (total + max_results - 1) // max_results) if total else 1
    log(
        f"Found {total:,} failed order(s) — fetching IDs only "
        f"({pages} page(s), projection=_id)"
    )

    ids: list[str] = []
    seen: set[str] = set()

    def _take(items: list) -> None:
        for item in items:
            oid = item.get("_id") if isinstance(item, dict) else None
            if not oid or oid in seen:
                continue
            seen.add(oid)
            ids.append(oid)

    _take(page1.get("_items") or [])

    # Sequential pages — parallel page fetches OOM on large result sets.
    for page in range(2, pages + 1):
        data = _fetch_orders_page(
            account,
            since=since,
            until=until,
            statuses=statuses,
            page=page,
            max_results=max_results,
            projection=ORDER_ID_PROJECTION,
        )
        _take(data.get("_items") or [])
        if page % 10 == 0 or page == pages:
            log(f"Fetched ID page {page}/{pages} ({len(ids):,} unique so far)")

    log(f"Loaded {len(ids):,} unique order ID(s)")
    return ids


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

    Default: ``_id``-only projection (wraps ``fetch_failed_order_ids``).
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

    page1 = _fetch_orders_page(
        account,
        since=since,
        until=until,
        statuses=statuses,
        page=1,
        projection=ORDER_PREVIEW_PROJECTION,
    )
    meta = page1.get("_meta") or {}
    total = int(meta.get("total") or 0)
    max_results = int(meta.get("max_results") or PAGE_SIZE)
    pages = max(1, (total + max_results - 1) // max_results) if total else 1
    log(f"Found {total:,} failed order(s) — preview fields ({pages} page(s))")

    by_id: dict[str, dict] = {}
    for item in page1.get("_items") or []:
        oid = item.get("_id")
        if oid:
            by_id[oid] = item

    for page in range(2, pages + 1):
        data = _fetch_orders_page(
            account,
            since=since,
            until=until,
            statuses=statuses,
            page=page,
            max_results=max_results,
            projection=ORDER_PREVIEW_PROJECTION,
        )
        for item in data.get("_items") or []:
            oid = item.get("_id")
            if oid:
                by_id[oid] = item
        if page % 10 == 0 or page == pages:
            log(f"Fetched preview page {page}/{pages} ({len(by_id):,} unique)")

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


def retry_orders(
    orders: list,
    *,
    dry_run: bool = False,
    max_workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    sleep_seconds: int = SLEEP_SECONDS,
    progress_callback: ProgressCallback = None,
) -> list[dict]:
    """Retry each order in batches; dry_run only lists what would be retried.

    ``orders`` may be dicts with ``_id`` or plain ID strings.
    Between batches (when more remain), sleeps ``sleep_seconds``.
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
        if start + batch_size < total and sleep_seconds > 0:
            log(f"Sleeping {sleep_seconds}s before next batch …")
            time.sleep(sleep_seconds)

    log(f"Done — {completed_before}/{total} retried")
    return results


def run_retry_failed_orders(
    account: str,
    *,
    since: str,
    until: str,
    statuses: Optional[list[int]] = None,
    dry_run: bool = False,
    max_workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    sleep_seconds: int = SLEEP_SECONDS,
    progress_callback: ProgressCallback = None,
) -> dict:
    """Fetch failed order IDs (minimal projection) and retry them."""
    statuses = list(statuses or DEFAULT_STATUSES)
    order_ids = fetch_failed_order_ids(
        account,
        since=since,
        until=until,
        statuses=statuses,
        progress_callback=progress_callback,
    )
    results = retry_orders(
        order_ids,
        dry_run=dry_run,
        max_workers=max_workers,
        batch_size=batch_size,
        sleep_seconds=sleep_seconds,
        progress_callback=progress_callback,
    )
    ok = sum(1 for r in results if r.get("success"))
    fail = len(results) - ok
    return {
        "accountId": account,
        "since": since,
        "until": until,
        "statuses": statuses,
        "dryRun": dry_run,
        "orderCount": len(order_ids),
        "successCount": ok,
        "failureCount": fail,
        "orderIds": order_ids,
        "results": results,
    }


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
            "List failed retail orders (status 120/122) and retry them via "
            "GET /retry/{orderId} with X-Deliverect-Version: retail."
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
        help="London calendar day YYYY-MM-DD (uses toolkit day window). Default: today London.",
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
        "--status",
        type=int,
        action="append",
        dest="statuses",
        help=f"Failed status filter (repeatable; default: {DEFAULT_STATUSES})",
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

    if (since and not until) or (until and not since):
        print("Provide both --since and --until, or neither.", file=sys.stderr)
        sys.exit(1)

    if since and until:
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
            statuses=args.statuses,
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
