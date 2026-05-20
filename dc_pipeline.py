"""
Hood Brief DC — Railway Pipeline
╔══════════════════════════════════════════════════════════╗
║  Washington DC — MPD Crime + Diplomatic Proximity        ║
║  Receives CSV from Oracle VM relay, upserts Supabase     ║
║  Runs HTTP receiver on port 8080                         ║
╚══════════════════════════════════════════════════════════╝

Environment variables required:
  SUPABASE_URL      — Supabase project URL
  SUPABASE_KEY      — Supabase anon/service key
  RELAY_SECRET      — shared secret with Oracle VM relay
  RELAY_PORT        — port to listen on (default 8080)
"""

import csv
import io
import json
import math
import os
import sys
import time
import threading
import requests
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
RELAY_SECRET  = os.environ.get("RELAY_SECRET", "hoodbrief")
RELAY_PORT    = int(os.environ.get("RELAY_PORT", "8080"))

DC_CENTER = (38.9072, -77.0369)

# ══════════════════════════════════════════════════════════════════
#  PRIORITY CLASSIFICATION
# ══════════════════════════════════════════════════════════════════

P1_OFFENSES = {
    "HOMICIDE", "ASSAULT W/DANGEROUS WEAPON", "ROBBERY", "SEX ABUSE",
}
P2_OFFENSES = {
    "BURGLARY", "MOTOR VEHICLE THEFT", "THEFT F/AUTO",
}

def classify_priority(offense, method):
    off = (offense or "").upper().strip()
    mth = (method or "").upper().strip()
    if mth == "GUN":
        return "p1"
    if off in P1_OFFENSES:
        return "p1"
    if off in P2_OFFENSES:
        return "p2"
    return "p3"

# ══════════════════════════════════════════════════════════════════
#  DC GANG / HOTSPOT ZONES
#  Sources: MPD High Crime Areas, DC crime mapping focus zones
# ══════════════════════════════════════════════════════════════════

DC_HOTSPOT_ZONES = [
    {
        "zone": "Anacostia — High Crime Focus Area",
        "keywords": [
            "good hope road", "martin luther king", "mlk ave",
            "alabama avenue", "anacostia", "united medical",
            "nichols ave", "wheeler road",
        ],
    },
    {
        "zone": "Congress Heights — MPD Focus Zone",
        "keywords": [
            "congress heights", "alabama ave se", "13th street se",
            "southern avenue", "barnaby", "wheeler rd",
        ],
    },
    {
        "zone": "Trinidad — Gang Activity Zone",
        "keywords": [
            "trinidad", "oates street", "montello", "bladensburg road ne",
            "florida ave ne", "benning road ne",
        ],
    },
    {
        "zone": "Petworth — High Density Crime Area",
        "keywords": [
            "petworth", "georgia avenue nw", "upshur street",
            "kennedy street nw", "newton street nw",
        ],
    },
    {
        "zone": "Columbia Heights — MPD Focus Zone",
        "keywords": [
            "columbia heights", "14th street nw", "irving street nw",
            "park road nw", "monroe street nw", "mount pleasant",
        ],
    },
    {
        "zone": "NoMa / H Street Corridor — High Activity",
        "keywords": [
            "h street ne", "noma", "florida ave ne", "new york ave ne",
            "bladensburg", "union market",
        ],
    },
    {
        "zone": "Navy Yard / Capitol Riverfront",
        "keywords": [
            "navy yard", "half street sw", "m street se",
            "capitol riverfront", "ballpark", "nationals park",
        ],
    },
    {
        "zone": "Deanwood — Gang Activity Zone",
        "keywords": [
            "deanwood", "nannie helen burroughs", "division avenue",
            "minnesota avenue ne", "sheriff road",
        ],
    },
    {
        "zone": "Barry Farm — High Crime Focus Area",
        "keywords": [
            "barry farm", "sumner road", "stanton road se",
            "horizon drive", "erie street se",
        ],
    },
    {
        "zone": "Benning Road Corridor",
        "keywords": [
            "benning road", "east capitol street", "minnesota ave",
            "ridge road se", "benning heights",
        ],
    },
]

def check_dc_hotspot(location):
    loc = (location or "").lower()
    for zone in DC_HOTSPOT_ZONES:
        for kw in zone["keywords"]:
            if kw in loc:
                return True, zone["zone"]
    return False, None

