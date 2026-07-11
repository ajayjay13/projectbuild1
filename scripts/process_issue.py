"""
Parses a GitHub Issue Form body (rendered as markdown by GitHub) into a
structured record and merges it into data/inspections.geojson.

Triggered by .github/workflows/process-inspection.yml when an issue
carrying the "verified" label is closed. Geocodes new addresses through
OSM Nominatim (free, rate-limited to 1 req/sec) and caches results in
data/geocode-cache.json so repeat addresses (e.g. re-inspections of the
same kitchen) never re-hit the API.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

DATA_PATH = "data/inspections.geojson"
CACHE_PATH = "data/geocode-cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "hyderabad-hygiene-map/1.0 (github.com/ajayjay13/projectbuild1.git)"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def extract_score_percent(raw: str) -> int | None:
    """
    Pulls a percentage out of flexible formats like '106/114 Marks (93%)',
    '93%', '93', or 'x/y' fraction-only entries. Returns None if nothing
    numeric can be recovered (used for legend color, not display).
    """
    if not raw:
        return None
    m = re.search(r"\((\d{1,3})\s*%\)", raw)  # "(93%)"
    if not m:
        m = re.search(r"(\d{1,3})\s*%", raw)  # "93%" anywhere
    if not m:
        m = re.search(r"^\s*(\d{1,3})\s*$", raw)  # bare "93"
    if m:
        val = int(m.group(1))
        return val if 0 <= val <= 100 else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", raw)  # "106/114" fraction
    if m:
        num, denom = float(m.group(1)), float(m.group(2))
        if denom > 0:
            return round(num / denom * 100)
    return None


def parse_issue_body(body: str) -> dict:
    """GitHub renders issue-form fields as '### Label\\n\\nvalue' blocks."""
    fields = {}
    blocks = re.split(r"\n### ", body)
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        label = lines[0].strip().rstrip(":")
        value = "\n".join(lines[1:]).strip()
        if value.lower() in ("_no response_", ""):
            value = ""
        fields[label] = value
    return fields


def resolve_short_url(url: str) -> str:
    """Follow redirects on maps.app.goo.gl / goo.gl links to get the full URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl()
    except Exception as e:
        print(f"::warning::Could not resolve short Maps link '{url}': {e}")
        return url


def extract_coords_from_maps_url(url: str) -> tuple[float, float] | None:
    """
    Returns (lon, lat) if the URL contains recoverable coordinates, else None.
    Handles the two common Google Maps URL shapes:
      - .../@17.4356,78.4483,17z/...        (viewport center)
      - .../!3d17.4356!4d78.4483            (exact pin, place URLs)
      - ...?q=17.4356,78.4483               (plain query)
    Short links (maps.app.goo.gl, goo.gl/maps) are resolved to their full
    form first since coordinates aren't present in the short form itself.
    """
    if not url:
        return None
    if "maps.app.goo.gl" in url or "goo.gl/maps" in url:
        url = resolve_short_url(url)

    for pattern in (
        r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
        r"@(-?\d+\.\d+),(-?\d+\.\d+)",
        r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
    ):
        m = re.search(pattern, url)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            return (lon, lat)

    print(f"::warning::Could not extract coordinates from Maps link '{url}', falling back to address")
    return None


