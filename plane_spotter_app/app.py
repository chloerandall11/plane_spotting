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

# Single-user, in-memory session state: the last fetched candidate
# list and where we are in it. Fine for one person using this on
# their own phone - not built for multiple concurrent users.
SESSION = {
    "candidates": [],
    "index": 0,
    "fetched_at": 0.0,
    "user_lat": None,
    "user_lon": None,
    "radius_km": core.SEARCH_RADIUS_KM,
}

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


def lookup_route(callsign):
    """Returns {'origin': str, 'destination': str} or None if unknown.
    Each value is formatted as 'Airport Name (Country)'. Only reliable
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
                result = {
                    "origin": fmt_airport(route.get("origin")),
                    "destination": fmt_airport(route.get("destination")),
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


def candidate_public_view(c: "core.Candidate"):
    """What the client is allowed to see BEFORE revealing - no
    callsign/airline, just enough to go find it in the sky."""
    return {
        "compass": c.compass,
        "bearing_deg": round(c.bearing_deg),
        "distance_km": round(c.distance_km, 1),
        "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
        "category_hint": c.category,  # fine to hint commercial/military/private
    }


def candidate_reveal_view(c: "core.Candidate"):
    speed_mph = round(c.aircraft.velocity_ms * 2.23694) if c.aircraft.velocity_ms else None
    heading_deg = round(c.aircraft.heading_deg) if c.aircraft.heading_deg is not None else None
    aircraft_info = lookup_aircraft_info(c.aircraft.icao24)
    route = lookup_route(c.aircraft.callsign) if c.category == "commercial" else None
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
    """Handles UK postcodes: full ('SW1A1AA') or partial/outcode ('TR8')."""
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

    return None


def try_open_meteo(query):
    """General place-name/city geocoding. Free, no key, and - unlike
    Nominatim - doesn't block requests from cloud-hosting IPs like
    Render's, which is what most free hosts run on."""
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": 1},
            timeout=6,
        )
        if resp.ok:
            results = resp.json().get("results")
            if results:
                r = results[0]
                label = r.get("name", query)
                if r.get("country"):
                    label = f"{label}, {r['country']}"
                return r["latitude"], r["longitude"], label
    except requests.RequestException as e:
        print(f"[geocode] open-meteo lookup failed: {e}")
    return None


@app.route("/api/geocode")
def api_geocode():
    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400

    hit = try_postcodes_io(query.replace(" ", ""))
    source = "postcodes.io"
    if not hit:
        hit = try_open_meteo(query)
        source = "open-meteo"

    if not hit:
        return jsonify({"error": "not found"}), 404

    lat, lon, label = hit
    return jsonify({"lat": lat, "lon": lon, "label": label, "source": source})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/next")
def api_next():
    lat = float(request.args.get("lat"))
    lon = float(request.args.get("lon"))
    radius_km = float(request.args.get("radius", core.SEARCH_RADIUS_KM))
    now = time.time()

    stale = (
        (now - SESSION["fetched_at"]) > 20
        or SESSION["user_lat"] != lat
        or SESSION["radius_km"] != radius_km
    )
    if stale or not SESSION["candidates"]:
        SESSION["candidates"] = core.find_candidates(lat, lon, radius_km=radius_km)
        SESSION["index"] = 0
        SESSION["fetched_at"] = now
        SESSION["user_lat"] = lat
        SESSION["user_lon"] = lon
        SESSION["radius_km"] = radius_km

    candidates = SESSION["candidates"]
    if not candidates:
        return jsonify({"found": False})

    idx = min(SESSION["index"], len(candidates) - 1)
    c = core.refresh_candidate(candidates[idx], lat, lon, 0.0, now - SESSION["fetched_at"])
    return jsonify({"found": True, **candidate_public_view(c)})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    SESSION["index"] += 1
    candidates = SESSION["candidates"]
    if not candidates or SESSION["index"] >= len(candidates):
        return jsonify({"found": False})
    c = core.refresh_candidate(
        candidates[SESSION["index"]], SESSION["user_lat"], SESSION["user_lon"],
        0.0, time.time() - SESSION["fetched_at"],
    )
    return jsonify({"found": True, **candidate_public_view(c)})


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    body = request.get_json() or {}
    guess = body.get("guess", {})
    lost_sight = body.get("lost_sight", False)

    candidates = SESSION["candidates"]
    idx = min(SESSION["index"], len(candidates) - 1) if candidates else None
    if idx is None:
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
    print(">>> plane spotter backend: v8 (fixed geocoding - postcodes.io outcodes + open-meteo) <<<")
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=(port == 5050))