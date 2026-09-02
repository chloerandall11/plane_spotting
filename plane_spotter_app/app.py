"""
Plane spotter - Flask backend.

Run with:
    python app.py

Then open http://<your-computer's-LAN-IP>:5000 on your phone, as long
as your phone is on the same WiFi as this computer. Find your LAN IP
with `ipconfig` (Windows) or `ifconfig` / `ip addr` (Mac/Linux) -
look for something like 192.168.x.x.
"""

import csv
import io
import json
import os
import random
import re
import time

import requests
from flask import Flask, jsonify, request, render_template

import core

app = Flask(__name__)

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.json")
AIRPORTS_CACHE_PATH = os.path.join(os.path.dirname(__file__), "airports_cache.json")
ADSBDB_BASE = "https://api.adsbdb.com/v0"
# OurAirports publishes a free, comprehensive worldwide airport list
# (covers small regional fields like Newquay/NQY, not just majors).
OURAIRPORTS_CSV_URL = "https://ourairports.com/data/airports.csv"

# Simple in-memory caches so we don't re-hit adsbdb.com for the same
# aircraft/callsign repeatedly in one session.
AIRCRAFT_TYPE_CACHE = {}
ROUTE_CACHE = {}
AIRLINE_CACHE = {}

# Single-user, in-memory session state: the last fetched candidate
# list and where we are in it. Fine for one person using this on
# their own phone - not built for multiple concurrent users.
SESSION = {
    "candidates": [],
    "selected_index": None,
    "fetched_at": 0.0,
    "user_lat": None,
    "user_lon": None,
    "radius_km": core.SEARCH_RADIUS_KM,
}

MAX_CANDIDATES_LISTED = 12

CASUAL_COMMENTS = {
    "correct": [
        "nailed it, nice eye",
        "spot on - literally",
        "yep, exactly right",
    ],
    "partial": [
        "close-ish! not quite though",
        "good instinct, wrong answer",
        "half right, we'll take it",
    ],
    "wrong": [
        "way off, but a solid guess",
        "nope - but who could've known",
        "not even close, love the confidence",
    ],
    "no_guess": [
        "fair enough, some get away",
        "the ones that got away, eh",
    ],
}


def lookup_aircraft_info(icao24):
    """Returns {'type': str|None, 'photo': str|None, 'photo_thumb': str|None}.
    adsbdb.com is a free, no-auth aircraft/route database that also
    hosts community-contributed photos for many registrations."""
    icao24 = icao24.lower()
    if icao24 in AIRCRAFT_TYPE_CACHE:
        return AIRCRAFT_TYPE_CACHE[icao24]
    result = {"type": None, "photo": None, "photo_thumb": None}
    try:
        resp = requests.get(f"{ADSBDB_BASE}/aircraft/{icao24}", timeout=5)
        if resp.ok:
            aircraft = (resp.json().get("response") or {}).get("aircraft")
            if aircraft:
                manufacturer = aircraft.get("manufacturer") or ""
                model = aircraft.get("type") or ""
                result["type"] = f"{manufacturer} {model}".strip() or None
                result["photo"] = aircraft.get("url_photo")
                result["photo_thumb"] = aircraft.get("url_photo_thumbnail")
    except requests.RequestException:
        pass
    AIRCRAFT_TYPE_CACHE[icao24] = result
    return result


