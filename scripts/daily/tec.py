#!/usr/bin/env python3

import csv
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
FOURSQUARE_ROOT = ROOT / "data" / "venues" / "raw" / "foursquare"
CITY_FILE = ROOT / "data" / "location" / "usa" / "city_master.csv"
RAW_ROOT = ROOT / "data" / "events" / "raw" / "tec"

TEC_PATH = "/wp-json/tribe/events/v1/events"

PROBE_WORKERS = 20
FETCH_WORKERS = 8

REQUEST_TIMEOUT_SECONDS = 12
MAX_ATTEMPTS = 2

PER_PAGE = 50
MAX_PAGES_PER_SITE = 250

CSV_ROWS_PER_FILE = 1000
COORDINATE_FALLBACK_MILES = 40.0

USER_AGENT = "events-tec-collector/1.4"

EXCLUDED_HOSTS = {
    "kwel.com",
}

RADIO_TEXT_MARKERS = (
    "radio show",
    "radio program",
    "radio hour",
    "on-air",
    "on air",
    "broadcast live",
    "live broadcast",
    "streaming live",
)


def request_json(url):
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read()
                payload = json.loads(raw)
                return payload, response.geturl()

        except HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"

            retryable = (
                exc.code == 429
                or 500 <= exc.code <= 599
            )

            if not retryable:
                break

        except URLError as exc:
            last_error = f"URL error: {exc.reason}"

        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc}"
            break

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < MAX_ATTEMPTS:
            time.sleep(min(2 ** (attempt - 1), 2))

    raise RuntimeError(last_error or "request failed")


def canonical_host(host):
    value = str(host or "").strip().lower()

    if value.startswith("www."):
        value = value[4:]

    return value


def website_bases(value):
    text = str(value or "").strip()

    if not text:
        return []

    if "://" not in text:
        text = "https://" + text

    try:
        parsed = urlparse(text)
    except ValueError:
        return []

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        return []

    origin = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "",
            "",
            "",
            "",
        )
    ).rstrip("/")

    path = (parsed.path or "").rstrip("/")

    if path:
        exact = (origin + path).rstrip("/")
    else:
        exact = origin

    result = []

    for item in (exact, origin):
        if item and item not in result:
            result.append(item)

    return result


def norm(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or "").strip(),
    ).casefold()


def norm_country(value):
    value = norm(value)

    aliases = {
        "us": "us",
        "usa": "us",
        "united states": "us",
        "united states of america": "us",
    }

    return aliases.get(value, value)


def load_city_mapping():
    with CITY_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        cities = list(csv.DictReader(handle))

    if not cities:
        raise RuntimeError("city_master.csv is empty")

    required = {
        "city_id",
        "state",
        "state_code",
        "city",
        "country_code",
        "latitude",
        "longitude",
    }

    missing = sorted(
        required - set(cities[0].keys())
    )

    if missing:
        raise RuntimeError(
            "missing city columns: "
            + ", ".join(missing)
        )

    lookup = {}
    points = []

    for row in cities:
        city_id = str(
            row.get("city_id") or ""
        ).strip()

        city = norm(row.get("city"))
        state_code = norm(row.get("state_code"))
        state_name = norm(row.get("state"))
        country = norm_country(
            row.get("country_code")
        )

        if city_id and city and state_code:
            for region in {
                state_code,
                state_name,
            } - {""}:
                lookup[
                    (city, region, country)
                ] = city_id
                lookup[
                    (city, region, "")
                ] = city_id

        try:
            latitude = float(row.get("latitude"))
            longitude = float(row.get("longitude"))
        except (TypeError, ValueError):
            continue

        if city_id:
            points.append(
                (
                    city_id,
                    latitude,
                    longitude,
                )
            )

    return lookup, points


def distance_miles(
    lat1,
    lon1,
    lat2,
    lon2,
):
    radius = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    value = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2) ** 2
    )

    arc = 2 * math.atan2(
        math.sqrt(value),
        math.sqrt(1 - value),
    )

    return radius * arc


def venue_records(value):
    if isinstance(value, dict):
        return [value]

    if isinstance(value, list):
        return [
            item
            for item in value
            if isinstance(item, dict)
        ]

    return []