def geocode(address: str, cache: dict) -> tuple[float, float] | None:
    if address in cache:
        return tuple(cache[address])
    query = urllib.parse.urlencode({
        "q": f"{address}, Hyderabad, Telangana, India",
        "format": "json",
        "limit": 1,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.load(resp)
    except Exception as e:
        print(f"::warning::Geocoding failed for '{address}': {e}")
        return None
    time.sleep(1)  # respect Nominatim's 1 req/sec policy
    if not results:
        print(f"::warning::No geocode match for '{address}'")
        return None
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    cache[address] = [lon, lat]
    return (lon, lat)


def haversine_meters(c1: tuple[float, float], c2: tuple[float, float]) -> float:
    """c1, c2 are (lon, lat) pairs. Returns great-circle distance in meters."""
    import math
    lon1, lat1 = c1
    lon2, lat2 = c2
    r = 6371000  # Earth radius in meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


SAME_KITCHEN_RADIUS_METERS = 60  # submissions within this distance AND a similar name are treated as the same kitchen
NAME_SIMILARITY_THRESHOLD = 0.6  # 0-1, difflib ratio; handles typos/variants of the same name


def name_similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def find_matching_kitchen(geo: dict, coords: tuple[float, float], primary_name: str):
    """
    Finds an existing feature that is both within SAME_KITCHEN_RADIUS_METERS
    of coords AND has a similar name. Proximity alone is not enough — two
    different restaurants in the same building/food court (e.g. KFC and
    Burger King sharing a mall) must NOT merge into one pin.
    """
    best, best_dist = None, None
    for f in geo["features"]:
        dist = haversine_meters(coords, tuple(f["geometry"]["coordinates"]))
        if dist > SAME_KITCHEN_RADIUS_METERS:
            continue
        if name_similarity(primary_name, f["properties"]["primary_name"]) < NAME_SIMILARITY_THRESHOLD:
            continue  # close by, but a different business — don't merge
        if best_dist is None or dist < best_dist:
            best, best_dist = f, dist
    return best


def unique_id(geo: dict, base_slug: str) -> str:
    """Appends -2, -3, ... if base_slug is already used by a different (non-matching) kitchen."""
    existing_ids = {f["properties"]["id"] for f in geo["features"]}
    if base_slug not in existing_ids:
        return base_slug
    n = 2
    while f"{base_slug}-{n}" in existing_ids:
        n += 1
    return f"{base_slug}-{n}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_brands(raw: str) -> list[dict]:
    brands = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(.+?)\s*\((.+?)\)\s*$", part)
        if m:
            brands.append({"name": m.group(1).strip(), "platform": m.group(2).strip()})
        else:
            brands.append({"name": part, "platform": "unknown"})
    return brands


def main():
    issue_body = sys.argv[1] if len(sys.argv) > 1 else os.environ["ISSUE_BODY"]
    fields = parse_issue_body(issue_body)

    address = fields.get("Address / landmark", "")
    primary_name = fields.get("Establishment / firm name", "").strip()
    if not address or not primary_name:
        print("::error::Missing required address or establishment name")
        sys.exit(1)

    geo = load_json(DATA_PATH, {"type": "FeatureCollection", "features": []})
    cache = load_json(CACHE_PATH, {})

    maps_url = fields.get("Google Maps link (optional, more accurate)", "").strip()
    coords = extract_coords_from_maps_url(maps_url)
    if coords is None:
        coords = geocode(address, cache)
    save_json(CACHE_PATH, cache)
    if coords is None:
        print("::error::Could not determine coordinates from Maps link or address, skipping. Add lat/lon manually.")
        sys.exit(1)

    inspection = {
        "date": fields.get("Inspection date", ""),
        "score_raw": fields.get("Hygiene score", "").strip() or None,
        "score_percent": extract_score_percent(fields.get("Hygiene score", "")),
        "good_practices": [l.strip("- ").strip() for l in fields.get("Good practices observed", "").splitlines() if l.strip()],
        "violations": [l.strip("- ").strip() for l in fields.get("Violations / observations", "").splitlines() if l.strip()],
        "action_taken": fields.get("Action taken", ""),
        "source_url": fields.get("Source post URL", ""),
        "media_urls": [u.strip() for u in fields.get("Photo/video URLs", "").split(",") if u.strip()],
    }

    existing = find_matching_kitchen(geo, coords, primary_name)
    if existing:
        existing["properties"]["inspections"].append(inspection)
        if maps_url and not existing["properties"].get("google_maps_url"):
            existing["properties"]["google_maps_url"] = maps_url
        brands_raw = fields.get("Ordering-app brand name(s)", "")
        if brands_raw:
            existing_names = {b["name"] for b in existing["properties"]["brands"]}
            for b in parse_brands(brands_raw):
                if b["name"] not in existing_names:
                    existing["properties"]["brands"].append(b)
    else:
        kitchen_id = unique_id(geo, slugify(primary_name))
        geo["features"].append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(coords)},
            "properties": {
                "id": kitchen_id,
                "kitchen_type": fields.get("Establishment type", "dedicated"),
                "primary_name": primary_name,
                "brands": parse_brands(fields.get("Ordering-app brand name(s)", "")) or [{"name": primary_name, "platform": "dine-in"}],
                "address": address,
                "area": address.split(",")[-1].strip() if "," in address else address,
                "google_maps_url": maps_url or None,
                "inspections": [inspection],
            },
        })

    save_json(DATA_PATH, geo)
    print(f"Merged inspection for '{primary_name}'")


if __name__ == "__main__":
    main()