def fetch_flight_trace(icao24):
    """Best-effort: pulls this aircraft's actual recent flown path from
    adsb.lol's globe-history trace data - the same data their own map
    viewer uses for its 'replay' feature. This is NOT part of the
    documented public API (no official endpoint for it), so it can
    occasionally be unavailable or change without notice - the caller
    should treat an empty list as normal, not an error."""
    icao24 = icao24.lower()
    bucket = icao24[-2:] if len(icao24) >= 2 else icao24
    url = f"https://globe.adsb.lol/data/traces/{bucket}/trace_full_{icao24}.json"
    try:
        resp = requests.get(url, timeout=6)
        if resp.ok:
            trace = resp.json().get("trace") or []
            # trace_full covers the WHOLE day for this airframe, often
            # several separate flights stitched together. Walk backward
            # from the most recent point and stop at the first big time
            # gap or explicit position gap - that isolates just the
            # current/most recent leg instead of mixing in old flights.
            leg = []
            prev_t = None
            for point in reversed(trace):
                t = point[0] if len(point) > 0 else None
                lat = point[1] if len(point) > 1 else None
                lon = point[2] if len(point) > 2 else None
                if lat is None or lon is None:
                    break  # explicit gap marker - previous leg starts here
                if prev_t is not None and t is not None and (prev_t - t) > 1200:
                    break  # >20 min gap - aircraft was grounded, different leg
                leg.append({"lat": lat, "lon": lon})
                prev_t = t
            leg.reverse()
            points = leg
            if len(points) > 60:  # downsample so the payload stays light
                step = max(1, len(points) // 60)
                points = points[::step]
            return points
    except (requests.RequestException, ValueError, TypeError, KeyError) as e:
        print(f"[trace] lookup failed for {icao24}: {e}")
    return []


def lookup_route(callsign):
    """Returns {'origin': str, 'destination': str, 'origin_coords': {...},
    'destination_coords': {...}} or None if unknown. Display strings are
    'Airport Name (Country)'; coords (when present) are real lat/lon from
    adsbdb's airport data, used to draw the flight path. Only reliable
    for scheduled commercial flights - adsbdb's route data comes from
    published schedules, not live ADS-B."""
    key = callsign.strip().upper()
    if key in ROUTE_CACHE:
        return ROUTE_CACHE[key]
    result = None
    try:
        resp = requests.get(f"{ADSBDB_BASE}/callsign/{key}", timeout=5)
        if resp.ok:
            route = (resp.json().get("response") or {}).get("flightroute")
            if route:
                def fmt_airport(a):
                    if not a:
                        return None
                    name = a.get("name") or a.get("municipality")
                    country = a.get("country_name")
                    if name and country:
                        return f"{name} ({country})"
                    return name

                def coords(a):
                    if not a:
                        return None
                    lat, lon = a.get("latitude"), a.get("longitude")
                    if lat is None or lon is None:
                        return None
                    return {"lat": lat, "lon": lon}

                result = {
                    "origin": fmt_airport(route.get("origin")),
                    "destination": fmt_airport(route.get("destination")),
                    "origin_coords": coords(route.get("origin")),
                    "destination_coords": coords(route.get("destination")),
                }
    except requests.RequestException:
        pass
    ROUTE_CACHE[key] = result
    return result


def strip_parenthetical(s):
    """'Newquay Cornwall (NQY)' -> 'newquay cornwall' - lets us compare
    a guess (which has an IATA code in brackets) against the revealed
    answer (which has a country in brackets) on the airport name alone."""
    if not s:
        return ""
    return re.sub(r"\(.*?\)", "", s).strip().lower()


def field_match(guess_val, actual_val):
    """Returns True/False if guess_val was provided, None if it wasn't
    (so the frontend knows not to color that cell at all)."""
    guess_val = (guess_val or "").strip()
    if not guess_val:
        return None
    g = strip_parenthetical(guess_val)
    a = strip_parenthetical(actual_val or "")
    if not a:
        return False
    return g in a or a in g


# Small fallback list used only if the full airport database can't be
# downloaded (e.g. no internet on first run).
FALLBACK_AIRPORTS = [
    "London Heathrow (LHR)", "London Gatwick (LGW)", "London Stansted (STN)",
    "London Luton (LTN)", "Manchester (MAN)", "Liverpool (LPL)",
    "Edinburgh (EDI)", "Glasgow (GLA)", "Dublin (DUB)",
    "Amsterdam Schiphol (AMS)", "Paris Charles de Gaulle (CDG)",
    "Frankfurt (FRA)", "Madrid (MAD)", "Dubai (DXB)", "New York JFK (JFK)",
]


def get_all_airports():
    """Loads the full worldwide airport list (name + IATA code) for the
    guess dropdown, caching it to disk after the first download so we
    don't re-fetch a multi-MB CSV on every restart."""
    if os.path.exists(AIRPORTS_CACHE_PATH):
        try:
            with open(AIRPORTS_CACHE_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    try:
        resp = requests.get(OURAIRPORTS_CSV_URL, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        airports = []
        for row in reader:
            iata = (row.get("iata_code") or "").strip()
            name = (row.get("name") or "").strip()
            if iata and name:
                airports.append(f"{name} ({iata})")
        airports = sorted(set(airports))
        if airports:
            with open(AIRPORTS_CACHE_PATH, "w") as f:
                json.dump(airports, f)
            return airports
    except (requests.RequestException, csv.Error):
        pass

    return FALLBACK_AIRPORTS


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r") as f:
        history = json.load(f)
    # backfill ids for entries saved before delete support existed
    changed = False
    for h in history:
        if not h.get("id"):
            h["id"] = f"legacy-{int(h.get('timestamp', 0) * 1000)}"
            changed = True
    if changed:
        save_history(history)
    return history


def save_history(entries):
    with open(HISTORY_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def lookup_airline(icao_prefix):
    """Looks up an airline by its 3-letter ICAO designator against
    adsbdb's airline database - which covers essentially every active
    commercial carrier worldwide (mainline, regional, and low-cost
    subsidiaries), unlike our small local KNOWN_AIRLINE_PREFIXES list.
    Returns the airline name, or None if not found."""
    if not icao_prefix:
        return None
    if icao_prefix in AIRLINE_CACHE:
        return AIRLINE_CACHE[icao_prefix]
    result = None
    try:
        resp = requests.get(f"{ADSBDB_BASE}/airline/{icao_prefix}", timeout=5)
        if resp.ok:
            entries = resp.json().get("response")
            if entries:
                result = entries[0].get("name")
    except requests.RequestException as e:
        print(f"[airline] lookup failed for {icao_prefix}: {e}")
    AIRLINE_CACHE[icao_prefix] = result
    return result


def ensure_commercial_classification(c: "core.Candidate"):
    """core.classify_aircraft() only checks a small local airline list
    and military callsign patterns - fine for military detection, but
    it'll miss most real-world carriers (regional subsidiaries, cargo
    divisions, less common airlines etc). If the local pass didn't
    resolve an operator and it's not military, this upgrades the
    classification using adsbdb's full airline database before the
    candidate is shown to the user."""
    if c.category == "military" or c.operator:
        return c
    prefix = c.aircraft.callsign.strip().upper()[:3]
    name = lookup_airline(prefix)
    if name:
        c.category = "commercial"
        c.operator = name
    return c


def candidate_public_view(c: "core.Candidate"):
    """What the client is allowed to see BEFORE revealing - no
    callsign/airline, just enough to go find it in the sky."""
    c = ensure_commercial_classification(c)
    return {
        "compass": c.compass,
        "bearing_deg": round(c.bearing_deg),
        "distance_km": round(c.distance_km, 1),
        "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
        "category_hint": c.category,  # fine to hint commercial/military/private
    }


def candidate_reveal_view(c: "core.Candidate"):
    c = ensure_commercial_classification(c)
    speed_mph = round(c.aircraft.velocity_ms * 2.23694) if c.aircraft.velocity_ms else None
    heading_deg = round(c.aircraft.heading_deg) if c.aircraft.heading_deg is not None else None
    aircraft_info = lookup_aircraft_info(c.aircraft.icao24)
    route = lookup_route(c.aircraft.callsign) if c.category == "commercial" else None
    trace_points = fetch_flight_trace(c.aircraft.icao24)
    return {
        "callsign": c.aircraft.callsign,
        "icao24": c.aircraft.icao24.upper(),
        "category": c.category,
        "operator": c.operator,
        "aircraft_type": aircraft_info["type"],
        "photo": aircraft_info["photo"],
        "photo_thumb": aircraft_info["photo_thumb"],
        "origin": route["origin"] if route else None,
        "destination": route["destination"] if route else None,
        "origin_coords": route["origin_coords"] if route else None,
        "destination_coords": route["destination_coords"] if route else None,
        "current_coords": {"lat": c.aircraft.lat, "lon": c.aircraft.lon},
        "trace_points": trace_points,
        "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
        "distance_km": round(c.distance_km, 1),
        "speed_mph": speed_mph,
        "heading_deg": heading_deg,
        "heading_compass": core.bearing_to_compass(heading_deg) if heading_deg is not None else None,
        "look_compass": c.compass,
        "elevation_deg": round(c.elevation_deg),
    }


def make_comment(guess_result):
    scored = [v for v in guess_result.values() if v is not None]
    if not scored:
        return random.choice(CASUAL_COMMENTS["no_guess"])
    hits = sum(1 for v in scored if v)
    ratio = hits / len(scored)
    if ratio >= 0.75:
        return random.choice(CASUAL_COMMENTS["correct"])
    if ratio > 0:
        return random.choice(CASUAL_COMMENTS["partial"])
    return random.choice(CASUAL_COMMENTS["wrong"])


@app.route("/api/reference-data")
def api_reference_data():
    airlines = sorted(set(core.KNOWN_AIRLINE_PREFIXES.values()))
    aircraft_types = [
        "Airbus A220", "Airbus A319", "Airbus A320", "Airbus A321",
        "Airbus A330", "Airbus A350", "Airbus A380",
        "Boeing 737", "Boeing 747", "Boeing 757", "Boeing 767",
        "Boeing 777", "Boeing 787",
        "Embraer E170", "Embraer E190", "Embraer E195",
        "Bombardier CRJ900", "ATR 72", "De Havilland Dash 8",
        "McDonnell Douglas MD-80",
        "C-17 Globemaster III", "A400M Atlas", "A330 MRTT Voyager",
        "Chinook", "Eurofighter Typhoon", "Hercules C-130",
        "Gulfstream", "Cessna Citation", "Bombardier Global Express",
    ]
    airports = get_all_airports()
    return jsonify({"airlines": airlines, "airports": airports, "aircraft_types": aircraft_types})


def try_postcodes_io(clean_query):
    """Handles UK postcodes: full ('SW1A1AA') or partial/outcode ('TR8').
    Also handles bare central-London-style codes like 'SW1' or 'EC1',
    which aren't valid outcodes on their own - they need the extra
    district letter (SW1A, EC1A etc) that postcodes.io's plain
    /outcodes/ lookup won't guess for you."""
    try:
        resp = requests.get(f"https://api.postcodes.io/postcodes/{clean_query}", timeout=6)
        if resp.ok:
            result = resp.json().get("result")
            if result:
                return result["latitude"], result["longitude"], result.get("postcode", clean_query)
    except requests.RequestException as e:
        print(f"[geocode] postcodes.io full lookup failed: {e}")

    try:
        resp = requests.get(f"https://api.postcodes.io/outcodes/{clean_query}", timeout=6)
        if resp.ok:
            result = resp.json().get("result")
            if result:
                return result["latitude"], result["longitude"], result.get("outcode", clean_query)
    except requests.RequestException as e:
        print(f"[geocode] postcodes.io outcode lookup failed: {e}")

    # Bare area+district with no letter (e.g. "SW1", "EC1", "WC1") -
    # try the common central-London district-letter suffixes.
    if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", clean_query.upper()):
        for suffix in "AEHPVWXY":
            try:
                candidate = f"{clean_query.upper()}{suffix}"
                resp = requests.get(f"https://api.postcodes.io/outcodes/{candidate}", timeout=6)
                if resp.ok:
                    result = resp.json().get("result")
                    if result:
                        return result["latitude"], result["longitude"], f"{clean_query.upper()} area, London"
            except requests.RequestException:
                continue

    return None


def try_open_meteo(query, count=5):
    """General place-name/city geocoding. Free, no key, and - unlike
    Nominatim - doesn't block requests from cloud-hosting IPs like
    Render's. Returns several candidates (UK results sorted first,
    since that's most likely what's meant) rather than silently
    guessing one - a short query like a place name can easily match
    somewhere on the other side of the world."""
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": count},
            timeout=6,
        )
        if resp.ok:
            results = resp.json().get("results") or []
            candidates = []
            for r in results:
                label = r.get("name", query)
                if r.get("admin1") and r["admin1"] != label:
                    label += f", {r['admin1']}"
                if r.get("country"):
                    label += f" ({r['country']})"
                candidates.append({
                    "lat": r["latitude"], "lon": r["longitude"], "label": label,
                    "is_uk": r.get("country_code") == "GB",
                })
            candidates.sort(key=lambda c: 0 if c["is_uk"] else 1)
            return candidates
    except requests.RequestException as e:
        print(f"[geocode] open-meteo lookup failed: {e}")
    return []


@app.route("/api/geocode")
def api_geocode():
    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400

    # A UK postcode/outcode match is unambiguous - return it directly.
    hit = try_postcodes_io(query.replace(" ", ""))
    if hit:
        lat, lon, label = hit
        return jsonify({"candidates": [{"lat": lat, "lon": lon, "label": label}]})

    # Otherwise it's a place name - could match multiple places
    # worldwide, so return several (UK first) and let the user confirm.
    candidates = try_open_meteo(query)
    if not candidates:
        return jsonify({"candidates": []}), 404
    return jsonify({"candidates": [
        {"lat": c["lat"], "lon": c["lon"], "label": c["label"]} for c in candidates
    ]})


@app.route("/")
def index():
    return render_template("index.html")


def ensure_session_candidates(lat, lon, radius_km):
    now = time.time()
    stale = (
        (now - SESSION["fetched_at"]) > 20
        or SESSION["user_lat"] != lat
        or SESSION["radius_km"] != radius_km
    )
    if stale or not SESSION["candidates"]:
        SESSION["candidates"] = core.find_candidates(lat, lon, radius_km=radius_km)
        SESSION["fetched_at"] = now
        SESSION["user_lat"] = lat
        SESSION["user_lon"] = lon
        SESSION["radius_km"] = radius_km
        SESSION["selected_index"] = None
    return SESSION["candidates"]


@app.route("/api/candidates")
def api_candidates():
    lat = float(request.args.get("lat"))
    lon = float(request.args.get("lon"))
    radius_km = float(request.args.get("radius", core.SEARCH_RADIUS_KM))
    candidates = ensure_session_candidates(lat, lon, radius_km)

    seconds_elapsed = time.time() - SESSION["fetched_at"]
    out = []
    for i, c in enumerate(candidates[:MAX_CANDIDATES_LISTED]):
        c = core.refresh_candidate(c, lat, lon, 0.0, seconds_elapsed)
        c = ensure_commercial_classification(c)
        aircraft_info = lookup_aircraft_info(c.aircraft.icao24)
        out.append({
            "index": i,
            "distance_km": round(c.distance_km, 1),
            "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
            "compass": c.compass,
            "category_hint": c.category,
            "aircraft_type": aircraft_info["type"],
        })
    return jsonify({"candidates": out})


@app.route("/api/select", methods=["POST"])
def api_select():
    body = request.get_json() or {}
    index = body.get("index")
    candidates = SESSION["candidates"]
    if index is None or not candidates or index < 0 or index >= len(candidates):
        return jsonify({"found": False}), 400

    SESSION["selected_index"] = index
    seconds_elapsed = time.time() - SESSION["fetched_at"]
    c = core.refresh_candidate(
        candidates[index], SESSION["user_lat"], SESSION["user_lon"], 0.0, seconds_elapsed
    )
    return jsonify({"found": True, **candidate_public_view(c)})


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    body = request.get_json() or {}
    guess = body.get("guess", {})
    lost_sight = body.get("lost_sight", False)

    candidates = SESSION["candidates"]
    idx = SESSION.get("selected_index")
    if idx is None or not candidates or idx >= len(candidates):
        return jsonify({"error": "no active candidate"}), 400

    c = candidates[idx]

    # The candidate's distance/bearing were computed when it was first
    # fetched - refresh them now so a slow guesser doesn't see a stale
    # distance for a plane that's since moved on.
    seconds_elapsed = time.time() - SESSION["fetched_at"]
    c = core.refresh_candidate(c, SESSION["user_lat"], SESSION["user_lon"], 0.0, seconds_elapsed)

    revealed = candidate_reveal_view(c)

    guess_result = {} if lost_sight else {
        "airline": field_match(guess.get("airline"), revealed["operator"]),
        "aircraft_type": field_match(guess.get("aircraft_type"), revealed["aircraft_type"]),
        "origin": field_match(guess.get("origin"), revealed["origin"]),
        "destination": field_match(guess.get("destination"), revealed["destination"]),
    }
    comment = make_comment(guess_result)

    history = load_history()
    entry_id = f"{c.aircraft.icao24}-{int(time.time() * 1000)}"
    history.append({
        "id": entry_id,
        "timestamp": time.time(),
        "callsign": revealed["callsign"],
        "category": revealed["category"],
        "operator": revealed["operator"],
        "aircraft_type": revealed["aircraft_type"],
        "photo_thumb": revealed["photo_thumb"],
        "origin": revealed["origin"],
        "destination": revealed["destination"],
        "guess": None if lost_sight else guess,
        "outcome": "lost_sight" if lost_sight else "guessed",
    })
    save_history(history)

    return jsonify({"revealed": revealed, "comment": comment, "guess_result": guess_result})


@app.route("/api/history")
def api_history():
    category = request.args.get("category")
    history = load_history()
    if category and category != "all":
        history = [h for h in history if h["category"] == category]
    history.sort(key=lambda h: h["timestamp"], reverse=True)
    return jsonify(history)


@app.route("/api/history/<entry_id>", methods=["DELETE"])
def api_delete_history_entry(entry_id):
    history = load_history()
    remaining = [h for h in history if h.get("id") != entry_id]
    if len(remaining) == len(history):
        return jsonify({"error": "not found"}), 404
    save_history(remaining)
    return jsonify({"deleted": entry_id})


if __name__ == "__main__":
    print(">>> plane spotter backend: v16 (fixed trace leg-filtering bug) <<<")
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=(port == 5050))