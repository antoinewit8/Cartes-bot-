import requests
import time
import os
import re
import json
from dotenv import load_dotenv

load_dotenv()

PTV_API_KEY = os.environ.get("PTV_API_KEY", "METS_TA_CLE_ICI")
BASE_URL = "https://api.myptv.com/routing/v1"
GEOCODE_URL = "https://api.myptv.com/geocoding/v1"

MAX_RETRIES = 3
RETRY_DELAY = 2
VEHICLE_PROFILE = "EUR_TRAILER_TRUCK"

HEADERS = {"apiKey": PTV_API_KEY}

PAYS_TO_ISO = {
    "france": "FR", "belgium": "BE", "germany": "DE", "netherlands": "NL",
    "luxembourg": "LU", "italy": "IT", "spain": "ES", "portugal": "PT",
    "united kingdom": "GB", "switzerland": "CH", "austria": "AT",
    "poland": "PL", "czech republic": "CZ", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "slovakia": "SK", "slovenia": "SI",
    "croatia": "HR", "denmark": "DK", "sweden": "SE", "norway": "NO",
    "finland": "FI", "ireland": "IE", "greece": "GR",
}

GPS_FIXES = {
    "basse indre": (47.2055, -1.6694),
    "indre":       (47.2055, -1.6694),
    "rumbek":      (50.9441,  3.1214),
    "rumbeke":     (50.9441,  3.1214),
}


# ─────────────────────────────────────────────
# RÉSOLUTION GPS FIXE
# ─────────────────────────────────────────────

def resolve_gps_fix(address: str):
    address_lower = address.strip().lower()
    if address_lower in GPS_FIXES:
        return GPS_FIXES[address_lower]
    ville = address_lower.split(",")[0].strip()
    if ville in GPS_FIXES:
        return GPS_FIXES[ville]
    return None


# ─────────────────────────────────────────────
# PARSE ORIGIN
# ─────────────────────────────────────────────

