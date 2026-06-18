"""
Streamlit UI for Deliverect toolkit: retail channel setup and opening hours export/import.

Run from the project directory:
  streamlit run app.py
"""

import csv
import io
import os
import re
import secrets
from datetime import date, datetime, timezone
from io import StringIO
from typing import Optional

import requests as _requests
import streamlit as st
from dotenv import load_dotenv

from channel_activation import (
    PARTNER_ORDER,
    activation_channels_excel_bytes,
    activation_channels_excel_filename,
    build_all_partner_emails,
    extract_unique_location_tags,
    fetch_activation_data,
)
from main import createRetailChannels
from utils import (
    OPENING_HOURS_CSV_COLUMNS,
    analyze_opening_hours_csv_rows,
    fetch_opening_hours_csv_rows,
    getAllLocations,
    import_opening_hours_payloads,
    load_opening_hours_import_payloads_from_rows,
    opening_hours_csv_text,
)

load_dotenv()

ZAPIER_WEBHOOK_URL = os.getenv("ZAPIER_WEBHOOK_URL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

CHANNEL_ORDER = ("Just Eat", "Deliveroo", "Uber Eats")
DEFAULT_WORKERS = 20
PREVIEW_ROWS = 100


def _track_page(page_name: str):
    """Fire-and-forget POST to Zapier with the page the user visited."""
    if not ZAPIER_WEBHOOK_URL:
        return
    try:
        _requests.post(
            ZAPIER_WEBHOOK_URL,
            json={
                "page": page_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            timeout=5,
        )
    except Exception:
        pass


def _track_hours_page(page: str) -> None:
    page_name = "OS Export" if page == "Export" else "OS Import"
    if st.session_state.get("hours_page_tracked") != page:
        _track_page(page_name)
        st.session_state["hours_page_tracked"] = page


def _require_password() -> None:
    if st.session_state.get("authenticated"):
        return

    st.title("Onestop Toolkit")
    st.caption("Enter the password to continue.")

    if not APP_PASSWORD:
        st.error("APP_PASSWORD is not configured. Set it in the environment or vault.")
        st.stop()

    with st.form("login", clear_on_submit=False):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if secrets.compare_digest(password, APP_PASSWORD):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("Incorrect password.")

    st.stop()


def _render_channel_activation_emails(account_id: str) -> None:
    st.markdown(
        "Build partner-specific emails for **Uber Eats**, **Deliveroo**, and **Just Eat** "
        "using locations filtered by tag. Each email lists the stores and IDs the partner "
        "needs to activate."
    )

    go_live_date = st.date_input(
        "Target go-live date",
        value=date.today(),
        help="When stores should be live and accepting orders.",
        key="activation_go_live_date",
    )

    load_tags = st.button("Load location tags", key="activation_load_tags")
    if load_tags:
        if not account_id.strip():
            st.error("ACCOUNT_ID is not set. Add it to your `.env` file.")
        else:
            with st.spinner("Fetching locations…"):
                try:
                    tags = extract_unique_location_tags(getAllLocations(account_id.strip()))
                    st.session_state["activation_tags"] = tags
                except Exception as exc:
                    st.exception(exc)
                    st.stop()
            st.success(f"Found {len(tags)} tag(s).")

    tags = st.session_state.get("activation_tags") or []
    if not tags:
        st.info("Load location tags to choose which store batch to include.")
        cohort_tag = st.text_input(
            "Or enter a tag manually",
            placeholder="e.g. New 17/06",
            key="activation_tag_manual",
        )
    else:
        cohort_tag = st.selectbox("Location tag", tags, key="activation_tag_select")

    generate = st.button("Generate emails", type="primary", key="activation_generate")

    if generate:
        if not account_id.strip():
            st.error("ACCOUNT_ID is not set. Add it to your `.env` file.")
        elif not (cohort_tag or "").strip():
            st.error("Select or enter a location tag.")
        else:
            with st.spinner("Fetching channel links and building emails…"):
                try:
                    grouped = fetch_activation_data(account_id.strip(), tag=cohort_tag.strip())
                    emails = build_all_partner_emails(
                        grouped,
                        cohort_tag=cohort_tag.strip(),
                        action_date=go_live_date,
                        go_live_date=go_live_date,
                    )
                except Exception as exc:
                    st.exception(exc)
                    st.stop()

            st.session_state["activation_emails"] = emails
            st.session_state["activation_grouped"] = grouped
            st.session_state["activation_cohort"] = cohort_tag.strip()
            total = sum(len(grouped.get(p) or []) for p in PARTNER_ORDER)
            _track_page("OS Generate Emails")
            st.success(f"Generated emails for {total} channel link(s) in tag “{cohort_tag.strip()}”.")
            st.rerun()

    if "activation_emails" in st.session_state:
        emails = st.session_state["activation_emails"]
        grouped = st.session_state.get("activation_grouped", {})

        st.divider()
        st.subheader("Generated emails")
        st.caption("Copy the subject and body below, download partner emails, or export channel data to Excel.")

        summary_cols = st.columns(len(PARTNER_ORDER))
        for idx, partner in enumerate(PARTNER_ORDER):
            summary_cols[idx].metric(partner, len(grouped.get(partner) or []))

        cohort = st.session_state.get("activation_cohort", "batch")
        st.download_button(
            label="Download channels Excel",
            data=activation_channels_excel_bytes(grouped),
            file_name=activation_channels_excel_filename(cohort),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            key="activation_channels_excel",
        )

        partner_tabs = st.tabs(list(PARTNER_ORDER))
        for tab, partner in zip(partner_tabs, PARTNER_ORDER):
            email = emails[partner]
            with tab:
                if email["store_count"] == 0:
                    st.info(f"No {partner} channel links in this tag.")
                    continue

                st.text_input("Subject", value=email["subject"], key=f"activation_subject_{partner}")
                st.text_area(
                    "Email body",
                    value=email["body"],
                    height=420,
                    key=f"activation_body_{partner}",
                )
                st.download_button(
                    label=f"Download {partner} email",
                    data=email["body"].encode("utf-8"),
                    file_name=f"{partner.lower().replace(' ', '_')}_activation.txt",
                    mime="text/plain",
                    key=f"activation_download_{partner}",
                )


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


def _import_preview_row(detail: dict) -> dict:
    status_labels = {
        "full": "Ready (7/7)",
        "partial": "Partial",
        "skipped_no_id": "No channel link ID",
        "skipped_no_location": "No location ID",
        "skipped_no_hours": "No opening hours",
        "skipped_invalid": "Invalid hours",
    }
    return {
        "Location": detail.get("locationName", ""),
        "Channel": detail.get("channelLinkName", ""),
        "Channel link ID": detail.get("channelLinkId", ""),
        "Days": f"{detail.get('day_count', 0)}/7",
        "Closed days": ", ".join(detail.get("missing_days") or []) or "—",
        "Invalid days": ", ".join(detail.get("invalid_days") or []) or "—",
        "Status": status_labels.get(detail.get("status", ""), detail.get("status", "")),
    }


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
            st.error("ACCOUNT_ID is not set. Add it to your `.env` file.")
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
            st.error("ACCOUNT_ID is not set. Add it to your `.env` file.")
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
        "Rows need a `channelLinkId`, `locationId`, and at least one valid day. "
        "Closed or blank days are omitted from the import payload."
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
        row_details = summary.get("row_details", [])
        preview_rows = [_import_preview_row(d) for d in row_details]
        partial_rows = [r for r in preview_rows if r["Status"] == "Partial"]
        invalid_rows = [r for r in preview_rows if r["Status"] == "Invalid hours"]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total rows", summary["total_rows"])
        c2.metric("Ready to import", summary["importable"])
        c3.metric("Partial (closed days)", summary.get("partial", 0))
        c4.metric(
            "Skipped",
            summary["skipped_no_id"]
            + summary.get("skipped_no_location", 0)
            + summary.get("skipped_no_hours", 0)
            + summary.get("skipped_invalid", 0),
        )

        if partial_rows:
            st.warning(
                f"{len(partial_rows)} row(s) have closed days. "
                "Those days will be left out of the opening hours payload."
            )
            st.dataframe(partial_rows, use_container_width=True, hide_index=True)

        if invalid_rows:
            st.error(
                f"{len(invalid_rows)} row(s) have invalid hour values. "
                "Fix these before importing."
            )
            st.dataframe(invalid_rows, use_container_width=True, hide_index=True)

        with st.expander(f"Preview (first {PREVIEW_ROWS} rows)", expanded=False):
            st.dataframe(
                preview_rows[:PREVIEW_ROWS],
                use_container_width=True,
                hide_index=True,
            )

        if summary["importable"] == 0:
            st.warning(
                "No rows are ready to import. Each row needs a channelLinkId, locationId, and at least one valid day."
            )
        elif st.button("Import opening hours", type="primary", key="hours_import_btn"):
            if not account_id.strip():
                st.error("ACCOUNT_ID is not set. Add it to your `.env` file.")
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


def _get_account_id() -> str:
    return (os.getenv("ACCOUNT_ID") or "").strip()


def _sign_out_sidebar() -> None:
    with st.sidebar:
        st.divider()
        if st.button("Sign out", key="sign_out", use_container_width=True):
            st.session_state.pop("authenticated", None)
            st.session_state.pop("hours_page_tracked", None)
            st.session_state.pop("tool_page_tracked", None)
            st.rerun()


def page_opening_hours_export() -> None:
    st.title("Opening hours")
    st.caption(
        "Export channel link opening hours to CSV, edit in Excel, then import back using the same format."
    )
    _track_hours_page("Export")
    _render_opening_hours_export(_get_account_id())


def page_opening_hours_import() -> None:
    st.title("Opening hours")
    st.caption(
        "Export channel link opening hours to CSV, edit in Excel, then import back using the same format."
    )
    _track_hours_page("Import")
    _render_opening_hours_import(_get_account_id())


def page_channel_activation() -> None:
    if st.session_state.get("tool_page_tracked") != "Channel activation emails":
        _track_page("OS Channel Activation")
        st.session_state["tool_page_tracked"] = "Channel activation emails"
    st.title("Channel activation emails")
    st.caption("Generate partner emails with store lists and channel link IDs.")
    _render_channel_activation_emails(_get_account_id())


st.set_page_config(
    page_title="Onestop Toolkit",
    layout="wide",
    initial_sidebar_state="expanded",
)
_require_password()

st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] > div:first-child {
        height: 100vh;
        display: flex;
        flex-direction: column;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        flex: 1;
        display: flex;
        flex-direction: column;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div:last-child {
        margin-top: auto !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

pages = {
    "Opening hours": [
        st.Page(page_opening_hours_export, title="Export", icon=":material/upload:"),
        st.Page(page_opening_hours_import, title="Import", icon=":material/download:"),
    ],
    "Channel activation": [
        st.Page(page_channel_activation, title="Partner emails", icon=":material/mail:"),
    ],
}

pg = st.navigation(pages, position="sidebar")
pg.run()
_sign_out_sidebar()
