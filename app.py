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
from datetime import date, datetime, time, timezone
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
from close_open_stores import (
    get_unique_location_tags as busy_mode_location_tags,
    load_account_busy_mode_data,
    run_busy_mode_for_channel_links,
    select_channel_links,
)
from main import createRetailChannels
from quest_prep import (
    PARTNER_ORDER as QUEST_PREP_PARTNERS,
    build_quest_prep_payloads,
    create_quest_channels,
    get_template_location_id,
)
from suspend_stores import (
    CHANNEL_LINK_STATUS_LABELS,
    parse_ids as parse_suspend_ids,
    run_set_statuses_by_ids,
)
from utils import (
    CHANNEL_LINK_STATUS_OPTIONS,
    CHANNEL_LINK_STATUS_SUSPENDED,
    LOCATION_STATUS_OPTIONS,
    LOCATION_STATUS_SUSPENDED,
    LOCATION_TO_CHANNEL_LINK_STATUS,
    OPENING_HOURS_CSV_COLUMNS,
    INVENTORY_SYNC_DEFAULT_CHANNELS,
    INVENTORY_SYNC_DEFAULT_OPERATION_TYPES,
    analyse_inventory_sync_reports,
    analyze_opening_hours_csv_rows,
    build_inventory_sync_table_rows,
    fetch_opening_hours_csv_rows,
    getAllLocations,
    getAllOperationReports,
    get_configured_account_id,
    import_opening_hours_payloads,
    inventory_sync_range_london,
    load_opening_hours_import_payloads_from_rows,
    london_now,
    opening_hours_csv_text,
)

load_dotenv()

_STREAMLIT_SECRET_KEYS = (
    "ACCOUNT_ID",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "APP_PASSWORD",
    "ZAPIER_WEBHOOK_URL",
    "JUST_EAT_API_KEY",
    "QUEST_PREP_TEMPLATE_LOCATION_ID",
)


def _hydrate_env_from_streamlit_secrets() -> None:
    """Copy Streamlit secrets into os.environ when the env var is unset."""
    try:
        streamlit_secrets = st.secrets
    except Exception:
        return
    for key in _STREAMLIT_SECRET_KEYS:
        if (os.getenv(key) or "").strip():
            continue
        try:
            value = streamlit_secrets[key]
        except Exception:
            continue
        if value is not None and str(value).strip():
            os.environ[key] = str(value).strip()


_hydrate_env_from_streamlit_secrets()

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
    return get_configured_account_id()


def _require_account_id() -> str:
    """Block the app unless ACCOUNT_ID is configured. Returns the account id."""
    account_id = _get_account_id()
    if not account_id:
        st.error(
            "ACCOUNT_ID is not set. Add it to your `.env` file or Streamlit secrets."
        )
        st.stop()
    return account_id


def _sign_out_sidebar() -> None:
    with st.sidebar:
        st.divider()
        if st.button("Sign out", key="sign_out", use_container_width=True):
            st.session_state.pop("authenticated", None)
            st.session_state.pop("hours_page_tracked", None)
            st.session_state.pop("tool_page_tracked", None)
            for key in list(st.session_state.keys()):
                if key.startswith("busy_mode_"):
                    st.session_state.pop(key, None)
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


