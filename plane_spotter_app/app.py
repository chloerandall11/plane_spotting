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

from flask import Flask, jsonify, request, render_template

import core

app = Flask(__name__)

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.json")

# Single-user, in-memory session state: the last fetched candidate
# list and where we are in it. Fine for one person using this on
# their own phone - not built for multiple concurrent users.
SESSION = {
    "candidates": [],
    "index": 0,
    "fetched_at": 0.0,
    "user_lat": None,
    "user_lon": None,
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


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r") as f:
        return json.load(f)


def save_history(entries):
    with open(HISTORY_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def candidate_public_view(c: "core.Candidate"):
    """What the client is allowed to see BEFORE revealing - no
    callsign/airline, just enough to go find it in the sky."""
    return {
        "compass": c.compass,
        "distance_km": round(c.distance_km, 1),
        "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
        "category_hint": c.category,  # fine to hint commercial/military/private
    }


def candidate_reveal_view(c: "core.Candidate"):
    return {
        "callsign": c.aircraft.callsign,
        "icao24": c.aircraft.icao24,
        "category": c.category,
        "operator": c.operator,
        "altitude_ft": round(c.aircraft.altitude_m * 3.28084),
        "distance_km": round(c.distance_km, 1),
    }


def make_comment(category, guess, revealed):
    guess = (guess or {})
    if not guess or not any(guess.values()):
        return random.choice(CASUAL_COMMENTS["no_guess"])

    if category == "commercial":
        guessed_airline = (guess.get("airline") or "").strip().lower()
        actual_operator = (revealed.get("operator") or "").strip().lower()
        if guessed_airline and actual_operator and guessed_airline in actual_operator:
            return random.choice(CASUAL_COMMENTS["correct"])
        return random.choice(CASUAL_COMMENTS["wrong"])

    # military / private: no ground truth to compare against easily,
    # so keep it light rather than pretending to grade it.
    return random.choice(CASUAL_COMMENTS["partial"])


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/next")
def api_next():
    lat = float(request.args.get("lat"))
    lon = float(request.args.get("lon"))
    now = time.time()

    stale = (now - SESSION["fetched_at"]) > 20 or SESSION["user_lat"] != lat
    if stale or not SESSION["candidates"]:
        SESSION["candidates"] = core.find_candidates(lat, lon)
        SESSION["index"] = 0
        SESSION["fetched_at"] = now
        SESSION["user_lat"] = lat
        SESSION["user_lon"] = lon

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
    history.append({
        "timestamp": time.time(),
        "callsign": revealed["callsign"],
        "category": revealed["category"],
        "operator": revealed["operator"],
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)