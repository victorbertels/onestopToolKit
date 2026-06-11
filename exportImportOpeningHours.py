import argparse
import os
import sys

from dotenv import load_dotenv

from utils import exportChannelLinksOpeningHoursCsv, importChannelLinksOpeningHoursCsv

load_dotenv()

DEFAULT_CSV = "opening_hours.csv"
WORKERS = 20


def _resolve_account_id(cli_account: str) -> str:
    account = (cli_account or os.getenv("ACCOUNT_ID") or "").strip()
    if not account:
        print(
            "Account ID required: set ACCOUNT_ID in the environment or pass --account",
            file=sys.stderr,
        )
        sys.exit(1)
    return account


def main():
    parser = argparse.ArgumentParser(description="Export or import channel link opening hours")
    parser.add_argument("mode", choices=["export", "import"], help="export to CSV or import from CSV")
    parser.add_argument("--account", default="", help="Deliverect account ID (default: ACCOUNT_ID env var)")
    parser.add_argument("--file", default=DEFAULT_CSV, help="CSV file path")
    parser.add_argument("--workers", type=int, default=WORKERS, help="parallel workers for import")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print etag, request URL, payload and API response per channel link",
    )
    args = parser.parse_args()
    account = _resolve_account_id(args.account)

    if args.mode == "export":
        exportChannelLinksOpeningHoursCsv(account, args.file)
        print(f"Exported opening hours to {args.file}")
    else:
        ok, total = importChannelLinksOpeningHoursCsv(
            account,
            args.file,
            workers=args.workers,
            debug=args.debug,
        )
        print(f"Done: {ok}/{total} updated (workers={args.workers})")


if __name__ == "__main__":
    main()
