#!/usr/bin/env python3
# scripts/daily/foursquare.py

import csv
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
RAW_ROOT = ROOT / "data" / "venues" / "raw" / "foursquare"
ERROR_FILE = ROOT / "errors" / "foursquare.txt"

API_URL = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"
LIMIT = 50
REQUEST_TIMEOUT_SECONDS = 30
MIN_REQUEST_INTERVAL_SECONDS = 0.30
MAX_ATTEMPTS = 4

CATEGORY_SEARCHES = (
    ("arts_entertainment", "4d4b7104d754a06370d81259"),
    ("bars", "4bf58dd8d48988d116941735"),
)

_last_request_started = 0.0


def write_report(lines):
    ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
    ERROR_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def miles_to_meters(value):
    miles = float(value)
    if not math.isfinite(miles) or miles <= 0:
        raise ValueError("radius_miles must be greater than zero")
    return min(100000, max(1, round(miles * 1609.344)))


def fetch_json(api_key, params):
    global _last_request_started

    url = f"{API_URL}?{urlencode(params)}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        elapsed = time.monotonic() - _last_request_started
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-Places-Api-Version": API_VERSION,
                "User-Agent": "events-foursquare-collector/1.0",
            },
        )

        _last_request_started = time.monotonic()

        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw)
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if retryable and attempt < MAX_ATTEMPTS:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 10)
                except ValueError:
                    delay = min(2 ** attempt, 10)
                time.sleep(max(delay, 1))
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            detail = f"HTTP {exc.code} {exc.reason}"
            if body:
                detail += f" | {body}"
            raise RuntimeError(detail) from exc
        except URLError as exc:
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise RuntimeError(f"URL error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON response: {exc}") from exc


