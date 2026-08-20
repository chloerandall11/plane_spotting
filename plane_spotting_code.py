""" 
Creating script to support fun plane application

Given a user's location, finds the best "overhead" aircraft candidate
from OpenSky Network data, works out which compass direction to look,
and classifies the aircraft as commercial / military / private so the
guessing UI can show the right fields.

Requires: pip install requests --break-system-packages
"""

import re 
import math 
from astropy import constants as const # for earth's radius
from dataclasses import dataclass 
from typing import Optional

opensky_api_url = "https://opensky-network.org/api/states/all"

# setting limits
min_elevation_deg = 15 # degrees above horizon
search_radius_km = 15 # distance to scan for aircraft 

# compass directions
compass_directions = [ "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# common commerical airline identifiers
#TODO should load from a real airline designator table for full scope
commercial_airline_dict = {"BAW": "British Airways", "RYR": "Ryanair", "EZY": "easyJet",
    "VIR": "Virgin Atlantic", "AFR": "Air France", "DLH": "Lufthansa",
    "UAE": "Emirates", "AAL": "American Airlines", "UAL": "United Airlines",
    "DAL": "Delta Air Lines", "KLM": "KLM", "QFA": "Qantas",
    "JBU": "JetBlue", "WZZ": "Wizz Air", "TAP": "TAP Air Portugal",}

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