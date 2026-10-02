#!/usr/bin/env python3
# scripts/daily/osm.py

import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
RAW_ROOT = ROOT / "data" / "venues" / "raw" / "osm"

OVERPASS_ENDPOINTS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

REQUEST_TIMEOUT_SECONDS = 75
OVERPASS_QUERY_TIMEOUT_SECONDS = 60
MIN_REQUEST_INTERVAL_SECONDS = 0.5
UA = "events-osm-venue-collector/1.0"

VENUE_AMENITIES = {
    "bar",
    "pub",
    "nightclub",
    "biergarten",
    "music_venue",
}

CSV_FIELDS = (
    "run_timestamp",
    "city_id",
    "city",
    "state_code",
    "osm_type",
    "osm_id",
    "name",
    "amenity",
    "latitude",
    "longitude",
    "website",
    "phone",
    "opening_hours",
    "happy_hours",
    "happy_hours_kitchen",
    "live_music",
    "karaoke",
    "music_genre",
    "microbrewery",
    "brewery",
    "outdoor_seating",
    "cuisine",
    "address_housenumber",
    "address_street",
    "address_city",
    "address_state",
    "address_postcode",
    "source",
    "source_date",
    "check_date",
    "tags_json",
)

_last_request_started = 0.0


def overpass_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def normalized_name(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def name_variants(value):
    normalized = normalized_name(value)
    variants = {normalized} if normalized else set()

    for prefix in (
        "city and county of ",
        "city of ",
        "town of ",
        "village of ",
        "municipality of ",
    ):
        if normalized.startswith(prefix):
            variants.add(normalized[len(prefix):].strip())

    if normalized.endswith(" city"):
        variants.add(normalized[:-5].strip())

    return {variant for variant in variants if variant}


def tags_value(tags, key):
    value = tags.get(key, "") if isinstance(tags, dict) else ""
    return "" if value is None else str(value).strip()


def wait_for_request_slot():
    global _last_request_started

    elapsed = time.monotonic() - _last_request_started
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)


def request_overpass(query):
    global _last_request_started

    errors = []

    for endpoint in OVERPASS_ENDPOINTS:
        wait_for_request_slot()
        _last_request_started = time.monotonic()

        try:
            response = requests.post(
                endpoint,
                data={"data": query},
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict) or not isinstance(
                payload.get("elements"), list
            ):
                raise ValueError("response does not contain an elements list")

            return payload, endpoint
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
            time.sleep(1.0)

    raise RuntimeError(" | ".join(errors))


def venue_query_body(area_set="cityarea"):
    return f"""
(
  nwr(area.{area_set})["amenity"~"^(bar|pub|nightclub|biergarten|music_venue)$"];
  nwr(area.{area_set})["amenity"~"^(restaurant|cafe)$"]["bar"="yes"];
);
out center tags;
""".strip()


def exact_boundary_query(city, state_code):
    state = overpass_string(f"US-{state_code}")
    name = overpass_string(city)

    if state_code == "DC":
        return f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];
area["ISO3166-2"={state}]["boundary"="administrative"]->.cityarea;
.cityarea out ids;
{venue_query_body()}
""".strip()

    return f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];
area["ISO3166-2"={state}]["boundary"="administrative"]->.state;
(
  rel(area.state)["boundary"="administrative"]["name"={name}];
  rel(area.state)["boundary"="administrative"]["official_name"={name}];
  rel(area.state)["boundary"="administrative"]["short_name"={name}];
  rel(area.state)["boundary"="administrative"]["name:en"={name}];
  rel(area.state)["boundary"="administrative"]["alt_name"={name}];
)->.boundary;
.boundary out tags;
.boundary map_to_area ->.cityarea;
.cityarea out ids;
{venue_query_body()}
""".strip()


def containing_areas_query(latitude, longitude):
    return f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];
is_in({latitude},{longitude})->.inside;
area.inside["boundary"="administrative"];
out tags;
""".strip()


def area_venue_query(area_id):
    return f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];
area({int(area_id)})->.cityarea;
.cityarea out ids;
{venue_query_body()}
""".strip()


def is_venue(element):
    tags = element.get("tags") or {}
    amenity = tags_value(tags, "amenity")

    if amenity in VENUE_AMENITIES:
        return True

    return amenity in {"restaurant", "cafe"} and tags_value(tags, "bar") == "yes"


def extract_venues(elements):
    venues = []
    seen = set()

    for element in elements:
        if not is_venue(element):
            continue

        key = (element.get("type"), element.get("id"))
        if key in seen:
            continue

        seen.add(key)
        venues.append(element)

    return venues


def exact_area_ids(elements):
    return sorted(
        {
            int(element["id"])
            for element in elements
            if element.get("type") == "area" and element.get("id") is not None
        }
    )


