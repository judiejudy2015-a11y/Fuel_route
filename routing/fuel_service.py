"""
fuel_service.py
---------------
Loads the OPIS fuel prices CSV and plans the cheapest fuel stop sequence.

CSV columns (real OPIS format):
    OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price

The CSV has no lat/lon, so we geocode each unique City+State pair using
OpenCage. Results are saved to routing/data/geocode_cache.json so this
only ever runs once — all future server starts load from the cache instantly.

If no OpenCage key is set, we fall back to state centroids (still works
well for long cross-country routes).

Fuel stop algorithm (greedy look-ahead):
    1. Start with a full 500-mile tank
    2. Look ahead up to 500 miles along the route
    3. Pick the cheapest station in that window
    4. Fill up to full, advance position, repeat
    5. Stop when the destination is reachable on remaining fuel
"""

import csv
import json
import logging
import math
import functools
from pathlib import Path
from collections import defaultdict

import requests
from django.conf import settings

from .routing_service import haversine_miles

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORRIDOR_MILES = 300
GEOCODE_CACHE_FILE = Path(__file__).parent / "data" / "geocode_cache.json"

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
}

# State geographic centroids — used as fallback when geocoding fails
STATE_CENTROIDS = {
    'AL': (32.806671, -86.791130), 'AK': (61.370716, -152.404419),
    'AZ': (33.729759, -111.431221), 'AR': (34.969704, -92.373123),
    'CA': (36.116203, -119.681564), 'CO': (39.059811, -105.311104),
    'CT': (41.597782, -72.755371), 'DE': (39.318523, -75.507141),
    'FL': (27.766279, -81.686783), 'GA': (33.040619, -83.643074),
    'HI': (21.094318, -157.498337), 'ID': (44.240459, -114.478828),
    'IL': (40.349457, -88.986137), 'IN': (39.849426, -86.258278),
    'IA': (42.011539, -93.210526), 'KS': (38.526600, -96.726486),
    'KY': (37.668140, -84.670067), 'LA': (31.169960, -91.867805),
    'ME': (44.693947, -69.381927), 'MD': (39.063946, -76.802101),
    'MA': (42.230171, -71.530106), 'MI': (43.326618, -84.536095),
    'MN': (45.694454, -93.900192), 'MS': (32.741646, -89.678696),
    'MO': (38.456085, -92.288368), 'MT': (46.921925, -110.454353),
    'NE': (41.125370, -98.268082), 'NV': (38.313515, -117.055374),
    'NH': (43.452492, -71.563896), 'NJ': (40.298904, -74.521011),
    'NM': (34.840515, -106.248482), 'NY': (42.165726, -74.948051),
    'NC': (35.630066, -79.806419), 'ND': (47.528912, -99.784012),
    'OH': (40.388783, -82.764915), 'OK': (35.565342, -96.928917),
    'OR': (44.572021, -122.070938), 'PA': (40.590752, -77.209755),
    'RI': (41.680893, -71.511780), 'SC': (33.856892, -80.945007),
    'SD': (44.299782, -99.438828), 'TN': (35.747845, -86.692345),
    'TX': (31.054487, -97.563461), 'UT': (40.150032, -111.862434),
    'VT': (44.045876, -72.710686), 'VA': (37.769337, -78.169968),
    'WA': (47.400902, -121.490494), 'WV': (38.491226, -80.954453),
    'WI': (44.268543, -89.616508), 'WY': (42.755966, -107.302490),
    'DC': (38.897438, -77.026817),
}


# ---------------------------------------------------------------------------
# Geocode cache helpers
# ---------------------------------------------------------------------------

