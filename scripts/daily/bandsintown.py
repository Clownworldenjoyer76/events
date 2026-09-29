#!/usr/bin/env python3

import csv
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
OUT_ROOT = ROOT / "data" / "events" / "raw" / "bandsintown"

BASE_URL = "https://www.bandsintown.com"
TIMEOUT = 30
REQUEST_DELAY = 0.5

_last_request = 0.0


def fetch(url):
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    req = Request(url, headers={"User-Agent": "events-collector/1.0"})
    _last_request = time.monotonic()

    with urlopen(req, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def slugify(value):
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


class EventLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return

        href = dict(attrs).get("href", "")
        match = re.search(r"/e/(\d+)-[^?#]+", href)

        if match:
            event_id = match.group(1)
            self.links[event_id] = urljoin(BASE_URL, href.split("?")[0])


class JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.buffer = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.capture = True
            self.buffer = []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            text = "".join(self.buffer).strip()
            if text:
                try:
                    self.documents.append(json.loads(text))
                except json.JSONDecodeError:
                    pass
            self.capture = False


def find_event(value):
    if isinstance(value, dict):
        event_type = value.get("@type")

        if event_type == "Event":
            return value

        if isinstance(event_type, list) and "Event" in event_type:
            return value

        for child in value.values():
            result = find_event(child)
            if result:
                return result

    elif isinstance(value, list):
        for child in value:
            result = find_event(child)
            if result:
                return result

    return None


def get_location(event):
    location = event.get("location") or {}

    if isinstance(location, list):
        location = next(
            (item for item in location if isinstance(item, dict)),
            {},
        )

    address = location.get("address") or {}

    if isinstance(address, str):
        return None, None

    city = (address.get("addressLocality") or "").strip()
    region = (address.get("addressRegion") or "").strip()

    return city, region


def main():
    run_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    with CITY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        cities = list(csv.DictReader(handle))

    city_lookup = {
        (
            row["city"].strip().lower(),
            row["state_code"].strip().lower(),
        ): row["city_id"].strip()
        for row in cities
    }

    results = {row["city_id"].strip(): {} for row in cities}
    event_cache = {}

    for row in cities:
        city = row["city"].strip()
        state = row["state_code"].strip()

        city_url = (
            f"{BASE_URL}/c/"
            f"{slugify(city)}-{slugify(state)}"
        )

        try:
            html = fetch(city_url)
        except Exception as exc:
            print(f"CITY FAILED: {city}, {state}: {exc}")
            continue

        parser = EventLinkParser()
        parser.feed(html)

        for event_id, event_url in parser.links.items():
            if event_id not in event_cache:
                try:
                    event_html = fetch(event_url)

                    json_parser = JsonLdParser()
                    json_parser.feed(event_html)

                    event_data = None
                    for document in json_parser.documents:
                        event_data = find_event(document)
                        if event_data:
                            break

                    event_cache[event_id] = event_data

                except Exception as exc:
                    print(f"EVENT FAILED: {event_id}: {exc}")
                    event_cache[event_id] = None

            event_data = event_cache[event_id]

            if not event_data:
                continue

            event_city, event_region = get_location(event_data)

            if not event_city or not event_region:
                continue

            city_id = city_lookup.get(
                (event_city.lower(), event_region.lower())
            )

            if not city_id:
                continue

            results[city_id][event_id] = {
                "bandsintown_event_id": event_id,
                "source_url": event_url,
                "data": event_data,
            }

    total_events = 0

    for city_id, events in results.items():
        output_dir = OUT_ROOT / city_id
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"{run_date}.json"
        records = list(events.values())

        output_file.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        total_events += len(records)

    print("BANDSINTOWN: PASS")
    print(f"CITIES: {len(cities)}")
    print(f"UNIQUE EVENTS: {total_events}")
    print(f"FILES: {len(cities)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