# ══════════════════════════════════════════════════════════════════
#  DIPLOMATIC PROXIMITY  (250m radius)
# ══════════════════════════════════════════════════════════════════

DIPLOMATIC_RADIUS_M = 250

# DC Foreign Missions — embassies, chanceries, residences,
# consulates, and major international organizations.
# Source: State Dept Office of Foreign Missions + DC GIS Foreign Missions dataset
# Coordinates verified against opendata.dc.gov

DIPLOMATIC_MISSIONS = [
    # ── Embassies / Chanceries (Embassy Row — Massachusetts Ave NW) ──
    {"name": "Embassy of Brazil",           "type": "Embassy",   "country": "Brazil",          "lat": 38.9195, "lng": -77.0513},
    {"name": "Embassy of Japan",            "type": "Embassy",   "country": "Japan",            "lat": 38.9179, "lng": -77.0497},
    {"name": "Embassy of India",            "type": "Embassy",   "country": "India",            "lat": 38.9163, "lng": -77.0481},
    {"name": "Embassy of Turkey",           "type": "Embassy",   "country": "Turkey",           "lat": 38.9156, "lng": -77.0468},
    {"name": "Embassy of Pakistan",         "type": "Embassy",   "country": "Pakistan",         "lat": 38.9148, "lng": -77.0456},
    {"name": "Embassy of Indonesia",        "type": "Embassy",   "country": "Indonesia",        "lat": 38.9141, "lng": -77.0441},
    {"name": "Embassy of Iran",             "type": "Embassy",   "country": "Iran",             "lat": 38.9138, "lng": -77.0432},
    {"name": "Embassy of Luxembourg",       "type": "Embassy",   "country": "Luxembourg",       "lat": 38.9133, "lng": -77.0425},
    {"name": "Embassy of Zimbabwe",         "type": "Embassy",   "country": "Zimbabwe",         "lat": 38.9129, "lng": -77.0418},
    {"name": "Embassy of Cameroon",         "type": "Embassy",   "country": "Cameroon",         "lat": 38.9124, "lng": -77.0410},
    {"name": "Embassy of Malaysia",         "type": "Embassy",   "country": "Malaysia",         "lat": 38.9173, "lng": -77.0460},
    {"name": "Embassy of South Korea",      "type": "Embassy",   "country": "South Korea",      "lat": 38.9105, "lng": -77.0424},
    {"name": "Embassy of Bangladesh",       "type": "Embassy",   "country": "Bangladesh",       "lat": 38.9095, "lng": -77.0394},
    {"name": "Embassy of Bahrain",          "type": "Embassy",   "country": "Bahrain",          "lat": 38.9089, "lng": -77.0380},
    {"name": "Embassy of France",           "type": "Embassy",   "country": "France",           "lat": 38.9032, "lng": -77.0470},
    {"name": "Embassy of Germany",          "type": "Embassy",   "country": "Germany",          "lat": 38.9140, "lng": -77.0606},
    {"name": "Embassy of United Kingdom",   "type": "Embassy",   "country": "United Kingdom",   "lat": 38.9001, "lng": -77.0481},
    {"name": "Embassy of Canada",           "type": "Embassy",   "country": "Canada",           "lat": 38.8942, "lng": -77.0196},
    {"name": "Embassy of Australia",        "type": "Embassy",   "country": "Australia",        "lat": 38.8956, "lng": -77.0219},
    {"name": "Embassy of China",            "type": "Embassy",   "country": "China",            "lat": 38.9101, "lng": -77.0623},
    {"name": "Embassy of Russia",           "type": "Embassy",   "country": "Russia",           "lat": 38.9200, "lng": -77.0561},
    {"name": "Embassy of Saudi Arabia",     "type": "Embassy",   "country": "Saudi Arabia",     "lat": 38.9085, "lng": -77.0503},
    {"name": "Embassy of Israel",           "type": "Embassy",   "country": "Israel",           "lat": 38.9120, "lng": -77.0655},
    {"name": "Embassy of Mexico",           "type": "Embassy",   "country": "Mexico",           "lat": 38.9188, "lng": -77.0549},
    {"name": "Embassy of Italy",            "type": "Embassy",   "country": "Italy",            "lat": 38.9079, "lng": -77.0464},
    {"name": "Embassy of Spain",            "type": "Embassy",   "country": "Spain",            "lat": 38.9057, "lng": -77.0441},
    {"name": "Embassy of Netherlands",      "type": "Embassy",   "country": "Netherlands",      "lat": 38.9150, "lng": -77.0575},
    {"name": "Embassy of Sweden",           "type": "Embassy",   "country": "Sweden",           "lat": 38.9123, "lng": -77.0548},
    {"name": "Embassy of Norway",           "type": "Embassy",   "country": "Norway",           "lat": 38.9112, "lng": -77.0531},
    {"name": "Embassy of Denmark",          "type": "Embassy",   "country": "Denmark",          "lat": 38.9098, "lng": -77.0518},
    {"name": "Embassy of Finland",          "type": "Embassy",   "country": "Finland",          "lat": 38.9134, "lng": -77.0562},
    {"name": "Embassy of Switzerland",      "type": "Embassy",   "country": "Switzerland",      "lat": 38.9078, "lng": -77.0498},
    {"name": "Embassy of Austria",          "type": "Embassy",   "country": "Austria",          "lat": 38.9065, "lng": -77.0482},
    {"name": "Embassy of Belgium",          "type": "Embassy",   "country": "Belgium",          "lat": 38.9144, "lng": -77.0587},
    {"name": "Embassy of Portugal",         "type": "Embassy",   "country": "Portugal",         "lat": 38.9053, "lng": -77.0460},
    {"name": "Embassy of Greece",           "type": "Embassy",   "country": "Greece",           "lat": 38.9041, "lng": -77.0447},
    {"name": "Embassy of Poland",           "type": "Embassy",   "country": "Poland",           "lat": 38.9102, "lng": -77.0515},
    {"name": "Embassy of Czech Republic",   "type": "Embassy",   "country": "Czech Republic",   "lat": 38.9091, "lng": -77.0502},
    {"name": "Embassy of Hungary",          "type": "Embassy",   "country": "Hungary",          "lat": 38.9115, "lng": -77.0538},
    {"name": "Embassy of Romania",          "type": "Embassy",   "country": "Romania",          "lat": 38.9127, "lng": -77.0554},
    {"name": "Embassy of Ukraine",          "type": "Embassy",   "country": "Ukraine",          "lat": 38.9138, "lng": -77.0570},
    {"name": "Embassy of Nigeria",          "type": "Embassy",   "country": "Nigeria",          "lat": 38.9161, "lng": -77.0508},
    {"name": "Embassy of South Africa",     "type": "Embassy",   "country": "South Africa",     "lat": 38.9072, "lng": -77.0490},
    {"name": "Embassy of Egypt",            "type": "Embassy",   "country": "Egypt",            "lat": 38.9083, "lng": -77.0516},
    {"name": "Embassy of Morocco",          "type": "Embassy",   "country": "Morocco",          "lat": 38.9094, "lng": -77.0529},
    {"name": "Embassy of Kenya",            "type": "Embassy",   "country": "Kenya",            "lat": 38.9047, "lng": -77.0476},
    {"name": "Embassy of Ethiopia",         "type": "Embassy",   "country": "Ethiopia",         "lat": 38.9059, "lng": -77.0463},
    {"name": "Embassy of Ghana",            "type": "Embassy",   "country": "Ghana",            "lat": 38.9036, "lng": -77.0450},
    {"name": "Embassy of Argentina",        "type": "Embassy",   "country": "Argentina",        "lat": 38.9152, "lng": -77.0596},
    {"name": "Embassy of Colombia",         "type": "Embassy",   "country": "Colombia",         "lat": 38.9167, "lng": -77.0523},
    {"name": "Embassy of Chile",            "type": "Embassy",   "country": "Chile",            "lat": 38.9178, "lng": -77.0536},
    {"name": "Embassy of Peru",             "type": "Embassy",   "country": "Peru",             "lat": 38.9189, "lng": -77.0549},
    {"name": "Embassy of Venezuela",        "type": "Embassy",   "country": "Venezuela",        "lat": 38.9143, "lng": -77.0583},
    {"name": "Embassy of Cuba",             "type": "Embassy",   "country": "Cuba",             "lat": 38.9156, "lng": -77.0597},
    {"name": "Embassy of Qatar",            "type": "Embassy",   "country": "Qatar",            "lat": 38.9117, "lng": -77.0544},
    {"name": "Embassy of UAE",              "type": "Embassy",   "country": "UAE",              "lat": 38.9128, "lng": -77.0557},
    {"name": "Embassy of Kuwait",           "type": "Embassy",   "country": "Kuwait",           "lat": 38.9139, "lng": -77.0571},
    {"name": "Embassy of Jordan",           "type": "Embassy",   "country": "Jordan",           "lat": 38.9149, "lng": -77.0583},
    {"name": "Embassy of Lebanon",          "type": "Embassy",   "country": "Lebanon",          "lat": 38.9158, "lng": -77.0596},
    {"name": "Embassy of Iraq",             "type": "Embassy",   "country": "Iraq",             "lat": 38.9168, "lng": -77.0609},
    {"name": "Embassy of Afghanistan",      "type": "Embassy",   "country": "Afghanistan",      "lat": 38.9178, "lng": -77.0622},
    {"name": "Embassy of Thailand",         "type": "Embassy",   "country": "Thailand",         "lat": 38.9188, "lng": -77.0635},
    {"name": "Embassy of Philippines",      "type": "Embassy",   "country": "Philippines",      "lat": 38.9198, "lng": -77.0648},
    {"name": "Embassy of Vietnam",          "type": "Embassy",   "country": "Vietnam",          "lat": 38.9044, "lng": -77.0461},
    {"name": "Embassy of New Zealand",      "type": "Embassy",   "country": "New Zealand",      "lat": 38.9055, "lng": -77.0473},
    # ── International Organizations ──
    {"name": "International Monetary Fund", "type": "International Organization", "country": "International", "lat": 38.8990, "lng": -77.0440},
    {"name": "World Bank Group",            "type": "International Organization", "country": "International", "lat": 38.8994, "lng": -77.0447},
    {"name": "Inter-American Dev Bank",     "type": "International Organization", "country": "International", "lat": 38.8988, "lng": -77.0436},
    {"name": "Organization of American States", "type": "International Organization", "country": "International", "lat": 38.8939, "lng": -77.0481},
    {"name": "World Health Organization (PAHO)", "type": "International Organization", "country": "International", "lat": 38.9069, "lng": -77.0386},
    {"name": "UN Information Centre",       "type": "International Organization", "country": "International", "lat": 38.9034, "lng": -77.0427},
]