def _render_quest_prep(account_id: str) -> None:
    st.markdown(
        "Paste a **destination location ID**. We copy **Just Eat**, **Deliveroo**, and "
        "**Uber Eats** retail channel links from a known-good template site, clear Uber’s "
        "`storeId`, then create the three links on the new location. "
        "Template and destination must both belong to the configured account."
    )
    st.warning("This creates live channel links in Deliverect.")
    st.caption(f"Account: `{account_id}`")

    default_template = get_template_location_id()
    template_location_id = st.text_input(
        "Template location ID (source site)",
        value=default_template,
        placeholder="Location that already has correct JE / Deliveroo / Uber retail links",
        key="quest_prep_template_id",
        help="Defaults to the known-good OneStop template site. Override if needed.",
    ).strip()

    destination_location_id = st.text_input(
        "Destination location ID",
        placeholder="e.g. 69df3bbc64312ebd0f8b5016",
        key="quest_prep_destination_id",
    ).strip()

    inputs_ready = bool(template_location_id and destination_location_id)

    if st.button("Preview channels", disabled=not inputs_ready, key="quest_prep_preview_btn"):
        with st.spinner("Loading template retail channels…"):
            preview, error = build_quest_prep_payloads(
                destination_location_id,
                template_location_id,
            )
        if error or preview is None:
            st.error(error or "Failed to build preview.")
            st.session_state.pop("quest_prep_preview", None)
        else:
            st.session_state["quest_prep_preview"] = preview
            st.session_state.pop("quest_prep_results", None)

    preview = st.session_state.get("quest_prep_preview")
    if not preview:
        return

    template_loc = preview["template_location"]
    dest_loc = preview["destination_location"]
    st.success("Preview ready.")
    st.markdown(
        f"**Template:** {template_loc.get('name') or '—'} "
        f"(`{template_loc.get('id')}`) → "
        f"**Destination:** {dest_loc.get('name') or '—'} "
        f"(`{dest_loc.get('id')}`)"
    )

    rows = []
    for partner in QUEST_PREP_PARTNERS:
        meta = preview["templates"].get(partner, {})
        payload = preview["payloads"].get(partner, {})
        uber_store = (payload.get("channelSettings") or {}).get("storeId", "")
        rows.append(
            {
                "Partner": partner,
                "Template link": meta.get("id") or "",
                "Template name": meta.get("name") or "",
                "sendToQuest": meta.get("sendToQuest"),
                "New name": payload.get("name") or "",
                "Uber storeId": uber_store if partner == "Uber Eats" else "—",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(
        "Uber `storeId` is cleared so the new site is not bound to the template store. "
        "Partner will provision a new store ID later."
    )

    with st.expander("Payloads (JSON)"):
        for partner in QUEST_PREP_PARTNERS:
            st.subheader(partner)
            st.json(preview["payloads"].get(partner, {}))

    if st.button("Create channel links", type="primary", key="quest_prep_create_btn"):
        with st.spinner("Creating Just Eat, Deliveroo, and Uber Eats channel links…"):
            results = create_quest_channels(preview["payloads"])
        st.session_state["quest_prep_results"] = results

    results = st.session_state.get("quest_prep_results")
    if not results:
        return

    st.subheader("Results")
    result_rows = [
        {
            "Partner": row.get("partner"),
            "Created OK": "yes" if row.get("success") else "no",
            "Channel link ID": row.get("channel_link_id") or "",
            "Name": row.get("name") or "",
            "Error": row.get("error") or "—",
        }
        for row in results
    ]
    st.dataframe(result_rows, use_container_width=True, hide_index=True)

    ok = sum(1 for row in results if row.get("success"))
    if ok == len(results):
        st.success(f"Created all {ok} retail channel links.")
    elif ok:
        st.warning(f"Created {ok} of {len(results)} channel links — check errors above.")
    else:
        st.error("No channel links were created.")


def page_quest_prep() -> None:
    if st.session_state.get("tool_page_tracked") != "Quest prep":
        _track_page("OS Quest Prep")
        st.session_state["tool_page_tracked"] = "Quest prep"
    st.title("Quest prep")
    st.caption("Prep a location for Quest by cloning retail channel links from a template site.")
    _render_quest_prep(_get_account_id())


def _render_inventory_sync_analysis(account_id: str) -> None:
    if not account_id:
        st.error("ACCOUNT_ID is not configured. Set it in the environment or vault.")
        st.stop()

    st.markdown(
        "Analyse **Deliveroo inventory sync** operation reports. "
        "Pick a report date and time range in **London time**; times in the table use the same timezone."
    )

    report_date = st.date_input(
        "Report date",
        value=london_now().date(),
        key="inventory_sync_report_date",
    )

    from_col, to_col = st.columns(2)
    with from_col:
        from_time = st.time_input(
            "From",
            value=time(0, 0),
            key="inventory_sync_from_time",
        )
    with to_col:
        to_time = st.time_input(
            "To",
            value=time(23, 59, 59),
            key="inventory_sync_to_time",
        )

    created_after, created_before, range_label = inventory_sync_range_london(
        report_date, from_time, to_time
    )
    filter_key = f"{report_date}|{from_time}|{to_time}"

    if created_after >= created_before:
        st.error("From must be earlier than To.")
        st.stop()

    st.caption(
        f"London window: **{range_label}** · "
        f"API query (UTC): `{created_after}` → `{created_before}`"
    )

    with st.expander("Filters"):
        operation_types = st.multiselect(
            "Operation types",
            options=INVENTORY_SYNC_DEFAULT_OPERATION_TYPES,
            default=INVENTORY_SYNC_DEFAULT_OPERATION_TYPES,
        )
        channels = st.multiselect(
            "Channels",
            options=INVENTORY_SYNC_DEFAULT_CHANNELS,
            default=INVENTORY_SYNC_DEFAULT_CHANNELS,
        )

    if st.button("Run analysis", type="primary", key="inventory_sync_run"):
        if not operation_types or not channels:
            st.error("Select at least one operation type and one channel.")
            st.stop()

        with st.spinner("Fetching operation reports and locations…"):
            try:
                reports = getAllOperationReports(
                    account_id,
                    operation_types=operation_types,
                    channels=channels,
                    created_after=created_after,
                    created_before=created_before,
                )
                locations = getAllLocations(account_id)
                location_names = {
                    loc.get("_id"): loc.get("name", "")
                    for loc in locations
                    if loc.get("_id")
                }
                analysis = analyse_inventory_sync_reports(reports)
                rows = build_inventory_sync_table_rows(reports, location_names)
            except RuntimeError as exc:
                st.error(str(exc))
                st.stop()

        st.session_state["inventory_sync_result"] = {
            "analysis": analysis,
            "rows": rows,
            "filter_key": filter_key,
            "range_label": range_label,
            "created_after": created_after,
            "created_before": created_before,
        }

    result = st.session_state.get("inventory_sync_result")
    if not result:
        return

    if result.get("filter_key") != filter_key:
        st.info("Time range changed — click **Run analysis** to refresh.")
        return

    analysis = result["analysis"]
    rows = result["rows"]

    st.subheader(f"Results — {result['range_label']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total reports", analysis["total"])
    c2.metric("Success", analysis["success_count"])
    c3.metric("Failed", analysis["fail_count"])
    c4.metric("Success rate", f"{analysis['success_rate']:.1f}%")

    c5, c6 = st.columns(2)
    c5.metric("Snooze fallback", len(analysis["snooze_backup"]))
    c6.metric("Listing update", len(analysis["normal_listings"]))

    if not rows:
        st.info("No operation reports found for this date and filters.")
        return

    st.dataframe(
        rows,
        column_config={
            "Report URL": st.column_config.LinkColumn(
                "Report URL",
                display_text="Open report",
            ),
        },
        use_container_width=True,
        hide_index=True,
    )


def page_inventory_sync() -> None:
    if st.session_state.get("tool_page_tracked") != "Inventory sync":
        _track_page("OS Inventory Sync")
        st.session_state["tool_page_tracked"] = "Inventory sync"
    st.title("Inventory sync")
    st.caption("Deliveroo inventory sync operation reports by location.")
    _render_inventory_sync_analysis(_get_account_id())


def _render_close_open_stores(account_id: str) -> None:
    st.markdown(
        "Set busy mode on **channel links** matching your location and channel selection. "
        "Close uses preparation delay **999**; open uses **0**."
    )
    st.warning(
        "This changes live store availability. Double-check your selection before proceeding."
    )
    st.caption(f"Account: `{account_id}`")
    st.markdown("---")

    load_col, _ = st.columns([1, 3])
    with load_col:
        load_clicked = st.button(
            "Load locations & channels",
            key="busy_mode_load",
        )

    if load_clicked:
        with st.spinner("Loading account data..."):
            try:
                data = load_account_busy_mode_data(account_id)
            except Exception as exc:
                st.error(f"Failed to load account data: {exc}")
                return
            st.session_state["busy_mode_cached_locations"] = data["locations"]
            st.session_state["busy_mode_cached_channels"] = data["channel_groups"]
            st.session_state["busy_mode_cached_channel_links"] = data["flat_channel_links"]
            st.session_state["busy_mode_cache_account"] = account_id

    cached_account = st.session_state.get("busy_mode_cache_account")
    locations = st.session_state.get("busy_mode_cached_locations", [])
    channel_groups = st.session_state.get("busy_mode_cached_channels", [])
    flat_channel_links = st.session_state.get("busy_mode_cached_channel_links", [])

    if cached_account and cached_account != account_id:
        st.info("Account changed — click **Load locations & channels** before proceeding.")
    elif locations or channel_groups:
        st.caption(
            f"Loaded **{len(locations)}** locations, **{len(channel_groups)}** channels, "
            f"**{len(flat_channel_links)}** channel links for account `{cached_account}`."
        )

    action = st.radio(
        "Action",
        options=["Close stores", "Open stores"],
        horizontal=True,
        key="busy_mode_action",
    )
    close_stores = action == "Close stores"

    st.subheader("1. Locations")
    location_mode = st.radio(
        "Which locations?",
        options=["All locations", "Location groups (tags)", "Selected locations"],
        horizontal=True,
        key="busy_mode_location_mode",
    )

    selected_location_ids = []
    selected_tags = []

    if location_mode == "Selected locations":
        location_options = {
            loc["id"]: f"{loc.get('name') or loc['id']} ({loc['id']})"
            for loc in locations
        }
        selected_location_ids = st.multiselect(
            "Select locations",
            options=list(location_options.keys()),
            format_func=lambda loc_id: location_options.get(loc_id, loc_id),
            key="busy_mode_locations",
        )
    elif location_mode == "Location groups (tags)":
        all_tags = busy_mode_location_tags(locations)
        selected_tags = st.multiselect(
            "Select location groups (tags)",
            options=all_tags,
            help="Locations that have any of the selected tags.",
            key="busy_mode_tags",
        )

    st.subheader("2. Channels")
    channel_mode = st.radio(
        "Which channels?",
        options=["All channels", "Selected channels"],
        horizontal=True,
        key="busy_mode_channel_mode",
    )

    selected_channel_ids = []
    if channel_mode == "Selected channels":
        channel_options = {
            group["channelId"]: (
                f"{group.get('channel') or group['channelId']} "
                f"({len(group.get('channelLinksIds') or [])} links)"
            )
            for group in channel_groups
            if group.get("channelId") is not None
        }
        selected_channel_ids = st.multiselect(
            "Select channels",
            options=list(channel_options.keys()),
            format_func=lambda channel_id: channel_options.get(channel_id, str(channel_id)),
            key="busy_mode_channels",
        )

    matched_links = []
    data_ready = bool(cached_account and cached_account == account_id)
    if data_ready:
        matched_links = select_channel_links(
            flat_channel_links,
            locations,
            channel_groups,
            location_mode,
            selected_location_ids,
            selected_tags,
            channel_mode,
            selected_channel_ids,
        )

    target_count = len(matched_links)
    target_label = (
        f"{target_count} channel link(s) matching your location and channel selection"
    )

    if not data_ready:
        st.info(
            "Load locations and channels for this account to see how many channel links will be updated."
        )
    elif not flat_channel_links:
        st.warning("No channel links were loaded for this account.")
    elif target_count == 0:
        st.warning(
            "No channel links match the current selection. "
            "Check that you picked locations/tags and channels, or try **All locations** + **All channels**."
        )
    else:
        st.info(f"**{target_count}** channel link(s) will be updated.")
        if target_count <= 20:
            preview = [
                {
                    "Location": link.get("location_name"),
                    "Channel": link.get("channel_name"),
                    "Channel link": link.get("name"),
                    "Channel link ID": link.get("id"),
                }
                for link in matched_links
            ]
            st.dataframe(preview, use_container_width=True, hide_index=True)

    max_workers = st.slider(
        "Parallel requests",
        min_value=1,
        max_value=50,
        value=10,
        key="busy_mode_max_workers",
    )

    confirm = st.checkbox(
        f"I confirm I want to **{'close' if close_stores else 'open'}** {target_label}",
        key="busy_mode_confirm",
    )

    execute_disabled = (
        not data_ready
        or not flat_channel_links
        or target_count == 0
        or not confirm
    )

    if st.button(
        f"{'Close' if close_stores else 'Open'} selected targets",
        type="primary",
        disabled=execute_disabled,
        key="busy_mode_run",
    ):
        _track_page("OS Close / Open Stores")
        progress_bar = st.progress(0)
        status_text = st.empty()

        def on_progress(completed, total, row):
            progress_bar.progress(completed / total if total else 1.0)
            status_text.text(
                f"{completed}/{total} — {row.get('target_type')} "
                f"{row.get('target_name') or row.get('target_id')}"
            )

        try:
            with st.spinner(f"{'Closing' if close_stores else 'Opening'}..."):
                results = run_busy_mode_for_channel_links(
                    matched_links,
                    close_stores,
                    max_workers=max_workers,
                    on_progress=on_progress,
                )

            progress_bar.empty()
            status_text.empty()
            st.session_state["busy_mode_results"] = results
            st.session_state["busy_mode_results_account"] = cached_account
            st.session_state["busy_mode_results_action"] = action
            success_count = sum(1 for r in results if r.get("success"))
            st.success(f"Finished: **{success_count}/{len(results)}** succeeded.")
        except Exception as exc:
            progress_bar.empty()
            status_text.empty()
            st.error(f"Busy mode update failed: {exc}")

    if st.session_state.get("busy_mode_results"):
        results = st.session_state["busy_mode_results"]
        display = []
        for row in results:
            display.append(
                {
                    "Success": row.get("success"),
                    "Location": row.get("location_name"),
                    "Channel": row.get("channel_name"),
                    "Channel link": row.get("target_name"),
                    "Channel link ID": row.get("target_id"),
                    "Tags": row.get("tags"),
                    "Delay": row.get("preparation_time_delay"),
                    "Status code": row.get("status_code"),
                    "Error": row.get("error") or "—",
                }
            )

        failed_only = st.checkbox("Show failures only", key="busy_mode_failures_only")
        shown = [r for r in display if not r["Success"]] if failed_only else display
        st.dataframe(shown, use_container_width=True, hide_index=True)

        safe_account = "".join(
            c
            for c in (st.session_state.get("busy_mode_results_account") or "account")
            if c.isalnum() or c in (" ", "-", "_")
        ).strip().replace(" ", "_")
        action_slug = (
            "close"
            if st.session_state.get("busy_mode_results_action") == "Close stores"
            else "open"
        )
        csv_buf = StringIO()
        if shown:
            writer = csv.DictWriter(csv_buf, fieldnames=list(shown[0].keys()))
            writer.writeheader()
            writer.writerows(shown)
        st.download_button(
            label="Download results CSV",
            data=csv_buf.getvalue(),
            file_name=f"busy_mode_{action_slug}_{safe_account}.csv",
            mime="text/csv",
            key="busy_mode_download",
        )


def page_close_open_stores() -> None:
    if st.session_state.get("tool_page_tracked") != "Close / Open Stores":
        _track_page("OS Close / Open Stores")
        st.session_state["tool_page_tracked"] = "Close / Open Stores"
    st.title("Close / Open Stores")
    st.caption("Temporarily close or reopen stores via channel-link busy mode.")
    _render_close_open_stores(_get_account_id())


def _render_suspend_stores(account_id: str) -> None:
    st.markdown(
        "Choose **one mode**: update **locations** (each location + all channel links on it), "
        "or update **channel links only**. "
        "Locations use string statuses; channel links use integer statuses. "
        "Not the same as busy-mode close."
    )
    st.warning(
        "This changes live store status. Double-check the IDs and statuses before proceeding."
    )
    st.caption(f"Account: `{account_id}`")
    st.markdown("---")

    mode = st.radio(
        "What are you updating?",
        options=("Locations", "Channel links"),
        horizontal=True,
        key="suspend_mode",
        help=(
            "Locations: set location status, and map that to the matching int status on "
            "every channel link listed on that location. Channel links: only the IDs you paste."
        ),
    )
    location_mode = mode == "Locations"

    if location_mode:
        location_status = st.selectbox(
            "Location status",
            options=list(LOCATION_STATUS_OPTIONS),
            index=list(LOCATION_STATUS_OPTIONS).index(LOCATION_STATUS_SUSPENDED),
            key="suspend_location_status",
            help="Written to location.status as a string. Matching channel-link ints: "
            "SUSPENDED→1, SUBSCRIBED→3, TESTING→2.",
        )
        channel_link_status = LOCATION_TO_CHANNEL_LINK_STATUS[location_status]
    else:
        location_status = LOCATION_STATUS_SUSPENDED
        channel_labels = [label for _value, label in CHANNEL_LINK_STATUS_OPTIONS]
        channel_values = [value for value, _label in CHANNEL_LINK_STATUS_OPTIONS]
        default_channel_index = channel_values.index(CHANNEL_LINK_STATUS_SUSPENDED)
        channel_label = st.selectbox(
            "Channel link status",
            options=channel_labels,
            index=default_channel_index,
            key="suspend_channel_status_only",
            help="Written to channelLink.status as an integer (0–4).",
        )
        channel_link_status = channel_values[channel_labels.index(channel_label)]

    if location_mode:
        ids_raw = st.text_area(
            "Location IDs",
            height=160,
            placeholder="Paste one location `_id` per line",
            help=(
                "For each location: set every channel link on that location to the channel "
                "status above, then set the location status."
            ),
            key="suspend_ids",
        )
    else:
        ids_raw = st.text_area(
            "Channel link IDs",
            height=160,
            placeholder="Paste one channel link `_id` per line",
            help="Only these channel links are updated. Locations are not changed.",
            key="suspend_ids",
        )

    ids = parse_suspend_ids(ids_raw)
    location_ids = ids if location_mode else []
    channel_link_ids = [] if location_mode else ids
    target_count = len(ids)

    channel_status_label = CHANNEL_LINK_STATUS_LABELS.get(
        channel_link_status, channel_link_status
    )
    if location_mode:
        target_label = (
            f"{len(location_ids)} location(s) → `{location_status}` "
            f"(+ their channel links → `{channel_status_label}`)"
            if location_ids
            else "0 locations"
        )
    else:
        target_label = (
            f"{len(channel_link_ids)} channel link(s) → `{channel_status_label}`"
            if channel_link_ids
            else "0 channel links"
        )

    if target_count == 0:
        st.info(
            "Enter at least one location ID."
            if location_mode
            else "Enter at least one channel link ID."
        )
    else:
        st.info(f"Will update **{target_label}**.")
        preview_rows = [
            {
                "Type": "Location" if location_mode else "Channel link",
                "ID": item_id,
                "Status": location_status if location_mode else channel_status_label,
            }
            for item_id in ids
        ]
        if location_mode:
            st.caption(
                "Result rows will include each location’s channel links as well "
                "(one row per link + one row per location)."
            )
        if len(preview_rows) <= 40:
            st.dataframe(preview_rows, use_container_width=True, hide_index=True)

    max_workers = st.slider(
        "Parallel requests",
        min_value=1,
        max_value=50,
        value=10,
        key="suspend_max_workers",
    )

    confirm = st.checkbox(
        f"I confirm I want to **update status** for {target_label}",
        key="suspend_confirm",
    )

    execute_disabled = target_count == 0 or not confirm

    if st.button(
        "Apply status",
        type="primary",
        disabled=execute_disabled,
        key="suspend_run",
    ):
        _track_page("OS Suspend Stores")
        progress_bar = st.progress(0)
        status_text = st.empty()

        def on_progress(completed, total, row):
            progress_bar.progress(completed / total if total else 1.0)
            status_text.text(
                f"{completed}/{total} — {row.get('target_type')} "
                f"{row.get('target_name') or row.get('target_id')}"
            )

        try:
            with st.spinner("Updating statuses..."):
                results = run_set_statuses_by_ids(
                    location_ids=location_ids,
                    channel_link_ids=channel_link_ids,
                    location_status=location_status,
                    channel_link_status=channel_link_status,
                    account_id=account_id,
                    also_update_location_channel_links=location_mode,
                    max_workers=max_workers,
                    on_progress=on_progress,
                )

            progress_bar.empty()
            status_text.empty()
            st.session_state["suspend_results"] = results
            st.session_state["suspend_results_account"] = account_id
            success_count = sum(1 for r in results if r.get("success"))
            st.success(f"Finished: **{success_count}/{len(results)}** succeeded.")
        except Exception as exc:
            progress_bar.empty()
            status_text.empty()
            st.error(f"Status update failed: {exc}")

    if st.session_state.get("suspend_results"):
        results = st.session_state["suspend_results"]
        display = []
        for row in results:
            applied = row.get("applied_status")
            if row.get("target_type") == "Channel link":
                applied_label = CHANNEL_LINK_STATUS_LABELS.get(applied, applied)
            else:
                applied_label = applied
            display.append(
                {
                    "Success": row.get("success"),
                    "Type": row.get("target_type"),
                    "Location": row.get("location_name"),
                    "Channel": row.get("channel_name"),
                    "Name": row.get("target_name"),
                    "ID": row.get("target_id"),
                    "Applied status": applied_label if applied_label is not None else "—",
                    "Error": row.get("error") or "—",
                }
            )

        failed_only = st.checkbox("Show failures only", key="suspend_failures_only")
        shown = [r for r in display if not r["Success"]] if failed_only else display
        st.dataframe(shown, use_container_width=True, hide_index=True)

        safe_account = "".join(
            c
            for c in (st.session_state.get("suspend_results_account") or "account")
            if c.isalnum() or c in (" ", "-", "_")
        ).strip().replace(" ", "_")
        csv_buf = StringIO()
        if shown:
            writer = csv.DictWriter(csv_buf, fieldnames=list(shown[0].keys()))
            writer.writeheader()
            writer.writerows(shown)
        st.download_button(
            label="Download results CSV",
            data=csv_buf.getvalue(),
            file_name=f"status_update_{safe_account}.csv",
            mime="text/csv",
            key="suspend_download",
        )


def page_suspend_stores() -> None:
    if st.session_state.get("tool_page_tracked") != "Suspend":
        _track_page("OS Suspend Stores")
        st.session_state["tool_page_tracked"] = "Suspend"
    st.title("Set status")
    st.caption("Set location status (and its links) or channel link status by ID — one mode at a time.")
    _render_suspend_stores(_get_account_id())


st.set_page_config(
    page_title="Onestop Toolkit",
    layout="wide",
    initial_sidebar_state="expanded",
)
_require_password()
_require_account_id()

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
    "Channel setup": [
        st.Page(
            page_quest_prep,
            title="Quest prep",
            icon=":material/storefront:",
        ),
        st.Page(page_channel_activation, title="Partner emails", icon=":material/mail:"),
    ],
    "Inventory sync": [
        st.Page(page_inventory_sync, title="Analysis", icon=":material/inventory_2:"),
    ],
    "Store availability": [
        st.Page(
            page_close_open_stores,
            title="Close / Open Stores",
            icon=":material/store:",
        ),
        st.Page(
            page_suspend_stores,
            title="Set status",
            icon=":material/block:",
        ),
    ],
}

pg = st.navigation(pages, position="sidebar")
pg.run()
_sign_out_sidebar()