def _load_geocode_cache() -> dict:
    if GEOCODE_CACHE_FILE.exists():
        try:
            with open(GEOCODE_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_geocode_cache(cache: dict):
    GEOCODE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GEOCODE_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _geocode_city_state(city, state, cache) -> tuple | None:
    key = f"{city}|{state}"

    if key in cache:
        return tuple(cache[key]) if cache[key] else None

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"{city}, {state}, USA",
                "format": "json",
                "limit": 1,
                "countrycodes": "us",
            },
            headers={"User-Agent": "fuel-route-app/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = (float(results[0]["lat"]), float(results[0]["lon"]))
            cache[key] = list(coords)
            return coords
    except Exception as exc:
        logger.warning(f"Nominatim geocode failed for {city}, {state}: {exc}")

    # Fallback: state centroid
    coords = STATE_CENTROIDS.get(state)
    cache[key] = list(coords) if coords else None
    return coords


# def _geocode_city_state(city: str, state: str, cache: dict) -> tuple | None:
#     """
#     Return (lat, lon) for a city+state.
#     Order of preference: disk cache → OpenCage API → state centroid fallback.
#     """
#     key = f"{city}|{state}"

#     if key in cache:
#         return tuple(cache[key]) if cache[key] else None

#     api_key = getattr(settings, "OPENCAGE_API_KEY", "")

#     if api_key:
#         try:
#             resp = requests.get(
#                 "https://api.opencagedata.com/geocode/v1/json",
#                 params={
#                     "q": f"{city}, {state}, USA",
#                     "key": api_key,
#                     "countrycode": "us",
#                     "limit": 1,
#                     "no_annotations": 1,
#                 },
#                 timeout=15,
#             )
#             resp.raise_for_status()
#             results = resp.json().get("results", [])
#             if results:
#                 geo = results[0]["geometry"]
#                 coords = (geo["lat"], geo["lng"])
#                 cache[key] = list(coords)
#                 return coords
#         except Exception as exc:
#             logger.warning(f"OpenCage geocode failed for {city}, {state}: {exc}")

    # # Fallback: state centroid
    # coords = STATE_CENTROIDS.get(state)
    # cache[key] = list(coords) if coords else None
    # return coords


# ---------------------------------------------------------------------------
# Load and process fuel CSV  (cached in memory after first call)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def load_fuel_stations() -> list:
    """
    Parse the OPIS CSV, deduplicate stations, filter to US only,
    and geocode each unique City+State pair.

    Uses lru_cache so the CSV is only read once per server process.
    Geocode results are saved to disk so they persist across restarts.
    """
    csv_path = Path(settings.FUEL_PRICES_CSV)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Fuel prices CSV not found at: {csv_path}\n"
            "Set the FUEL_PRICES_CSV environment variable to the correct path."
        )

    # Step 1 — parse CSV, group by OPIS station ID
    raw = {}
    prices_by_id = defaultdict(list)

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            state = row.get("State", "").strip()
            if state not in US_STATES:
                continue

            opis_id = row.get("OPIS Truckstop ID", "").strip()
            if not opis_id:
                continue

            try:
                price = float(row["Retail Price"].strip())
            except (ValueError, KeyError):
                continue

            prices_by_id[opis_id].append(price)

            if opis_id not in raw:
                raw[opis_id] = {
                    "opis_id": opis_id,
                    "name": row.get("Truckstop Name", "").strip(),
                    "address": row.get("Address", "").strip(),
                    "city": row.get("City", "").strip().rstrip(),
                    "state": state,
                }

    logger.info(f"Parsed {len(raw)} unique US stations from CSV")

    # Step 2 — geocode all unique city+state pairs (hits cache first)
    geocode_cache = _load_geocode_cache()
    cache_dirty = False

    for info in raw.values():
        key = f"{info['city']}|{info['state']}"
        if key not in geocode_cache:
            _geocode_city_state(info["city"], info["state"], geocode_cache)
            cache_dirty = True

    if cache_dirty:
        _save_geocode_cache(geocode_cache)

    # Step 3 — build final list with coordinates and averaged prices
    stations = []
    for opis_id, info in raw.items():
        coords = _geocode_city_state(info["city"], info["state"], geocode_cache)
        if not coords:
            continue

        avg_price = sum(prices_by_id[opis_id]) / len(prices_by_id[opis_id])
        stations.append({
            **info,
            "lat": coords[0],
            "lon": coords[1],
            "price": round(avg_price, 4),
        })

    logger.info(f"{len(stations)} stations ready")
    return stations


# ---------------------------------------------------------------------------
# Corridor search
# ---------------------------------------------------------------------------

