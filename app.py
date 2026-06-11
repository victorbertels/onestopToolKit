"""
Streamlit UI for Deliverect toolkit: retail channel setup and opening hours export/import.

Run from the project directory:
  streamlit run app.py
"""

import csv
import io
import os
import re
from io import StringIO
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

from main import createRetailChannels
from utils import (
    OPENING_HOURS_CSV_COLUMNS,
    analyze_opening_hours_csv_rows,
    fetch_opening_hours_csv_rows,
    import_opening_hours_payloads,
    load_opening_hours_import_payloads_from_rows,
    opening_hours_csv_text,
)

load_dotenv()

CHANNEL_ORDER = ("Just Eat", "Deliveroo", "Uber Eats")
DEFAULT_WORKERS = 20
PREVIEW_ROWS = 100


def _parse_location_ids(raw: str) -> list:
    lines = [line.strip() for line in raw.strip().splitlines()]
    return [x for x in lines if x]


def _link_ok(link_id) -> bool:
    return link_id not in (False, None) and bool(link_id)


def _build_channel_csv(account_id: str, channel_label: str, by_location: dict) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "account_id",
            "integration_channel",
            "location_name",
            "channel_link_id",
            "created_ok",
        ]
    )
    for location_name in sorted(by_location.keys(), key=lambda s: s.lower()):
        link_id = by_location.get(location_name)
        ok = _link_ok(link_id)
        writer.writerow(
            [
                account_id,
                channel_label,
                location_name,
                str(link_id) if ok else "",
                "yes" if ok else "no",
            ]
        )
    return buf.getvalue()


