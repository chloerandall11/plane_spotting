# Plane Spotter

**Live app:** [plane-spotting.onrender.com](https://plane-spotting.onrender.com)

> Built with [Claude](https://claude.com) (Anthropic) — I designed and
> directed the app, and worked with Claude to write the code, debug
> issues, and figure out the free APIs/hosting to use. Flagging this
> upfront since it's how the whole thing came together.

A personal game: it tells you roughly where to look in the sky for the
nearest plane — with a live compass arrow that lights up once you're
actually facing it — you guess the airline, aircraft type, and route
before it reveals the answer, then it shows a full breakdown (photo
included, when one exists) and logs the spot.

Hosted for free on Render, installable to your phone's home screen as a
proper app icon (PWA). No app store, no laptop needed to run it.

## Quick start

1. Open **[plane-spotting.onrender.com](https://plane-spotting.onrender.com)** on your phone.
2. Tap **"Add to Home Screen"** (iOS Safari) or accept the **install
   prompt** (Android Chrome) — it'll sit on your home screen with its
   own icon and open full-screen, like a real app.
3. Set your radius and location (GPS or postcode), tap **start
   scanning**, and go find some planes.

> **First open after a while may take ~30-60 seconds.** The free
> hosting tier sleeps after 15 minutes of inactivity and needs to wake
> up on your next visit — this is normal, not a bug.

## What it does

1. **Spot** — finds the closest real aircraft near you and shows how
   far, how high, and which direction to look. Adjustable radius
   (2–60km). Nothing scans until you tap **start scanning** — changing
   radius/location beforehand won't trigger anything on its own.
2. **Location** — uses your phone's GPS by default, or switch to a UK
   postcode / place name any time via the "change" link if GPS isn't
   available or you want to check a different spot.
3. **Live compass** — tap "enable compass" and a dial shows your
   phone's actual heading against the plane's true bearing. Line them
   up and the arrow glows green.
4. **Guess** — airline, aircraft type, and (for commercial flights)
   origin/destination, via searchable dropdowns. Free-typing also
   works if nothing matches.
5. **Reveal** — full breakdown: callsign, operator, aircraft type,
   route (with country), altitude, live-recalculated distance/speed/
   heading, and a real photo when one exists (falls back to a generic
   silhouette otherwise). Each guessed field is highlighted green
   (correct) or red (wrong) in the table, alongside a casual comment.
   No scoring, no pressure — just for fun.
6. **Log** — history of everything spotted, filterable by commercial /
   military / private, with a delete button per entry.

## How it works (architecture)

Everything free, no API keys required anywhere:

| Purpose | Service | Notes |
|---|---|---|
| Live aircraft positions | [adsb.lol](https://api.adsb.lol) | Point+radius query. Used instead of OpenSky, which blocks traffic from cloud-hosting IPs (their own docs say so) — that breaks on free hosts like Render. |
| Aircraft type, photo, route | [adsbdb.com](https://www.adsbdb.com/) | Keyed by the aircraft's Mode S / ICAO24 address (type + photo) and by callsign (route). Community-maintained, so coverage varies. |
| Airline / operator recognition | adsbdb's airline database | Looks up the callsign's 3-letter ICAO prefix against adsbdb's full airline list — covers essentially all active carriers (mainline, regional, low-cost subsidiaries), not just a hand-picked shortlist. A tiny local list is checked first as an instant, no-network fallback. |
| Military detection | Local regex patterns | Callsign patterns (RRR, ASCOT, REACH, etc.) in `core.py` — extend `MILITARY_CALLSIGN_PATTERNS` as you spot more. |
| UK postcode → coordinates | [postcodes.io](https://postcodes.io) | Handles full postcodes and partial "outcodes" (e.g. "TR8"). |
| Place name → coordinates | [Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api) | Fallback when it's not a UK postcode. Chosen over Nominatim/OSM, which also blocks cloud-hosting IPs. |
| Airport list (guess dropdown) | [OurAirports](https://ourairports.com/data/) | Every airport worldwide with an IATA code (~9,000). Downloaded once and cached to `airports_cache.json` on first request. |

**Distance/bearing/elevation math** (`core.py`) is all local — haversine
distance, great-circle bearing, and an elevation-angle calculation
using the aircraft's altitude and ground distance. A plane's position
is re-extrapolated (using its live speed/heading) both while you're
deciding and again right before reveal, so a slow guesser doesn't see
a stale distance for a plane that's since moved on.

**Categorization flow**: military check (local) → local airline list
(instant) → adsbdb airline database (comprehensive, cached per airline
code so it's only ever queried once per carrier) → falls back to
"private" if nothing matches.

## Managing your log

Each entry in the Log tab has a `×` to delete it individually. There's
no bulk-clear built in.

> **Note on Render's free tier**: there's no persistent disk, so
> `history.json` (and the airports cache) reset whenever the service
> sleeps/wakes or redeploys. Your log won't survive indefinitely on
> the free tier — that trade-off was chosen deliberately to keep
> hosting free and simple.

## Development / running locally

You don't need this for normal use — the live site above works
standalone. This is only for making further changes to the app itself.

1. Clone the repo and check out the **`app-creation`** branch:
   ```bash
   git clone https://github.com/chloerandall11/plane_spotting.git
   cd plane_spotting
   git checkout app-creation
   ```
2. Install dependencies:
   ```bash
   cd plane_spotter_app
   pip install -r requirements.txt --break-system-packages
   ```
3. Run it:
   ```bash
   python app.py
   ```
   You should see the current version marker printed, e.g.:
   ```
   >>> plane spotter backend: v10 (comprehensive airline recognition via adsbdb) <<<
   ```
4. For location/compass to work on a phone during local testing, you
   need HTTPS — either use [ngrok](https://ngrok.com) (`ngrok http
   5050` after signing up and running `ngrok config add-authtoken
   YOUR_TOKEN`), or just test in your own laptop's browser at
   `http://127.0.0.1:5050` (counts as secure/localhost).

## Deploying your own copy (Render, free)

The live site above is already deployed this way — these steps are for
reference or redeploying elsewhere:

1. Push the code (with `requirements.txt` and `Procfile` present) to a
   GitHub repo.
2. On [render.com](https://render.com), **New → Web Service**, connect
   the repo/branch.
3. Settings: **Root Directory**: `plane_spotter_app` · **Build
   Command**: `pip install -r requirements.txt` · **Start Command**:
   leave blank (uses the `Procfile`) · **Instance Type**: Free.
4. Deploy. You'll get a permanent `https://your-app.onrender.com` URL —
   real HTTPS out of the box, so location/compass work immediately, no
   ngrok needed.

## Known limitations

- **OpenSky is not used** (see architecture table) — if you ever see
  references to it in old notes, that's outdated; `adsb.lol` is the
  live data source now.
- **Free-tier cold starts**: ~30-60s wake-up after 15 minutes idle.
- **Log doesn't persist** across sleep/restart on Render's free tier.
- **Route data** (origin/destination) only shows for flights
  classified as commercial, and only when adsbdb has that callsign in
  its schedule records — not every flight will have it.
- **Photos** depend on adsbdb's community-contributed database having
  that specific aircraft registration — falls back to a generic
  silhouette when it doesn't.