def candidate_score(tags, city):
    city_norm = normalized_name(city)
    city_variants = name_variants(city)
    names = [
        tags_value(tags, "name"),
        tags_value(tags, "official_name"),
        tags_value(tags, "short_name"),
        tags_value(tags, "name:en"),
        tags_value(tags, "alt_name"),
    ]

    name_score = 0

    for value in names:
        candidate = normalized_name(value)
        if not candidate:
            continue

        if candidate == city_norm:
            name_score = max(name_score, 100)
        elif city_variants & name_variants(value):
            name_score = max(name_score, 80)
        elif city_norm and city_norm in candidate:
            name_score = max(name_score, 75)
        elif candidate and candidate in city_norm:
            name_score = max(name_score, 65)

    if name_score == 0:
        return 0

    try:
        admin_level = int(tags_value(tags, "admin_level"))
    except ValueError:
        admin_level = 0

    admin_bonus = {
        8: 25,
        6: 20,
        5: 20,
        7: 15,
        9: 10,
        10: 5,
    }.get(admin_level, 0)

    border_type = tags_value(tags, "border_type").casefold()
    place = tags_value(tags, "place").casefold()

    type_bonus = 0

    if any(
        token in border_type
        for token in ("city", "town", "village", "municipality", "borough")
    ):
        type_bonus += 20

    if place in {"city", "town", "village", "municipality"}:
        type_bonus += 20

    return name_score + admin_bonus + type_bonus


def choose_containing_area(elements, city):
    scored = []

    for element in elements:
        if element.get("type") != "area" or element.get("id") is None:
            continue

        tags = element.get("tags") or {}
        score = candidate_score(tags, city)

        if score >= 100:
            scored.append((score, int(element["id"]), tags_value(tags, "name")))

    if not scored:
        raise RuntimeError("no matching administrative boundary contains city point")

    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]

    if len(scored) > 1 and scored[1][0] == best[0]:
        raise RuntimeError(
            "ambiguous administrative boundary: "
            + ", ".join(
                f"{name or area_id} score={score}"
                for score, area_id, name in scored[:3]
            )
        )

    return best[1]


def fetch_city_venues(city_row):
    city = city_row["city"].strip()
    state_code = city_row["state_code"].strip()
    latitude = city_row["latitude"].strip()
    longitude = city_row["longitude"].strip()

    payload, endpoint = request_overpass(
        exact_boundary_query(city, state_code)
    )
    elements = payload["elements"]
    areas = exact_area_ids(elements)

    if len(areas) == 1:
        return extract_venues(elements), endpoint, "boundary_exact"

    if not latitude or not longitude:
        detail = "not found" if not areas else f"ambiguous ({len(areas)} matches)"
        raise RuntimeError(
            f"exact administrative boundary {detail}; city coordinates unavailable"
        )

    fallback_payload, fallback_endpoint = request_overpass(
        containing_areas_query(latitude, longitude)
    )
    area_id = choose_containing_area(
        fallback_payload["elements"],
        city,
    )

    venue_payload, venue_endpoint = request_overpass(
        area_venue_query(area_id)
    )
    venue_areas = exact_area_ids(venue_payload["elements"])

    if area_id not in venue_areas:
        raise RuntimeError(f"resolved area {area_id} could not be loaded")

    endpoint_chain = (
        f"{endpoint} -> {fallback_endpoint} -> {venue_endpoint}"
    )

    return (
        extract_venues(venue_payload["elements"]),
        endpoint_chain,
        "boundary_fallback",
    )


def element_coordinates(element):
    if element.get("lat") is not None and element.get("lon") is not None:
        return element["lat"], element["lon"]

    center = element.get("center") or {}
    return center.get("lat", ""), center.get("lon", "")


