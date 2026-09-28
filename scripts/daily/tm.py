#!/usr/bin/env python3
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
RAW_ROOT = ROOT / "data" / "raw" / "ticketmaster"
ERROR_FILE = ROOT / "errors" / "tm.txt"

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 20
MAX_CALLS_PER_CITY = 15
MIN_REQUEST_INTERVAL_SECONDS = 0.21
REQUEST_TIMEOUT_SECONDS = 30

_last_request_started = 0.0


def write_report(lines):
    ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
    ERROR_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def fetch_raw(params):
    global _last_request_started

    elapsed = time.monotonic() - _last_request_started
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={
            "Accept": "application/json",
            "User-Agent": "events-ticketmaster-collector/1.0",
        },
    )

    _last_request_started = time.monotonic()
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def main():
    run_started = datetime.now(timezone.utc)
    run_date = run_started.strftime("%Y%m%d")
    start_filter = run_started.strftime("%Y-%m-%dT%H:%M:%SZ")

    report = [
        "Ticketmaster daily collector",
        f"run_started_utc: {start_filter}",
        f"page_size: {PAGE_SIZE}",
        f"max_calls_per_city: {MAX_CALLS_PER_CITY}",
        "",
    ]

    api_key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
    if not api_key:
        report.extend(["status: FAILED", "error: TICKETMASTER_API_KEY is not set"])
        write_report(report)
        print("TICKETMASTER: FAILED")
        print("ERROR: TICKETMASTER_API_KEY is not set")
        return 2

    try:
        with CITY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
            cities = list(csv.DictReader(handle))
    except Exception as exc:
        report.extend(["status: FAILED", f"error: unable to read city file: {exc}"])
        write_report(report)
        print("TICKETMASTER: FAILED")
        print("ERROR: unable to read city_master.csv")
        return 2

    required = ("city_id", "city", "state_code", "country_code")
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
        print("TICKETMASTER: FAILED")
        print(f"ERROR: {detail}")
        return 2

    total_calls = 0
    total_events = 0
    total_files = 0
    successful_cities = 0
    failed_cities = 0
    capped_cities = 0
    city_lines = []
    error_lines = []

    for row_number, city_row in enumerate(cities, start=2):
        values = {
            name: (city_row.get(name) or "").strip()
            for name in required
        }

        missing_values = [
            name for name, value in values.items()
            if not value
        ]

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

        city_dir = RAW_ROOT / values["city_id"] / run_date

        # Only today's run folder may be replaced. Older dated folders are untouched.
        if city_dir.exists():
            shutil.rmtree(city_dir)
        city_dir.mkdir(parents=True, exist_ok=True)

        city_calls = 0
        city_events = 0
        city_files = 0
        total_pages = None
        failed = False
        failure_message = ""

        for page in range(MAX_CALLS_PER_CITY):
            params = {
                "apikey": api_key,
                "city": values["city"],
                "stateCode": values["state_code"],
                "countryCode": values["country_code"],
                "startDateTime": start_filter,
                "size": PAGE_SIZE,
                "page": page,
                "sort": "date,asc",
            }

            city_calls += 1
            total_calls += 1

            try:
                raw = fetch_raw(params)
            except HTTPError as exc:
                failed = True
                failure_message = f"HTTP {exc.code} {exc.reason}"
                break
            except URLError as exc:
                failed = True
                failure_message = f"URL error: {exc.reason}"
                break
            except Exception as exc:
                failed = True
                failure_message = f"{type(exc).__name__}: {exc}"
                break

            page_file = city_dir / f"page_{page:03d}.json"
            page_file.write_bytes(raw)
            city_files += 1
            total_files += 1

            try:
                payload = json.loads(raw)
            except Exception as exc:
                failed = True
                failure_message = f"invalid JSON on page {page}: {exc}"
                break

            events = payload.get("_embedded", {}).get("events", [])
            city_events += len(events)
            total_events += len(events)

            page_info = payload.get("page", {})
            try:
                total_pages = int(page_info.get("totalPages", 0))
            except (TypeError, ValueError):
                failed = True
                failure_message = f"invalid totalPages on page {page}"
                break

            if total_pages == 0 or page + 1 >= total_pages:
                break

        if failed:
            failed_cities += 1
            message = (
                f"{label}: {failure_message}; "
                f"calls={city_calls}; events={city_events}; files={city_files}"
            )
            error_lines.append(message)
            city_lines.append(f"FAIL | {message}")
        else:
            successful_cities += 1
            capped = (
                total_pages is not None
                and total_pages > MAX_CALLS_PER_CITY
            )

            if capped:
                capped_cities += 1

            city_lines.append(
                f"OK | {label} | calls={city_calls} | "
                f"events={city_events} | files={city_files} | "
                f"source_pages={total_pages} | "
                f"capped={'yes' if capped else 'no'}"
            )

    status = "PASS" if failed_cities == 0 else "PARTIAL"

    report.extend(
        [
            f"status: {status}",
            f"city_rows: {len(cities)}",
            f"cities_successful: {successful_cities}",
            f"cities_failed: {failed_cities}",
            f"cities_capped_at_15_calls: {capped_cities}",
            f"api_calls: {total_calls}",
            f"events_downloaded: {total_events}",
            f"raw_files_written: {total_files}",
            "",
            "CITY RESULTS",
            *city_lines,
            "",
            "ERRORS",
            *(error_lines if error_lines else ["none"]),
        ]
    )

    write_report(report)

    print(f"TICKETMASTER: {status}")
    print(f"CITIES: {len(cities)}")
    print(f"SUCCESS: {successful_cities}")
    print(f"FAILED: {failed_cities}")
    print(f"CAPPED: {capped_cities}")
    print(f"API CALLS: {total_calls}")
    print(f"EVENTS: {total_events}")
    print(f"RAW FILES: {total_files}")
    print("ERROR FILE: errors/tm.txt")

    return 0


if __name__ == "__main__":
    sys.exit(main())

