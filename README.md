# Fuel Route API

A Django REST API that plans the most cost-effective fuel stops for any US road trip. Give it a start and end location, and it returns the full driving route, the cheapest gas stations to stop at along the way, and the total fuel cost for the trip.

---

## What it does

- Takes a start and end location anywhere in the USA
- Returns the full driving route as a polyline (ready to render on a map)
- Finds the cheapest fuel stops along the route, ensuring the vehicle never runs out of gas (500-mile max range)
- Calculates total gallons used and total money spent on fuel (at 10 MPG)
- Uses real OPIS truck stop pricing data (8,000+ stations across the USA)

---

## How it works

```
POST /api/route/
     │
     ├── 1. Geocode start & end locations       (Nominatim — 2 calls)
     ├── 2. Fetch full driving route             (OSRM — 1 call)
     ├── 3. Find fuel stations near the route    (in-memory, no API call)
     ├── 4. Pick cheapest stops (greedy algorithm, in-memory)
     └── 5. Return JSON with route + stops + total cost
```

**Total external API calls per request: 3** (2 geocodes + 1 routing call)

---

## Tech Stack

| | |
|---|---|
| Framework | Django 6 + Django REST Framework |
| Routing | OSRM (free, no key required) |
| Geocoding | Nominatim / OpenStreetMap (free, no key required) |
| Fuel Data | OPIS CSV (loaded once at startup, cached in memory) |
| Language | Python 3.12 |

---

## Project Structure

```
fuel_route/
├── config/
│   ├── settings.py         # Django settings + project config
│   ├── urls.py             # Root URL configuration
│   ├── wsgi.py
│   └── asgi.py
├── routing/
│   ├── views.py            # API endpoint (RouteView)
│   ├── urls.py             # URL routing for the app
│   ├── routing_service.py  # Geocoding + OSRM route fetching
│   ├── fuel_service.py     # CSV loading + stop planning algorithm
│   ├── data/
│   │   ├── fuel-prices-for-be-assessment.csv  # OPIS fuel price data
│   │   └── geocode_cache.json                 # Auto-generated on first run
│   └── management/
│       └── commands/
│           └── warm_geocache.py  # Pre-warm geocode cache command
├── manage.py
├── Pipfile
└── .env
```

---

## Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd fuel_route
```

### 2. Install dependencies

```bash
pipenv install
pipenv shell
```

### 3. Add environment variables

Create a `.env` file in the project root:

```
DJANGO_SECRET_KEY=your-secret-key-here
DEBUG=True
```

No API keys required — both Nominatim (geocoding) and OSRM (routing) are completely free with no signup.

### 4. Run migrations

```bash
python manage.py migrate
```

### 5. Pre-warm the geocode cache (recommended)

This geocodes all ~3,800 city/state pairs from the fuel CSV and saves them to disk. Only needs to run once. Future server starts load from cache instantly.

```bash
python manage.py warm_geocache
```

### 6. Start the server

```bash
python manage.py runserver
```

---

## API Reference

### `POST /api/route/`

**Request body:**
```json
{
    "start": "Chicago, IL",
    "end": "Los Angeles, CA"
}
```

**Success response (200):**
```json
{
    "route": {
        "start": "Chicago, IL",
        "end": "Los Angeles, CA",
        "start_coords": { "lat": 41.87, "lon": -87.62 },
        "end_coords": { "lat": 34.05, "lon": -118.24 },
        "total_distance_miles": 2018.1,
        "polyline": [[41.87, -87.62], [41.86, -87.80], "..."]
    },
    "fuel_stops": [
        {
            "name": "LOVES TRAVEL STOP #766",
            "address": "I-80, EXIT 27",
            "city": "Atkinson",
            "state": "IL",
            "lat": 41.40,
            "lon": -89.92,
            "price_per_gallon": 3.39,
            "route_miles": 112.4,
            "dist_from_route_miles": 2.1,
            "gallons_purchased": 50.0,
            "stop_cost_usd": 169.50
        }
    ],
    "summary": {
        "num_stops": 4,
        "total_gallons": 201.8,
        "total_fuel_cost_usd": 693.20,
        "vehicle_range_miles": 500,
        "vehicle_mpg": 10
    }
}
```

**Error responses:**

| Status | Meaning |
|---|---|
| `400` | Missing start/end, location not found, no stations in range |
| `500` | Fuel CSV missing or routing API failure |

---

## The Fuel Stop Algorithm

The planner uses a **greedy look-ahead** strategy:

1. Vehicle starts at origin with a full tank (500 miles of range)
2. Look ahead up to 500 miles along the route
3. Find all fuel stations in that window
4. Pick the **cheapest** one
5. Stop there, fill up to full, advance position
6. Repeat until the destination is reachable on remaining fuel

This ensures the vehicle never runs out of gas while minimising fuel cost at every decision point.

---

## Performance

- **Fuel CSV** is read once at startup using `@functools.lru_cache` — all subsequent requests use the in-memory data instantly
- **Geocode cache** is saved to disk so city coordinates are never looked up twice across server restarts
- **Typical response time** is 2-3 seconds (dominated by the 2 geocode + 1 OSRM network calls; the fuel stop algorithm itself runs in milliseconds)

---

## Testing with Postman

1. Set method to `POST`
2. URL: `http://127.0.0.1:8000/api/route/`
3. Headers: `Content-Type: application/json`
4. Body (raw JSON):
```json
{
    "start": "Chicago, IL",
    "end": "Los Angeles, CA"
}
```
5. To visualise the route, copy the `polyline` array and paste it into [geojson.io](https://geojson.io)