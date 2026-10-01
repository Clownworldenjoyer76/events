#!/usr/bin/env python3
import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import extruct
import requests

ROOT = Path(__file__).resolve().parents[2]
CITY_FILE = ROOT / "data/location/usa/city_master.csv"
FOURSQUARE_ROOT = ROOT / "data/venues/raw/foursquare"
RAW_ROOT = ROOT / "data/events/raw/schema_org"

TIMEOUT = 10
MAX_SITEMAPS = 12
MAX_SITEMAP_URLS = 5000
MAX_CANDIDATES = 50
MAX_HOME_EVENT_LINKS = 10
MAX_EVENT_PAGE_LINKS = 30
SITE_WORKERS = 16
PAGE_WORKERS = 8
FALLBACK_MILES = 40.0
UA = "events-schema-org-collector/3.0"

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

EVENT_HINTS = (
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
    "ticket",
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
                x for x in (txt(v) for v in value) if x
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

        states = {
            txt(row.get("state_code")).casefold(),
            txt(row.get("state")).casefold(),
        } - {""}

        country = txt(row.get("country_code")).casefold()

        if country in {
            "usa",
            "united states",
            "united states of america",
        }:
            country = "us"

        for state in states:
            lookup[(city, state, country)] = city_id
            lookup[(city, state, "")] = city_id

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
    names = set()
    ids = set()

    for category in place.get("categories") or []:
        if not isinstance(category, dict):
            continue

        category_id = txt(
            category.get("fsq_category_id")
        )
        category_name = txt(
            category.get("name")
        ).casefold()

        if category_id:
            ids.add(category_id)

        if category_name:
            names.add(category_name)

    return names, ids


def event_seed_categories(place):
    names, ids = category_values(place)

    return sorted(
        {
            value
            for value in ids
            if value in EVENT_SEED_CATEGORY_IDS
        }
        |
        {
            value
            for value in names
            if value in EVENT_SEED_CATEGORY_NAMES
        }
    )


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
                files[0].read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            continue

        city_id = txt(
            (payload.get("city") or {}).get(
                "city_id"
            )
        )

        for search in payload.get("searches") or []:
            results = (
                search.get("response") or {}
            ).get("results") or []

            for place in results:
                if not isinstance(place, dict):
                    continue

                site = origin(
                    txt(place.get("website"))
                )

                if not site:
                    continue

                matched = event_seed_categories(
                    place
                )

                if not matched:
                    continue

                names, _ = category_values(place)

                if (
                    names
                    and names.issubset(
                        GENERIC_CATEGORY_NAMES
                    )
                ):
                    continue

                site_host = host(site)

                group = groups.setdefault(
                    site_host,
                    {
                        "host": site_host,
                        "origin": site,
                        "city_ids": set(),
                        "categories": set(),
                    },
                )

                if city_id:
                    group["city_ids"].add(
                        city_id
                    )

                group["categories"].update(
                    matched
                )

    return list(groups.values())


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "a":
            return

        href = dict(attrs).get("href")

        if href:
            self.links.append(href)


def event_links(html, base, limit):
    parser = LinkParser()

    try:
        parser.feed(html)
    except Exception:
        return []

    found = {}

    for href in parser.links:
        url = urljoin(base, href)

        if not same_host(url, base):
            continue

        parsed = urlparse(url)

        if parsed.scheme not in (
            "http",
            "https",
        ):
            continue

        clean = parsed._replace(
            fragment=""
        ).geturl()

        path = (
            parsed.path
            + "?"
            + parsed.query
        ).casefold()

        score = sum(
            1
            for hint in EVENT_HINTS
            if hint in path
        )

        if score:
            found[clean] = max(
                found.get(clean, 0),
                score,
            )

    ordered = sorted(
        found.items(),
        key=lambda item: (
            -item[1],
            item[0],
        ),
    )

    return [
        url
        for url, _ in ordered[:limit]
    ]


def parse_map(data):
    root = ET.fromstring(data)

    kind = root.tag.rsplit(
        "}",
        1,
    )[-1].casefold()

    return [
        (
            "sitemap"
            if kind == "sitemapindex"
            else "url",
            element.text.strip(),
        )
        for element in root.iter()
        if (
            element.tag.rsplit(
                "}",
                1,
            )[-1].casefold()
            == "loc"
            and element.text
        )
    ]


def sitemap_roots(
    site,
    robots_text,
):
    urls = []

    for line in robots_text.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(
            ":",
            1,
        )

        if (
            key.strip().casefold()
            == "sitemap"
            and value.strip()
        ):
            urls.append(
                value.strip()
            )

    for path in (
        "/wp-sitemap.xml",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
    ):
        urls.append(
            site.rstrip("/")
            + path
        )

    return list(
        dict.fromkeys(urls)
    )


def discover(site):
    robot = None
    robots_text = ""
    sitemap_count = 0
    sitemap_urls_seen = 0
    candidates = set()

    try:
        response = req(
            site["origin"].rstrip("/")
            + "/robots.txt"
        )

        robots_text = response.text

        robot = robotparser.RobotFileParser()
        robot.parse(
            robots_text.splitlines()
        )
    except Exception:
        pass

    queue = sitemap_roots(
        site["origin"],
        robots_text,
    )

    seen = set()

    while (
        queue
        and sitemap_count < MAX_SITEMAPS
    ):
        sitemap = queue.pop(0)

        if (
            sitemap in seen
            or not same_host(
                sitemap,
                site["host"],
            )
        ):
            continue

        seen.add(sitemap)

        try:
            entries = parse_map(
                req(sitemap).content
            )
        except Exception:
            continue

        sitemap_count += 1

        sitemap_hint = any(
            hint in sitemap.casefold()
            for hint in SITEMAP_HINTS
        )

        for kind, url in entries:
            if kind == "sitemap":
                if any(
                    hint in url.casefold()
                    for hint in SITEMAP_HINTS
                ):
                    queue.append(url)

                continue

            sitemap_urls_seen += 1

            if (
                sitemap_urls_seen
                > MAX_SITEMAP_URLS
            ):
                break

            if not same_host(
                url,
                site["host"],
            ):
                continue

            parsed = urlparse(url)

            path = (
                parsed.path
                + "?"
                + parsed.query
            ).casefold()

            if (
                sitemap_hint
                or any(
                    hint in path
                    for hint in EVENT_HINTS
                )
            ):
                candidates.add(url)

        if (
            sitemap_urls_seen
            > MAX_SITEMAP_URLS
        ):
            break

    if not candidates:
        try:
            response = req(
                site["origin"]
            )

            candidates.update(
                event_links(
                    response.text,
                    response.url,
                    MAX_HOME_EVENT_LINKS,
                )
            )

            homepage = response.text

        except Exception:
            homepage = ""

        if homepage:
            landing = list(candidates)

            for page in landing:
                if (
                    len(candidates)
                    >= MAX_CANDIDATES
                ):
                    break

                try:
                    response = req(page)
                except Exception:
                    continue

                candidates.update(
                    event_links(
                        response.text,
                        response.url,
                        MAX_EVENT_PAGE_LINKS,
                    )
                )

    return (
        sorted(candidates)[
            :MAX_CANDIDATES
        ],
        robot,
        sitemap_count,
        sitemap_urls_seen,
    )


def types(value):
    if isinstance(value, str):
        return [
            value.rsplit(
                "/",
                1,
            )[-1]
        ]

    if isinstance(value, list):
        output = []

        for item in value:
            output.extend(
                types(item)
            )

        return output

    return []


def walk(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            if isinstance(
                child,
                (dict, list),
            ):
                yield from walk(child)

    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def is_event(value):
    if not isinstance(value, dict):
        return False

    return any(
        event_type in EVENT_TYPES
        or event_type.endswith(
            "Event"
        )
        for event_type in types(
            value.get("@type")
        )
    )


def event_key(event, url):
    event_url = txt(
        event.get("url")
    )

    if event_url:
        return (
            "url",
            event_url.casefold(),
        )

    return (
        "composite",
        txt(
            event.get("name")
        ).casefold(),
        txt(
            event.get("startDate")
        ).casefold(),
        url.casefold(),
    )


def extract_events(
    html,
    url,
):
    try:
        extracted = extruct.extract(
            html,
            base_url=url,
            syntaxes=["json-ld"],
            uniform=True,
        )
    except Exception:
        return []

    output = []
    seen = set()

    for block in extracted.get(
        "json-ld",
        [],
    ):
        for event in walk(block):
            if not is_event(event):
                continue

            key = event_key(
                event,
                url,
            )

            if key in seen:
                continue

            seen.add(key)
            output.append(event)

    return output


def location_info(event):
    values = event.get(
        "location"
    )

    if not isinstance(
        values,
        list,
    ):
        values = [values]

    names = []
    cities = []
    regions = []
    countries = []
    streets = []
    postals = []
    coords = []
    types_ = []
    virtual = []

    for location in values:
        if not isinstance(
            location,
            dict,
        ):
            continue

        types_.extend(
            types(
                location.get(
                    "@type"
                )
            )
        )

        name = txt(
            location.get("name")
        )

        if name:
            names.append(name)

        location_types = types(
            location.get("@type")
        )

        if (
            "VirtualLocation"
            in location_types
            and txt(
                location.get("url")
            )
        ):
            virtual.append(
                txt(
                    location.get("url")
                )
            )

        address = location.get(
            "address"
        )

        if isinstance(
            address,
            dict,
        ):
            for key, target in (
                (
                    "streetAddress",
                    streets,
                ),
                (
                    "addressLocality",
                    cities,
                ),
                (
                    "addressRegion",
                    regions,
                ),
                (
                    "postalCode",
                    postals,
                ),
                (
                    "addressCountry",
                    countries,
                ),
            ):
                value = txt(
                    address.get(key)
                )

                if value:
                    target.append(value)

        geo = location.get(
            "geo"
        )

        if isinstance(
            geo,
            dict,
        ):
            lat = txt(
                geo.get("latitude")
            )
            lon = txt(
                geo.get("longitude")
            )

            if lat or lon:
                coords.append(
                    f"{lat},{lon}"
                )

    info = {
        "name": "|".join(
            dict.fromkeys(names)
        ),
        "city": "|".join(
            dict.fromkeys(cities)
        ),
        "region": "|".join(
            dict.fromkeys(regions)
        ),
        "country": "|".join(
            dict.fromkeys(countries)
        ),
        "street": "|".join(
            dict.fromkeys(streets)
        ),
        "postal": "|".join(
            dict.fromkeys(postals)
        ),
        "coords": "|".join(
            dict.fromkeys(coords)
        ),
        "types": "|".join(
            dict.fromkeys(types_)
        ),
        "virtual_url": "|".join(
            dict.fromkeys(virtual)
        ),
    }

    info["physical"] = bool(
        streets
        or cities
        or regions
        or postals
        or coords
    ) or any(
        value in {
            "Place",
            "MusicVenue",
            "StadiumOrArena",
            "CivicStructure",
        }
        for value in types_
    )

    info["virtual"] = bool(
        virtual
    ) or (
        "VirtualLocation"
        in types_
    )

    return info


def coord(info):
    if not info["coords"]:
        return None

    try:
        lat, lon = map(
            float,
            info["coords"]
            .split("|", 1)[0]
            .split(",", 1),
        )
    except (
        ValueError,
        TypeError,
    ):
        return None

    if (
        -90 <= lat <= 90
        and -180 <= lon <= 180
    ):
        return lat, lon

    return None


def map_city(
    info,
    seed_ids,
    lookup,
    points,
):
    city = info["city"].split(
        "|",
        1,
    )[0].casefold()

    state = info["region"].split(
        "|",
        1,
    )[0].casefold()

    country = info["country"].split(
        "|",
        1,
    )[0].casefold()

    if country in {
        "usa",
        "united states",
        "united states of america",
    }:
        country = "us"

    if city and state:
        city_id = (
            lookup.get(
                (
                    city,
                    state,
                    country,
                )
            )
            or lookup.get(
                (
                    city,
                    state,
                    "",
                )
            )
        )

        if city_id:
            return (
                city_id,
                "mapped_exact",
                0,
            )

    coordinates = coord(info)

    if coordinates and points:
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
            )
        )

        if (
            nearest[0]
            <= FALLBACK_MILES
        ):
            return (
                nearest[1],
                "mapped_radius",
                nearest[0],
            )

    seeds = {
        value
        for value in seed_ids
        if value
    }

    if (
        info["physical"]
        and len(seeds) == 1
        and not city
        and not coordinates
    ):
        return (
            next(iter(seeds)),
            "mapped_seed_site",
            None,
        )

    return (
        None,
        "unmapped",
        None,
    )


