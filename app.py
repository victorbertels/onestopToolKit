"""
Streamlit UI for creating Deliverect retail channel links (Just Eat, Deliveroo, Uber Eats).

Run from the project directory:
  streamlit run app.py
"""

import os

import streamlit as st
from dotenv import load_dotenv

from main import createRetailChannels

load_dotenv()


def _parse_location_ids(raw: str) -> list[str]:
    lines = [line.strip() for line in raw.strip().splitlines()]
    return [x for x in lines if x]


st.set_page_config(page_title="Retail channel setup", layout="wide")
st.title("Retail channel setup")
st.caption(
    "Creates Just Eat, Deliveroo, and Uber Eats retail channel links for selected locations. "
    "API credentials and optional default account come from `.env` "
    "(`CLIENT_ID`, `CLIENT_SECRET`, `ACCOUNT_ID`)."
)

if "account_id_input" not in st.session_state:
    st.session_state.account_id_input = (os.getenv("ACCOUNT_ID") or "").strip()

with st.sidebar:
    st.header("Account")
    account_id = st.text_input(
        "Account ID",
        placeholder="e.g. 6963884edc8e7760066fa547",
        help=(
            "Deliverect account `_id` used to fetch locations. "
            "You can set a default in `.env` as `ACCOUNT_ID=...` and override here when needed."
        ),
        key="account_id_input",
    )

st.subheader("Locations")
location_raw = st.text_area(
    "Location IDs (one per line)",
    height=220,
    placeholder="Paste one MongoDB location `_id` per line",
    help="Only these locations are processed; each must belong to the account above.",
)

run = st.button("Create retail channels", type="primary")

if run:
    if not account_id.strip():
        st.error("Enter an account ID.")
        st.stop()

    location_ids = _parse_location_ids(location_raw)
    if not location_ids:
        st.error("Enter at least one location ID.")
        st.stop()

    log_lines: list[str] = []
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

    st.success("Done.")

    st.subheader("Results by channel")
    if not grouped:
        st.info("No channel links were created (no locations matched or all steps failed).")
    else:
        for channel_label, by_location in grouped.items():
            with st.expander(channel_label, expanded=True):
                rows = [
                    {"Location": loc_name, "Channel link ID": link_id}
                    for loc_name, link_id in by_location.items()
                ]
                st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("Raw grouped JSON"):
        st.json(grouped)