def main():
    run_started = datetime.now(timezone.utc)
    run_date = run_started.strftime("%Y%m%d")
    run_started_text = run_started.strftime("%Y-%m-%dT%H:%M:%SZ")

    report = [
        "Foursquare venue collector",
        f"run_started_utc: {run_started_text}",
        f"api_version: {API_VERSION}",
        f"limit_per_search: {LIMIT}",
        "searches_per_city: " + ", ".join(name for name, _ in CATEGORY_SEARCHES),
        "",
    ]

    api_key = os.environ.get("FOURSQUARE_SERVICE_API_KEY", "").strip()
    if not api_key:
        report.extend([
            "status: FAILED",
            "error: FOURSQUARE_SERVICE_API_KEY is not set",
        ])
        write_report(report)
        print("FOURSQUARE: FAILED")
        print("ERROR: FOURSQUARE_SERVICE_API_KEY is not set")
        return 2

    try:
        with CITY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
            cities = list(csv.DictReader(handle))
    except Exception as exc:
        report.extend([
            "status: FAILED",
            f"error: unable to read city file: {exc}",
        ])
        write_report(report)
        print("FOURSQUARE: FAILED")
        print("ERROR: unable to read city_master.csv")
        return 2

    required = (
        "city_id",
        "city",
        "state_code",
        "country_code",
        "latitude",
        "longitude",
    )
    headers = cities[0].keys() if cities else ()
    missing_headers = [name for name in required if name not in headers]

    if not cities or missing_headers:
        detail = (
            "city file is empty"
            if not cities
            else f"missing headers: {', '.join(missing_headers)}"
        )
        report.extend(["status: FAILED", f"error: {detail}"])
        write_report(report)
        print("FOURSQUARE: FAILED")
        print(f"ERROR: {detail}")
        return 2

    total_calls = 0
    total_unique_places = 0
    total_files = 0
    successful_cities = 0
    failed_cities = 0
    partial_cities = 0
    capped_searches = 0
    city_lines = []
    error_lines = []

    for row_number, city_row in enumerate(cities, start=2):
        values = {
            name: (city_row.get(name) or "").strip()
            for name in required
        }

        missing_values = [name for name, value in values.items() if not value]
        label = (
            f"{values['city'] or 'UNKNOWN'}, "
            f"{values['state_code'] or '??'} "
            f"({values['city_id'] or 'NO_ID'})"
        )

        if missing_values:
            failed_cities += 1
            message = (
                f"{label}: missing {', '.join(missing_values)} "
                f"at CSV row {row_number}"
            )
            error_lines.append(message)
            city_lines.append(f"FAIL | {message}")
            continue

        try:
            latitude = float(values["latitude"])
            longitude = float(values["longitude"])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("coordinates outside valid range")
        except ValueError as exc:
            failed_cities += 1
            message = f"{label}: invalid coordinates: {exc}"
            error_lines.append(message)
            city_lines.append(f"FAIL | {message}")
            continue

        radius_miles_text = (city_row.get("radius_miles") or "").strip()
        radius_meters = None
        if radius_miles_text:
            try:
                radius_meters = miles_to_meters(radius_miles_text)
            except ValueError as exc:
                failed_cities += 1
                message = f"{label}: invalid radius_miles: {exc}"
                error_lines.append(message)
                city_lines.append(f"FAIL | {message}")
                continue

        city_searches = []
        city_unique_ids = set()
        city_failures = []
        city_caps = 0

        for search_name, category_id in CATEGORY_SEARCHES:
            params = {
                "ll": f"{latitude:.7f},{longitude:.7f}",
                "fsq_category_ids": category_id,
                "limit": LIMIT,
                "sort": "DISTANCE",
                "tel_format": "E164",
            }

            if radius_meters is not None:
                params["radius"] = radius_meters

            total_calls += 1

            try:
                payload = fetch_json(api_key, params)
            except Exception as exc:
                message = f"{label} | {search_name}: {type(exc).__name__}: {exc}"
                city_failures.append(message)
                error_lines.append(message)
                continue

            results = payload.get("results")
            if not isinstance(results, list):
                message = f"{label} | {search_name}: response missing results array"
                city_failures.append(message)
                error_lines.append(message)
                continue

            if len(results) >= LIMIT:
                city_caps += 1
                capped_searches += 1

            for place in results:
                if isinstance(place, dict):
                    fsq_id = place.get("fsq_place_id")
                    if fsq_id:
                        city_unique_ids.add(str(fsq_id))

            city_searches.append({
                "search_name": search_name,
                "fsq_category_ids": category_id,
                "request": params,
                "response": payload,
            })

        if not city_searches:
            failed_cities += 1
            city_lines.append(
                f"FAIL | {label} | searches=0/{len(CATEGORY_SEARCHES)}"
            )
            continue

        if city_failures:
            partial_cities += 1
            city_status = "PARTIAL"
        else:
            successful_cities += 1
            city_status = "PASS"

        city_dir = RAW_ROOT / values["city_id"]
        city_dir.mkdir(parents=True, exist_ok=True)
        city_file = city_dir / f"{run_date}.json"

        output = {
            "source": "foursquare_places_api",
            "run_started_utc": run_started_text,
            "api_version": API_VERSION,
            "city": {
                "city_id": values["city_id"],
                "city": values["city"],
                "state_code": values["state_code"],
                "country_code": values["country_code"],
                "latitude": latitude,
                "longitude": longitude,
                "radius_miles": radius_miles_text or None,
            },
            "searches": city_searches,
        }

        city_file.write_text(
            json.dumps(output, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        total_files += 1
        total_unique_places += len(city_unique_ids)

        city_lines.append(
            f"{city_status} | {label} | "
            f"searches={len(city_searches)}/{len(CATEGORY_SEARCHES)} | "
            f"unique_places={len(city_unique_ids)} | capped={city_caps}"
        )

    if successful_cities == 0 and partial_cities == 0:
        status = "FAILED"
        exit_code = 2
    elif failed_cities or partial_cities:
        status = "PARTIAL"
        exit_code = 1
    elif total_unique_places == 0:
        status = "FAILED"
        exit_code = 2
        error_lines.append("all successful searches returned zero places")
    else:
        status = "PASS"
        exit_code = 0

    report.extend([
        *city_lines,
        "",
        f"status: {status}",
        f"cities_in_master: {len(cities)}",
        f"successful_cities: {successful_cities}",
        f"partial_cities: {partial_cities}",
        f"failed_cities: {failed_cities}",
        f"api_calls: {total_calls}",
        f"files_written: {total_files}",
        f"sum_unique_places_by_city: {total_unique_places}",
        f"capped_searches: {capped_searches}",
    ])

    if error_lines:
        report.extend(["", "errors:", *error_lines])

    write_report(report)

    print(f"FOURSQUARE: {status}")
    print(f"CITIES IN MASTER: {len(cities)}")
    print(f"CITIES SUCCESSFUL: {successful_cities}")
    print(f"CITIES PARTIAL: {partial_cities}")
    print(f"CITIES FAILED: {failed_cities}")
    print(f"API CALLS: {total_calls}")
    print(f"FILES WRITTEN: {total_files}")
    print(f"SUM UNIQUE PLACES BY CITY: {total_unique_places}")
    print(f"CAPPED SEARCHES: {capped_searches}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
