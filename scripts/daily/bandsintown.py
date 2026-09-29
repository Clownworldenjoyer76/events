#!/usr/bin/env python3

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
OUT_ROOT = ROOT / "data" / "events" / "raw" / "bandsintown"

BASE_URL = "https://www.bandsintown.com"
NAV_TIMEOUT_MS = 45_000
CITY_SETTLE_MS = 2_000
EVENT_SETTLE_MS = 700
EVENT_RE = re.compile(r"/e/(\d+)-", re.IGNORECASE)


def slugify(value):
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def find_event(value):
    if isinstance(value, dict):
        event_type = value.get("@type")
        if event_type == "Event":
            return value
        if isinstance(event_type, list) and "Event" in event_type:
            return value
        for child in value.values():
            found = find_event(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_event(child)
            if found:
                return found
    return None


def extract_json_ld_event(page):
    for text in page.locator('script[type="application/ld+json"]').all_text_contents():
        text = text.strip()
        if not text:
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            continue
        event = find_event(document)
        if event:
            return event
    return None


def get_location(event):
    location = event.get("location") or {}
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), {})
    if not isinstance(location, dict):
        return None, None

    address = location.get("address") or {}
    if not isinstance(address, dict):
        return None, None

    city = str(address.get("addressLocality") or "").strip()
    region = str(address.get("addressRegion") or "").strip()
    return city or None, region or None


def normalize_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def main():
    run_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    with CITY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        cities = list(csv.DictReader(handle))

    if not cities:
        raise RuntimeError("city_master.csv contains no cities")

    city_lookup = {}
    for row in cities:
        city = normalize_key(row["city"])
        city_id = row["city_id"].strip()
        for region in (row.get("state_code"), row.get("state")):
            if region:
                city_lookup[(city, normalize_key(region))] = city_id

    results = {row["city_id"].strip(): {} for row in cities}
    event_cache = {}
    city_pages_with_links = 0
    discovered_links = 0
    event_pages_parsed = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/153.0.0.0 Safari/537.36"
            ),
        )
        city_page = context.new_page()
        event_page = context.new_page()
        city_page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        event_page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        for index, row in enumerate(cities, start=1):
            city = row["city"].strip()
            state = row["state_code"].strip()
            source_city_id = row["city_id"].strip()
            city_url = f"{BASE_URL}/c/{slugify(city)}-{slugify(state)}"

            try:
                response = city_page.goto(city_url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    print(f"CITY HTTP {response.status}: {city}, {state}")
                    continue

                try:
                    city_page.locator('a[href*="/e/"]').first.wait_for(
                        state="attached", timeout=10_000
                    )
                except PlaywrightTimeoutError:
                    pass

                city_page.wait_for_timeout(CITY_SETTLE_MS)

                hrefs = city_page.locator('a[href*="/e/"]').evaluate_all(
                    """els => [...new Set(
                        els.map(el => el.href).filter(Boolean)
                    )]"""
                )
            except Exception as exc:
                print(f"CITY FAILED: {city}, {state}: {exc}")
                continue

            links = {}
            for href in hrefs:
                match = EVENT_RE.search(href)
                if not match:
                    continue
                event_id = match.group(1)
                clean_url = href.split("?", 1)[0]
                links[event_id] = clean_url

            if links:
                city_pages_with_links += 1
                discovered_links += len(links)

            print(
                f"CITY {index:03d}/{len(cities)} "
                f"{source_city_id} {city}, {state}: {len(links)} links"
            )

            for event_id, event_url in links.items():
                if event_id not in event_cache:
                    event_data = None
                    try:
                        response = event_page.goto(
                            event_url, wait_until="domcontentloaded"
                        )
                        if response is None or response.status < 400:
                            event_page.wait_for_timeout(EVENT_SETTLE_MS)
                            event_data = extract_json_ld_event(event_page)
                            if event_data:
                                event_pages_parsed += 1
                    except Exception as exc:
                        print(f"EVENT FAILED: {event_id}: {exc}")

                    event_cache[event_id] = event_data

                event_data = event_cache[event_id]
                if not event_data:
                    continue

                event_city, event_region = get_location(event_data)
                if not event_city or not event_region:
                    continue

                city_id = city_lookup.get(
                    (normalize_key(event_city), normalize_key(event_region))
                )
                if not city_id:
                    continue

                results[city_id][event_id] = {
                    "bandsintown_event_id": event_id,
                    "source_city_id": source_city_id,
                    "source_city_url": city_url,
                    "source_url": event_url,
                    "data": event_data,
                }

        context.close()
        browser.close()

    if discovered_links == 0:
        raise RuntimeError(
            "Bandsintown city pages produced zero event links; refusing empty output"
        )

    total_events = sum(len(events) for events in results.values())
    populated_cities = sum(bool(events) for events in results.values())

    if total_events == 0:
        raise RuntimeError(
            f"Discovered {discovered_links} event links but mapped zero events; "
            "refusing empty output"
        )

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    for old_file in OUT_ROOT.glob(f"*/{run_date}.json"):
        old_file.unlink()

    written_files = 0
    for city_id, events in results.items():
        if not events:
            continue

        output_dir = OUT_ROOT / city_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{run_date}.json"
        records = list(events.values())
        output_file.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written_files += 1

    print("BANDSINTOWN: PASS")
    print(f"CITIES CHECKED: {len(cities)}")
    print(f"CITY PAGES WITH LINKS: {city_pages_with_links}")
    print(f"EVENT LINKS DISCOVERED: {discovered_links}")
    print(f"EVENT PAGES PARSED: {event_pages_parsed}")
    print(f"POPULATED CITIES: {populated_cities}")
    print(f"UNIQUE EVENTS: {total_events}")
    print(f"FILES WRITTEN: {written_files}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
