#!/usr/bin/env python3
"""
Google Maps key check — runs only on a manual "Run workflow" of the dashboard refresh.

Why this exists
---------------
fetch-data.py geocodes through a cache and treats Google failures as non-fatal, which is
correct for a data refresh: a dead key must never take the dashboard down. But it means a
broken key is invisible. Every current address is already cached, so a scheduled run never
calls Google at all, and the job goes green whether the key works or not. The failure only
surfaces weeks later as a new job with no pin on the map.

This check calls Google directly, bypassing the cache, and fails loudly. So a manual run of
the refresh workflow is a genuine test of GOOGLE_MAPS_API_KEY: green means the key works.

Never prints the key.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# A stable, unambiguous address that will not move or be renamed.
PROBE_ADDRESS = "1 King William Street, Adelaide SA 5000, Australia"

# Generous bounds for South Australia — proves we got a real, plausible result rather
# than merely a 200 response.
SA_BOUNDS = {"lat": (-38.5, -25.5), "lng": (128.5, 141.5)}


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        fail("GOOGLE_MAPS_API_KEY is not set. Add it under Settings -> Secrets and variables -> Actions.")

    print(f"Key present: {len(key)} characters.")
    print(f"Probe address: {PROBE_ADDRESS}")

    params = urllib.parse.urlencode({"address": PROBE_ADDRESS, "key": key})
    request = urllib.request.Request(
        "https://maps.googleapis.com/maps/api/geocode/json?" + params,
        headers={"Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        fail(f"HTTP {error.code} from Google. {error.read().decode()[:300]}")
    except Exception as error:  # noqa: BLE001 - any transport problem is a failure here
        fail(f"Could not reach the Google Geocoding API: {error}")

    status = payload.get("status", "unknown")
    detail = payload.get("error_message", "")

    if status != "OK":
        # Google's own words are the most useful diagnostic we can print. REQUEST_DENIED
        # with an IP message means the key has been locked to the server and cannot be
        # used from GitHub's runners — i.e. the split was not done, or the wrong key is
        # in the secret.
        fail(f"Google returned {status}. {detail}".strip())

    results = payload.get("results") or []
    if not results:
        fail("Google returned OK but no results.")

    location = results[0].get("geometry", {}).get("location", {})
    lat, lng = location.get("lat"), location.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        fail("Google returned OK but no usable coordinates.")

    lat_min, lat_max = SA_BOUNDS["lat"]
    lng_min, lng_max = SA_BOUNDS["lng"]
    if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
        fail(f"Coordinates {lat}, {lng} are outside South Australia — result is not trustworthy.")

    print(f"PASS: geocoded to {lat}, {lng}. The Google Maps key works from this runner.")


if __name__ == "__main__":
    main()
