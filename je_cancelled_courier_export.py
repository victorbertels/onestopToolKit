"""Just Eat cancelled-order export with last courier update status.

Used to spot likely courier fraud / uncollected-return cases: cancelled orders
whose last courier status is Arrived at Pickup, En Route To Dropoff, or Delivered.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests
from auth import getHeaders

from utils import getAllLocations

API_BASE = "https://api.deliverect.io"


def _http_ok(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def _api_response_detail(response: requests.Response) -> str:
    try:
        body = response.text
    except Exception:
        body = ""
    snippet = (body or "")[:400]
    return f"HTTP {response.status_code}: {snippet}"


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
            f"({len(duplicates)} total)."
        )

# Just Eat Retail (6009) + legacy Just Eat (9)
JUST_EAT_CHANNELS = [6009, 9]
CANCELLED_STATUS = 110
DEFAULT_DAYS = 7
# Deliverect rejects `_created` lower bounds older than ~90 days
# ("Attempted to query orders that are too old time frame").
MAX_ORDER_LOOKBACK_DAYS = 89
# Keep each request window moderate so huge result sets don't time out.
MAX_QUERY_DAYS = 14
PAGE_SIZE = 100
MAX_WORKERS = 10

COURIER_STATUS_LABELS = {
    80: "In Delivery",
    81: "Delivery Created",
    83: "En Route to Pickup",
    84: "Almost at Pickup",
    85: "Arrived at Pickup",
    87: "En Route To Dropoff",
    89: "Arrived At Drop Off",
    90: "Delivered",
    115: "Delivery Canceled",
}

# User interest set for fraud / unpaid-collected investigation
FRAUD_INTEREST_STATUSES = {85, 87, 90}

CHANNEL_LABELS = {
    6009: "Just Eat",
    9: "Just Eat (legacy)",
}

ORDER_TYPE_LABELS = {1: "Delivery", 2: "Pickup", 3: "Eat-in"}
DELIVERY_TYPE_LABELS = {0: "Unknown", 1: "Delivery", 2: "Pickup", 3: "Eat-in"}

ORDER_PROJECTION = {
    "_created": 1,
    "_updated": 1,
    "date": 1,
    "status": 1,
    "channel": 1,
    "location": 1,
    "channelLink": 1,
    "channelOrderId": 1,
    "channelOrderDisplayId": 1,
    "channelOrderKey": 1,
    "courierUpdateHistory": 1,
    "courier": 1,
    "payment": 1,
    "total": 1,
    "taxTotal": 1,
    "deliveryType": 1,
    "orderType": 1,
    "pickupTime": 1,
    "deliveryTime": 1,
    "deliveryIsAsap": 1,
    "customer": 1,
    "decimalDigits": 1,
    "deliveryCost": 1,
    "tip": 1,
    "driverTip": 1,
    "discountTotal": 1,
    "serviceCharge": 1,
    "posReceiptId": 1,
    "posId": 1,
    "note": 1,
    "by": 1,
}

CSV_COLUMNS = [
    "Created (UTC)",
    "Order date (UTC)",
    "Location",
    "Location ID",
    "Channel",
    "Channel ID",
    "Channel link ID",
    "Order ID (Deliverect)",
    "Channel order ID",
    "Channel order display ID",
    "Channel order key",
    "POS receipt ID",
    "Status",
    "Status code",
    "Order type",
    "Delivery type",
    "Pickup time (UTC)",
    "Delivery time (UTC)",
    "ASAP",
    "Customer",
    "Customer phone",
    "Total",
    "Delivery cost",
    "Tip",
    "Discount total",
    "Payment",
    "Courier delivery by",
    "Courier current status",
    "Last courier status code",
    "Last courier status",
    "Last courier update (UTC)",
    "Courier fraud interest (arrived pickup / en-route dropoff / delivered)",
    "Courier update count",
    "Note",
]


ProgressCallback = Optional[Callable[[str], None]]


def to_z_utc(dt: datetime) -> str:
    u = dt.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S") + f".{u.microsecond // 1000:03d}Z"


def window_from_days(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, int(days)))
    return to_z_utc(start), to_z_utc(now)


def _parse_z_utc(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def chunk_time_window(
    since: str,
    until: str,
    *,
    max_days: int = MAX_QUERY_DAYS,
) -> list[tuple[str, str]]:
    """Split [since, until] into consecutive chunks of at most max_days.

    Boundary overlaps (same _created on the cut) are fine — callers dedupe by _id.
    """
    start = _parse_z_utc(since)
    end = _parse_z_utc(until)
    if end < start:
        raise ValueError(f"until must be >= since (got {since} → {until})")
    if max_days < 1:
        raise ValueError("max_days must be >= 1")

    chunks: list[tuple[str, str]] = []
    cursor = start
    step = timedelta(days=max_days)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        chunks.append((to_z_utc(cursor), to_z_utc(chunk_end)))
        cursor = chunk_end
    if not chunks:
        chunks.append((to_z_utc(start), to_z_utc(end)))
    return chunks


def courier_status_label(status: Any) -> str:
    if status is None:
        return ""
    if status in COURIER_STATUS_LABELS:
        return COURIER_STATUS_LABELS[status]
    return str(status)


def last_courier_update(history: Any) -> tuple[Optional[int], str, str]:
    """Return (status_code, label, received_iso) for the latest courier update."""
    if not history or not isinstance(history, list):
        return None, "", ""
    entries = [h for h in history if isinstance(h, dict)]
    if not entries:
        return None, "", ""
    entries.sort(key=lambda h: h.get("received") or h.get("arrivalTime") or "")
    last = entries[-1]
    status = last.get("status")
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    return status_int, courier_status_label(status_int if status_int is not None else status), last.get("received") or ""


def _money(cents: Any, digits: Any = 2) -> str:
    if cents is None:
        return ""
    try:
        return f"{int(cents) / (10 ** int(digits or 2)):.2f}"
    except (TypeError, ValueError):
        return str(cents)


def _customer_name(customer: Any) -> str:
    if not isinstance(customer, dict):
        return ""
    parts = [customer.get("name") or "", customer.get("companyName") or ""]
    return " ".join(p for p in parts if p).strip() or (customer.get("phoneNumber") or "")


def _get_with_retry(url: str, *, retries: int = 3) -> requests.Response:
    last: Optional[requests.Response] = None
    for attempt in range(retries):
        response = requests.get(url, headers=getHeaders(), timeout=120)
        last = response
        if response.status_code != 429:
            return response
        time.sleep(1.5 * (attempt + 1))
    return last  # type: ignore[return-value]


def _fetch_orders_page(
    account: str,
    channel: int,
    since: str,
    until: str,
    page: int,
    max_results: int = PAGE_SIZE,
) -> dict:
    where = {
        "account": account,
        "channel": channel,
        "status": CANCELLED_STATUS,
        "_created": {"$gte": since, "$lte": until},
    }
    params = (
        f"where={quote(json.dumps(where, separators=(',', ':')))}"
        f"&projection={quote(json.dumps(ORDER_PROJECTION, separators=(',', ':')))}"
        f"&max_results={max_results}&page={page}&sort=-_created"
    )
    url = f"{API_BASE}/orders?{params}"
    response = _get_with_retry(url)
    if not _http_ok(response):
        raise RuntimeError(
            f"GET orders channel={channel} page={page}: {_api_response_detail(response)}"
        )
    return response.json()


def _fetch_cancelled_orders_window(
    account: str,
    *,
    since: str,
    until: str,
    channels: list[int],
    progress_callback: ProgressCallback = None,
) -> list[dict]:
    """Fetch cancelled Just Eat orders for a single API-safe time window."""
    orders: list[dict] = []

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    for channel in channels:
        page1 = _fetch_orders_page(account, channel, since, until, 1)
        meta = page1.get("_meta") or {}
        total = int(meta.get("total") or 0)
        max_results = int(meta.get("max_results") or PAGE_SIZE)
        pages = max(1, (total + max_results - 1) // max_results) if total else 1
        log(f"Channel {channel}: {total:,} cancelled orders ({pages} pages)")
        orders.extend(page1.get("_items") or [])

        if pages <= 1:
            continue

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _fetch_orders_page, account, channel, since, until, page, max_results
                ): page
                for page in range(2, pages + 1)
            }
            done = 0
            for future in as_completed(futures):
                data = future.result()
                orders.extend(data.get("_items") or [])
                done += 1
                if done % 10 == 0 or done == pages - 1:
                    log(f"Channel {channel}: fetched {done}/{pages - 1} extra pages")

    return orders


def fetch_cancelled_just_eat_orders(
    account: str,
    *,
    since: str,
    until: str,
    channels: Optional[list[int]] = None,
    progress_callback: ProgressCallback = None,
    max_query_days: int = MAX_QUERY_DAYS,
) -> list[dict]:
    """Fetch cancelled Just Eat orders in [since, until] (UTC ISO).

    Large windows are automatically split into chunks so Deliverect's max
    order query timeframe is not exceeded.
    """
    channels = list(channels or JUST_EAT_CHANNELS)
    chunks = chunk_time_window(since, until, max_days=max_query_days)

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    orders: list[dict] = []
    if len(chunks) > 1:
        log(f"Splitting into {len(chunks)} windows of ≤{max_query_days} days")

    for index, (chunk_since, chunk_until) in enumerate(chunks, start=1):
        if len(chunks) > 1:
            log(f"Chunk {index}/{len(chunks)}: {chunk_since} → {chunk_until}")
        orders.extend(
            _fetch_cancelled_orders_window(
                account,
                since=chunk_since,
                until=chunk_until,
                channels=channels,
                progress_callback=progress_callback,
            )
        )

    by_id = {o["_id"]: o for o in orders if o.get("_id")}
    unique = list(by_id.values())
    _assert_unique_ids(unique, "orders")
    log(f"Fetched {len(unique):,} unique cancelled Just Eat orders")
    return unique


def build_export_rows(
    orders: list[dict],
    location_names: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in orders:
        status_code, status_label, received = last_courier_update(
            order.get("courierUpdateHistory")
        )
        digits = order.get("decimalDigits") or 2
        courier = order.get("courier") if isinstance(order.get("courier"), dict) else {}
        customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
        payment = order.get("payment")
        if isinstance(payment, (dict, list)):
            payment_value = json.dumps(payment, separators=(",", ":"))
        else:
            payment_value = "" if payment is None else str(payment)

        fraud_flag = "Y" if status_code in FRAUD_INTEREST_STATUSES else ""
        location_id = order.get("location") or ""
        rows.append(
            {
                "Created (UTC)": order.get("_created") or "",
                "Order date (UTC)": order.get("date") or "",
                "Location": location_names.get(location_id, location_id),
                "Location ID": location_id,
                "Channel": CHANNEL_LABELS.get(
                    order.get("channel"), str(order.get("channel") or "")
                ),
                "Channel ID": order.get("channel") if order.get("channel") is not None else "",
                "Channel link ID": order.get("channelLink") or "",
                "Order ID (Deliverect)": order.get("_id") or "",
                "Channel order ID": order.get("channelOrderId") or "",
                "Channel order display ID": order.get("channelOrderDisplayId") or "",
                "Channel order key": order.get("channelOrderKey") or "",
                "POS receipt ID": order.get("posReceiptId") or "",
                "Status": "Canceled",
                "Status code": order.get("status") if order.get("status") is not None else "",
                "Order type": ORDER_TYPE_LABELS.get(
                    order.get("orderType"), order.get("orderType") or ""
                ),
                "Delivery type": DELIVERY_TYPE_LABELS.get(
                    order.get("deliveryType"), order.get("deliveryType") or ""
                ),
                "Pickup time (UTC)": order.get("pickupTime") or "",
                "Delivery time (UTC)": order.get("deliveryTime") or "",
                "ASAP": (
                    order.get("deliveryIsAsap")
                    if order.get("deliveryIsAsap") is not None
                    else ""
                ),
                "Customer": _customer_name(customer),
                "Customer phone": (customer.get("phoneNumber") or "") if customer else "",
                "Total": _money(order.get("total"), digits),
                "Delivery cost": _money(order.get("deliveryCost"), digits),
                "Tip": _money(order.get("tip"), digits),
                "Discount total": _money(order.get("discountTotal"), digits),
                "Payment": payment_value,
                "Courier delivery by": courier.get("deliveryBy") or "",
                "Courier current status": courier_status_label(courier.get("status")),
                "Last courier status code": status_code if status_code is not None else "",
                "Last courier status": status_label,
                "Last courier update (UTC)": received,
                "Courier fraud interest (arrived pickup / en-route dropoff / delivered)": fraud_flag,
                "Courier update count": len(order.get("courierUpdateHistory") or []),
                "Note": str(order.get("note") or "").replace("\n", " ")[:500],
            }
        )
    rows.sort(key=lambda row: row.get("Created (UTC)") or "", reverse=True)
    return rows


def summarise_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    breakdown: Counter[str] = Counter()
    fraud_interest = 0
    for row in rows:
        label = row.get("Last courier status") or "(none)"
        breakdown[str(label)] += 1
        if row.get(
            "Courier fraud interest (arrived pickup / en-route dropoff / delivered)"
        ) == "Y":
            fraud_interest += 1
    return {
        "orderCount": len(rows),
        "fraudInterestCount": fraud_interest,
        "lastCourierStatusBreakdown": dict(breakdown.most_common()),
    }


def rows_to_csv_text(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    return buffer.getvalue()


def export_filename(account: str, *, days: int, stamp: Optional[datetime] = None) -> str:
    when = stamp or datetime.now(timezone.utc)
    return (
        f"onestop-justeat-cancelled-courier-"
        f"{account[:8]}-{days}d-{when.strftime('%Y%m%dT%H%M%SZ')}.csv"
    )


def _clamp_order_window(
    since: str,
    until: str,
    *,
    max_lookback_days: int = MAX_ORDER_LOOKBACK_DAYS,
) -> tuple[str, str, Optional[str]]:
    """Clamp `since` so it is not older than Deliverect allows. Returns (since, until, note)."""
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=max_lookback_days)
    start = _parse_z_utc(since)
    end = _parse_z_utc(until)
    note = None
    if start < earliest:
        note = (
            f"Clamped lookback to {max_lookback_days} days "
            f"(API cannot query orders older than that; requested since {since})"
        )
        start = earliest
    if end < start:
        end = now
    return to_z_utc(start), to_z_utc(end), note


def run_je_cancelled_courier_export(
    account: str,
    *,
    days: int = DEFAULT_DAYS,
    since: Optional[str] = None,
    until: Optional[str] = None,
    channels: Optional[list[int]] = None,
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    """Fetch cancelled JE orders and return rows + CSV + summary."""
    if since and until:
        window_since, window_until = since, until
        days_used = days
    else:
        window_since, window_until = window_from_days(days)
        days_used = max(1, int(days))

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    window_since, window_until, clamp_note = _clamp_order_window(
        window_since, window_until
    )
    if clamp_note:
        log(clamp_note)
        # Reflect effective lookback in filename/summary when using --days
        if not (since and until):
            days_used = min(
                days_used,
                max(
                    1,
                    int(
                        (_parse_z_utc(window_until) - _parse_z_utc(window_since)).total_seconds()
                        // 86400
                    )
                    or 1,
                ),
            )

    log(f"Window (UTC): {window_since} → {window_until}")
    orders = fetch_cancelled_just_eat_orders(
        account,
        since=window_since,
        until=window_until,
        channels=channels,
        progress_callback=progress_callback,
    )
    log("Loading location names…")
    locations = getAllLocations(account)
    location_names = {
        loc.get("_id"): loc.get("name") or ""
        for loc in locations
        if loc.get("_id")
    }
    rows = build_export_rows(orders, location_names)
    summary = summarise_rows(rows)
    csv_text = rows_to_csv_text(rows)
    return {
        "accountId": account,
        "channels": list(channels or JUST_EAT_CHANNELS),
        "status": CANCELLED_STATUS,
        "days": days_used,
        "since": window_since,
        "until": window_until,
        "rows": rows,
        "csv": csv_text,
        "summary": summary,
        "filename": export_filename(account, days=days_used),
    }
