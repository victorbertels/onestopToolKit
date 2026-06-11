import argparse

from utils import exportChannelLinksOpeningHoursCsv, importChannelLinksOpeningHoursCsv

ACCOUNT = "67e527dc3acf4b582fdc360b"
DEFAULT_CSV = "opening_hours.csv"
WORKERS = 20


def main():
    parser = argparse.ArgumentParser(description="Export or import channel link opening hours")
    parser.add_argument("mode", choices=["export", "import"], help="export to CSV or import from CSV")
    parser.add_argument("--file", default=DEFAULT_CSV, help="CSV file path")
    parser.add_argument("--workers", type=int, default=WORKERS, help="parallel workers for import")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print etag, request URL, payload and API response per channel link",
    )
    args = parser.parse_args()

    if args.mode == "export":
        exportChannelLinksOpeningHoursCsv(ACCOUNT, args.file)
        print(f"Exported opening hours to {args.file}")
    else:
        ok, total = importChannelLinksOpeningHoursCsv(
            ACCOUNT,
            args.file,
            workers=args.workers,
            debug=args.debug,
        )
        print(f"Done: {ok}/{total} updated (workers={args.workers})")


if __name__ == "__main__":
    main()