def _filename_slug(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", text.strip().lower())
    return re.sub(r"_+", "_", s).strip("_") or "channel"


def _export_csv_filename(account_id: str, channel_label: str) -> str:
    short_acc = re.sub(r"[^\w]", "", account_id)[:12] or "account"
    return f"deliverect_retail_{_filename_slug(channel_label)}_{short_acc}.csv"


def _opening_hours_export_filename(account_id: str) -> str:
    short_acc = re.sub(r"[^\w]", "", account_id)[:12] or "account"
    return f"opening_hours_{short_acc}.csv"


def _count_rows_with_full_hours(rows: list) -> int:
    return sum(
        1
        for row in rows
        if (row.get("channelLinkId") or "").strip()
        and all((row.get(col) or "").strip() for col in OPENING_HOURS_CSV_COLUMNS[4:])
    )


def _render_retail_channels(account_id: str) -> None:
    st.subheader("Locations")
    location_raw = st.text_area(
        "Location IDs (one per line)",
        height=220,
        placeholder="Paste one MongoDB location `_id` per line",
        help="Only these locations are processed; each must belong to the account above.",
    )

    run = st.button("Create retail channels", type="primary", key="retail_run")

    if run:
        if not account_id.strip():
            st.error("Enter an account ID.")
            st.stop()

        location_ids = _parse_location_ids(location_raw)
        if not location_ids:
            st.error("Enter at least one location ID.")
            st.stop()

        log_lines = []
        log_container = st.empty()

        def progress(msg: str) -> None:
            log_lines.append(msg)
            log_container.markdown("\n\n".join(f"- {line}" for line in log_lines))

        with st.spinner("Calling Deliverect API…"):
            try:
                grouped = createRetailChannels(
                    account_id.strip(),
                    location_ids,
                    progress_callback=progress,
                )
            except ValueError as e:
                st.error(str(e))
                st.stop()
            except Exception as e:
                st.exception(e)
                st.stop()

        st.session_state["results_grouped"] = grouped
        st.session_state["results_account_id"] = account_id.strip()
        st.success("Done.")

    if "results_grouped" in st.session_state:
        grouped = st.session_state["results_grouped"]
        results_account_id = st.session_state["results_account_id"]

        st.divider()
        st.subheader("Results by channel")
        if not grouped:
            st.info("No channel links were created (no locations matched or all steps failed).")
        else:
            for channel_label in CHANNEL_ORDER:
                by_location = grouped.get(channel_label)
                if not by_location:
                    continue
                with st.expander(channel_label, expanded=True):
                    rows = []
                    for loc_name, link_id in sorted(
                        by_location.items(), key=lambda x: x[0].lower()
                    ):
                        ok = _link_ok(link_id)
                        rows.append(
                            {
                                "Location": loc_name,
                                "Channel link ID": str(link_id) if ok else "",
                                "Created OK": "yes" if ok else "no",
                            }
                        )
                    st.dataframe(rows, use_container_width=True, hide_index=True)

        st.subheader("Export CSV")
        st.caption(
            "Separate UTF-8 CSV per integration (Excel-friendly). "
            "Each file lists locations for that channel only, with link IDs and a created_ok flag."
        )
        cols = st.columns(3)
        for idx, channel_label in enumerate(CHANNEL_ORDER):
            by_location = grouped.get(channel_label, {}) if grouped else {}
            csv_text = _build_channel_csv(results_account_id, channel_label, by_location)
            fname = _export_csv_filename(results_account_id, channel_label)
            cols[idx].download_button(
                label=f"Download {channel_label}",
                data=csv_text.encode("utf-8-sig"),
                file_name=fname,
                mime="text/csv",
                key=f"export_csv_{idx}",
            )

        with st.expander("Raw grouped JSON"):
            st.json(grouped)


def _render_opening_hours_export(account_id: str) -> None:
    st.subheader("Export to CSV")
    st.markdown(
        "Fetches all channel links for the account and builds a wide CSV: "
        "`locationName`, `locationId`, `channelLinkName`, `channelLinkId`, "
        "then `Mon`–`Sun` as `HH:MM-HH:MM`."
    )

    if st.button("Fetch opening hours", type="primary", key="hours_export_btn"):
        if not account_id.strip():
            st.error("Enter an account ID.")
        else:
            progress_bar = st.progress(0.0, text="Starting export…")
            status = st.empty()

            def on_fetch_progress(phase: str, page: int, page_items: int, total: int) -> None:
                if phase == "channelLinks":
                    fraction = min(0.45, 0.05 + page * 0.08)
                    progress_bar.progress(
                        fraction,
                        text=f"Channel links — page {page} · {total:,} loaded",
                    )
                    status.caption(f"+{page_items:,} on page {page}")
                elif phase == "locations":
                    fraction = min(0.85, 0.50 + page * 0.08)
                    progress_bar.progress(
                        fraction,
                        text=f"Locations — page {page} · {total:,} loaded",
                    )
                    status.caption(f"+{page_items:,} on page {page}")
                else:
                    progress_bar.progress(0.95, text=f"Building CSV — {total:,} channel links")
                    status.caption("Formatting rows for download…")

            try:
                rows = fetch_opening_hours_csv_rows(
                    account_id.strip(),
                    progress_callback=on_fetch_progress,
                )
            except Exception as exc:
                progress_bar.empty()
                status.empty()
                st.exception(exc)
                st.stop()

            progress_bar.progress(1.0, text=f"Done — {len(rows):,} channel links")
            status.empty()

            csv_text = opening_hours_csv_text(rows)
            st.session_state["hours_export_rows"] = rows
            st.session_state["hours_export_csv"] = csv_text
            st.session_state["hours_export_account_id"] = account_id.strip()
            st.success(f"Fetched {len(rows)} channel links.")

    if "hours_export_csv" in st.session_state:
        rows = st.session_state["hours_export_rows"]
        full_hours = _count_rows_with_full_hours(rows)
        c1, c2, c3 = st.columns(3)
        c1.metric("Channel links", len(rows))
        c2.metric("With all 7 days", full_hours)
        c3.metric("Partial or empty", len(rows) - full_hours)

        st.download_button(
            label="Download CSV",
            data=st.session_state["hours_export_csv"].encode("utf-8-sig"),
            file_name=_opening_hours_export_filename(st.session_state["hours_export_account_id"]),
            mime="text/csv",
            type="primary",
            key="hours_download",
        )

        with st.expander(f"Preview (first {PREVIEW_ROWS} rows)", expanded=True):
            st.dataframe(rows[:PREVIEW_ROWS], use_container_width=True, hide_index=True)


def _render_opening_hours_import(account_id: str) -> None:
    st.subheader("Import from CSV")
    st.markdown(
        "Upload the same CSV format produced by export. "
        "Only rows with a `channelLinkId` and **all seven** day columns filled are updated."
    )

    uploaded = st.file_uploader(
        "CSV file",
        type=["csv"],
        help="Must include columns: " + ", ".join(OPENING_HOURS_CSV_COLUMNS),
        key="hours_upload",
    )

    if uploaded is not None:
        raw = uploaded.getvalue().decode("utf-8-sig")
        try:
            rows = list(csv.DictReader(io.StringIO(raw)))
            summary = analyze_opening_hours_csv_rows(rows)
            payloads = load_opening_hours_import_payloads_from_rows(rows)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        except Exception as exc:
            st.exception(exc)
            st.stop()

        st.session_state["hours_import_rows"] = rows
        st.session_state["hours_import_payloads"] = payloads
        st.session_state["hours_import_summary"] = summary

    if "hours_import_summary" in st.session_state:
        summary = st.session_state["hours_import_summary"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total rows", summary["total_rows"])
        c2.metric("Ready to import", summary["importable"])
        c3.metric("Skipped (no ID)", summary["skipped_no_id"])
        c4.metric("Skipped (incomplete days)", summary["skipped_incomplete"])

        with st.expander(f"Preview (first {PREVIEW_ROWS} rows)", expanded=False):
            st.dataframe(
                st.session_state["hours_import_rows"][:PREVIEW_ROWS],
                use_container_width=True,
                hide_index=True,
            )

        if summary["importable"] == 0:
            st.warning(
                "No rows are ready to import. Each row needs a channelLinkId and hours for all 7 days."
            )
        elif st.button("Import opening hours", type="primary", key="hours_import_btn"):
            if not account_id.strip():
                st.error("Enter an account ID.")
            else:
                payloads = st.session_state["hours_import_payloads"]
                progress = st.progress(0.0, text="Starting import…")
                status = st.empty()
                counts = {"ok": 0, "fail": 0}
                total = len(payloads)

                def on_progress(result: str, channel_link_id: str, error: Optional[str]) -> None:
                    if result == "ok":
                        counts["ok"] += 1
                    else:
                        counts["fail"] += 1
                    done = counts["ok"] + counts["fail"]
                    progress.progress(
                        done / total if total else 1.0,
                        text=f"Updated {counts['ok']}/{total} · failed {counts['fail']}",
                    )
                    if result == "fail":
                        status.caption(f"Latest failure: `{channel_link_id}` — {error}")

                with st.spinner("Patching channel links…"):
                    try:
                        ok, _, failures = import_opening_hours_payloads(
                            account_id.strip(),
                            payloads,
                            workers=DEFAULT_WORKERS,
                            progress_callback=on_progress,
                        )
                    except Exception as exc:
                        st.exception(exc)
                        st.stop()

                progress.progress(1.0, text=f"Done: {ok}/{total} updated")
                if failures:
                    st.error(f"{len(failures)} channel link(s) failed.")
                    st.dataframe(failures, use_container_width=True, hide_index=True)
                else:
                    st.success(f"Imported opening hours for {ok} channel link(s).")


st.set_page_config(page_title="Onestop Toolkit", layout="wide")
st.title("Opening hours")
st.caption(
    "Export channel link opening hours to CSV, edit in Excel, then import back using the same format. "
    "API credentials and optional default account come from `.env` "
    "(`CLIENT_ID`, `CLIENT_SECRET`, `ACCOUNT_ID`)."
)

if "account_id_input" not in st.session_state:
    st.session_state.account_id_input = (os.getenv("ACCOUNT_ID") or "").strip()

with st.sidebar:
    st.header("Account")
    account_id = st.text_input(
        "Account ID",
        placeholder="Deliverect account _id",
        help=(
            "Deliverect account `_id`. "
            "Set a default in `.env` as `ACCOUNT_ID=...` or enter it here."
        ),
        key="account_id_input",
    )

hours_export_tab, hours_import_tab = st.tabs(["Export", "Import"])
with hours_export_tab:
    _render_opening_hours_export(account_id)
with hours_import_tab:
    _render_opening_hours_import(account_id)
