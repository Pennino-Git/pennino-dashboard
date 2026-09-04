#!/usr/bin/env python3
"""
Google Maps key check.

Why this exists
---------------
fetch-data.py geocodes through a cache and treats Google failures as non-fatal, which is
correct for a data refresh: a dead key must never take the dashboard down. But it means a
broken key is invisible. Every current address is already cached, so a scheduled run never
calls Google at all, and the job goes green whether the key works or not. The failure would
only surface weeks later, as a new job with no pin on the map.

So fetch-data.py calls run_check() on manual runs only (GITHUB_EVENT_NAME=workflow_dispatch)
and stops if it fails. That makes "Run workflow -> green tick" a genuine proof that
GOOGLE_MAPS_API_KEY works. Scheduled runs are untouched and make no extra API calls.

Never prints or returns the key itself.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# A stable, unambiguous address that will not move or be renamed.
PROBE_ADDRESS = "1 King William Street, Adelaide SA 5000, Australia"

# Generous bounds for South Australia. Proves we got a real, plausible result rather than
# merely a 200 response.
SA_LAT = (-38.5, -25.5)
SA_LNG = (128.5, 141.5)


def run_check(api_key, address=PROBE_ADDRESS, timeout=20):
    """Geocode a known address, bypassing every cache.

    Returns (ok, message). Never raises, so a caller can decide what a failure means.
    """
    key = (api_key or "").strip()
    if not key:
        return False, "GOOGLE_MAPS_API_KEY is not set (Settings -> Secrets and variables -> Actions)."

    params = urllib.parse.urlencode({"address": address, "key": key})
    request = urllib.request.Request(
        "https://maps.googleapis.com/maps/api/geocode/json?" + params,
        headers={"Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code} from Google. {error.read().decode()[:300]}"
    except Exception as error:  # noqa: BLE001 - any transport problem is a failure here
        return False, f"Could not reach the Google Geocoding API: {error}"

    status = payload.get("status", "unknown")
    if status != "OK":
        # Google's own words are the most useful diagnostic available. REQUEST_DENIED with
        # an IP message means this key has been locked to the app server and cannot be used
        # from GitHub's runners - i.e. the wrong key is in the secret, or the key was locked
        # before being split.
        return False, f"Google returned {status}. {payload.get('error_message', '')}".strip()

    results = payload.get("results") or []
    if not results:
        return False, "Google returned OK but no results."

    location = results[0].get("geometry", {}).get("location", {})
    lat, lng = location.get("lat"), location.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return False, "Google returned OK but no usable coordinates."

    if not (SA_LAT[0] <= lat <= SA_LAT[1] and SA_LNG[0] <= lng <= SA_LNG[1]):
        return False, f"Coordinates {lat}, {lng} are outside South Australia - not trustworthy."

    return True, f"Google Maps key works from this runner (key is {len(key)} characters; probe resolved to {lat}, {lng})."


def main():
    ok, message = run_check(os.environ.get("GOOGLE_MAPS_API_KEY", ""))
    if ok:
        print(f"PASS: {message}")
        return 0
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
