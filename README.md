# Plane Spotter

A little personal game: it tells you roughly where to look in the sky for
the nearest plane — and, if your phone's compass is on, gives you a live
arrow that lights up once you're actually facing it. You guess the
airline, aircraft type, and route before it reveals the answer, then it
shows you a full breakdown (including a photo, when one's available) and
logs the spot.

Built as a local web app — runs on your own laptop, you open it on your
phone's browser over WiFi. No app store, no build step.

## What it does

1. **Spot** — using your phone's location, it finds the closest real
   aircraft overhead (via live [OpenSky Network](https://opensky-network.org/)
   data) and shows how far, how high, and which direction to look.
   You can adjust the search radius (2–60km) with the slider at the top.
2. **Live compass (optional)** — tap "enable compass" and the dial shows
   your phone's actual heading against the plane's true bearing. Turn
   your body until they line up and the arrow glows green — "facing it
   now."
3. **Guess** — before revealing anything, guess the airline, aircraft
   type, and (for commercial flights) origin/destination, using
   searchable dropdowns.
4. **Reveal** — a full breakdown: callsign, operator, aircraft type,
   route, altitude, speed, heading, and a photo of the actual aircraft
   when one exists in the community database (falls back to a generic
   silhouette otherwise). Plus a casual comment on how your guess did.
   No scoring, no pressure — just for fun.
5. **Log** — a running history of everything you've spotted, filterable
   by commercial / military / private, with a delete button on each
   entry if you want to clear one out.

## Requirements

- Python 3.9+ (older versions like the 3.7 bundled with some Anaconda
  installs may hit dependency issues — if you run into odd errors,
  check your Python version with `python --version` first)
- A phone and laptop on the **same WiFi network**
- [ngrok](https://ngrok.com) (free) — needed for location and compass
  access to work on your phone, see below

## Setup

1. Clone this repo and check out the `web-interface` branch:
   ```bash
   git clone https://github.com/chloerandall11/plane_spotting.git
   cd plane_spotting
   git checkout web-interface
   ```

2. Install dependencies:
   ```bash
   pip install flask requests --break-system-packages
   ```

3. Make sure your folder looks like this:
   ```
   plane_spotter_app/
   ├── app.py
   ├── core.py
   └── templates/
       └── index.html
   ```

## Running it

1. Start the server:
   ```bash
   cd plane_spotter_app
   python app.py
   ```
   You should see something like `Running on http://0.0.0.0:5050`.

   > **macOS note:** if you get `OSError: Address already in use`, it's
   > usually AirPlay Receiver squatting on port 5000 — this app already
   > uses port `5050` instead to dodge that. If it happens on 5050 too,
   > change the port number at the bottom of `app.py`.

2. On your **laptop**, you can sanity-check it works at:
   ```
   http://127.0.0.1:5050
   ```
   (Not `0.0.0.0:5050` — that's just Flask saying "listening everywhere,"
   not an actual address to visit.) Location may work here too since
   `127.0.0.1` counts as secure, but for real use you'll want your phone.

3. **Set up ngrok** so your phone can reach it securely (see next
   section) — this is required for location and compass access to work
   properly on a phone.

## Setting up ngrok (one-time)

Phones (and browsers generally) block GPS and compass access on plain
`http://` addresses that aren't `localhost`. ngrok gives your local
server a temporary `https://` address your phone can use instead.

1. **Create a free account**: go to
   [ngrok.com/signup](https://dashboard.ngrok.com/signup).
2. **Get your auth token**: once logged in, go to
   [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)
   and copy the token shown there.
3. **Install ngrok**: [ngrok.com/download](https://ngrok.com/download) —
   on a Mac with Homebrew, `brew install ngrok` is easiest.
4. **Add your token** (one-time):
   ```bash
   ngrok config add-authtoken YOUR_TOKEN_HERE
   ```
5. **Every time you want to use the app on your phone**: with the Flask
   app already running in one terminal, open a **second terminal** and
   run:
   ```bash
   ngrok http 5050
   ```
   It'll print something like:
   ```
   Forwarding   https://a1b2-c3d4.ngrok-free.app -> http://localhost:5050
   ```
   Open that `https://...ngrok-free.app` link on your phone. On the free
   tier this address changes each time you restart ngrok, so you'll
   re-copy it each session.

6. Allow location access when prompted, and tap "enable compass" on the
   Spot screen for the live pointing arrow.

## Location / compass troubleshooting

- **Fails only on phone, works on laptop** — you're probably using the
  plain `http://192.168.x.x` address instead of the ngrok `https://`
  link. Switch to ngrok (above).
- **Fails on both laptop and phone** — check OS-level permissions:
  - macOS: System Settings → Privacy & Security → Location Services —
    make sure it's on, and your browser is checked in the app list.
  - Also check the browser's own per-site permission (the padlock/site
    info icon next to the address bar) hasn't got the site blocked.
- **Compass button does nothing on iPhone** — iOS requires the
  permission prompt to come from a real tap (not auto-run), which the
  "enable compass" button already handles — but it only appears/works
  over `https://`, same requirement as location.
- **Compass heading feels off by a consistent amount** — Android's
  heading calculation is approximate in this app; if it's a stable
  offset for your phone, it can be calibrated out, but that's not
  built in yet.

## Managing your log

Each spot in the Log tab has a small `×` button to delete it individually.
There's no bulk-clear built in — delete one at a time, or just delete
`history.json` (next to `app.py`) to wipe the whole log and start fresh.

## Notes on data limits

- **OpenSky** (live positions) is free but rate-limited — if you spot in
  quick succession, occasional slow responses are expected.
- **Airline/military recognition** is based on a small starter list of
  callsign prefixes in `core.py` — it'll misclassify anything it doesn't
  recognize as "private." Worth expanding `KNOWN_AIRLINE_PREFIXES` and
  `MILITARY_CALLSIGN_PATTERNS` as you spot more real traffic in your area.
- **Aircraft type, photos, and routes** come from
  [adsbdb.com](https://www.adsbdb.com/), a free community-maintained
  database — coverage is good but not complete. A missing photo or route
  just means that particular aircraft/flight isn't in their records yet,
  not a bug.
- **Origin/destination guessing** is only offered for flights classified
  as commercial, since reliable route data isn't available for
  military/private aircraft without a paid API.