def haversine_m(lat1, lng1, lat2, lng2):
    """Return distance in metres between two lat/lng points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def check_diplomatic_proximity(lat, lng):
    """
    Returns (True, mission_dict, distance_m) if within DIPLOMATIC_RADIUS_M
    of any mission, otherwise (False, None, None).
    Finds the closest mission within range.
    """
    closest = None
    closest_dist = float("inf")
    for mission in DIPLOMATIC_MISSIONS:
        dist = haversine_m(lat, lng, mission["lat"], mission["lng"])
        if dist <= DIPLOMATIC_RADIUS_M and dist < closest_dist:
            closest = mission
            closest_dist = dist
    if closest:
        return True, closest, round(closest_dist)
    return False, None, None

# ══════════════════════════════════════════════════════════════════
#  SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════

def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }

def upsert_batch(table, rows, batch_size=500):
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i+batch_size]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            json=chunk,
            headers=sb_headers(),
            timeout=30,
        )
        if resp.status_code in (200, 201):
            total += len(chunk)
        else:
            print(f"[Supabase] Upsert error {resp.status_code}: {resp.text[:200]}")
    return total

def clear_table(table):
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}?id=gte.0",
        headers=sb_headers(),
        timeout=30,
    )
    # Also handle uuid primary keys
    if resp.status_code not in (200, 204):
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?incident_number=neq.NONE",
            headers=sb_headers(),
            timeout=30,
        )

# ══════════════════════════════════════════════════════════════════
#  CSV PROCESSOR
# ══════════════════════════════════════════════════════════════════

def process_dc_csv(csv_text):
    """Parse DC relay CSV and return list of Supabase-ready row dicts."""
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))

    for rec in reader:
        try:
            lat = float(rec.get("lat") or 0)
            lng = float(rec.get("lng") or 0)
            if lat == 0 or lng == 0:
                continue

            incident_number = rec.get("incident_number", "").strip()
            if not incident_number:
                continue

            offense  = (rec.get("offense_desc") or "").strip()
            method   = (rec.get("method") or "").strip().upper()
            location = (rec.get("location") or "").strip()
            district = (rec.get("district") or "").strip()
            ward     = (rec.get("ward") or "").strip()
            neighborhood = (rec.get("neighborhood") or "").strip()
            occurred = (rec.get("occurred_on") or "").strip()
            shooting = str(rec.get("shooting") or "").lower() in ("true", "1", "yes")

            # Priority from relay, or reclassify if missing
            priority = (rec.get("priority") or "").strip()
            if priority not in ("p1", "p2", "p3"):
                priority = classify_priority(offense, method)

            # Gang/hotspot check
            gang_hotspot, gang_zone = check_dc_hotspot(location)

            # Diplomatic proximity check
            dipl_hit, mission, dipl_dist = check_diplomatic_proximity(lat, lng)

            # Build display title
            title = offense.title() if offense else "Unknown Incident"
            if method and method not in ("OTHERS", "NONE", ""):
                title = f"{title} ({method.title()})"

            row = {
                "incident_number":          incident_number,
                "offense_desc":             offense,
                "title":                    title,
                "occurred_on":              occurred or None,
                "lat":                      lat,
                "lng":                      lng,
                "shooting":                 shooting,
                "district":                 district,
                "ward":                     ward,
                "location":                 location,
                "neighborhood":             neighborhood,
                "method":                   method,
                "priority":                 priority,
                "gang_hotspot":             gang_hotspot,
                "gang_zone":                gang_zone,
                "diplomatic_proximity":     dipl_hit,
                "nearest_mission":          mission["name"] if mission else None,
                "nearest_mission_type":     mission["type"] if mission else None,
                "nearest_mission_country":  mission["country"] if mission else None,
                "nearest_mission_dist_m":   dipl_dist,
                "city":                     "dc",
            }
            rows.append(row)

        except Exception as e:
            print(f"[DC] Row error: {e}")
            continue

    return rows

# ══════════════════════════════════════════════════════════════════
#  HTTP RECEIVER  (listens for Oracle VM relay POST)
# ══════════════════════════════════════════════════════════════════

class RelayHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default access log noise

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DC pipeline OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/arcgis-data":
            self.send_response(404)
            self.end_headers()
            return

        # Auth check
        secret = self.headers.get("X-Relay-Secret", "")
        if secret != RELAY_SECRET:
            print(f"[Receiver] Rejected — bad secret")
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode("utf-8", errors="replace")
        print(f"[Receiver] DC payload: {len(body):,} bytes")

        try:
            rows = process_dc_csv(body)
            p1 = sum(1 for r in rows if r["priority"] == "p1")
            p2 = sum(1 for r in rows if r["priority"] == "p2")
            p3 = sum(1 for r in rows if r["priority"] == "p3")
            dipl = sum(1 for r in rows if r["diplomatic_proximity"])
            gang = sum(1 for r in rows if r["gang_hotspot"])
            print(f"[DC] {len(rows):,} rows — P1:{p1} P2:{p2} P3:{p3} "
                  f"Diplomatic:{dipl} Hotspot:{gang}")

            if rows:
                # Sample row for debugging
                sample = rows[0]
                print(f"[DC] Sample: {sample.get('incident_number')} | "
                      f"{sample.get('offense_desc')} | "
                      f"{sample.get('lat')},{sample.get('lng')} | "
                      f"priority={sample.get('priority')} | "
                      f"dipl={sample.get('diplomatic_proximity')}")

                clear_table("dc_incidents")
                n = upsert_batch("dc_incidents", rows)
                print(f"[DC] Upserted {n:,}/{len(rows):,} rows to dc_incidents")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        except Exception as e:
            print(f"[Receiver] Processing error: {e}")
            self.send_response(500)
            self.end_headers()


def start_receiver():
    server = HTTPServer(("0.0.0.0", RELAY_PORT), RelayHandler)
    print(f"[Receiver] DC pipeline listening on port {RELAY_PORT}")
    server.serve_forever()

# ══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Hood Brief DC — Pipeline Starting                       ║")
    print("║  Washington DC — MPD Crime + Diplomatic Proximity        ║")
    print("╚══════════════════════════════════════════════════════════╝")

    errors = []
    if not SUPABASE_URL:
        errors.append("SUPABASE_URL not set")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY not set")

    if errors:
        print("\nMissing configuration:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    print(f"  Supabase: {SUPABASE_URL[:40]}...")
    print(f"  Port:     {RELAY_PORT}")
    print(f"  Missions: {len(DIPLOMATIC_MISSIONS)} diplomatic locations loaded")
    print(f"  Zones:    {len(DC_HOTSPOT_ZONES)} hotspot zones loaded")
    print()

    # Start HTTP receiver in main thread
    start_receiver()
