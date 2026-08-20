""" 
Creating script to support fun plane application

Given a user's location, finds the best "overhead" aircraft candidate
from OpenSky Network data, works out which compass direction to look,
and classifies the aircraft as commercial / military / private so the
guessing UI can show the right fields.

Requires: 
pip install requests --break-system-packages
pip install astropy
"""

import re 
import math 
from astropy import constants as const # for earth's radius
from dataclasses import dataclass # stores data in airplane search
from typing import Optional
import requests

opensky_api_url = "https://opensky-network.org/api/states/all"

# setting limits
min_elevation_deg = 15 # degrees above horizon
search_radius_km = 15 # distance to scan for aircraft 

# compass directions
compass_directions = [ "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# common commerical airline identifiers
#TODO should load from a real airline designator table for full scope
commercial_airline_dict = {
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

# common military airline identifiers
military_airline_dict = [
    r"^RRR\d+$",       # RAF
    r"^ASCOT\d*$",     # RAF transport
    r"^NATO\d*$",
    r"^CFC\d+$",       # Canadian Forces
    r"^GAF\d+$",       # German Air Force
    r"^FAF\d+$",       # French Air Force
    r"^REACH\d*$",     # USAF airlift
    r"^RCH\d+$",       # USAF airlift (alt format)
]

@dataclass #TODO learn about dataclasses
class Aircraft:
    """Contains aircraft info"""
    icao24: str
    callsign: str
    lat: float
    lon: float
    altitude_m: Optional[float]
    velocity_ms: Optional[float]
    heading_deg: Optional[float]


@dataclass
class Candidate:
    """Contains candidate info"""
    aircraft: Aircraft
    distance_km: float
    elevation_deg: float
    bearing_deg: float
    compass: str
    category: str          # "commercial" | "military" | "private"
    operator: Optional[str]  # resolved airline/operator name, if known


def haversine_equation(latA, lonA, latB, lonB):
    """
    Calculates the shortest great-circle distance 
    between two points on a sphere using their latitudes
    and longitudes
    """
    r = const.R_earth.to("km").value # type: ignore
    p1, p2 = math.radians(latA), math.radians(latB)
    dphi = math.radians(latB - latA)
    dlambda = math.radians(lonB - lonA)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2

    haversine = 2 * r * math.asin(math.sqrt(a))

    return haversine

def direction_angle(latA, lonA, latB, lonB):
    """Direction in degrees from point A towards point B"""
    p1, p2 = math.radians(latA), math.radians(latB)
    dlambda = math.radians(lonB - lonA)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    dir_ang =  (math.degrees(math.atan2(x, y)) + 360) % 360

    return dir_ang

def angle_to_compass(dir_ang):
    """Converts angle to compass direction"""
    dict_id = round(dir_ang/22.5) % 16 #TODO why?
    return compass_directions[dict_id]

def elevation_angle_deg(altitude_m, user_altitude_m, ground_distance_km):
    """Elevation angle between point A and B"""
    ground_distance_m = 1000 * ground_distance_km
    height_diff_m = altitude_m - user_altitude_m
    if ground_distance_m == 0:
        return 90
    
    elevation_angle = math.degrees(math.atan2(height_diff_m, ground_distance_m))
    return elevation_angle

def extrapolate_position(aircraft: Aircraft, seconds_elapsed: float):
    """FROM CLAUDE - Nudge a stale position forward using velocity + heading, so the
    'look NNE' instruction doesn't go stale between polls."""
    if not aircraft.velocity_ms or not aircraft.heading_deg:
        return aircraft.lat, aircraft.lon
    distance_m = aircraft.velocity_ms * seconds_elapsed
    heading_rad = math.radians(aircraft.heading_deg)
    r = const.R_earth.value # type: ignore # units: m
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
    """Classifies aircraft identified""" # TODO add all proper syntax and docs for this
    cs = callsign.strip().upper()
    for aircraft in military_airline_dict:
        if re.match(aircraft, cs):
            return "military", None

    airline_tag = cs[:3]
    if airline_tag in commercial_airline_dict:
        return "commercial", commercial_airline_dict[airline_tag]

    return "private", None # need to update commercial list

def fetch_nearby_aircraft(user_lat, user_lon, radius_km=search_radius_km):
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
    resp = requests.get(opensky_api_url, params=params, timeout=10)
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


def find_candidates(user_lat, user_lon, user_altitude_m=0.0, seconds_since_fetch=0.0):
    """Returns candidates sorted best-first (easiest realistic spot first)."""
    raw_aircraft = fetch_nearby_aircraft(user_lat, user_lon)
    candidates = []

    for ac in raw_aircraft:
        if ac.altitude_m is None:
            continue

        lat, lon = extrapolate_position(ac, seconds_since_fetch)
        distance_km = haversine_equation(user_lat, user_lon, lat, lon)
        elevation = elevation_angle_deg(ac.altitude_m, user_altitude_m, distance_km)

        if elevation < min_elevation_deg:
            continue

        bearing = direction_angle(user_lat, user_lon, lat, lon)
        category, operator = classify_aircraft(ac.callsign)

        candidates.append(Candidate(
            aircraft=ac, distance_km=distance_km, elevation_deg=elevation,
            bearing_deg=bearing, compass=angle_to_compass(bearing),
            category=category, operator=operator,
        ))

    # Prefer closer + lower altitude (easier real-world spot), then
    # higher elevation angle (more overhead) as a tiebreak.
    candidates.sort(key=lambda c: (c.distance_km + c.aircraft.altitude_m / 1000, -c.elevation_deg))
    return candidates


if __name__ == "__main__":
    # Example: London
    user_lat, user_lon = 51.4751, -0.1131

    candidates = find_candidates(user_lat, user_lon)
    if not candidates:
        print("Nothing overhead right now - try again in a bit.")
    else:
        best = candidates[0]
        print(f"Look {best.compass}, roughly {best.distance_km:.0f}km away, "
              f"altitude {best.aircraft.altitude_m:.0f}m, category: {best.category}")