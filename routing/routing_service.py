"""
routing_service.py
------------------
Handles two things:
  1. Geocoding — converts a city/state string into lat/lon (OpenCage API/nominatim)
  2. Route fetching — gets the full driving route from OSRM in ONE API call

OSRM returns:
  - A GeoJSON polyline (list of [lon, lat] coordinates for map rendering)
  - Annotation distances between each coordinate pair
  - Total route distance in meters

We use the annotation distances to build a list of waypoints with cumulative
mileage — this is what fuel_service.py uses to find stations near the route.
"""

import math
import requests
from django.conf import settings


# ---------------------------------------------------------------------------
# Geocoding (OpenCage/Nominatim)
# ---------------------------------------------------------------------------

def geocode(location: str) -> tuple:
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": f"{location}, USA",
            "format": "json",
            "limit": 1,
            "countrycodes": "us",
        },
        headers={"User-Agent": "fuel-route-app/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode location: {location!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])

# def geocode(location: str) -> tuple:
#     """
#     Convert a location string like "Chicago, IL" into (latitude, longitude).
#     Raises ValueError if the location cannot be resolved.
#     """
#     url = "https://api.opencagedata.com/geocode/v1/json"
#     params = {
#         "q": location,
#         "key": settings.OPENCAGE_API_KEY,
#         "countrycode": "us",
#         "limit": 1,
#         "no_annotations": 1,
#     }
#     resp = requests.get(url, params=params, timeout=10)
#     resp.raise_for_status()
#     data = resp.json()

#     if not data.get("results"):
#         raise ValueError(f"Could not geocode location: {location!r}")

#     geo = data["results"][0]["geometry"]
#     return geo["lat"], geo["lng"]


# ---------------------------------------------------------------------------
# OSRM Routing — single API call
# ---------------------------------------------------------------------------

def get_route(origin_ll, dest_ll) -> dict:
    """
    Fetch the full driving route from OSRM using a single API call.

    We request:
      - overview=full        → complete polyline, not simplified
      - geometries=geojson   → coordinates as [lon, lat] pairs
      - annotations=distance → per-segment distances between coords

    Returns a dict:
      {
        "polyline": [[lat, lon], ...],          # for map rendering
        "total_distance_miles": float,
        "waypoints": [(lat, lon, cumulative_miles), ...]
      }

    The waypoints list is the key output — it's used by fuel_service.py to
    find which fuel stations fall within the route corridor.
    """
    # OSRM expects coordinates as lon,lat (note: reversed from lat,lon)
    coords = f"{origin_ll[1]},{origin_ll[0]};{dest_ll[1]},{dest_ll[0]}"
    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords}"
        f"?overview=full&geometries=geojson&annotations=distance&steps=false"
    )

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "Ok":
        raise ValueError(f"OSRM error: {data.get('message', 'unknown error')}")

    route = data["routes"][0]
    total_miles = route["distance"] / 1609.344

    # GeoJSON coordinates come as [lon, lat] pairs
    geojson_coords = route["geometry"]["coordinates"]

    # annotation distances = meters between consecutive coordinate pairs
    annotation_distances = route["legs"][0].get("annotation", {}).get("distance", [])

    # Build waypoints with cumulative mileage
    waypoints = []
    cumulative_miles = 0.0
    waypoints.append((geojson_coords[0][1], geojson_coords[0][0], 0.0))

    for i, seg_meters in enumerate(annotation_distances):
        cumulative_miles += seg_meters / 1609.344
        coord = geojson_coords[i + 1]
        waypoints.append((coord[1], coord[0], cumulative_miles))

    # Flip to [lat, lon] for map rendering
    polyline = [[c[1], c[0]] for c in geojson_coords]

    return {
        "polyline": polyline,
        "total_distance_miles": total_miles,
        "waypoints": waypoints,
    }


# ---------------------------------------------------------------------------
# Haversine distance (straight-line miles between two lat/lon points)
# ---------------------------------------------------------------------------

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    # Earth radius in miles
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))