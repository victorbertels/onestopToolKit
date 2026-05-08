from pathlib import Path
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests
from auth import getHeaders

def getAllLocations(account: str):
    """
    Get all locations for an account.
    
    Args:
        account: Account ID
        return_format: "list" returns list of dicts with name/id, 
                      "ids" returns just location IDs,
                      "raw" returns raw API response locations array
                      
    Returns:
        List of locations in requested format
    """
    try:
        location_list = []
        page = 1
        max_results = 500
        while True:
            url = f"https://api.deliverect.io/locations?where={{\"account\":\"{account}\"}}&page={page}&max_results={max_results}"
            response = requests.get(url, headers=getHeaders())
            data = response.json()
            if response.status_code != 200:
                return []
            
            items = data.get("_items", [])
            if not items:
                break
            location_list.extend(items)
            page += 1
            print("Fetching locations on page" , page)
        return location_list
    except Exception as e:
        print(f"Error getting locations: {e}")
        return []


def getLocation(location_id, all_locations: list):
    for location in all_locations:
        if location.get("_id") == location_id:
            return location
    return None


def createRetailChannel(location: dict ,channelPayload: dict):
    locationId = location.get("_id")
    accountId = location.get("account")
    locationPosSettings = location.get("posSettings")
    channelPayload['posSettings'] = locationPosSettings
    channelPayload['location'] = locationId
    channelPayload['account'] = accountId
    url = f"https://api.deliverect.io/channelLinks"
    response = requests.post(url, headers=getHeaders(), json=channelPayload)
    if response.status_code != 201:
        return False
    return response.json().get("_id")


def updateLocation(locationId: str, locationPayload: dict, _etag):
    url = f"https://api.deliverect.io/locations/{locationId}"
    headers = getHeaders()
    headers['If-Match'] = _etag
    response = requests.put(url, headers=headers, json=locationPayload)
    if response.status_code != 201:
        return False
    return response.json().get("_id")


def checkIfRetailOrderAutoAcceptEnabled(location: str):
    posSettings = location.get("posSettings")
    generic = posSettings.get("generic")
    return generic.get("autoAcceptRetailOrder")

# Keys produced by create_retail_channels (location → channels) → human-readable group titles
CHANNEL_GROUP_LABELS = {
    "justEatRetail": "Just Eat",
    "deliverooRetail": "Deliveroo",
    "uberEatsRetail": "Uber Eats",
}


def groupResultsByChannel(results_by_location: dict) -> dict:
    """
    { locationName: { channelKey: linkId } } → { "Just Eat": { locationName: linkId }, ... }
    """
    out = {}
    for location_name, channels in results_by_location.items():
        for channel_key, link_id in channels.items():
            label = CHANNEL_GROUP_LABELS.get(channel_key)
            if label is None:
                continue
            out.setdefault(label, {})[location_name] = link_id
    return out


def getChannelLink(channelLinkId: str):
    url = f"https://api.deliverect.io/channelLinks/{channelLinkId}"
    response = requests.get(url, headers=getHeaders())
    if response.status_code != 200:
        return False
    return response.json()

def checkApplication(channelLink: str):
    channelSettings = channelLink.get("channelSettings")
    application = channelSettings.get("application")
    return application

def updateChannelLink(channelLinkId: str, payload: str, _etag):
    url = f"https://api.deliverect.io/channelLinks/{channelLinkId}"
    headers = getHeaders()
    headers['If-Match'] = _etag
    response = requests.put(url, headers=headers, json=payload)
    if response.status_code != 200:
        return False
    return True