def virtual_only(
    event,
    info,
):
    mode = txt(
        event.get(
            "eventAttendanceMode"
        )
    ).casefold()

    return (
        not info["physical"]
        and (
            "onlineeventattendancemode"
            in mode
            or info["virtual"]
        )
    )


def radio(
    event,
    info,
):
    if info["physical"]:
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
    if value in (
        None,
        "",
        [],
        {},
    ):
        return ""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    )


def fetch_page(item):
    url, robot = item

    if robot:
        try:
            if not robot.can_fetch(
                UA,
                url,
            ):
                return (
                    url,
                    None,
                    "robots",
                )
        except Exception:
            pass

    try:
        response = req(url)

        content_type = (
            response.headers
            .get(
                "Content-Type",
                "",
            )
            .casefold()
        )

        if (
            content_type
            and "html" not in content_type
            and "xhtml" not in content_type
        ):
            return (
                url,
                None,
                "non_html",
            )

        return (
            url,
            response.text,
            "",
        )

    except Exception as exc:
        return (
            url,
            None,
            f"{type(exc).__name__}: {exc}",
        )


FIELDS = [
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


def process_event(
    event,
    site,
    lookup,
    points,
    run_text,
    rows,
    seen,
    stats,
    source_url,
):
    info = location_info(event)

    if virtual_only(
        event,
        info,
    ):
        stats[
            "virtual_filtered"
        ] += 1
        return

    if radio(
        event,
        info,
    ):
        stats[
            "radio_filtered"
        ] += 1
        return

    if (
        not txt(event.get("name"))
        or not txt(
            event.get(
                "startDate"
            )
        )
    ):
        return

    key = event_key(
        event,
        source_url,
    )

    if key in seen:
        return

    seen.add(key)

    (
        city_id,
        mapping,
        distance,
    ) = map_city(
        info,
        site["city_ids"],
        lookup,
        points,
    )

    if not city_id:
        stats[
            "unmapped"
        ] += 1
        return

    row = {
        "schema_org_run_started_utc": run_text,
        "schema_org_seed_host": site["host"],
        "schema_org_seed_city_id": "|".join(
            sorted(
                site["city_ids"]
            )
        ),
        "schema_org_seed_categories": "|".join(
            sorted(
                site["categories"]
            )
        ),
        "schema_org_source_url": source_url,
        "schema_org_event_type": "|".join(
            types(
                event.get("@type")
            )
        ),
        "schema_org_city_mapping": mapping,
        "schema_org_city_mapping_distance_miles": (
            ""
            if distance is None
            else f"{distance:.3f}"
        ),
        "event_name": txt(
            event.get("name")
        ),
        "event_start_date": txt(
            event.get(
                "startDate"
            )
        ),
        "event_end_date": txt(
            event.get(
                "endDate"
            )
        ),
        "event_url": (
            txt(
                event.get("url")
            )
            or source_url
        ),
        "event_status": txt(
            event.get(
                "eventStatus"
            )
        ),
        "event_attendance_mode": txt(
            event.get(
                "eventAttendanceMode"
            )
        ),
        "event_description": txt(
            event.get(
                "description"
            )
        ),
        "event_location_json": json_cell(
            event.get(
                "location"
            )
        ),
        "event_offers_json": json_cell(
            event.get(
                "offers"
            )
        ),
        "event_organizer_json": json_cell(
            event.get(
                "organizer"
            )
        ),
        "event_performer_json": json_cell(
            event.get(
                "performer"
            )
        ),
        "event_image_json": json_cell(
            event.get(
                "image"
            )
        ),
        "event_keywords": txt(
            event.get(
                "keywords"
            )
        ),
        "event_json": json_cell(
            event
        ),
    }

    rows.setdefault(
        city_id,
        []
    ).append(row)

    stats[
        "events_written"
    ] += 1


def process(
    site,
    lookup,
    points,
    run_text,
):
    stats = {
        key: 0
        for key in (
            "sitemaps",
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

    (
        candidates,
        robot,
        sitemap_count,
        sitemap_urls_seen,
    ) = discover(site)

    stats[
        "sitemaps"
    ] = sitemap_count

    stats[
        "sitemap_urls_seen"
    ] = sitemap_urls_seen

    stats[
        "candidate_urls"
    ] = len(candidates)

    rows = {}
    seen = set()

    with ThreadPoolExecutor(
        max_workers=PAGE_WORKERS
    ) as executor:
        futures = [
            executor.submit(
                fetch_page,
                (
                    url,
                    robot,
                ),
            )
            for url in candidates
        ]

        for future in as_completed(
            futures
        ):
            (
                url,
                html,
                error,
            ) = future.result()

            if error == "robots":
                stats[
                    "robots_blocked"
                ] += 1
                continue

            if html is None:
                stats[
                    "page_errors"
                ] += 1
                continue

            stats[
                "pages_fetched"
            ] += 1

            events = extract_events(
                html,
                url,
            )

            if not events:
                continue

            stats[
                "pages_with_events"
            ] += 1

            stats[
                "event_objects"
            ] += len(events)

            for event in events:
                process_event(
                    event,
                    site,
                    lookup,
                    points,
                    run_text,
                    rows,
                    seen,
                    stats,
                    url,
                )

    return rows, stats


def write_files(
    run_date,
    rows_by_city,
):
    files = 0
    rows = 0

    for (
        city_id,
        city_rows,
    ) in sorted(
        rows_by_city.items()
    ):
        city_rows.sort(
            key=lambda item: (
                item[
                    "event_start_date"
                ],
                item[
                    "event_name"
                ],
                item[
                    "event_url"
                ],
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
                / (
                    f"page_"
                    f"{offset // 1000:03d}"
                    f".csv"
                )
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
                    fieldnames=FIELDS,
                )

                writer.writeheader()
                writer.writerows(
                    chunk
                )

            files += 1
            rows += len(chunk)

    return files, rows


def clear_run(
    run_date,
):
    if not RAW_ROOT.exists():
        return

    for citydir in RAW_ROOT.iterdir():
        run_dir = (
            citydir
            / run_date
        )

        if not run_dir.is_dir():
            continue

        for path in run_dir.glob(
            "page_*.csv"
        ):
            path.unlink()

        try:
            run_dir.rmdir()
        except OSError:
            pass


def main():
    started = datetime.now(
        timezone.utc
    )

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
        print(
            "SCHEMA.ORG: FAILED"
        )
        print(
            f"ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )
        return 2

    if not sites:
        print(
            "SCHEMA.ORG: FAILED"
        )
        print(
            "ERROR: no "
            "category-qualified "
            "Foursquare website "
            "seeds"
        )
        return 2

    clear_run(
        run_date
    )

    print(
        f"WEBSITE SEEDS: "
        f"{len(sites)}",
        flush=True,
    )

    totals = {
        key: 0
        for key in (
            "sitemaps",
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
        max_workers=SITE_WORKERS
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

        for future in as_completed(
            futures
        ):
            completed += 1

            site_host = futures[
                future
            ]

            try:
                (
                    rows,
                    stats,
                ) = future.result()

            except Exception as exc:
                print(
                    f"SITE ERROR "
                    f"{site_host}: "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True,
                )
                continue

            for key in totals:
                totals[key] += stats[key]

            for (
                city_id,
                city_rows,
            ) in rows.items():
                all_rows.setdefault(
                    city_id,
                    []
                ).extend(
                    city_rows
                )

            print(
                f"SITES "
                f"{completed}/"
                f"{len(sites)} "
                f"{site_host}: "
                f"candidates="
                f"{stats['candidate_urls']} "
                f"events="
                f"{stats['event_objects']} "
                f"written="
                f"{stats['events_written']}",
                flush=True,
            )

    files, rows = write_files(
        run_date,
        all_rows,
    )

    print("")
    print(
        "SCHEMA.ORG: PASS"
    )
    print(
        f"RUN DATE: {run_date}"
    )
    print(
        f"WEBSITE SEEDS: "
        f"{len(sites)}"
    )
    print(
        f"SITEMAPS INSPECTED: "
        f"{totals['sitemaps']}"
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
        f"EVENTS WRITTEN: "
        f"{rows}"
    )
    print(
        f"CITIES WRITTEN: "
        f"{len(all_rows)}"
    )
    print(
        f"CSV FILES: "
        f"{files}"
    )
    print(
        "OUTPUT: "
        "data/events/raw/schema_org/"
        "<city_id>/<YYYYMMDD>/"
        "page_###.csv"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )