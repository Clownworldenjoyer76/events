#!/usr/bin/env python3
import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import extruct
import requests


ROOT = Path(__file__).resolve().parents[2]

CITY_FILE = ROOT / "data/location/usa/city_master.csv"
FOURSQUARE_ROOT = ROOT / "data/venues/raw/foursquare"
RAW_ROOT = ROOT / "data/events/raw/schema_org"

TIMEOUT = 20
SITEMAP_MAX = 20
SITEMAP_URL_MAX = 10000
EVENT_URL_MAX = 250
SITEMAP_WORKERS = 8
PAGE_WORKERS = 12
FALLBACK_MILES = 40.0

UA = "events-schema-org-collector/1.0"

EVENT_TYPES = {
    "Event",
    "BusinessEvent",
    "ChildrensEvent",
    "ComedyEvent",
    "ConferenceEvent",
    "CourseInstance",
    "DanceEvent",
    "DeliveryEvent",
    "EducationEvent",
    "ExhibitionEvent",
    "Festival",
    "FoodEvent",
    "Hackathon",
    "LiteraryEvent",
    "MusicEvent",
    "PerformingArtsEvent",
    "PublicationEvent",
    "SaleEvent",
    "ScreeningEvent",
    "SocialEvent",
    "SportsEvent",
    "TheaterEvent",
    "VisualArtsEvent",
    "EventSeries",
}

PATH_HINTS = (
    "event",
    "events",
    "calendar",
    "concert",
    "concerts",
    "show",
    "shows",
    "performance",
    "performances",
    "festival",
    "festivals",
    "comedy",
    "schedule",
    "program",
    "programs",
    "tickets",
)

SITEMAP_HINTS = (
    "event",
    "events",
    "calendar",
    "tribe",
    "schedule",
    "program",
)

RADIO_MARKERS = (
    "radio show",
    "radio program",
    "radio hour",
    "on-air",
    "on air",
    "broadcast live",
    "live broadcast",
)

EVENT_SEED_CATEGORY_NAMES = {
    "amphitheater",
    "aquarium",
    "arena",
    "art gallery",
    "art museum",
    "auditorium",
    "attraction",
    "baseball stadium",
    "basketball stadium",
    "bingo center",
    "bowling alley",
    "casino",
    "circus",
    "civic center",
    "college & university",
    "college arts building",
    "college theater",
    "colleges and universities",
    "comedy club",
    "community center",
    "concert hall",
    "conference center",
    "conference room",
    "convention center",
    "cultural center",
    "country dance club",
    "dance hall",
    "dance studio",
    "exhibition center",
    "fairground",
    "festival",
    "football stadium",
    "historic site",
    "history museum",
    "hockey arena",
    "hockey stadium",
    "indie movie theater",
    "indie theater",
    "jazz club",
    "library",
    "laser tag",
    "mini golf",
    "movie theater",
    "museum",
    "music venue",
    "night club",
    "opera house",
    "performing arts venue",
    "planetarium",
    "party center",
    "pool hall",
    "racetrack",
    "racecourse",
    "rock club",
    "roller rink",
    "science museum",
    "soccer stadium",
    "stadium",
    "tennis stadium",
    "theater",
    "theatre",
    "track stadium",
    "university",
    "visual arts",
    "water park",
    "zoo",
}

EVENT_SEED_CATEGORY_IDS = {
    "4bf58dd8d48988d1e5931735",
    "4bf58dd8d48988d18e941735",
    "4bf58dd8d48988d1e4931735",
    "4bf58dd8d48988d17c941735",
    "4bf58dd8d48988d181941735",
    "4bf58dd8d48988d18f941735",
    "4bf58dd8d48988d190941735",
    "4bf58dd8d48988d191941735",
    "4bf58dd8d48988d192941735",
    "4bf58dd8d48988d1f2931735",
    "4bf58dd8d48988d137941735",
    "4bf58dd8d48988d136941735",
    "4bf58dd8d48988d135941735",
    "4bf58dd8d48988d1ac941735",
    "4bf58dd8d48988d189941735",
    "4bf58dd8d48988d184941735",
    "4bf58dd8d48988d185941735",
    "4bf58dd8d48988d1b7941735",
    "4bf58dd8d48988d1b6941735",
    "4bf58dd8d48988d1a8941735",
    "4d4b7104d754a06370d81259",
}

GENERIC_CATEGORY_NAMES = {
    "arts and entertainment",
    "arts & entertainment",
    "general entertainment",
}


def txt(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip())

    if isinstance(value, bool):
        return str(value).lower()

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, dict):
        for key in ("name", "value", "text", "@id", "url"):
            if key in value:
                result = txt(value[key])
                if result:
                    return result

    if isinstance(value, list):
        return " | ".join(
            dict.fromkeys(
                result
                for result in (txt(item) for item in value)
                if result
            )
        )

    return re.sub(r"\s+", " ", str(value).strip())


def host(value):
    if "://" not in str(value):
        value = "https://" + str(value)

    try:
        hostname = urlparse(value).hostname or ""
    except ValueError:
        return ""

    return hostname[4:] if hostname.startswith("www.") else hostname


def origin(value):
    if "://" not in str(value):
        value = "https://" + str(value)

    try:
        parsed = urlparse(value)
    except ValueError:
        return ""

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""

    return f"{parsed.scheme}://{parsed.netloc}"


def same_host(first, second):
    return host(first) == host(second)


def req(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xml,*/*;q=0.8",
        },
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def cities():
    with CITY_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        rows = list(csv.DictReader(handle))

    lookup = {}
    points = []

    for row in rows:
        city_id = txt(row.get("city_id"))
        city = txt(row.get("city")).casefold()
        state_code = txt(row.get("state_code")).casefold()
        state = txt(row.get("state")).casefold()
        country = txt(row.get("country_code")).casefold()

        if country in (
            "usa",
            "united states",
            "united states of america",
        ):
            country = "us"

        for state_value in {state_code, state} - {""}:
            lookup[(city, state_value, country)] = city_id
            lookup[(city, state_value, "")] = city_id

        try:
            points.append(
                (
                    city_id,
                    float(row["latitude"]),
                    float(row["longitude"]),
                )
            )
        except (ValueError, TypeError, KeyError):
            pass

    return lookup, points


def miles(lat1, lon1, lat2, lon2):
    radius = 3958.7613

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2) ** 2
    )

    return radius * 2 * math.atan2(
        math.sqrt(max(0, value)),
        math.sqrt(max(0, 1 - value)),
    )


def category_values(place):
    categories = place.get("categories") or []

    names = set()
    ids = set()

    for category in categories:
        if not isinstance(category, dict):
            continue

        category_id = txt(category.get("fsq_category_id"))
        category_name = txt(category.get("name")).casefold()

        if category_id:
            ids.add(category_id)

        if category_name:
            names.add(category_name)

    return names, ids


def event_seed_categories(place):
    names, ids = category_values(place)

    matched = set()

    for category_id in ids:
        if category_id in EVENT_SEED_CATEGORY_IDS:
            matched.add(category_id)

    for category_name in names:
        if category_name in EVENT_SEED_CATEGORY_NAMES:
            matched.add(category_name)

    return sorted(matched)


def fsq_seeds():
    groups = {}

    if not FOURSQUARE_ROOT.exists():
        return []

    for citydir in sorted(
        path
        for path in FOURSQUARE_ROOT.iterdir()
        if path.is_dir()
    ):
        files = sorted(
            citydir.glob("*.json"),
            key=lambda path: path.name,
            reverse=True,
        )

        if not files:
            continue

        try:
            payload = json.loads(
                files[0].read_text(encoding="utf-8")
            )
        except Exception:
            continue

        city = payload.get("city") or {}
        city_id = txt(city.get("city_id"))

        for search in payload.get("searches") or []:
            response = search.get("response") or {}

            for place in response.get("results") or []:
                if not isinstance(place, dict):
                    continue

                website = txt(place.get("website"))
                site_origin = origin(website)

                if not site_origin:
                    continue

                matched_categories = event_seed_categories(place)

                if not matched_categories:
                    continue

                names, _ = category_values(place)

                if names and names.issubset(GENERIC_CATEGORY_NAMES):
                    continue

                site_host = host(site_origin)

                group = groups.setdefault(
                    site_host,
                    {
                        "host": site_host,
                        "origin": site_origin,
                        "city_ids": set(),
                        "categories": set(),
                    },
                )

                if city_id:
                    group["city_ids"].add(city_id)

                group["categories"].update(matched_categories)

    return list(groups.values())


def sitemap_roots(site_origin):
    output = []

    try:
        response = req(
            site_origin.rstrip("/") + "/robots.txt"
        )

        for line in response.text.splitlines():
            if ":" not in line:
                continue

            key, value = line.split(":", 1)

            if key.strip().casefold() != "sitemap":
                continue

            sitemap_url = value.strip()

            if sitemap_url and sitemap_url not in output:
                output.append(sitemap_url)

    except Exception:
        pass

    for path in (
        "/wp-sitemap.xml",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
    ):
        sitemap_url = site_origin.rstrip("/") + path

        if sitemap_url not in output:
            output.append(sitemap_url)

    return output


def parse_map(data):
    root = ET.fromstring(data)

    kind = root.tag.rsplit("}", 1)[-1].casefold()

    output = []

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() != "loc":
            continue

        if not element.text:
            continue

        output.append(
            (
                "sitemap"
                if kind == "sitemapindex"
                else "url",
                element.text.strip(),
            )
        )

    return output


def sitemap_urls(site):
    queue = sitemap_roots(site["origin"])
    seen = set()
    urls = set()

    sitemap_files = 0
    seen_urls = 0

    while queue and sitemap_files < SITEMAP_MAX:
        sitemap_url = queue.pop(0)

        if sitemap_url in seen:
            continue

        if not same_host(
            sitemap_url,
            site["host"],
        ):
            continue

        seen.add(sitemap_url)

        try:
            entries = parse_map(
                req(sitemap_url).content
            )
        except Exception:
            continue

        sitemap_files += 1

        sitemap_is_event_oriented = any(
            hint in sitemap_url.casefold()
            for hint in SITEMAP_HINTS
        )

        for kind, url in entries:
            if kind == "sitemap":
                if url not in seen:
                    queue.append(url)

                continue

            if seen_urls >= SITEMAP_URL_MAX:
                break

            seen_urls += 1

            if not same_host(url, site["host"]):
                continue

            parsed = urlparse(url)

            path = (
                parsed.path
                + "?"
                + parsed.query
            ).casefold()

            score = sum(
                2
                for hint in PATH_HINTS
                if hint in path
            )

            if sitemap_is_event_oriented:
                score += 5

            if re.search(
                r"/20\d{2}(?:/|-)\d{1,2}(?:/|-)\d{1,2}",
                path,
            ):
                score += 2

            if score:
                urls.add((score, url))

    ordered = [
        url
        for _, url in sorted(
            urls,
            key=lambda item: (
                -item[0],
                item[1],
            ),
        )
    ]

    return (
        ordered[:EVENT_URL_MAX],
        sitemap_files,
        seen_urls,
    )


def types(value):
    if isinstance(value, str):
        return [value.rsplit("/", 1)[-1]]

    if isinstance(value, list):
        output = []

        for item in value:
            output.extend(types(item))

        return output

    return []


def walk(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk(child)

    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def is_event(value):
    if not isinstance(value, dict):
        return False

    event_types = types(value.get("@type"))

    return any(
        event_type in EVENT_TYPES
        or event_type.endswith("Event")
        for event_type in event_types
    )


def loc_info(event):
    values = event.get("location")

    if not isinstance(values, list):
        values = [values]

    names = []
    cities_ = []
    regions = []
    countries = []
    streets = []
    postals = []
    coordinates = []
    location_types = []
    virtual_urls = []

    for location in values:
        if not isinstance(location, dict):
            continue

        location_type_values = types(
            location.get("@type")
        )

        location_types.extend(
            location_type_values
        )

        name = txt(location.get("name"))
        location_url = txt(location.get("url"))

        if name:
            names.append(name)

        if (
            "VirtualLocation"
            in location_type_values
            and location_url
        ):
            virtual_urls.append(location_url)

        address = location.get("address")

        if isinstance(address, dict):
            fields = (
                ("streetAddress", streets),
                ("addressLocality", cities_),
                ("addressRegion", regions),
                ("postalCode", postals),
                ("addressCountry", countries),
            )

            for key, target in fields:
                value = txt(address.get(key))

                if value:
                    target.append(value)

        geo = location.get("geo")

        if isinstance(geo, dict):
            latitude = txt(geo.get("latitude"))
            longitude = txt(geo.get("longitude"))

            if latitude or longitude:
                coordinates.append(
                    f"{latitude},{longitude}"
                )

    info = {
        "name": "|".join(dict.fromkeys(names)),
        "city": "|".join(dict.fromkeys(cities_)),
        "region": "|".join(dict.fromkeys(regions)),
        "country": "|".join(dict.fromkeys(countries)),
        "street": "|".join(dict.fromkeys(streets)),
        "postal": "|".join(dict.fromkeys(postals)),
        "coords": "|".join(
            dict.fromkeys(coordinates)
        ),
        "types": "|".join(
            dict.fromkeys(location_types)
        ),
        "virtual_url": "|".join(
            dict.fromkeys(virtual_urls)
        ),
    }

    info["physical"] = bool(
        streets
        or cities_
        or regions
        or postals
        or coordinates
    ) or any(
        location_type in {
            "Place",
            "MusicVenue",
            "StadiumOrArena",
            "CivicStructure",
        }
        for location_type in location_types
    )

    info["virtual"] = bool(
        virtual_urls
    ) or "VirtualLocation" in location_types

    return info


def coord(info):
    if not info["coords"]:
        return None

    try:
        latitude, longitude = (
            info["coords"]
            .split("|", 1)[0]
            .split(",", 1)
        )

        latitude = float(latitude)
        longitude = float(longitude)

    except (ValueError, TypeError):
        return None

    if (
        -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        return latitude, longitude

    return None


def map_city(
    info,
    seed_ids,
    lookup,
    points,
):
    city = info["city"].split("|", 1)[0].casefold()
    state = info["region"].split("|", 1)[0].casefold()
    country = info["country"].split("|", 1)[0].casefold()

    if country in (
        "usa",
        "united states",
        "united states of america",
    ):
        country = "us"

    if city and state:
        city_id = (
            lookup.get(
                (city, state, country)
            )
            or lookup.get(
                (city, state, "")
            )
        )

        if city_id:
            return city_id, "mapped_exact", 0

    coordinates = coord(info)

    if coordinates:
        nearest = min(
            (
                (
                    miles(
                        coordinates[0],
                        coordinates[1],
                        point[1],
                        point[2],
                    ),
                    point[0],
                )
                for point in points
            ),
            default=None,
        )

        if (
            nearest
            and nearest[0] <= FALLBACK_MILES
        ):
            return (
                nearest[1],
                "mapped_radius",
                nearest[0],
            )

    seed_values = {
        value
        for value in seed_ids
        if value
    }

    if (
        info["physical"]
        and len(seed_values) == 1
        and not city
        and not coordinates
    ):
        return (
            next(iter(seed_values)),
            "mapped_seed_site",
            None,
        )

    return None, "unmapped", None


def event_key(event, url):
    event_url = txt(event.get("url"))

    if event_url:
        return (
            "url",
            event_url.casefold(),
        )

    return (
        "composite",
        txt(event.get("name")).casefold(),
        txt(event.get("startDate")).casefold(),
        url.casefold(),
    )


def virtual_only(event, location):
    attendance_mode = txt(
        event.get("eventAttendanceMode")
    ).casefold()

    return (
        not location["physical"]
        and (
            "onlineeventattendancemode"
            in attendance_mode
            or location["virtual"]
        )
    )


def radio(event, location):
    if location["physical"]:
        return False

    text = " ".join(
        txt(event.get(field))
        for field in (
            "name",
            "description",
            "keywords",
        )
    ).casefold()

    return any(
        marker in text
        for marker in RADIO_MARKERS
    )


def json_cell(value):
    if value in (None, "", [], {}):
        return ""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def fetch_page(item):
    url, robot = item

    if robot:
        try:
            if not robot.can_fetch(UA, url):
                return url, None, "robots"
        except Exception:
            pass

    try:
        response = req(url)

        content_type = (
            response.headers
            .get("Content-Type", "")
            .casefold()
        )

        if (
            content_type
            and "html" not in content_type
            and "xhtml" not in content_type
        ):
            return url, None, "non_html"

        return url, response.text, ""

    except Exception as exc:
        return (
            url,
            None,
            f"{type(exc).__name__}: {exc}",
        )


def process(
    site,
    lookup,
    points,
    run_text,
):
    urls, sitemap_files, sitemap_seen = (
        sitemap_urls(site)
    )

    robot = None

    try:
        response = req(
            site["origin"].rstrip("/")
            + "/robots.txt"
        )

        robot = robotparser.RobotFileParser()
        robot.parse(
            response.text.splitlines()
        )

    except Exception:
        pass

    rows = {}
    seen = set()

    stats = {
        "sitemap_files": sitemap_files,
        "sitemap_urls_seen": sitemap_seen,
        "candidate_urls": len(urls),
        "pages_fetched": 0,
        "pages_with_events": 0,
        "event_objects": 0,
        "events_written": 0,
        "virtual_filtered": 0,
        "radio_filtered": 0,
        "unmapped": 0,
        "robots_blocked": 0,
        "page_errors": 0,
    }

    with ThreadPoolExecutor(
        max_workers=PAGE_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                fetch_page,
                (url, robot),
            )
            for url in urls
        ]

        for future in as_completed(futures):
            url, html, error = future.result()

            if error == "robots":
                stats["robots_blocked"] += 1
                continue

            if html is None:
                stats["page_errors"] += 1
                continue

            stats["pages_fetched"] += 1

            try:
                extracted = extruct.extract(
                    html,
                    base_url=url,
                    syntaxes=["json-ld"],
                    uniform=True,
                )

                events = []
                page_seen = set()

                for block in (
                    extracted.get("json-ld", [])
                ):
                    for event in walk(block):
                        if not is_event(event):
                            continue

                        key = event_key(
                            event,
                            url,
                        )

                        if key in page_seen:
                            continue

                        page_seen.add(key)
                        events.append(event)

            except Exception:
                stats["page_errors"] += 1
                continue

            if not events:
                continue

            stats["pages_with_events"] += 1
            stats["event_objects"] += len(events)

            for event in events:
                location = loc_info(event)

                if virtual_only(
                    event,
                    location,
                ):
                    stats["virtual_filtered"] += 1
                    continue

                if radio(
                    event,
                    location,
                ):
                    stats["radio_filtered"] += 1
                    continue

                if not txt(event.get("name")):
                    continue

                if not txt(event.get("startDate")):
                    continue

                key = event_key(
                    event,
                    url,
                )

                if key in seen:
                    continue

                seen.add(key)

                city_id, mapping_method, distance = (
                    map_city(
                        location,
                        site["city_ids"],
                        lookup,
                        points,
                    )
                )

                if not city_id:
                    stats["unmapped"] += 1
                    continue

                row = {
                    "schema_org_run_started_utc": run_text,
                    "schema_org_seed_host": site["host"],
                    "schema_org_seed_city_id": "|".join(
                        sorted(site["city_ids"])
                    ),
                    "schema_org_seed_categories": "|".join(
                        sorted(site["categories"])
                    ),
                    "schema_org_source_url": url,
                    "schema_org_event_type": "|".join(
                        types(event.get("@type"))
                    ),
                    "schema_org_city_mapping": mapping_method,
                    "schema_org_city_mapping_distance_miles": (
                        ""
                        if distance is None
                        else f"{distance:.3f}"
                    ),
                    "event_name": txt(
                        event.get("name")
                    ),
                    "event_start_date": txt(
                        event.get("startDate")
                    ),
                    "event_end_date": txt(
                        event.get("endDate")
                    ),
                    "event_url": (
                        txt(event.get("url"))
                        or url
                    ),
                    "event_status": txt(
                        event.get("eventStatus")
                    ),
                    "event_attendance_mode": txt(
                        event.get(
                            "eventAttendanceMode"
                        )
                    ),
                    "event_description": txt(
                        event.get("description")
                    ),
                    "event_location_json": json_cell(
                        event.get("location")
                    ),
                    "event_offers_json": json_cell(
                        event.get("offers")
                    ),
                    "event_organizer_json": json_cell(
                        event.get("organizer")
                    ),
                    "event_performer_json": json_cell(
                        event.get("performer")
                    ),
                    "event_image_json": json_cell(
                        event.get("image")
                    ),
                    "event_keywords": txt(
                        event.get("keywords")
                    ),
                    "event_json": json_cell(event),
                }

                rows.setdefault(
                    city_id,
                    [],
                ).append(row)

                stats["events_written"] += 1

    return rows, stats


def write_files(
    run_date,
    rows_by_city,
):
    fields = [
        "schema_org_run_started_utc",
        "schema_org_seed_host",
        "schema_org_seed_city_id",
        "schema_org_seed_categories",
        "schema_org_source_url",
        "schema_org_event_type",
        "schema_org_city_mapping",
        "schema_org_city_mapping_distance_miles",
        "event_name",
        "event_start_date",
        "event_end_date",
        "event_url",
        "event_status",
        "event_attendance_mode",
        "event_description",
        "event_location_json",
        "event_offers_json",
        "event_organizer_json",
        "event_performer_json",
        "event_image_json",
        "event_keywords",
        "event_json",
    ]

    files = 0
    rows = 0

    for city_id, city_rows in sorted(
        rows_by_city.items()
    ):
        city_rows.sort(
            key=lambda item: (
                item["event_start_date"],
                item["event_name"],
                item["event_url"],
            )
        )

        for offset in range(
            0,
            len(city_rows),
            1000,
        ):
            chunk = city_rows[
                offset:offset + 1000
            ]

            path = (
                RAW_ROOT
                / city_id
                / run_date
                / f"page_{offset // 1000:03d}.csv"
            )

            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with path.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                )

                writer.writeheader()
                writer.writerows(chunk)

            files += 1
            rows += len(chunk)

    return files, rows


def clear_run(run_date):
    if not RAW_ROOT.exists():
        return

    for city_directory in RAW_ROOT.iterdir():
        if not city_directory.is_dir():
            continue

        run_directory = (
            city_directory / run_date
        )

        if not run_directory.exists():
            continue

        for path in run_directory.glob(
            "page_*.csv"
        ):
            path.unlink()

        try:
            run_directory.rmdir()
        except OSError:
            pass


def main():
    started = datetime.now(timezone.utc)

    run_date = started.strftime(
        "%Y%m%d"
    )

    run_text = started.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        lookup, points = cities()
        sites = fsq_seeds()

    except Exception as exc:
        print("SCHEMA.ORG: FAILED")
        print(
            f"ERROR: {type(exc).__name__}: {exc}"
        )
        return 2

    if not sites:
        print("SCHEMA.ORG: FAILED")
        print(
            "ERROR: no category-qualified "
            "Foursquare website seeds"
        )
        return 2

    clear_run(run_date)

    print(
        f"WEBSITE SEEDS: {len(sites)}",
        flush=True,
    )

    totals = {
        key: 0
        for key in (
            "sitemap_files",
            "sitemap_urls_seen",
            "candidate_urls",
            "pages_fetched",
            "pages_with_events",
            "event_objects",
            "events_written",
            "virtual_filtered",
            "radio_filtered",
            "unmapped",
            "robots_blocked",
            "page_errors",
        )
    }

    all_rows = {}

    with ThreadPoolExecutor(
        max_workers=SITEMAP_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                process,
                site,
                lookup,
                points,
                run_text,
            ): site["host"]
            for site in sites
        }

        completed = 0

        for future in as_completed(futures):
            completed += 1
            site_host = futures[future]

            try:
                rows, stats = future.result()

            except Exception as exc:
                print(
                    "SITE ERROR "
                    f"{site_host}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue

            for key in totals:
                totals[key] += stats[key]

            for city_id, rows_for_city in rows.items():
                all_rows.setdefault(
                    city_id,
                    [],
                ).extend(rows_for_city)

            print(
                f"SITES {completed}/{len(sites)} "
                f"{site_host}: "
                f"candidates={stats['candidate_urls']} "
                f"events={stats['event_objects']} "
                f"written={stats['events_written']}",
                flush=True,
            )

    files, rows = write_files(
        run_date,
        all_rows,
    )

    print("")
    print("SCHEMA.ORG: PASS")
    print(f"RUN DATE: {run_date}")
    print(
        f"WEBSITE SEEDS: {len(sites)}"
    )
    print(
        f"SITEMAP FILES: "
        f"{totals['sitemap_files']}"
    )
    print(
        f"SITEMAP URLS SEEN: "
        f"{totals['sitemap_urls_seen']}"
    )
    print(
        f"EVENT URL CANDIDATES: "
        f"{totals['candidate_urls']}"
    )
    print(
        f"PAGES FETCHED: "
        f"{totals['pages_fetched']}"
    )
    print(
        f"PAGES WITH EVENTS: "
        f"{totals['pages_with_events']}"
    )
    print(
        f"EVENT OBJECTS: "
        f"{totals['event_objects']}"
    )
    print(
        f"VIRTUAL-ONLY FILTERED: "
        f"{totals['virtual_filtered']}"
    )
    print(
        f"RADIO FILTERED: "
        f"{totals['radio_filtered']}"
    )
    print(
        f"UNMAPPED EVENTS: "
        f"{totals['unmapped']}"
    )
    print(
        f"ROBOTS BLOCKED: "
        f"{totals['robots_blocked']}"
    )
    print(
        f"PAGE ERRORS: "
        f"{totals['page_errors']}"
    )
    print(
        f"EVENTS WRITTEN: {rows}"
    )
    print(
        f"CITIES WRITTEN: "
        f"{len(all_rows)}"
    )
    print(
        f"CSV FILES: {files}"
    )
    print(
        "OUTPUT: "
        "data/events/raw/schema_org/"
        "<city_id>/<YYYYMMDD>/page_###.csv"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())