def venue_coordinates(venue):
    try:
        latitude = float(venue.get("geo_lat"))
        longitude = float(venue.get("geo_lng"))
    except (TypeError, ValueError):
        return None

    if not (
        -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        return None

    return latitude, longitude


def resolve_event_city(
    event,
    lookup,
    city_points,
):
    venues = venue_records(
        event.get("venue")
    )

    if not venues:
        return None, "no_physical_venue", None

    usable = []
    non_us = 0

    for venue in venues:
        if not venue_has_physical_location(
            venue
        ):
            continue

        country = norm_country(
            venue.get("country")
        )

        if country and country != "us":
            non_us += 1
            continue

        usable.append(venue)

        locality = norm(venue.get("city"))
        region = norm(
            venue.get("state")
            or venue.get("province")
        )

        if locality and region:
            city_id = (
                lookup.get(
                    (locality, region, country)
                )
                or lookup.get(
                    (locality, region, "")
                )
            )

            if city_id:
                return city_id, "mapped_exact", 0.0

    nearest_city_id = None
    nearest_distance = None

    for venue in usable:
        coordinates = venue_coordinates(venue)

        if not coordinates:
            continue

        latitude, longitude = coordinates

        for (
            city_id,
            city_latitude,
            city_longitude,
        ) in city_points:
            miles = distance_miles(
                latitude,
                longitude,
                city_latitude,
                city_longitude,
            )

            if (
                nearest_distance is None
                or miles < nearest_distance
            ):
                nearest_distance = miles
                nearest_city_id = city_id

    if (
        nearest_city_id
        and nearest_distance is not None
        and nearest_distance
        <= COORDINATE_FALLBACK_MILES
    ):
        return (
            nearest_city_id,
            "mapped_radius",
            nearest_distance,
        )

    if non_us and not usable:
        return None, "non_us_country", None

    if nearest_distance is not None:
        return (
            None,
            "outside_40_miles",
            nearest_distance,
        )

    return None, "city_not_in_master", None


def latest_foursquare_files():
    if not FOURSQUARE_ROOT.exists():
        raise RuntimeError(
            f"Foursquare raw directory not found: "
            f"{FOURSQUARE_ROOT}"
        )

    files = []

    city_dirs = sorted(
        path
        for path in FOURSQUARE_ROOT.iterdir()
        if path.is_dir()
    )

    for city_dir in city_dirs:
        candidates = sorted(
            city_dir.glob("*.json"),
            key=lambda path: path.name,
            reverse=True,
        )

        if candidates:
            files.append(candidates[0])

    return files


def discover_website_groups():
    groups = {}

    for source_file in latest_foursquare_files():
        try:
            payload = json.loads(
                source_file.read_text(
                    encoding="utf-8",
                )
            )
        except Exception:
            continue

        city = payload.get("city") or {}

        city_id = str(
            city.get("city_id") or ""
        ).strip()

        city_name = str(
            city.get("city") or ""
        ).strip()

        state_code = str(
            city.get("state_code") or ""
        ).strip()

        for search in payload.get("searches") or []:
            response = search.get("response") or {}

            for place in response.get("results") or []:
                if not isinstance(place, dict):
                    continue

                website = str(
                    place.get("website") or ""
                ).strip()

                if not website:
                    continue

                bases = website_bases(website)

                if not bases:
                    continue

                parsed = urlparse(bases[0])

                host_key = canonical_host(
                    parsed.hostname
                )

                if not host_key:
                    continue

                if host_key in EXCLUDED_HOSTS:
                    continue

                group = groups.setdefault(
                    host_key,
                    {
                        "canonical_host": host_key,
                        "bases": [],
                        "discoveries": [],
                    },
                )

                for base in bases:
                    if base not in group["bases"]:
                        group["bases"].append(base)

                discovery = {
                    "website": website,
                    "place": str(
                        place.get("name") or ""
                    ).strip(),
                    "city_id": city_id,
                    "city": city_name,
                    "state_code": state_code,
                }

                if discovery not in group["discoveries"]:
                    group["discoveries"].append(
                        discovery
                    )

    return list(groups.values())


def with_query(url, params):
    parsed = urlparse(url)

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    query.update(
        {
            key: str(value)
            for key, value in params.items()
        }
    )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


def endpoint_from_final_url(url):
    parsed = urlparse(url)
    path = parsed.path

    index = path.lower().find(
        TEC_PATH.lower()
    )

    if index >= 0:
        path = path[
            : index + len(TEC_PATH)
        ]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            "",
            "",
        )
    ).rstrip("/")


