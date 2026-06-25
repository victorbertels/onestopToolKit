"""CLI: find and analyse inventory sync operation reports (Deliveroo listings)."""

import argparse
import os
import sys

from dotenv import load_dotenv

from utils import (
    INVENTORY_SYNC_DEFAULT_CHANNELS,
    INVENTORY_SYNC_DEFAULT_OPERATION_TYPES,
    INVENTORY_SYNC_SUCCESS_STATUS,
    analyse_inventory_sync_reports,
    getAllOperationReports,
    inventory_sync_created_range_for_date,
)

load_dotenv()


def _resolve_account_id(cli_account: str) -> str:
    account = (cli_account or os.getenv("ACCOUNT_ID") or "").strip()
    if not account:
        print(
            "Account ID required: set ACCOUNT_ID in the environment or pass --account",
            file=sys.stderr,
        )
        sys.exit(1)
    return account


def _resolve_created_range(args) -> tuple[str, str]:
    if args.after and args.before:
        return args.after, args.before
    if args.date:
        return inventory_sync_created_range_for_date(args.date)
    print("Provide --date YYYY-MM-DD or both --after and --before.", file=sys.stderr)
    sys.exit(1)


def _print_analysis(result: dict, *, verbose: bool) -> None:
    if verbose:
        for detail in result["rate_limited_details"]:
            print(detail["_id"])
            message = detail.get("message") or ""
            if message:
                print(message[:90])
            print("--------------------------------")

    print(f"Snooze fallback: {len(result['snooze_backup'])}")
    print(f"Snooze fallback: {result['snooze_backup']}")
    print(f"Listing update: {len(result['normal_listings'])}")
    print("\nOperation status:")
    print(f"  Success ({INVENTORY_SYNC_SUCCESS_STATUS}): {result['success_count']}")
    print(f"  Failed:       {result['fail_count']}")
    print(f"  Success rate: {result['success_rate']:.2f}%")
    print("\nStatus breakdown:")
    for status, count in sorted(
        result["status_counts"].items(),
        key=lambda x: (x[0] is None, x[0]),
    ):
        label = "success" if status == INVENTORY_SYNC_SUCCESS_STATUS else "fail"
        print(f"  {status} ({label}): {count}")


def main():
    parser = argparse.ArgumentParser(
        description="Find and analyse inventory sync operation reports"
    )
    parser.add_argument("--account", default="", help="Deliverect account ID (default: ACCOUNT_ID env var)")
    parser.add_argument(
        "--date",
        help="Report date YYYY-MM-DD (window: prev day 23:00 UTC → date 22:59 UTC)",
    )
    parser.add_argument("--after", default="", help="Created after (ISO UTC, e.g. 2026-06-24T23:00:00.000Z)")
    parser.add_argument("--before", default="", help="Created before (ISO UTC, e.g. 2026-06-25T22:59:59.999Z)")
    parser.add_argument(
        "--operation-type",
        type=int,
        action="append",
        dest="operation_types",
        help=f"Operation type filter (default: {INVENTORY_SYNC_DEFAULT_OPERATION_TYPES})",
    )
    parser.add_argument(
        "--channel",
        type=int,
        action="append",
        dest="channels",
        help=f"Channel filter (default: {INVENTORY_SYNC_DEFAULT_CHANNELS})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each rate-limited operation and log snippet",
    )
    args = parser.parse_args()

    account = _resolve_account_id(args.account)
    created_after, created_before = _resolve_created_range(args)
    operation_types = args.operation_types or INVENTORY_SYNC_DEFAULT_OPERATION_TYPES
    channels = args.channels or INVENTORY_SYNC_DEFAULT_CHANNELS

    print(
        f"Fetching operation reports for account {account}\n"
        f"  operationType: {operation_types}\n"
        f"  channel:       {channels}\n"
        f"  _created:      {created_after} → {created_before}\n"
    )

    reports = getAllOperationReports(
        account,
        operation_types=operation_types,
        channels=channels,
        created_after=created_after,
        created_before=created_before,
    )
    result = analyse_inventory_sync_reports(reports)
    _print_analysis(result, verbose=args.verbose)


if __name__ == "__main__":
    main()