def _thin_waypoints(waypoints: list, interval_miles: float = 3.0) -> list:
    """
    Reduce waypoint density to roughly one point every `interval_miles`.
    This makes the corridor search much faster without losing accuracy.
    """
    if not waypoints:
        return []
    thinned = [waypoints[0]]
    last_miles = waypoints[0][2]
    for wp in waypoints[1:]:
        if wp[2] - last_miles >= interval_miles:
            thinned.append(wp)
            last_miles = wp[2]
    return thinned


def stations_near_route(waypoints: list, corridor_miles=CORRIDOR_MILES) -> list:
    """
    Find all fuel stations within `corridor_miles` of the driving route.
    Annotates each station with `route_miles` (where along the route it sits).
    """
    stations = load_fuel_stations()
    thinned = _thin_waypoints(waypoints, interval_miles=10.0)

    near = {}
    for i, station in enumerate(stations):
        best_dist = math.inf
        best_route_miles = 0.0

        for (w_lat, w_lon, w_miles) in thinned:
            d = haversine_miles(station["lat"], station["lon"], w_lat, w_lon)
            if d < best_dist:
                best_dist = d
                best_route_miles = w_miles

        if best_dist <= corridor_miles:
            annotated = dict(station)
            annotated["route_miles"] = best_route_miles
            annotated["dist_from_route_miles"] = round(best_dist, 2)
            near[i] = annotated

    return list(near.values())


# ---------------------------------------------------------------------------
# Greedy fuel stop planner
# ---------------------------------------------------------------------------

def plan_fuel_stops(
    nearby_stations,
    total_route_miles,
    max_range=None,
    mpg=None,
) -> list:
    """
    Plan the cheapest sequence of fuel stops so the tank never hits zero.

    Algorithm:
        - current position starts at 0, tank starts full (max_range miles)
        - while destination not reachable on current fuel:
            - look at all stations within the next max_range miles
            - pick the cheapest one
            - stop there, fill to full
            - advance current position to that station
        - return the list of chosen stops
    """
    if max_range is None:
        max_range = settings.VEHICLE_MAX_RANGE_MILES
    if mpg is None:
        mpg = settings.VEHICLE_MPG

    sorted_stations = sorted(nearby_stations, key=lambda s: s["route_miles"])

    stops = []
    current_miles = 0.0
    # full tank at departure
    range_remaining = max_range

    while current_miles + range_remaining < total_route_miles:
        window_end = current_miles + range_remaining
        candidates = [
            s for s in sorted_stations
            if current_miles < s["route_miles"] <= window_end
        ]

        if not candidates:
            raise ValueError(
                f"No fuel stations found between mile {current_miles:.0f} "
                f"and mile {window_end:.0f}. "
                "Try widening the search corridor or check your fuel data."
            )

        best = min(candidates, key=lambda s: s["price"])
        # always fill to full
        gallons = max_range / mpg

        stop = dict(best)
        stop["gallons_purchased"] = round(gallons, 2)
        stop["stop_cost"] = round(gallons * best["price"], 2)
        stops.append(stop)

        current_miles = best["route_miles"]
        # reset — tank is full again
        range_remaining = max_range

    return stops


# ---------------------------------------------------------------------------
# Cost summary
# ---------------------------------------------------------------------------

def compute_total_fuel_cost(stops, total_route_miles, mpg=None) -> dict:
    """
    Calculate the total gallons used and total USD spent for the trip.
    The final leg (last stop → destination) is priced at the last stop's rate.
    """
    if mpg is None:
        mpg = settings.VEHICLE_MPG

    total_gallons = total_route_miles / mpg

    if not stops:
        # Entire route fits within one tank
        return {
            "total_gallons": round(total_gallons, 2),
            "total_fuel_cost_usd": round(total_gallons * 3.50, 2),
            "note": "Route fits within one tank. Cost estimated at $3.50/gal.",
        }

    stop_costs = sum(s["stop_cost"] for s in stops)

    # Final leg cost: miles after last stop, priced at last stop's rate
    remaining_miles = total_route_miles - stops[-1]["route_miles"]
    remaining_cost = (remaining_miles / mpg) * stops[-1]["price"]

    return {
        "total_gallons": round(total_gallons, 2),
        "total_fuel_cost_usd": round(stop_costs + remaining_cost, 2),
    }
