"""
Plane spotting app - core logic

Given a user's location, finds the best "overhead" aircraft candidate
from OpenSky Network data, works out which compass direction to look,
and classifies the aircraft as commercial / military / private so the
guessing UI can show the right fields.

Requires: pip install requests --break-system-packages
"""

import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"

# Minimum elevation angle (degrees above horizon) to bother suggesting
# a plane - anything lower is hard to spot and easy to lose in clutter.
MIN_ELEVATION_DEG = 15.0

# How far out to even consider aircraft, in km. Kept tight so results
# are things you can plausibly make out with the naked eye.
SEARCH_RADIUS_KM = 15.0

# Hard cutoff: candidates further than this are dropped entirely,
# regardless of altitude/elevation.
MAX_DISTANCE_KM = 15.0

COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

# A short list of known 3-letter ICAO airline prefixes used for the
# commercial-vs-not check. In a real build, load this from a full
# ICAO airline designator table (a few thousand rows) instead.
KNOWN_AIRLINE_PREFIXES = {
    "BAW": "British Airways", "RYR": "Ryanair", "EZY": "easyJet",
    "VIR": "Virgin Atlantic", "AFR": "Air France", "DLH": "Lufthansa",
    "UAE": "Emirates", "AAL": "American Airlines", "UAL": "United Airlines",
    "DAL": "Delta Air Lines", "KLM": "KLM", "QFA": "Qantas",
    "JBU": "JetBlue", "WZZ": "Wizz Air", "TAP": "TAP Air Portugal",
    "EIN": "Aer Lingus", "NOZ": "Norwegian", "TOM": "TUI Airways",
    "EXS": "Jet2", "THY": "Turkish Airlines", "QTR": "Qatar Airways",
    "IBE": "Iberia", "SAS": "SAS", "SWR": "Swiss", "AUA": "Austrian Airlines",
    "FIN": "Finnair", "ITY": "ITA Airways", "LOT": "LOT Polish Airlines",
    "ACA": "Air Canada", "CPA": "Cathay Pacific", "SIA": "Singapore Airlines",
    "ANA": "All Nippon Airways", "JAL": "Japan Airlines", "ETD": "Etihad Airways",
}

# Military callsign patterns vary a lot by country/branch. This is a
# starter set - extend as you find more real-world examples.
MILITARY_CALLSIGN_PATTERNS = [
    r"^RRR\d+$",       # RAF
    r"^ASCOT\d*$",     # RAF transport
    r"^NATO\d*$",
    r"^CFC\d+$",       # Canadian Forces
    r"^GAF\d+$",       # German Air Force
    r"^FAF\d+$",       # French Air Force
    r"^REACH\d*$",     # USAF airlift
    r"^RCH\d+$",       # USAF airlift (alt format)
]


@dataclass
class Aircraft:
    icao24: str
    callsign: str
    lat: float
    lon: float
    altitude_m: Optional[float]
    velocity_ms: Optional[float]
    heading_deg: Optional[float]


@dataclass
class Candidate:
    aircraft: Aircraft
    distance_km: float
    elevation_deg: float
    bearing_deg: float
    compass: str
    category: str          # "commercial" | "military" | "private"
    operator: Optional[str]  # resolved airline/operator name, if known


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_to_compass(bearing):
    idx = round(bearing / 22.5) % 16
    return COMPASS_POINTS[idx]


def elevation_angle_deg(altitude_m, user_altitude_m, ground_distance_km):
    height_diff_m = altitude_m - user_altitude_m
    ground_distance_m = ground_distance_km * 1000
    if ground_distance_m == 0:
        return 90.0
    return math.degrees(math.atan2(height_diff_m, ground_distance_m))


