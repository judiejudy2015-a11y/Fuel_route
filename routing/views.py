"""
views.py
--------
Single endpoint: POST /api/route/

Request body (JSON):
    {
        "start": "Chicago, IL",
        "end":   "Los Angeles, CA"
    }

Response (JSON):
    {
        "route": {
            "start": "Chicago, IL",
            "end": "Los Angeles, CA",
            "start_coords": { "lat": 41.85, "lon": -87.65 },
            "end_coords":   { "lat": 34.05, "lon": -118.24 },
            "total_distance_miles": 2017.3,
            "polyline": [[lat, lon], ...]
        },
        "fuel_stops": [
            {
                "name": "LOVES TRAVEL STOP #766",
                "address": "I-80, EXIT 27",
                "city": "Atkinson",
                "state": "IL",
                "lat": 41.40,
                "lon": -89.92,
                "price_per_gallon": 3.45,
                "route_miles": 112.4,
                "dist_from_route_miles": 2.1,
                "gallons_purchased": 50.0,
                "stop_cost_usd": 172.50
            },
            ...
        ],
        "summary": {
            "num_stops": 4,
            "total_gallons": 201.7,
            "total_fuel_cost_usd": 693.20,
            "vehicle_range_miles": 500,
            "vehicle_mpg": 10
        }
    }
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from .routing_service import geocode, get_route
from .fuel_service import stations_near_route, plan_fuel_stops, compute_total_fuel_cost

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class RouteView(APIView):
    """
    POST /api/route/
    Plans an optimal (cheapest) fuel stop itinerary for a USA road trip.
    """

    def post(self, request):
        start = (request.data.get("start") or "").strip()
        end = (request.data.get("end") or "").strip()

        if not start or not end:
            return Response(
                {"error": "Both 'start' and 'end' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # 1. Geocode start and end (2 lightweight API calls)
            start_ll = geocode(start)
            end_ll = geocode(end)

            # 2. Fetch full driving route — ONE OSRM call
            route_data = get_route(start_ll, end_ll)

            # 3. Find fuel stations near the route corridor (pure in-memory)
            nearby = stations_near_route(route_data["waypoints"])

            # 4. Plan cheapest stops (greedy algorithm, in-memory)
            stops = plan_fuel_stops(nearby, route_data["total_distance_miles"])

            # 5. Calculate total fuel cost
            cost_summary = compute_total_fuel_cost(stops,
                                                   route_data["total_distance_miles"])

        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except FileNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:
            logger.exception({"Unexpected error in RouteView"})
            return Response(
                {"error": "An unexpected error occurred. Check server logs."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "route": {
                    "start": start,
                    "end": end,
                    "start_coords": {"lat": start_ll[0], "lon": start_ll[1]},
                    "end_coords": {"lat": end_ll[0], "lon": end_ll[1]},
                    "total_distance_miles": round(route_data["total_distance_miles"], 1),
                    "polyline": route_data["polyline"],
                },
                "fuel_stops": [
                    {
                        "name": s["name"],
                        "address": s["address"],
                        "city": s["city"],
                        "state": s["state"],
                        "lat": s["lat"],
                        "lon": s["lon"],
                        "price_per_gallon": s["price"],
                        "route_miles": round(s["route_miles"], 1),
                        "dist_from_route_miles": s["dist_from_route_miles"],
                        "gallons_purchased": s["gallons_purchased"],
                        "stop_cost_usd": s["stop_cost"],
                    }
                    for s in stops
                ],
                "summary": {
                    "num_stops": len(stops),
                    **cost_summary,
                    "vehicle_range_miles": 500,
                    "vehicle_mpg": 10,
                },
            },
            status=status.HTTP_200_OK,
        )