def probe_group(group, start_date):
    errors = []

    for base in group["bases"]:
        endpoint = (
            base.rstrip("/")
            + TEC_PATH
        )

        probe_url = with_query(
            endpoint,
            {
                "per_page": 1,
                "page": 1,
                "start_date": start_date,
            },
        )

        try:
            payload, final_url = request_json(
                probe_url
            )
        except Exception as exc:
            errors.append(
                f"{base}: {exc}"
            )
            continue

        if (
            not isinstance(payload, dict)
            or not isinstance(
                payload.get("events"),
                list,
            )
        ):
            errors.append(
                f"{base}: response did not "
                f"contain events array"
            )
            continue

        return {
            "canonical_host":
                group["canonical_host"],
            "endpoint":
                endpoint_from_final_url(
                    final_url
                ),
            "discoveries":
                group["discoveries"],
            "probe_total":
                payload.get("total"),
            "probe_total_pages":
                payload.get("total_pages"),
            "status":
                "DISCOVERED",
            "error":
                "",
        }

    return {
        "canonical_host":
            group["canonical_host"],
        "endpoint":
            "",
        "discoveries":
            group["discoveries"],
        "probe_total":
            "",
        "probe_total_pages":
            "",
        "status":
            "NOT_TEC",
        "error":
            " | ".join(errors[-3:]),
    }


def event_identity(event):
    event_id = str(
        event.get("id") or ""
    ).strip()

    global_id = str(
        event.get("global_id") or ""
    ).strip()

    event_url = str(
        event.get("url") or ""
    ).strip()

    start = str(
        event.get("start_date")
        or event.get("utc_start_date")
        or ""
    ).strip()

    title = str(
        event.get("title") or ""
    ).strip()

    return "|".join(
        [
            event_id
            or global_id
            or event_url
            or title,
            start,
        ]
    )


def venue_has_physical_location(value):
    if isinstance(value, list):
        return any(
            venue_has_physical_location(item)
            for item in value
        )

    if not isinstance(value, dict):
        return False

    location_fields = (
        "address",
        "city",
        "state",
        "province",
        "zip",
        "geo_lat",
        "geo_lng",
    )

    return any(
        value.get(field) not in (None, "", [], {})
        for field in location_fields
    )


def event_is_virtual_only(event):
    if venue_has_physical_location(event.get("venue")):
        return False

    if event.get("is_virtual") is True:
        return True

    return bool(
        str(event.get("virtual_url") or "").strip()
        or str(event.get("virtual_video_source") or "").strip()
    )


def event_is_radio_program(event):
    if venue_has_physical_location(event.get("venue")):
        return False

    text_parts = [
        event.get("title"),
        event.get("description"),
        event.get("excerpt"),
    ]

    for collection_name in ("categories", "tags"):
        collection = event.get(collection_name) or []

        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict):
                    text_parts.extend(
                        (item.get("name"), item.get("slug"))
                    )
                else:
                    text_parts.append(item)

    text = " ".join(
        str(value or "")
        for value in text_parts
    ).lower()

    return any(
        marker in text
        for marker in RADIO_TEXT_MARKERS
    )