def extrapolate_position(aircraft: Aircraft, seconds_elapsed: float):
    """Nudge a stale position forward using velocity + heading, so the
    'look NNE' instruction doesn't go stale between polls."""
    if not aircraft.velocity_ms or not aircraft.heading_deg:
        return aircraft.lat, aircraft.lon
    distance_m = aircraft.velocity_ms * seconds_elapsed
    heading_rad = math.radians(aircraft.heading_deg)
    r = 6371000.0
    lat1 = math.radians(aircraft.lat)
    lon1 = math.radians(aircraft.lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_m / r)
        + math.cos(lat1) * math.sin(distance_m / r) * math.cos(heading_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(heading_rad) * math.sin(distance_m / r) * math.cos(lat1),
        math.cos(distance_m / r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def classify_aircraft(callsign: str):
    """Returns (category, operator_name_or_None)."""
    cs = callsign.strip().upper()

    for pattern in MILITARY_CALLSIGN_PATTERNS:
        if re.match(pattern, cs):
            return "military", None

    prefix = cs[:3]
    if prefix in KNOWN_AIRLINE_PREFIXES:
        return "commercial", KNOWN_AIRLINE_PREFIXES[prefix]

    return "private", None


def fetch_nearby_aircraft(user_lat, user_lon, radius_km=SEARCH_RADIUS_KM):
    """Queries OpenSky's bounding-box endpoint. No auth needed for the
    public /states/all endpoint at low request rates, but for anything
    beyond casual personal use, set up OAuth2 client credentials per
    OpenSky's docs and pass a bearer token here."""
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(user_lat)))

    params = {
        "lamin": user_lat - lat_delta,
        "lamax": user_lat + lat_delta,
        "lomin": user_lon - lon_delta,
        "lomax": user_lon + lon_delta,
    }
    resp = requests.get(OPENSKY_STATES_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    aircraft = []
    for state in data.get("states", []) or []:
        icao24, callsign = state[0], (state[1] or "").strip()
        lon, lat, baro_alt = state[5], state[6], state[7]
        velocity, heading = state[9], state[10]
        if lat is None or lon is None or not callsign:
            continue
        aircraft.append(Aircraft(
            icao24=icao24, callsign=callsign, lat=lat, lon=lon,
            altitude_m=baro_alt, velocity_ms=velocity, heading_deg=heading,
        ))
    return aircraft


def refresh_candidate(candidate: Candidate, user_lat, user_lon, user_altitude_m, seconds_elapsed):
    """Recomputes distance/elevation/bearing for a candidate using its
    extrapolated position at 'seconds_elapsed' since it was first
    fetched, rather than the stale snapshot from find_candidates().
    Mutates and returns the same candidate."""
    lat, lon = extrapolate_position(candidate.aircraft, seconds_elapsed)
    distance_km = haversine_km(user_lat, user_lon, lat, lon)
    elevation = elevation_angle_deg(candidate.aircraft.altitude_m, user_altitude_m, distance_km)
    bearing = bearing_deg(user_lat, user_lon, lat, lon)

    candidate.distance_km = distance_km
    candidate.elevation_deg = elevation
    candidate.bearing_deg = bearing
    candidate.compass = bearing_to_compass(bearing)
    return candidate


def find_candidates(user_lat, user_lon, user_altitude_m=0.0, seconds_since_fetch=0.0, radius_km=None):
    """Returns candidates sorted best-first (easiest realistic spot first).
    radius_km overrides the default search/cutoff distance for this call."""
    radius_km = radius_km or SEARCH_RADIUS_KM
    raw_aircraft = fetch_nearby_aircraft(user_lat, user_lon, radius_km=radius_km)
    candidates = []

    for ac in raw_aircraft:
        if ac.altitude_m is None:
            continue

        lat, lon = extrapolate_position(ac, seconds_since_fetch)
        distance_km = haversine_km(user_lat, user_lon, lat, lon)
        elevation = elevation_angle_deg(ac.altitude_m, user_altitude_m, distance_km)

        if elevation < MIN_ELEVATION_DEG:
            continue
        if distance_km > radius_km:
            continue

        bearing = bearing_deg(user_lat, user_lon, lat, lon)
        category, operator = classify_aircraft(ac.callsign)

        candidates.append(Candidate(
            aircraft=ac, distance_km=distance_km, elevation_deg=elevation,
            bearing_deg=bearing, compass=bearing_to_compass(bearing),
            category=category, operator=operator,
        ))

    # Distance is now the primary sort key (closest first). Altitude
    # only breaks ties between similarly-close planes.
    candidates.sort(key=lambda c: (c.distance_km, c.aircraft.altitude_m))
    return candidates


if __name__ == "__main__":
    # Example: London
    user_lat, user_lon = 51.5074, -0.1278

    candidates = find_candidates(user_lat, user_lon)
    if not candidates:
        print("Nothing overhead right now - try again in a bit.")
    else:
        best = candidates[0]
        print(f"Look {best.compass}, roughly {best.distance_km:.0f}km away, "
              f"altitude {best.aircraft.altitude_m:.0f}m, category: {best.category}")
