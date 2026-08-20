# Plane Spotter

A little personal game: it tells you roughly where to look in the sky for
the nearest plane, you guess the airline/route before it reveals the
answer, and it keeps a log of everything you've spotted — with military
and private aircraft called out separately.

Built as a local web app — runs on your own laptop, you open it on your
phone's browser over WiFi. No app store, no build step.

## What it does

1. **Spot** — using your phone's location, it finds the closest real
   aircraft overhead (via live [OpenSky Network](https://opensky-network.org/)
   data) and shows you roughly how far, how high, and which direction to
   look — with a compass needle pointing at it.
2. **Guess** — before revealing anything, guess the airline, origin, and
   destination (commercial flights only — military/private show
   different fields, since route data isn't public for those).
3. **Reveal** — see the real callsign, operator, and a casual comment on
   your guess. No scoring, no pressure — just for fun.
4. **Log** — a running history of everything you've spotted, filterable
   by commercial / military / private.

## Requirements

- Python 3.9+ (older versions like the 3.7 bundled with some Anaconda
  installs may hit dependency issues — if you run into odd errors,
  check your Python version with `python --version` first)
- A phone and laptop on the **same WiFi network**

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
   > uses port `5050` instead to dodge that, so this shouldn't come up,
   > but if it does with 5050 too, just change the port number at the
   > bottom of `app.py`.

2. On your **laptop**, test it first at:
   ```
   http://127.0.0.1:5050
   ```
   (Not `0.0.0.0:5050` — that's just Flask saying "listening everywhere,"
   not an actual address to visit.)

3. Find your laptop's local network address:
   - Mac/Linux: `ifconfig` or `ip addr` — look for something like `192.168.x.x`
   - Windows: `ipconfig`

4. On your **phone**, connect to the same WiFi, then open:
   ```
   http://<your-laptop's-192.168.x.x-address>:5050
   ```

5. Allow location access when prompted.

## Location access troubleshooting

- **Browsers block GPS access on plain `http://` addresses** except for
  `localhost`/`127.0.0.1`. If location fails specifically when testing
  on your *phone* (not your laptop), this is almost always why. Fix:
  use [ngrok](https://ngrok.com/download) to get a temporary `https://`
  tunnel — run `ngrok http 5050` alongside the app and open the
  `https://...ngrok-free.app` link it gives you on your phone instead.

- **If location fails on both laptop and phone**, check your OS-level
  location permissions:
  - macOS: System Settings → Privacy & Security → Location Services —
    make sure it's on, and your browser is checked in the app list.
  - Also check the browser's own per-site permission (the padlock/site
    info icon next to the address bar) hasn't got the site blocked.

## How the "look direction" works

The app doesn't use your phone's compass — it just tells you a rough
compass direction (like "look toward NNE") based on real bearing math
from your GPS position to the aircraft's position. Distance and altitude
come straight from the aircraft's live ADS-B data.

## Notes on data limits

- Uses OpenSky's free public endpoint, which is rate-limited — if you
  spot in quick succession, occasional slow responses are expected.
- Airline/military recognition is based on a small starter list of
  callsign prefixes in `core.py` — it'll misclassify anything it
  doesn't recognize as "private." Worth expanding
  `KNOWN_AIRLINE_PREFIXES` and `MILITARY_CALLSIGN_PATTERNS` as you spot
  more real traffic in your area.
- Origin/destination guessing is only offered for flights it
  classifies as commercial, since that route data isn't reliably
  available for military/private aircraft without a paid API.