def fetch_site(
    site,
    start_date,
    run_started_text,
):
    endpoint = site["endpoint"]

    rows = []
    seen = set()

    pages_fetched = 0
    duplicate_identity_rows = 0
    filtered_virtual_only = 0
    filtered_radio = 0

    status = "PASS"
    error = ""

    for page in range(
        1,
        MAX_PAGES_PER_SITE + 1,
    ):
        url = with_query(
            endpoint,
            {
                "per_page": PER_PAGE,
                "page": page,
                "start_date": start_date,
            },
        )

        try:
            payload, _ = request_json(url)

        except Exception as exc:
            status = (
                "PARTIAL"
                if pages_fetched
                else "FAILED"
            )

            error = str(exc)
            break

        if (
            not isinstance(payload, dict)
            or not isinstance(
                payload.get("events"),
                list,
            )
        ):
            status = (
                "PARTIAL"
                if pages_fetched
                else "FAILED"
            )

            error = (
                "response did not contain "
                "events array"
            )

            break

        events = payload.get("events") or []

        if not events:
            break

        pages_fetched += 1

        for event in events:
            if not isinstance(event, dict):
                continue

            if event_is_virtual_only(event):
                filtered_virtual_only += 1
                continue

            if event_is_radio_program(event):
                filtered_radio += 1
                continue

            identity = event_identity(event)

            if identity in seen:
                duplicate_identity_rows += 1
            else:
                seen.add(identity)

            rows.append(
                {
                    "tec_run_started_utc":
                        run_started_text,
                    "tec_canonical_host":
                        site["canonical_host"],
                    "tec_endpoint":
                        endpoint,
                    "tec_page":
                        page,
                    "event":
                        event,
                }
            )

        total_pages = payload.get(
            "total_pages"
        )

        try:
            total_pages = (
                int(total_pages)
                if total_pages is not None
                else None
            )
        except (TypeError, ValueError):
            total_pages = None

        if (
            total_pages is not None
            and page >= total_pages
        ):
            break

        if len(events) < PER_PAGE:
            break

    else:
        status = "CAPPED"
        error = (
            f"reached page cap of "
            f"{MAX_PAGES_PER_SITE}"
        )

    return {
        "canonical_host":
            site["canonical_host"],
        "endpoint":
            endpoint,
        "discoveries":
            site["discoveries"],
        "probe_total":
            site["probe_total"],
        "probe_total_pages":
            site["probe_total_pages"],
        "status":
            status,
        "error":
            error,
        "pages_fetched":
            pages_fetched,
        "events_fetched":
            len(rows),
        "duplicate_identity_rows":
            duplicate_identity_rows,
        "filtered_virtual_only":
            filtered_virtual_only,
        "filtered_radio":
            filtered_radio,
        "rows":
            rows,
    }


def compact_dict(value, keys):
    if not isinstance(value, dict):
        return value

    return {
        key: value[key]
        for key in keys
        if key in value and value[key] not in (None, "", [], {})
    }


def compact_list_of_dicts(value, keys):
    if not isinstance(value, list):
        return value

    result = []

    for item in value:
        if isinstance(item, dict):
            result.append(compact_dict(item, keys))
        else:
            result.append(item)

    return result


def compact_categories(value):
    return compact_list_of_dicts(
        value,
        (
            "id",
            "name",
            "slug",
            "parent",
        ),
    )


def compact_image(value):
    return compact_dict(
        value,
        (
            "url",
            "id",
            "extension",
            "width",
            "height",
        ),
    )


def compact_venue(value):
    return compact_dict(
        value,
        (
            "id",
            "global_id",
            "venue",
            "address",
            "city",
            "country",
            "province",
            "state",
            "zip",
            "website",
            "phone",
            "url",
            "geo_lat",
            "geo_lng",
            "show_map",
            "show_map_link",
        ),
    )


def compact_organizer(value):
    keys = (
        "id",
        "global_id",
        "organizer",
        "url",
        "website",
        "phone",
        "email",
    )

    if isinstance(value, list):
        return compact_list_of_dicts(value, keys)

    return compact_dict(value, keys)


def json_cell(value):
    if value in (None, ""):
        return ""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class DescriptionTextParser(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
        "ol",
    }

    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self.parts.append(" ")

        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f" {unescape(str(alt))} ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return

        if self.skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip_depth and data:
            self.parts.append(data)


def clean_description(value):
    text = str(value or "").strip()
    if not text:
        return ""

    parser = DescriptionTextParser()

    try:
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)

    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def scalar_cell(value):
    if value is None:
        return ""

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (dict, list)):
        return ""

    return str(value)


def decoded_scalar_cell(value):
    return unescape(scalar_cell(value))


