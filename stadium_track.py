"""Stadium geometry with two straights and two semicircles.

Published examples use a nominal 400 m model near the public stadium seed.
Existing image-calibrated route files remain supported, including their metadata.
"""
import argparse
import json
import math
from pathlib import Path

from emulator_motion import MotionError
from emulator_route import EARTH_RADIUS, STADIUM_SEED, metres, validate_route

ROOT = Path(__file__).resolve().parent
DEFAULT_ROUTE = ROOT / ".lab/routes/neusoft-track-route.json"
EXAMPLE_ROUTE = ROOT / "examples/stadium-route.json"


class StadiumPath:
    def __init__(self, data):
        if data.get("closed") is not True:
            raise MotionError("Stadium route must be closed")
        track = data.get("track", {})
        if track.get("kind") != "stadium":
            raise MotionError("Unknown stadium geometry")
        self.origin = tuple(track["center_wgs84"])
        self.radius = float(track["radius_m"])
        self.straight = float(track["straight_length_m"])
        self.angle = math.radians(float(track["axis_degrees_from_east"]))
        self.start_phase = float(track.get("start_phase_m", 0))
        if len(self.origin) != 2 or not all(math.isfinite(v) for v in (*self.origin, self.radius, self.straight, self.angle, self.start_phase)):
            raise MotionError("Invalid stadium parameters")
        if not 20 <= self.radius <= 60 or not 40 <= self.straight <= 140 or metres(STADIUM_SEED, self.origin) > 600:
            raise MotionError("Stadium geometry is outside the supported boundary")
        self.lon_scale = EARTH_RADIUS*math.cos(math.radians(self.origin[1]))*math.pi/180
        self.lat_scale = EARTH_RADIUS*math.pi/180
        self.length = 2*self.straight+2*math.pi*self.radius
        declared = float(track["confirmed_lap_m"])
        if not math.isfinite(declared) or abs(self.length-declared) > .05:
            raise MotionError("Stadium dimensions do not match the confirmed lap length")
        knots = [i*.2 for i in range(math.ceil(self.length/.2))]+[self.length]
        for boundary in (0, self.straight, self.straight+math.pi*self.radius,
                         2*self.straight+math.pi*self.radius):
            knots.append((boundary-self.start_phase) % self.length)
        self.distances = sorted(set(knots))
        self.points, self.headings, self.curvatures = [], [], []
        for distance in self.distances:
            point, heading, curvature = self.at(distance)
            self.points.append(point)
            self.headings.append(heading)
            self.curvatures.append(curvature)
        self.heading_winding = 2*math.pi
        if "points" in data:
            vertices, _ = validate_route(data)
            if any(self.distance_to(vertex) > .3 for vertex in vertices):
                raise MotionError("Route vertices disagree with the stadium model")

    def at(self, distance):
        absolute = distance+self.start_phase
        lap = math.floor(absolute/self.length)
        arc = absolute-lap*self.length
        half = self.straight/2
        if arc < self.straight:
            u, v, heading, curvature = -half+arc, -self.radius, 0.0, 0.0
        elif arc < self.straight+math.pi*self.radius:
            angle = -math.pi/2+(arc-self.straight)/self.radius
            u, v = half+self.radius*math.cos(angle), self.radius*math.sin(angle)
            heading, curvature = angle+math.pi/2, 1/self.radius
        elif arc < 2*self.straight+math.pi*self.radius:
            u, v = half-(arc-self.straight-math.pi*self.radius), self.radius
            heading, curvature = math.pi, 0.0
        else:
            angle = math.pi/2+(arc-2*self.straight-math.pi*self.radius)/self.radius
            u, v = -half+self.radius*math.cos(angle), self.radius*math.sin(angle)
            heading, curvature = angle+math.pi/2, 1/self.radius
        east = u*math.cos(self.angle)-v*math.sin(self.angle)
        north = u*math.sin(self.angle)+v*math.cos(self.angle)
        return (east, north), heading+self.angle+lap*2*math.pi, curvature

    def coordinates(self, point):
        return (self.origin[0]+point[0]/self.lon_scale, self.origin[1]+point[1]/self.lat_scale)

    def project(self, coordinate):
        return ((coordinate[0]-self.origin[0])*self.lon_scale, (coordinate[1]-self.origin[1])*self.lat_scale)

    def distance_to(self, coordinate):
        east, north = self.project(coordinate)
        u = east*math.cos(self.angle)+north*math.sin(self.angle)
        v = -east*math.sin(self.angle)+north*math.cos(self.angle)
        if abs(u) <= self.straight/2:
            return abs(abs(v)-self.radius)
        return abs(math.hypot(abs(u)-self.straight/2, v)-self.radius)


def create_example_route(destination=DEFAULT_ROUTE, *, source=EXAMPLE_ROUTE):
    """Validate and copy the public model without needing private map or App data."""
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    path = StadiumPath(data)
    data["checkpoint_distances_m"] = {
        key: round(path.distance_to(value), 2)
        for key, value in data.get("checkpoint_markers", {}).items()
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return data


def create_neusoft_route(destination=DEFAULT_ROUTE):
    """Compatibility entry point for scripts using the historical route name."""
    return create_example_route(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Copy a validated public stadium model")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--source", type=Path, default=EXAMPLE_ROUTE)
    arguments = parser.parse_args()
    data = create_example_route(arguments.output, source=arguments.source)
    print(arguments.output)
    print(json.dumps(data["checkpoint_distances_m"], ensure_ascii=False))
