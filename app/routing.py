"""Bounded Kakao adapters. Unknown routes are never estimated from straight lines."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from http.client import HTTPSConnection
from urllib.parse import urlencode

MAX_RESPONSE_BYTES = 512_000
MAX_GEOMETRY_POINTS = 2048


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _integer(value):
    return type(value) is int and 0 <= value <= 2**32 - 1


def _coord(value):
    if not isinstance(value, dict) or set(value) != {"latitude", "longitude", "crs"} or value["crs"] != "WGS84":
        return False
    lat, lon = value["latitude"], value["longitude"]
    return (type(lat) in (int, float) and type(lon) in (int, float) and math.isfinite(lat)
            and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180)


def unknown_route(mode, code):
    return {"status": "unknown", "mode": mode, "durationSeconds": None, "distanceMeters": None,
            "knownCostKrw": None, "costComplete": False, "walkingSeconds": None, "geometry": [],
            "checkedAt": _now(), "reasonCodes": [code]}


def _geometry(pairs):
    if not isinstance(pairs, list) or len(pairs) > MAX_GEOMETRY_POINTS:
        return []
    points = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return []
        lon, lat = pair
        coord = {"latitude": lat, "longitude": lon, "crs": "WGS84"}
        if not _coord(coord):
            return []
        points.append({"latitude": lat, "longitude": lon})
    return points


class KakaoRouter:
    def __init__(self, key=None, timeout_seconds=10):
        self._key = key
        self.timeout_seconds = min(10, max(0.05, timeout_seconds))

    def __repr__(self):
        return "KakaoRouter(<private configuration>)"

    def route(self, origin, destination, mode):
        if mode not in {"car", "walk", "transit"}:
            return unknown_route(mode, "ROUTE_MODE_UNSUPPORTED")
        if not _coord(origin) or not _coord(destination):
            return unknown_route(mode, "ROUTE_COORDINATE_INVALID")
        if not self._key:
            return unknown_route(mode, "ROUTE_NOT_CONFIGURED")
        if mode == "car":
            host, path = "apis-navi.kakaomobility.com", "/v1/directions"
            params = {"origin": f"{origin['longitude']},{origin['latitude']}",
                      "destination": f"{destination['longitude']},{destination['latitude']}",
                      "alternatives": "false", "summary": "false"}
        else:
            host = "dapi.kakao.com"
            path = "/v2/routing/walk" if mode == "walk" else "/v2/routing/publictraffic"
            params = {"start_x": origin["longitude"], "start_y": origin["latitude"],
                      "end_x": destination["longitude"], "end_y": destination["latitude"],
                      "input_coord": "WGS84", "output_coord": "WGS84"}
        connection = None
        try:
            connection = HTTPSConnection(host, timeout=self.timeout_seconds)
            connection.request("GET", path + "?" + urlencode(params),
                               headers={"Authorization": "KakaoAK " + self._key, "Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200:
                return unknown_route(mode, "ROUTE_RATE_LIMITED" if response.status == 429 else "ROUTE_UNAVAILABLE")
            if len(raw) > MAX_RESPONSE_BYTES:
                return unknown_route(mode, "ROUTE_RESPONSE_TOO_LARGE")
            data = json.loads(raw)
            return self._parse(data, mode)
        except TimeoutError:
            return unknown_route(mode, "ROUTE_TIMEOUT")
        except Exception:
            return unknown_route(mode, "ROUTE_RESPONSE_INVALID")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _parse(self, data, mode):
        if not isinstance(data, dict):
            raise ValueError
        if mode == "car":
            route = data["routes"][0]
            if type(route["result_code"]) is not int or route["result_code"] != 0:
                return unknown_route(mode, "ROUTE_NOT_FOUND")
            summary = route["summary"]
            seconds, meters = summary["duration"], summary["distance"]
            cost = summary.get("fare", {}).get("toll")
            cost = cost if _integer(cost) else None
            walking, complete = [], False
            pairs = []
            for section in route.get("sections", []):
                for road in section.get("roads", []):
                    vertices = road.get("vertexes", [])
                    if not isinstance(vertices, list) or len(vertices) % 2:
                        raise ValueError
                    pairs.extend(zip(vertices[::2], vertices[1::2]))
                    if len(pairs) > MAX_GEOMETRY_POINTS:
                        break
                if len(pairs) > MAX_GEOMETRY_POINTS:
                    break
        elif mode == "walk":
            if data.get("status") != "OK":
                return unknown_route(mode, "ROUTE_NOT_FOUND")
            route = data["route"]
            seconds, meters = route["properties"]["totalTime"], route["properties"]["totalDistance"]
            cost, complete, walking = 0, True, [seconds]
            pairs = [point for leg in route.get("legs", []) for step in leg.get("steps", [])
                     for point in step.get("path", {}).get("points", [])]
        else:
            if data.get("status") != "OK":
                return unknown_route(mode, "ROUTE_NOT_FOUND")
            choices = [r for r in data["routes"] if isinstance(r, dict)
                       and _integer(r.get("properties", {}).get("totalTime"))]
            if not choices:
                raise ValueError
            route = min(choices, key=lambda r: r["properties"]["totalTime"])
            seconds, meters = route["properties"]["totalTime"], route["properties"]["totalDistance"]
            fare = route["properties"].get("fare", {}).get("value")
            cost, complete = (fare, True) if _integer(fare) else (None, False)
            steps = route.get("steps", [])
            walking = []
            if not isinstance(steps, list) or not steps:
                walking = None
            else:
                continuous_walk = 0
                in_walk = False
                for step in steps:
                    props = step.get("properties", {})
                    if props.get("type") not in {"BUS", "SUBWAY", "WALKING"} or not _integer(props.get("time")):
                        walking = None
                        break
                    if props["type"] == "WALKING":
                        continuous_walk += props["time"]
                        in_walk = True
                    elif in_walk:
                        walking.append(continuous_walk)
                        continuous_walk, in_walk = 0, False
                if walking is not None and in_walk:
                    walking.append(continuous_walk)
            pairs = [point for step in steps for point in step.get("path", {}).get("points", [])] if isinstance(steps, list) else []
        if not _integer(seconds) or not _integer(meters):
            raise ValueError
        geometry = _geometry(pairs)
        return {"status": "verified", "mode": mode, "durationSeconds": seconds, "distanceMeters": meters,
                "knownCostKrw": cost, "costComplete": complete, "walkingSeconds": walking,
                "geometry": geometry, "checkedAt": _now(),
                "reasonCodes": [] if geometry else ["ROUTE_GEOMETRY_UNAVAILABLE"]}