def serialize_event_fields(event):
    row = {
        "event.id": scalar_cell(event.get("id")),
        "event.global_id": scalar_cell(event.get("global_id")),
        "event.title": decoded_scalar_cell(event.get("title")),
        "event.start_date": scalar_cell(event.get("start_date")),
        "event.utc_start_date": scalar_cell(event.get("utc_start_date")),
        "event.end_date": scalar_cell(event.get("end_date")),
        "event.utc_end_date": scalar_cell(event.get("utc_end_date")),
        "event.timezone": scalar_cell(event.get("timezone")),
        "event.all_day": scalar_cell(event.get("all_day")),
        "event.featured": scalar_cell(event.get("featured")),
        "event.status": scalar_cell(event.get("status")),
        "event.url": scalar_cell(event.get("url")),
        "event.website": scalar_cell(event.get("website")),
        "event.purchase_link": scalar_cell(event.get("purchase_link")),
        "event.cost": scalar_cell(event.get("cost")),
        "event.cost_details_json": json_cell(event.get("cost_details")),
        "event.categories_json": json_cell(compact_categories(event.get("categories"))),
        "event.tags_json": json_cell(event.get("tags")),
        "event.venue_json": json_cell(compact_venue(event.get("venue"))),
        "event.organizer_json": json_cell(compact_organizer(event.get("organizer"))),
        "event.description": clean_description(event.get("description")),
        "event.excerpt": decoded_scalar_cell(event.get("excerpt")),
        "event.image_json": json_cell(compact_image(event.get("image"))),
        "event.custom_fields_json": json_cell(event.get("custom_fields")),
        "event.hide_from_listings": scalar_cell(event.get("hide_from_listings")),
        "event.is_virtual": scalar_cell(event.get("is_virtual")),
        "event.virtual_url": scalar_cell(event.get("virtual_url")),
        "event.virtual_video_source": scalar_cell(event.get("virtual_video_source")),
        "event.modified": scalar_cell(event.get("modified")),
        "event.modified_utc": scalar_cell(event.get("modified_utc")),
        "event.show_map": scalar_cell(event.get("show_map")),
        "event.show_map_link": scalar_cell(event.get("show_map_link")),
        "event.subevents_json": json_cell(event.get("subevents")),
    }

    ticketed = event.get("ticketed")

    if isinstance(ticketed, (dict, list)):
        row["event.ticketed"] = ""
        row["event.ticketed_json"] = json_cell(ticketed)
    else:
        row["event.ticketed"] = scalar_cell(ticketed)
        row["event.ticketed_json"] = ""

    return row


def build_event_csv_rows(raw_rows):
    result = []

    for item in raw_rows:
        row = {
            "tec_run_started_utc":
                item["tec_run_started_utc"],
            "tec_canonical_host":
                item["tec_canonical_host"],
            "tec_endpoint":
                item["tec_endpoint"],
            "tec_page":
                item["tec_page"],
        }

        row.update(
            serialize_event_fields(
                item["event"]
            )
        )

        result.append(row)

    return result


def write_csv(
    path,
    rows,
    fieldnames,
):
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
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def ordered_event_fields(rows):
    return [
        "tec_run_started_utc",
        "tec_canonical_host",
        "tec_endpoint",
        "tec_page",
        "event.id",
        "event.global_id",
        "event.title",
        "event.start_date",
        "event.utc_start_date",
        "event.end_date",
        "event.utc_end_date",
        "event.timezone",
        "event.all_day",
        "event.featured",
        "event.status",
        "event.url",
        "event.website",
        "event.purchase_link",
        "event.cost",
        "event.cost_details_json",
        "event.categories_json",
        "event.tags_json",
        "event.venue_json",
        "event.organizer_json",
        "event.description",
        "event.excerpt",
        "event.image_json",
        "event.custom_fields_json",
        "event.hide_from_listings",
        "event.is_virtual",
        "event.virtual_url",
        "event.virtual_video_source",
        "event.modified",
        "event.modified_utc",
        "event.show_map",
        "event.show_map_link",
        "event.subevents_json",
        "event.ticketed",
        "event.ticketed_json",
    ]


def clear_city_run_files(run_date, diagnostic_dir):
    for stale in diagnostic_dir.glob(
        "events_*.csv"
    ):
        stale.unlink()

    for city_dir in RAW_ROOT.iterdir():
        if (
            not city_dir.is_dir()
            or not city_dir.name.isdigit()
        ):
            continue

        date_dir = city_dir / run_date

        if not date_dir.exists():
            continue

        for stale in date_dir.glob(
            "page_*.csv"
        ):
            stale.unlink()

        try:
            date_dir.rmdir()
        except OSError:
            pass


