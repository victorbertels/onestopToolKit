import os
from typing import Callable, Optional

from dotenv import load_dotenv

from utils import (
    getAllLocations,
    createRetailChannel,
    getLocation,
    checkIfRetailOrderAutoAcceptEnabled,
    updateLocation,
    groupResultsByChannel,
)
from retail_channels_payload import justEatPayload, deliverooPayload, uberEatsPayload


def createRetailChannels(
    account_id: str,
    location_to_create: list,
    progress_callback: Optional[Callable[[str], None]] = None,
):
    """
    For each location ID: enable retail auto-accept if needed, then create
    Just Eat, Deliveroo, and Uber Eats retail channel links.
    Returns results grouped by channel (see groupResultsByChannel).
    """
    locations = getAllLocations(account_id)

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    created_information = {}
    for location_id in location_to_create:
        log(f"Processing location `{location_id}`")
        location = getLocation(location_id, locations)
        if location is None:
            log(f"Location `{location_id}` not found in account locations — skipped.")
            continue

        location_name = location.get("name") or location_id
        if not checkIfRetailOrderAutoAcceptEnabled(location):
            log(f"Updating `{location_name}` to enable auto-accept retail orders")
            location_payload = {"posSettings": {"generic": {"autoAcceptRetailOrder": True}}}
            updateLocation(location_id, location_payload, location.get("_etag"))

        created_information[location_name] = {}
        created_information[location_name]["justEatRetail"] = createRetailChannel(
            location, justEatPayload
        )
        created_information[location_name]["deliverooRetail"] = createRetailChannel(
            location, deliverooPayload
        )
        created_information[location_name]["uberEatsRetail"] = createRetailChannel(
            location, uberEatsPayload
        )

    grouped_information = groupResultsByChannel(created_information)
    return grouped_information


if __name__ == "__main__":
    load_dotenv()
    # Example CLI usage — set `ACCOUNT_ID` in `.env` or edit below.
    _account = (os.getenv("ACCOUNT_ID") or "").strip() or "6963884edc8e7760066fa547"
    _locations = [
        "6970b84f39cf45ffda504c46",
        "6970c33dbe723e0d5286010e",
    ]
    out = createRetailChannels(_account, _locations, progress_callback=print)
    print(out)
