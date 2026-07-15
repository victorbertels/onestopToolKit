"""CLI: export Just Eat cancelled orders with last courier status (weekly-friendly)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from je_cancelled_courier_export import (
    DEFAULT_DAYS,
    JUST_EAT_CHANNELS,
    run_je_cancelled_courier_export,
    window_from_days,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export Just Eat cancelled orders with last courierUpdateHistory status. "
            "Default window is the last 7 days (weekly run)."
        )
    )
    parser.add_argument(
        "--account",
        default="",
        help="Deliverect account ID (default: ACCOUNT_ID env var)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--since",
        default="",
        help="UTC ISO lower bound (overrides --days when used with --until)",
    )
    parser.add_argument(
        "--until",
        default="",
        help="UTC ISO upper bound (overrides --days when used with --since)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        action="append",
        dest="channels",
        help=f"Channel filter (repeatable; default: {JUST_EAT_CHANNELS})",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output CSV path (default: ./exports/<auto-name>.csv)",
    )
    parser.add_argument(
        "--summary-json",
        default="",
        help="Optional path to write summary JSON",
    )
    args = parser.parse_args()

    account = _resolve_account_id(args.account)
    since = (args.since or "").strip() or None
    until = (args.until or "").strip() or None
    if (since and not until) or (until and not since):
        print("Provide both --since and --until, or neither.", file=sys.stderr)
        sys.exit(1)

    if since is None:
        # Validate days early for clear CLI errors
        _ = window_from_days(args.days)

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        result = run_je_cancelled_courier_export(
            account,
            days=args.days,
            since=since,
            until=until,
            channels=args.channels,
            progress_callback=progress,
        )
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else Path("exports") / result["filename"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result["csv"], encoding="utf-8-sig")

    summary = {
        "accountId": result["accountId"],
        "channels": result["channels"],
        "status": result["status"],
        "days": result["days"],
        "since": result["since"],
        "until": result["until"],
        "csv": str(out_path),
        **result["summary"],
    }
    print(json.dumps(summary, indent=2))

    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