def venue_row(element, city_row, run_timestamp):
    tags = element.get("tags") or {}
    latitude, longitude = element_coordinates(element)

    website = (
        tags_value(tags, "website")
        or tags_value(tags, "contact:website")
    )
    phone = (
        tags_value(tags, "phone")
        or tags_value(tags, "contact:phone")
    )

    return {
        "run_timestamp": run_timestamp,
        "city_id": city_row["city_id"].strip(),
        "city": city_row["city"].strip(),
        "state_code": city_row["state_code"].strip(),
        "osm_type": element.get("type", ""),
        "osm_id": element.get("id", ""),
        "name": tags_value(tags, "name"),
        "amenity": tags_value(tags, "amenity"),
        "latitude": latitude,
        "longitude": longitude,
        "website": website,
        "phone": phone,
        "opening_hours": tags_value(tags, "opening_hours"),
        "happy_hours": tags_value(tags, "happy_hours"),
        "happy_hours_kitchen": tags_value(
            tags,
            "happy_hours:kitchen",
        ),
        "live_music": tags_value(tags, "live_music"),
        "karaoke": tags_value(tags, "karaoke"),
        "music_genre": tags_value(tags, "music_genre"),
        "microbrewery": tags_value(tags, "microbrewery"),
        "brewery": tags_value(tags, "brewery"),
        "outdoor_seating": tags_value(tags, "outdoor_seating"),
        "cuisine": tags_value(tags, "cuisine"),
        "address_housenumber": tags_value(
            tags,
            "addr:housenumber",
        ),
        "address_street": tags_value(tags, "addr:street"),
        "address_city": tags_value(tags, "addr:city"),
        "address_state": tags_value(tags, "addr:state"),
        "address_postcode": tags_value(tags, "addr:postcode"),
        "source": tags_value(tags, "source"),
        "source_date": tags_value(tags, "source:date"),
        "check_date": tags_value(tags, "check_date"),
        "tags_json": json.dumps(
            tags,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


def write_city_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS,
        )
        writer.writeheader()
        writer.writerows(rows)


def load_cities():
    with CITY_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        cities = list(csv.DictReader(handle))

    required = {
        "city_id",
        "city",
        "state_code",
        "latitude",
        "longitude",
    }

    if not cities:
        raise RuntimeError("city_master.csv is empty")

    missing = sorted(required - set(cities[0]))

    if missing:
        raise RuntimeError(
            f"city_master.csv missing headers: {', '.join(missing)}"
        )

    return cities


def main():
    run_started = datetime.now(timezone.utc)
    run_timestamp = run_started.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    run_date = run_started.strftime("%Y%m%d")

    try:
        cities = load_cities()
    except Exception as exc:
        print("OSM: FAILED")
        print(f"ERROR: {exc}")
        return 2

    successful = 0
    failed = 0
    total_venues = 0
    total_files = 0
    missing_coordinates = 0
    fallback_boundaries = 0
    failures = []

    for index, city_row in enumerate(
        cities,
        start=1,
    ):
        city_id = (
            city_row.get("city_id") or ""
        ).strip()
        city = (
            city_row.get("city") or ""
        ).strip()
        state_code = (
            city_row.get("state_code") or ""
        ).strip()

        label = f"{city}, {state_code} ({city_id})"

        print(
            f"CITY {index}/{len(cities)}: {label} ... ",
            end="",
            flush=True,
        )

        if not city_id or not city or not state_code:
            failed += 1
            message = (
                "missing city_id, city, or state_code"
            )
            failures.append(
                f"{label}: {message}"
            )
            print(
                f"FAIL | {message}",
                flush=True,
            )
            continue

        try:
            venues, endpoint, boundary_mode = (
                fetch_city_venues(city_row)
            )

            rows = [
                venue_row(
                    element,
                    city_row,
                    run_timestamp,
                )
                for element in venues
            ]

            city_file = (
                RAW_ROOT
                / city_id
                / run_date
                / "page_000.csv"
            )

            write_city_csv(
                city_file,
                rows,
            )

            successful += 1
            total_files += 1
            total_venues += len(rows)

            missing_coordinates += sum(
                1
                for row in rows
                if row["latitude"] == ""
                or row["longitude"] == ""
            )

            if boundary_mode == "boundary_fallback":
                fallback_boundaries += 1

            print(
                f"PASS | VENUES={len(rows)} | "
                f"MODE={boundary_mode} | "
                f"ENDPOINT={endpoint}",
                flush=True,
            )

        except Exception as exc:
            failed += 1
            message = (
                f"{type(exc).__name__}: {exc}"
            )
            failures.append(
                f"{label}: {message}"
            )
            print(
                f"FAIL | {message}",
                flush=True,
            )

    status = (
        "PASS"
        if failed == 0
        else "PARTIAL"
    )

    print("")
    print(f"OSM: {status}")
    print(f"CITIES: {len(cities)}")
    print(f"SUCCESS: {successful}")
    print(f"FAILED: {failed}")
    print(
        f"BOUNDARY FALLBACKS: "
        f"{fallback_boundaries}"
    )
    print(f"VENUES: {total_venues}")
    print(f"RAW FILES: {total_files}")
    print(
        f"MISSING LAT/LON: "
        f"{missing_coordinates}"
    )
    print(
        "OUTPUT: "
        "data/venues/raw/osm/"
        "<city_id>/<YYYYMMDD>/page_000.csv"
    )

    if failures:
        print("FAILURES:")

        for failure in failures:
            print(f"- {failure}")

    return 0 if successful > 0 else 1


if __name__ == "__main__":
    sys.exit(main())