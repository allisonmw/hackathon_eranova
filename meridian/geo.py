"""Geography and time helpers for tracking. Owner: agent & evals lead.

The TMS has no coordinates (facility latitude/longitude are empty), so facilities are placed by
city from the table below. Landmarks are only used to say where a truck is ("near Joplin, MO").
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

# (city, state) -> (lat, lon). Facility cities first, then landmarks along the main freight lanes.
CITIES: dict[tuple[str, str], tuple[float, float]] = {
    ("Allentown", "PA"): (40.6084, -75.4902), ("Aurora", "CO"): (39.7294, -104.8319),
    ("Bedford Park", "IL"): (41.7681, -87.7856), ("Boston", "MA"): (42.3601, -71.0589),
    ("Charlotte", "NC"): (35.2271, -80.8431), ("Chicago", "IL"): (41.8781, -87.6298),
    ("Columbus", "OH"): (39.9612, -82.9988), ("Dallas", "TX"): (32.7767, -96.7970),
    ("Des Moines", "IA"): (41.5868, -93.6250), ("El Paso", "TX"): (31.7619, -106.4850),
    ("Fort Worth", "TX"): (32.7555, -97.3308), ("Fresno", "CA"): (36.7378, -119.7871),
    ("Grand Rapids", "MI"): (42.9634, -85.6681), ("Houston", "TX"): (29.7604, -95.3698),
    ("Joliet", "IL"): (41.5250, -88.0817), ("Kansas City", "MO"): (39.0997, -94.5786),
    ("Kent", "WA"): (47.3809, -122.2348), ("McDonough", "GA"): (33.4473, -84.1469),
    ("Memphis", "TN"): (35.1495, -90.0490), ("Naperville", "IL"): (41.7508, -88.1535),
    ("Nashville", "TN"): (36.1627, -86.7816), ("Newark", "NJ"): (40.7357, -74.1724),
    ("Ontario", "CA"): (34.0633, -117.6509), ("Phoenix", "AZ"): (33.4484, -112.0740),
    ("Pittsburgh", "PA"): (40.4406, -79.9959), ("Pooler", "GA"): (32.1155, -81.2470),
    ("Portland", "OR"): (45.5152, -122.6784), ("Rockford", "IL"): (42.2711, -89.0940),
    ("Salt Lake City", "UT"): (40.7608, -111.8910), ("Scottsdale", "AZ"): (33.4942, -111.9261),
    # landmarks
    ("St. Louis", "MO"): (38.6270, -90.1994), ("Indianapolis", "IN"): (39.7684, -86.1581),
    ("Louisville", "KY"): (38.2527, -85.7585), ("Cincinnati", "OH"): (39.1031, -84.5120),
    ("Cleveland", "OH"): (41.4993, -81.6944), ("Detroit", "MI"): (42.3314, -83.0458),
    ("Toledo", "OH"): (41.6528, -83.5379), ("Fort Wayne", "IN"): (41.0793, -85.1394),
    ("Kalamazoo", "MI"): (42.2917, -85.5872), ("Milwaukee", "WI"): (43.0389, -87.9065),
    ("Minneapolis", "MN"): (44.9778, -93.2650), ("Omaha", "NE"): (41.2565, -95.9345),
    ("Lincoln", "NE"): (40.8136, -96.7026), ("North Platte", "NE"): (41.1239, -100.7654),
    ("Wichita", "KS"): (37.6872, -97.3301), ("Salina", "KS"): (38.8403, -97.6114),
    ("Goodland", "KS"): (39.3508, -101.7101), ("Tulsa", "OK"): (36.1540, -95.9928),
    ("Oklahoma City", "OK"): (35.4676, -97.5164), ("Joplin", "MO"): (37.0842, -94.5133),
    ("Springfield", "MO"): (37.2090, -93.2923), ("Columbia", "MO"): (38.9517, -92.3341),
    ("Little Rock", "AR"): (34.7465, -92.2896), ("Texarkana", "TX"): (33.4251, -94.0477),
    ("Shreveport", "LA"): (32.5252, -93.7502), ("Jackson", "MS"): (32.2988, -90.1848),
    ("Birmingham", "AL"): (33.5186, -86.8104), ("Atlanta", "GA"): (33.7490, -84.3880),
    ("Chattanooga", "TN"): (35.0456, -85.3097), ("Knoxville", "TN"): (35.9606, -83.9207),
    ("Lexington", "KY"): (38.0406, -84.5037), ("Columbia", "SC"): (34.0007, -81.0348),
    ("Savannah", "GA"): (32.0809, -81.0912), ("Richmond", "VA"): (37.5407, -77.4360),
    ("Washington", "DC"): (38.9072, -77.0369), ("Baltimore", "MD"): (39.2904, -76.6122),
    ("Philadelphia", "PA"): (39.9526, -75.1652), ("Harrisburg", "PA"): (40.2732, -76.8867),
    ("Altoona", "PA"): (40.5187, -78.3947), ("Hartford", "CT"): (41.7658, -72.6734),
    ("Providence", "RI"): (41.8240, -71.4128), ("New York", "NY"): (40.7128, -74.0060),
    ("Albany", "NY"): (42.6526, -73.7562), ("Scranton", "PA"): (41.4090, -75.6624),
    ("Erie", "PA"): (42.1292, -80.0851), ("Buffalo", "NY"): (42.8864, -78.8784),
    ("Austin", "TX"): (30.2672, -97.7431), ("San Antonio", "TX"): (29.4241, -98.4936),
    ("Waco", "TX"): (31.5493, -97.1467), ("Abilene", "TX"): (32.4487, -99.7331),
    ("Lubbock", "TX"): (33.5779, -101.8552), ("Amarillo", "TX"): (35.2220, -101.8313),
    ("Midland", "TX"): (31.9973, -102.0779), ("Van Horn", "TX"): (31.0399, -104.8308),
    ("Las Cruces", "NM"): (32.3199, -106.7637), ("Albuquerque", "NM"): (35.0844, -106.6504),
    ("Tucson", "AZ"): (32.2226, -110.9747), ("Lordsburg", "NM"): (32.3501, -108.7087),
    ("Flagstaff", "AZ"): (35.1983, -111.6513), ("Las Vegas", "NV"): (36.1699, -115.1398),
    ("Barstow", "CA"): (34.8958, -117.0173), ("Bakersfield", "CA"): (35.3733, -119.0187),
    ("Los Angeles", "CA"): (34.0522, -118.2437), ("Sacramento", "CA"): (38.5816, -121.4944),
    ("Redding", "CA"): (40.5865, -122.3917), ("Reno", "NV"): (39.5296, -119.8138),
    ("Winnemucca", "NV"): (40.9730, -117.7357), ("Elko", "NV"): (40.8324, -115.7631),
    ("Wendover", "UT"): (40.7371, -114.0378), ("Medford", "OR"): (42.3265, -122.8756),
    ("Eugene", "OR"): (44.0521, -123.0868), ("Seattle", "WA"): (47.6062, -122.3321),
    ("Tacoma", "WA"): (47.2529, -122.4443), ("Olympia", "WA"): (47.0379, -122.9007),
    ("Boise", "ID"): (43.6150, -116.2023), ("Pocatello", "ID"): (42.8713, -112.4455),
    ("Denver", "CO"): (39.7392, -104.9903), ("Colorado Springs", "CO"): (38.8339, -104.8214),
    ("Grand Junction", "CO"): (39.0639, -108.5506), ("Cheyenne", "WY"): (41.1400, -104.8202),
    ("Limon", "CO"): (39.2639, -103.6922), ("Hays", "KS"): (38.8792, -99.3268),
    ("Topeka", "KS"): (39.0473, -95.6752), ("Effingham", "IL"): (39.1200, -88.5434),
    ("Bloomington", "IL"): (40.4842, -88.9937), ("Peoria", "IL"): (40.6936, -89.5890),
    ("Davenport", "IA"): (41.5236, -90.5776), ("Iowa City", "IA"): (41.6611, -91.5302),
    ("Terre Haute", "IN"): (39.4667, -87.4139), ("Dayton", "OH"): (39.7589, -84.1916),
}

FACILITY_CITY_RADIUS_MI = 12


def haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def interpolate(a: tuple[float, float], b: tuple[float, float], frac: float) -> tuple[float, float]:
    frac = min(max(frac, 0.0), 1.0)
    return a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac


def city_coords(city: str | None, state: str | None) -> tuple[float, float] | None:
    return CITIES.get(((city or "").strip(), (state or "").strip().upper()))


def facility_coords(facility: dict | None) -> tuple[float, float] | None:
    if not facility:
        return None
    if facility.get("latitude") is not None and facility.get("longitude") is not None:
        return float(facility["latitude"]), float(facility["longitude"])
    return city_coords(facility.get("city"), facility.get("state"))


def stop_coords(stop: dict, facilities: dict[str, dict]) -> tuple[float, float] | None:
    fac = facilities.get(stop.get("facility_id") or "")
    if c := facility_coords(fac):
        return c
    addr = stop.get("address_snapshot") or {}
    return city_coords(addr.get("city"), addr.get("state"))


def near(pos: tuple[float, float]) -> str:
    (city, state), dist = min(((k, haversine_mi(pos, v)) for k, v in CITIES.items()), key=lambda kv: kv[1])
    if dist <= FACILITY_CITY_RADIUS_MI:
        return f"at {city}, {state}"
    if dist <= 60:
        return f"near {city}, {state}"
    return f"~{dist:.0f} mi from {city}, {state}"


# --------------------------------------------------------------------------- time

def tz(name: str | None) -> ZoneInfo:
    return ZoneInfo(name or "America/Chicago")


def local_to_utc(local: str | datetime | None, tz_name: str | None) -> datetime | None:
    """A facility-local datetime string ('2026-11-14T08:00:00') to an aware UTC datetime."""
    if not local:
        return None
    dt = datetime.fromisoformat(local) if isinstance(local, str) else local
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(tz_name))
    return dt.astimezone(UTC)


def end_of_day_utc(d: str | date | None, tz_name: str | None) -> datetime | None:
    if not d:
        return None
    day = date.fromisoformat(d[:10]) if isinstance(d, str) else d
    return datetime.combine(day, time(23, 59), tzinfo=tz(tz_name)).astimezone(UTC)


def parse_utc(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fmt_local(dt: datetime | None, tz_name: str | None, with_day: bool = True) -> str:
    if dt is None:
        return "—"
    loc = dt.astimezone(tz(tz_name))
    return loc.strftime("%a %m/%d %H:%M %Z" if with_day else "%H:%M %Z")


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_open(facility: dict | None, at_utc: datetime) -> datetime:
    """Earliest time at or after `at_utc` when the facility's receiving dock is open.
    No hours on file means always open."""
    hours = (facility or {}).get("receiving_hours") or (facility or {}).get("operating_hours")
    if not hours:
        return at_utc
    zone = tz((facility or {}).get("timezone"))
    loc = at_utc.astimezone(zone)
    for add in range(8):
        day = loc.date() + timedelta(days=add)
        h = hours.get(_DAYS[day.weekday()])
        if not h or not h.get("open") or not h.get("close"):
            continue
        opens = datetime.combine(day, time.fromisoformat(h["open"]), tzinfo=zone)
        closes = datetime.combine(day, time.fromisoformat(h["close"]), tzinfo=zone)
        if loc < opens:
            return opens.astimezone(UTC)
        if loc < closes:
            return loc.astimezone(UTC)
    return at_utc