def write_city_event_files(
    run_date,
    rows_by_city,
    diagnostic_dir,
):
    clear_city_run_files(
        run_date,
        diagnostic_dir,
    )

    fieldnames = ordered_event_fields([])
    files_written = 0

    for city_id in sorted(rows_by_city):
        rows = rows_by_city[city_id]

        rows.sort(
            key=lambda row: (
                row.get("event.start_date", ""),
                row.get("event.title", ""),
                row.get("event.id", ""),
            )
        )

        date_dir = (
            RAW_ROOT
            / city_id
            / run_date
        )

        for offset in range(
            0,
            len(rows),
            CSV_ROWS_PER_FILE,
        ):
            chunk = rows[
                offset:
                offset + CSV_ROWS_PER_FILE
            ]

            page_number = (
                offset // CSV_ROWS_PER_FILE
            )

            path = (
                date_dir
                / f"page_{page_number:03d}.csv"
            )

            write_csv(
                path,
                chunk,
                fieldnames,
            )

            files_written += 1

    return files_written


def main():
    run_started = datetime.now(
        timezone.utc
    )

    run_started_text = (
        run_started.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )

    run_date = run_started.strftime(
        "%Y%m%d"
    )

    start_date = run_started.strftime(
        "%Y-%m-%d"
    )

    run_dir = RAW_ROOT / run_date

    run_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        city_lookup, city_points = (
            load_city_mapping()
        )
    except Exception as exc:
        print("TEC: FAILED")
        print(f"ERROR: {exc}")
        return 2

    groups = discover_website_groups()

    if not groups:
        print("TEC: FAILED")
        print(
            "ERROR: no Foursquare "
            "venue websites found"
        )
        return 2

    print(
        f"FOURSQUARE WEBSITE DOMAINS: "
        f"{len(groups)}",
        flush=True,
    )

    print(
        "DISCOVERING TEC ENDPOINTS...",
        flush=True,
    )

    discovery_results = []

    with ThreadPoolExecutor(
        max_workers=PROBE_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                probe_group,
                group,
                start_date,
            ): group["canonical_host"]
            for group in groups
        }

        for future in as_completed(
            futures
        ):
            discovery_results.append(
                future.result()
            )

    tec_sites = [
        item
        for item in discovery_results
        if item["status"] == "DISCOVERED"
    ]

    print(
        f"TEC SITES FOUND: "
        f"{len(tec_sites)}",
        flush=True,
    )

    if not tec_sites:
        sites_rows = []

        for item in sorted(
            discovery_results,
            key=lambda row:
                row["canonical_host"],
        ):
            sites_rows.append(
                {
                    "canonical_host":
                        item["canonical_host"],
                    "endpoint":
                        item["endpoint"],
                    "status":
                        item["status"],
                    "probe_total":
                        item["probe_total"],
                    "probe_total_pages":
                        item[
                            "probe_total_pages"
                        ],
                    "pages_fetched":
                        "",
                    "events_fetched":
                        "",
                    "duplicate_identity_rows":
                        "",
                    "error":
                        item["error"],
                    "discoveries_json":
                        json.dumps(
                            item["discoveries"],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                }
            )

        write_csv(
            run_dir / "sites.csv",
            sites_rows,
            list(sites_rows[0].keys()),
        )

        print("TEC: FAILED")
        print("ERROR: no TEC sites found")
        return 2

    print(
        "FETCHING TEC EVENTS...",
        flush=True,
    )

    fetch_results = []

    with ThreadPoolExecutor(
        max_workers=FETCH_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                fetch_site,
                site,
                start_date,
                run_started_text,
            ): site["canonical_host"]
            for site in tec_sites
        }

        for future in as_completed(
            futures
        ):
            fetch_results.append(
                future.result()
            )

    fetched_by_host = {
        item["canonical_host"]: item
        for item in fetch_results
    }

    sites_rows = []

    for item in sorted(
        discovery_results,
        key=lambda row:
            row["canonical_host"],
    ):
        fetched = fetched_by_host.get(
            item["canonical_host"]
        )

        sites_rows.append(
            {
                "canonical_host":
                    item["canonical_host"],
                "endpoint":
                    item["endpoint"],
                "status":
                    (
                        fetched["status"]
                        if fetched
                        else item["status"]
                    ),
                "probe_total":
                    item["probe_total"],
                "probe_total_pages":
                    item[
                        "probe_total_pages"
                    ],
                "pages_fetched":
                    (
                        fetched[
                            "pages_fetched"
                        ]
                        if fetched
                        else ""
                    ),
                "events_fetched":
                    (
                        fetched[
                            "events_fetched"
                        ]
                        if fetched
                        else ""
                    ),
                "duplicate_identity_rows":
                    (
                        fetched[
                            "duplicate_identity_rows"
                        ]
                        if fetched
                        else ""
                    ),
                "error":
                    (
                        fetched["error"]
                        if fetched
                        else item["error"]
                    ),
                "discoveries_json":
                    json.dumps(
                        item["discoveries"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
            }
        )

    write_csv(
        run_dir / "sites.csv",
        sites_rows,
        list(sites_rows[0].keys()),
    )

    raw_rows = []

    for item in fetch_results:
        raw_rows.extend(
            item["rows"]
        )

    rows_by_city = {}
    mapping_counts = {}
    mapped_raw_rows = []

    for item in raw_rows:
        city_id, reason, _ = resolve_event_city(
            item["event"],
            city_lookup,
            city_points,
        )

        mapping_counts[reason] = (
            mapping_counts.get(reason, 0) + 1
        )

        if not city_id:
            continue

        mapped_raw_rows.append(item)

        row = build_event_csv_rows(
            [item]
        )[0]

        rows_by_city.setdefault(
            city_id,
            [],
        ).append(row)

    event_rows_count = len(
        mapped_raw_rows
    )

    event_files = write_city_event_files(
        run_date,
        rows_by_city,
        run_dir,
    )

    successful_sites = sum(
        1
        for item in fetch_results
        if item["status"]
        in {"PASS", "CAPPED"}
    )

    partial_sites = sum(
        1
        for item in fetch_results
        if item["status"] == "PARTIAL"
    )

    failed_sites = sum(
        1
        for item in fetch_results
        if item["status"] == "FAILED"
    )

    category_events = sum(
        1
        for item in mapped_raw_rows
        if item["event"].get(
            "categories"
        )
    )

    tag_events = sum(
        1
        for item in mapped_raw_rows
        if item["event"].get("tags")
    )

    duplicate_identity_rows = sum(
        int(
            item[
                "duplicate_identity_rows"
            ]
        )
        for item in fetch_results
    )

    filtered_virtual_only = sum(
        int(item.get("filtered_virtual_only", 0))
        for item in fetch_results
    )

    filtered_radio = sum(
        int(item.get("filtered_radio", 0))
        for item in fetch_results
    )

    status = (
        "PASS"
        if (
            failed_sites == 0
            and partial_sites == 0
        )
        else "PARTIAL"
    )

    print(f"TEC: {status}")
    print(f"RUN DATE: {run_date}")

    print(
        f"WEBSITE DOMAINS PROBED: "
        f"{len(groups)}"
    )

    print(
        f"TEC SITES: "
        f"{len(tec_sites)}"
    )

    print(
        f"SITES SUCCESSFUL: "
        f"{successful_sites}"
    )

    print(
        f"SITES PARTIAL: "
        f"{partial_sites}"
    )

    print(
        f"SITES FAILED: "
        f"{failed_sites}"
    )

    print(
        f"EVENTS WRITTEN: "
        f"{event_rows_count}"
    )

    print(
        f"DUPLICATE IDENTITY ROWS: "
        f"{duplicate_identity_rows}"
    )

    print(
        f"FILTERED VIRTUAL ONLY: "
        f"{filtered_virtual_only}"
    )

    print(
        f"FILTERED RADIO PROGRAMS: "
        f"{filtered_radio}"
    )

    print(
        f"EVENTS WITH CATEGORIES: "
        f"{category_events}"
    )

    print(
        f"EVENTS WITH TAGS: "
        f"{tag_events}"
    )

    print(
        f"EVENTS FETCHED AFTER FILTERS: "
        f"{len(raw_rows)}"
    )

    print(
        f"CITY MAPPED EXACT: "
        f"{mapping_counts.get('mapped_exact', 0)}"
    )

    print(
        f"CITY MAPPED <=40 MI: "
        f"{mapping_counts.get('mapped_radius', 0)}"
    )

    unmapped_events = (
        len(raw_rows) - event_rows_count
    )

    print(
        f"CITY UNMAPPED: "
        f"{unmapped_events}"
    )

    print(
        f"CITIES WRITTEN: "
        f"{len(rows_by_city)}"
    )

    print(
        f"EVENT CSV FILES: "
        f"{event_files}"
    )

    print(
        "OUTPUT: "
        "data/events/raw/tec/<city_id>/"
        f"{run_date}/page_###.csv"
    )

    return 0 if event_rows_count else 2


if __name__ == "__main__":
    raise SystemExit(main())