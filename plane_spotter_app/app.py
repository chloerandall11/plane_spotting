"""
Plane spotter - Flask backend.

Run with:
    python app.py

Then open http://<your-computer's-LAN-IP>:5000 on your phone, as long
as your phone is on the same WiFi as this computer. Find your LAN IP
with `ipconfig` (Windows) or `ifconfig` / `ip addr` (Mac/Linux) -
look for something like 192.168.x.x.
"""

import json
import os
import random
import time

import requests
from flask import Flask, jsonify, request, render_template

import core

app = Flask(__name__)

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.json")
ADSBDB_BASE = "https://api.adsbdb.com/v0"

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


def make_comment(category, guess, revealed):
    guess = (guess or {})
    if not guess or not any(guess.values()):
        return random.choice(CASUAL_COMMENTS["no_guess"])

    hits, attempts = 0, 0

    guessed_airline = (guess.get("airline") or "").strip().lower()
    if guessed_airline:
        attempts += 1
        actual = (revealed.get("operator") or "").strip().lower()
        if actual and guessed_airline in actual:
            hits += 1

    guessed_type = (guess.get("aircraft_type") or "").strip().lower()
    if guessed_type:
        attempts += 1
        actual = (revealed.get("aircraft_type") or "").strip().lower()
        if actual and (guessed_type in actual or actual in guessed_type):
            hits += 1

    if category == "commercial":
        guessed_origin = (guess.get("origin") or "").strip().lower()
        if guessed_origin:
            attempts += 1
            actual = (revealed.get("origin") or "").strip().lower()
            if actual and guessed_origin in actual:
                hits += 1

        guessed_dest = (guess.get("destination") or "").strip().lower()
        if guessed_dest:
            attempts += 1
            actual = (revealed.get("destination") or "").strip().lower()
            if actual and guessed_dest in actual:
                hits += 1

    if attempts == 0:
        return random.choice(CASUAL_COMMENTS["no_guess"])
    ratio = hits / attempts
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
    airports = [
        "London Heathrow (LHR)", "London Gatwick (LGW)", "London Stansted (STN)",
        "London Luton (LTN)", "London City (LCY)", "Manchester (MAN)",
        "Liverpool (LPL)", "Leeds Bradford (LBA)", "Birmingham (BHX)",
        "Edinburgh (EDI)", "Glasgow (GLA)", "Bristol (BRS)", "Newcastle (NCL)",
        "Belfast International (BFS)", "Dublin (DUB)", "Amsterdam Schiphol (AMS)",
        "Paris Charles de Gaulle (CDG)", "Paris Orly (ORY)", "Frankfurt (FRA)",
        "Munich (MUC)", "Madrid (MAD)", "Barcelona (BCN)", "Rome Fiumicino (FCO)",
        "Milan Malpensa (MXP)", "Zurich (ZRH)", "Vienna (VIE)", "Brussels (BRU)",
        "Copenhagen (CPH)", "Stockholm Arlanda (ARN)", "Oslo (OSL)",
        "Lisbon (LIS)", "Prague (PRG)", "Warsaw (WAW)", "Athens (ATH)",
        "Istanbul (IST)", "Doha (DOH)", "Dubai (DXB)", "Abu Dhabi (AUH)",
        "New York JFK (JFK)", "Newark (EWR)", "Los Angeles (LAX)",
        "Chicago O'Hare (ORD)", "Toronto Pearson (YYZ)", "Singapore Changi (SIN)",
        "Hong Kong (HKG)", "Tokyo Narita (NRT)", "Tokyo Haneda (HND)",
        "Sydney (SYD)",
    ]
    return jsonify({"airlines": airlines, "airports": airports, "aircraft_types": aircraft_types})


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
    c = candidates[idx]
    return jsonify({"found": True, **candidate_public_view(c)})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    SESSION["index"] += 1
    candidates = SESSION["candidates"]
    if not candidates or SESSION["index"] >= len(candidates):
        return jsonify({"found": False})
    c = candidates[SESSION["index"]]
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
    revealed = candidate_reveal_view(c)
    comment = make_comment(c.category, None if lost_sight else guess, revealed)

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

    return jsonify({"revealed": revealed, "comment": comment})


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
    print(">>> plane spotter backend: v5 (route with country, full table) <<<")
    app.run(host="0.0.0.0", port=5050, debug=True)
