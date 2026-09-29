#!/usr/bin/env python3
import csv
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
RAW_ROOT = ROOT / "data" / "events" / "raw" / "mobilizon"

INSTANCE_DIRECTORY = "https://instances.mobilizon.org/"
TIMEOUT = 25
PAGE_SIZE = 50
MIN_INTERVAL = 0.10
_last_request = 0.0

SEARCH_QUERY = """
query SearchEvents($page: Int, $limit: Int, $beginsOn: DateTime) {
  searchEvents(page: $page, limit: $limit, beginsOn: $beginsOn) {
    total
    elements {
      uuid
      url
      title
      description
      beginsOn
      endsOn
      status
      category
      physicalAddress {
        street
        locality
        postalCode
        region
        country
        description
        geom
      }
      organizerActor {
        preferredUsername
        domain
        name
      }
      attributedTo {
        preferredUsername
        domain
        name
        type
      }
      tags {
        slug
        title
      }
    }
  }
}
"""

EVENT_QUERY = """
query EventByUUID($uuid: UUID!) {
  event(uuid: $uuid) {
    uuid
    url
    title
    description
    beginsOn
    endsOn
    status
    category
    physicalAddress {
      street
      locality
      postalCode
      region
      country
      description
      geom
    }
    organizerActor {
      preferredUsername
      domain
      name
    }
    attributedTo {
      preferredUsername
      domain
      name
      type
    }
    tags {
      slug
      title
    }
  }
}
"""

CONFIG_QUERY = "query { config { name } }"

UUID_RE = re.compile(
    r"(?i)\\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\\b"
)


class InstanceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tbody = False
        self.hosts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tbody":
            self.in_tbody = True
            return
        if not self.in_tbody or tag != "a":
            return
        href = (attrs.get("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            return
        host = (urlparse(href).hostname or "").lower()
        if host and host not in self.hosts:
            self.hosts.append(host)

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_tbody = False


def throttle():
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request = time.monotonic()


def get_bytes(url, accept="text/html,application/xhtml+xml,*/*"):
    throttle()
    req = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "events-mobilizon-collector/1.0",
        },
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def graphql(host, query, variables=None):
    throttle()
    body = json.dumps(
        {"query": query, "variables": variables or {}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    req = Request(
        f"https://{host}/api",
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "events-mobilizon-collector/1.0",
        },
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        payload = json.loads(response.read())
    if payload.get("errors"):
        messages = [
            str(item.get("message") or item)
            for item in payload["errors"]
            if isinstance(item, dict)
        ]
        raise RuntimeError("; ".join(messages) or "GraphQL returned errors")
    return payload.get("data") or {}


def norm(value):
    return re.sub(r"\\s+", " ", str(value or "").strip()).casefold()


def norm_country(value):
    value = norm(value)
    aliases = {
        "us": "us",
        "usa": "us",
        "united states": "us",
        "united states of america": "us",
    }
    return aliases.get(value, value)


def build_city_lookup(cities):
    lookup = {}
    for row in cities:
        city = norm(row.get("city"))
        state_code = norm(row.get("state_code"))
        state_name = norm(row.get("state"))
        country = norm_country(row.get("country_code"))
        city_id = str(row.get("city_id") or "").strip()
        if not city or not state_code or not city_id:
            continue
        for region in {state_code, state_name} - {""}:
            lookup[(city, region, country)] = city_id
            lookup[(city, region, "")] = city_id
    return lookup


def map_city(event, lookup):
    address = event.get("physicalAddress") or {}
    if not isinstance(address, dict):
        return None
    locality = norm(address.get("locality"))
    region = norm(address.get("region"))
    country = norm_country(address.get("country"))
    if not locality or not region:
        return None
    if country and country != "us":
        return None
    return (
        lookup.get((locality, region, country))
        or lookup.get((locality, region, ""))
    )


def event_key(event):
    url = str(event.get("url") or "").strip()
    if url:
        return url
    uuid = str(event.get("uuid") or "").strip()
    if uuid:
        return uuid
    return "|".join(
        [
            norm(event.get("title")),
            norm(event.get("beginsOn")),
            norm((event.get("physicalAddress") or {}).get("description")),
        ]
    )


def discover_instances():
    html = get_bytes(INSTANCE_DIRECTORY).decode("utf-8", errors="replace")
    parser = InstanceParser()
    parser.feed(html)

    valid = []
    failed = []

    for host in parser.hosts:
        try:
            data = graphql(host, CONFIG_QUERY)
            if isinstance(data.get("config"), dict):
                valid.append(host)
        except Exception as exc:
            failed.append(f"{host}: {type(exc).__name__}: {exc}")

    return valid, failed


def fetch_instance_events(host, begins_on):
    collected = []
    page = 1
    total = None

    while True:
        data = graphql(
            host,
            SEARCH_QUERY,
            {"page": page, "limit": PAGE_SIZE, "beginsOn": begins_on},
        )
        result = data.get("searchEvents") or {}
        elements = result.get("elements") or []
        if not isinstance(elements, list) or not elements:
            break

        if total is None:
            try:
                total = int(result.get("total") or 0)
            except (TypeError, ValueError):
                total = 0

        collected.extend(x for x in elements if isinstance(x, dict))

        if total and len(collected) >= total:
            break
        if len(elements) < PAGE_SIZE:
            break
        page += 1

    return collected


def group_feed(event, discovered_host):
    actor = event.get("attributedTo") or {}
    if not isinstance(actor, dict) or norm(actor.get("type")) != "group":
        return None
    username = str(actor.get("preferredUsername") or "").strip()
    if not username:
        return None
    domain = str(actor.get("domain") or "").strip().lower()
    host = domain or discovered_host
    return f"https://{host}/@{username}/feed/ics"


def unfold_ics(text):
    lines = []
    for line in text.replace("\\r\\n", "\\n").replace("\\r", "\\n").split("\\n"):
        if line.startswith((" ", "\\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_ics(text):
    items = []
    current = None
    for line in unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                items.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        key = left.split(";", 1)[0].upper()
        if key in {"UID", "URL", "SUMMARY", "DTSTART", "DTEND", "LOCATION"}:
            current[key] = value.strip()
    return items


def ics_event_ref(feed_url, item):
    url = str(item.get("URL") or "").strip()
    uid = str(item.get("UID") or "").strip()
    match = UUID_RE.search(url or uid)
    if not match:
        return None, None
    host = (urlparse(url).hostname or "").lower() if url else ""
    if not host:
        host = (urlparse(feed_url).hostname or "").lower()
    return (host, match.group(0)) if host else (None, None)


def fetch_event(host, uuid):
    data = graphql(host, EVENT_QUERY, {"uuid": uuid})
    event = data.get("event")
    return event if isinstance(event, dict) else None


def main():
    started = datetime.now(timezone.utc)
    started_text = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_date = started.strftime("%Y%m%d")

    with CITY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        cities = list(csv.DictReader(handle))

    required = {"city_id", "state", "state_code", "city", "country_code"}
    if not cities:
        print("MOBILIZON: FAILED")
        print("ERROR: city_master.csv is empty")
        return 2

    missing = sorted(required - set(cities[0].keys()))
    if missing:
        print("MOBILIZON: FAILED")
        print("ERROR: missing city columns: " + ", ".join(missing))
        return 2

    lookup = build_city_lookup(cities)
    city_rows = {
        str(row.get("city_id") or "").strip(): row
        for row in cities
        if str(row.get("city_id") or "").strip()
    }
    city_events = {city_id: {} for city_id in city_rows}

    instances, directory_failures = discover_instances()
    if not instances:
        print("MOBILIZON: FAILED")
        print("ERROR: no valid Mobilizon instances discovered")
        return 2

    graphql_failures = []
    feeds = set()
    graphql_seen = 0
    graphql_mapped = 0

    for i, host in enumerate(instances, 1):
        try:
            events = fetch_instance_events(host, started_text)
        except Exception as exc:
            graphql_failures.append(f"{host}: {type(exc).__name__}: {exc}")
            print(f"INSTANCE {i:03d}/{len(instances)} {host}: FAILED")
            continue

        graphql_seen += len(events)
        mapped = 0

        for event in events:
            feed = group_feed(event, host)
            if feed:
                feeds.add(feed)

            city_id = map_city(event, lookup)
            if not city_id:
                continue

            key = event_key(event)
            if key not in city_events[city_id]:
                city_events[city_id][key] = {
                    "source_method": "graphql",
                    "discovered_on_instance": host,
                    "event": event,
                }
                graphql_mapped += 1
                mapped += 1

        print(
            f"INSTANCE {i:03d}/{len(instances)} {host}: "
            f"events={len(events)} mapped={mapped}"
        )

    feed_failures = []
    feed_seen = 0
    feed_added = 0
    event_cache = {}

    feed_list = sorted(feeds)
    for i, feed_url in enumerate(feed_list, 1):
        try:
            raw = get_bytes(feed_url, accept="text/calendar,*/*")
            items = parse_ics(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            feed_failures.append(f"{feed_url}: {type(exc).__name__}: {exc}")
            print(f"FEED {i:03d}/{len(feed_list)}: FAILED")
            continue

        feed_seen += len(items)
        added = 0

        for item in items:
            host, uuid = ics_event_ref(feed_url, item)
            if not host or not uuid:
                continue

            cache_key = f"{host}|{uuid}"
            if cache_key not in event_cache:
                try:
                    event_cache[cache_key] = fetch_event(host, uuid)
                except Exception:
                    event_cache[cache_key] = None

            event = event_cache[cache_key]
            if not event:
                continue

            city_id = map_city(event, lookup)
            if not city_id:
                continue

            key = event_key(event)
            if key in city_events[city_id]:
                continue

            city_events[city_id][key] = {
                "source_method": "ics",
                "discovered_from_feed": feed_url,
                "event": event,
            }
            feed_added += 1
            added += 1

        print(
            f"FEED {i:03d}/{len(feed_list)}: "
            f"items={len(items)} added={added}"
        )

    RAW_ROOT.mkdir(parents=True, exist_ok=True)

    populated = 0
    total_events = 0
    files_written = 0

    for city_id, row in city_rows.items():
        records = list(city_events[city_id].values())
        records.sort(
            key=lambda item: (
                str((item.get("event") or {}).get("beginsOn") or ""),
                str((item.get("event") or {}).get("title") or ""),
            )
        )

        if records:
            populated += 1
        total_events += len(records)

        city_dir = RAW_ROOT / city_id
        city_dir.mkdir(parents=True, exist_ok=True)

        output = {
            "source": "mobilizon",
            "run_started_utc": started_text,
            "city": {
                "city_id": city_id,
                "city": row.get("city"),
                "state": row.get("state"),
                "state_code": row.get("state_code"),
                "country_code": row.get("country_code"),
            },
            "events": records,
        }

        (city_dir / f"{run_date}.json").write_text(
            json.dumps(output, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        files_written += 1

    print("MOBILIZON: PASS")
    print(f"CITIES IN MASTER: {len(city_rows)}")
    print(f"INSTANCES DISCOVERED: {len(instances)}")
    print(f"GRAPHQL EVENTS SEEN: {graphql_seen}")
    print(f"GRAPHQL EVENTS MAPPED: {graphql_mapped}")
    print(f"GROUP ICS FEEDS DISCOVERED: {len(feed_list)}")
    print(f"ICS EVENTS SEEN: {feed_seen}")
    print(f"ICS EVENTS ADDED: {feed_added}")
    print(f"POPULATED CITIES: {populated}")
    print(f"UNIQUE EVENTS: {total_events}")
    print(f"FILES WRITTEN: {files_written}")
    print(f"INSTANCE DIRECTORY FAILURES: {len(directory_failures)}")
    print(f"GRAPHQL FAILURES: {len(graphql_failures)}")
    print(f"ICS FEED FAILURES: {len(feed_failures)}")

    if total_events == 0:
        print("MOBILIZON: FAILED")
        print("ERROR: zero events mapped to city_master.csv")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