def parse_origin(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    parts = raw.split()
    city_guess = parts[-1].lower() if parts else ""
    if city_guess in GPS_FIXES:
        lat, lon = GPS_FIXES[city_guess]
        return {"lat": lat, "lon": lon, "raw": raw}
    return geocode_address(raw)


# ─────────────────────────────────────────────
# GEOCODAGE PAR CODE POSTAL
# ─────────────────────────────────────────────

def geocode_by_postal_code(postal_code, country_code):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{GEOCODE_URL}/locations/by-postal-code",
                params={"postalCode": postal_code, "countryCode": country_code},
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            if resp.status_code in (404, 400):
                return None
            resp.raise_for_status()
            data = resp.json()
            locations = data.get("locations", [])
            if locations:
                ref = locations[0].get("referencePosition", {})
                lat = ref.get("latitude")
                lon = ref.get("longitude")
                if lat and lon:
                    return (lat, lon)
            return None
        except Exception:
            time.sleep(RETRY_DELAY)
    return None


# ─────────────────────────────────────────────
# GEOCODAGE BY-TEXT
# ─────────────────────────────────────────────

def _geocode_by_text(address):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                f"{GEOCODE_URL}/locations/by-text",
                params={"searchText": address},
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("locations"):
                lat = data["locations"][0]["referencePosition"]["latitude"]
                lon = data["locations"][0]["referencePosition"]["longitude"]
                return (lat, lon)
            return None
        except Exception:
            time.sleep(RETRY_DELAY)
    return None


# ─────────────────────────────────────────────
# GEOCODAGE PRINCIPAL
# ─────────────────────────────────────────────

def geocode_address(address):
    address = str(address).strip()

    match_gps = re.match(r'^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$', address)
    if match_gps:
        return (float(match_gps.group(1)), float(match_gps.group(2)))

    fix = resolve_gps_fix(address)
    if fix:
        return fix

    match_cp_only  = re.match(r'^(\d{4,7}),\s*(.+)$', address)
    match_ville_cp = re.match(r'^(.+),\s*(\d{4,7}),\s*(.+)$', address)

    if match_ville_cp:
        ville       = match_ville_cp.group(1).strip()
        cp          = match_ville_cp.group(2).strip()
        pays_str    = match_ville_cp.group(3).strip()
        country_iso = PAYS_TO_ISO.get(pays_str.lower())

        result = _geocode_by_text(f"{ville}, {pays_str}")
        if result:
            return result
        if country_iso:
            result = geocode_by_postal_code(cp, country_iso)
            if result:
                return result
        return _geocode_by_text(address)

    if match_cp_only:
        cp          = match_cp_only.group(1).strip()
        pays_raw    = match_cp_only.group(2).strip().lower()
        country_iso = PAYS_TO_ISO.get(pays_raw)

        if country_iso:
            result = geocode_by_postal_code(cp, country_iso)
            if result:
                return result
        return _geocode_by_text(address)

    return _geocode_by_text(address)


# ─────────────────────────────────────────────
# CALCUL ROUTE KM
# ─────────────────────────────────────────────

def decode_polyline(encoded: str) -> list:
    coords = []
    index, lat, lon = 0, 0, 0
    while index < len(encoded):
        for is_lon in [False, True]:
            result, shift = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            value = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += value
            else:
                lat += value
        coords.append([lat / 1e5, lon / 1e5])
    return coords

def calculate_km_route(lat_start, lon_start, lat_end, lon_end, waypoints=None, calculer_peage=False):
    """
    Calcule l'itinéraire via PTV API (GET) avec le paramètre waypoints et un radius de tolérance.
    """
    
    # 1. Géocodage des waypoints intermédiaires
    waypoints_coords = []
    if waypoints:
        for wp_address in waypoints:
            if isinstance(wp_address, (list, tuple)) and len(wp_address) == 2:
                try:
                    lat, lon = float(wp_address[0]), float(wp_address[1])
                    waypoints_coords.append((lat, lon))
                    print(f"      📌 Waypoint direct : ({lat:.4f}, {lon:.4f})")
                    continue
                except (ValueError, TypeError):
                    pass
            
            if isinstance(wp_address, str) and "," in wp_address:
                parts = wp_address.split(",")
                if len(parts) == 2:
                    try:
                        lat, lon = float(parts[0].strip()), float(parts[1].strip())
                        waypoints_coords.append((lat, lon))
                        print(f"      📌 Waypoint GPS : ({lat:.4f}, {lon:.4f})")
                        continue
                    except ValueError:
                        pass
            
            coords = geocode_address(wp_address)
            if coords:
                waypoints_coords.append(coords)
                print(f"      📌 Waypoint géocodé : {wp_address} → {coords}")
            else:
                print(f"      ⚠️  Waypoint ignoré : {wp_address}")

    # 2. Préparation des paramètres pour la requête GET
    results_values = ["POLYLINE"]
    if calculer_peage:
        results_values.append("TOLL_COSTS")

    query_params = [
        ("profile", VEHICLE_PROFILE),
        ("results", ",".join(results_values)),
    ]
    if calculer_peage:
        query_params.append(("options[currency]", "EUR"))

    # 3. Rassemblement de tous les points (Départ + Intermédiaires + Arrivée)
    all_points = [(lat_start, lon_start)] + waypoints_coords + [(lat_end, lon_end)]
    print(f"      🗺️  {len(all_points)} points au total ({len(waypoints_coords)} intermédiaires)")

    # 4. Ajout des points avec "radius=5000" pour les étapes intermédiaires
    for i, (lat, lon) in enumerate(all_points):
        if 0 < i < len(all_points) - 1:
            # Étape intermédiaire : on laisse 5km de tolérance
            query_params.append(("waypoints", f"{lat},{lon};radius=5000"))
        else:
            # Vrai Départ / Vraie Arrivée : précision stricte
            query_params.append(("waypoints", f"{lat},{lon}"))

    # 5. Appel à l'API PTV
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # On utilise GET comme sur le serveur Render
            response = requests.get(
                f"{BASE_URL}/routes",
                headers=HEADERS,
                params=query_params,
                timeout=30
            )
            
            print(f"      🔗 PTV Status: {response.status_code}")

            if response.status_code != 200:
                print(f"      ❌ PTV Erreur: {response.text[:500]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                return None

            data = response.json()

            km = round(data.get("distance", 0) / 1000, 1)
            travel_time_h = round(data.get("travelTime", 0) / 3600, 2)

            # Extraction de la ligne pour la carte
            polyline_raw = data.get("polyline", None)
            polyline_coords = []

            if polyline_raw:
                try:
                    geojson_data = json.loads(polyline_raw) if isinstance(polyline_raw, str) else polyline_raw
                    if "coordinates" in geojson_data:
                        polyline_coords = [[lat, lon] for lon, lat in geojson_data["coordinates"]]
                        print(f"      📐 Polyline : {len(polyline_coords)} points")
                except Exception as e:
                    print(f"      ⚠️ Extraction polyline échouée : {e}")

            # Extraction du péage
            prix_peage = 0.0
            if calculer_peage:
                toll_data = data.get("toll", {}).get("costs", {})
                prix_peage = (
                    toll_data.get("convertedPrice", {}).get("price")
                    or toll_data.get("prices", [{}])[0].get("price")
                    or 0.0
                )

            return {
                "km":              km,
                "travel_time_h":   travel_time_h,
                "violated":        data.get("violated", False),
                "polyline":        polyline_raw,
                "polyline_coords": polyline_coords,
                "prix_peage":      round(float(prix_peage), 2)
            }

        except Exception as e:
            print(f"      ⚠️  Tentative {attempt}/{MAX_RETRIES} : {e}")
            time.sleep(RETRY_DELAY)

    return None




# ─────────────────────────────────────────────
# FONCTION PRINCIPALE
# ─────────────────────────────────────────────

def get_route(origin_address, dest_address):
    origin_coords = geocode_address(origin_address)
    if not origin_coords:
        return None
    dest_coords = geocode_address(dest_address)
    if not dest_coords:
        return None
    return calculate_km_route(
        origin_coords[0], origin_coords[1],
        dest_coords[0], dest_coords[1]
    )
