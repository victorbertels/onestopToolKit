"""CLI: extract channel activation data and print partner email templates."""

import argparse
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from channel_activation import (
    PARTNER_ORDER,
    build_all_partner_emails,
    extract_unique_location_tags,
    fetch_activation_data,
)
from utils import getAllLocations

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


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _slug(text: str) -> str:
    return text.lower().replace(" ", "_").replace("/", "-")


def main():
    parser = argparse.ArgumentParser(
        description="Extract channel activation data and build partner email templates"
    )
    parser.add_argument("--account", default="", help="Deliverect account ID")
    parser.add_argument("--tag", default="", help="Location tag to filter on (required unless --list-tags)")
    parser.add_argument(
        "--list-tags",
        action="store_true",
        help="Print unique location tags and exit",
    )
    parser.add_argument(
        "--stores-only",
        action="store_true",
        help="Print store list only, no email bodies",
    )
    parser.add_argument(
        "--action-date",
        default=date.today().isoformat(),
        help="Partner action deadline (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--go-live-date",
        default="",
        help="Target go-live date (YYYY-MM-DD, default: same as action date)",
    )
    parser.add_argument(
        "--contact-name",
        default="",
        help="Sign-off name for emails",
    )
    parser.add_argument(
        "--contact-email",
        default="",
        help="Contact email for replies",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Additional notes appended to each email",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional folder to save one .txt file per partner email",
    )
    args = parser.parse_args()
    account = _resolve_account_id(args.account)

    if args.list_tags:
        tags = extract_unique_location_tags(getAllLocations(account))
        for tag in tags:
            print(tag)
        return

    if not args.tag.strip():
        print("Error: --tag is required (or use --list-tags).", file=sys.stderr)
        sys.exit(1)

    action_date = _parse_date(args.action_date)
    go_live_date = _parse_date(args.go_live_date) if args.go_live_date else action_date

    grouped = fetch_activation_data(account, tag=args.tag.strip())

    if args.stores_only:
        for partner in PARTNER_ORDER:
            rows = grouped.get(partner) or []
            print(f"\n{partner} ({len(rows)} stores)")
            for row in rows:
                if partner == "Uber Eats":
                    print(
                        f"  {row['location_name']}: "
                        f"uberStoreId={row['partner_ref'] or '—'} "
                        f"channelLinkId={row['channel_link_id']}"
                    )
                else:
                    print(
                        f"  {row['location_name']}: "
                        f"channelLinkId={row['channel_link_id']}"
                    )
        return

    emails = build_all_partner_emails(
        grouped,
        cohort_tag=args.tag.strip(),
        action_date=action_date,
        go_live_date=go_live_date,
        contact_name=args.contact_name,
        contact_email=args.contact_email,
        extra_notes=args.notes,
    )

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        tag_slug = _slug(args.tag.strip())

    for partner in PARTNER_ORDER:
        email = emails[partner]
        rows = grouped.get(partner) or []
        print("\n" + "=" * 72)
        print(f"{partner} — {len(rows)} store(s)")
        print("=" * 72)
        if email["store_count"] == 0:
            print(f"No {partner} channel links in tag “{args.tag.strip()}”.")
            continue

        print(f"\nSubject: {email['subject']}\n")
        print(email["body"])

        if args.output_dir:
            filename = f"{tag_slug}_{_slug(partner)}_activation.txt"
            filepath = os.path.join(args.output_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Subject: {email['subject']}\n\n")
                f.write(email["body"])
            print(f"\nSaved to {filepath}")


if __name__ == "__main__":
    main()
