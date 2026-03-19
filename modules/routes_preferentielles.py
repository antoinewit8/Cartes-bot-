import os
import json
import math
import unicodedata
import re
import requests
from modules.villes_jalons import detecter_villes_jalons


# ==========================================
# CONFIG
# ==========================================
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
JSON_PATH  = os.path.join(BASE_DIR, "..", "routes_preferentielles.json")
CACHE_PATH = os.path.join(BASE_DIR, "..", "cache_geocodage.json")

PTV_API_KEY = os.environ.get("PTV_API_KEY", "")
PTV_GEO_URL = "https://api.myptv.com/geocoding/v1/locations/by-text"
RAYON_KM    = 50

# ==========================================
# CACHE PERSISTANT
# ==========================================
def charger_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def sauvegarder_cache(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"⚠️ Impossible de sauvegarder le cache : {e}")

_geocache: dict = charger_cache()

# ==========================================
# CHARGEMENT JSON (une seule fois)
# ==========================================
_routes_cache: list | None = None

def charger_routes() -> list:
    global _routes_cache
    if _routes_cache is not None:
        return _routes_cache

    path = os.path.abspath(JSON_PATH)
    if not os.path.exists(path):
        print(f"⚠️ routes_preferentielles.json introuvable : {path}")
        _routes_cache = []
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            _routes_cache = json.load(f)
            return _routes_cache
    except json.JSONDecodeError as e:
        print(f"⚠️ Erreur lecture JSON : {e}")
        _routes_cache = []
        return []

# ==========================================
# NORMALISATION
# ==========================================
def normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"['\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ==========================================
# GÉOCODAGE PTV
# ==========================================
def geocoder_ville(ville: str) -> tuple[float, float] | None:
    key = normalize(ville)

    if key in _geocache:
        coords = _geocache[key]
        print(f"      📦 Cache hit '{ville}' → ({coords[0]:.4f}, {coords[1]:.4f})")
        return (coords[0], coords[1])

    if not PTV_API_KEY:
        print("⚠️ PTV_API_KEY manquante")
        return None

    try:
        params = {"searchText": ville, "language": "fr"}

        # ── Détecter le pays ──
        ville_lower = ville.lower()
        COUNTRY_KEYWORDS = {
            "FR": ["france"],
            "BE": ["belgium", "belgique", "belgi"],
            "DE": ["germany", "deutschland", "allemagne"],
            "NL": ["netherlands", "nederland", "pays-bas"],
            "LU": ["luxembourg"],
            "IT": ["italy", "italia", "itali"],
            "ES": ["spain", "espagne", "españa"],
            "PT": ["portugal"],
            "GB": ["united kingdom", "uk", "angleterre"],
            "CH": ["switzerland", "suisse", "schweiz"],
            "AT": ["austria", "autriche"],
            "PL": ["poland", "pologne"],
            "CZ": ["czech republic", "tchéquie"],
        }

        country_filter = None
        for iso, keywords in COUNTRY_KEYWORDS.items():
            if any(kw in ville_lower for kw in keywords):
                country_filter = iso
                break

        if not country_filter:
            country_filter = "FR"

        params["countryFilter"] = country_filter

        response = requests.get(
            PTV_GEO_URL,
            headers={"apiKey": PTV_API_KEY},
            params=params,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        locations = data.get("locations", [])

        if not locations:
            print(f"      ⚠️ Aucun résultat géocodage pour '{ville}'")
            return None

        # ── Filtrer : privilégier match sur nom de ville, pas rue ──
        ville_nom = ville.split(",")[0].strip()
        ville_nom_norm = normalize(ville_nom)

        best = None

        # 1. Match sur le nom de ville
        for loc in locations:
            addr = loc.get("address", {})
            city_norm = normalize(addr.get("city", ""))
            if ville_nom_norm in city_norm or city_norm in ville_nom_norm:
                best = loc
                break

        # 2. Sinon résultat sans rue (= localité)
        if not best:
            for loc in locations:
                if not loc.get("address", {}).get("street"):
                    best = loc
                    break

        # 3. Fallback premier résultat
        if not best:
            best = locations[0]
            addr = best.get("address", {})
            print(f"      ⚠️ Fallback: '{ville}' → {addr.get('city', '?')}, rue={addr.get('street', '')}")

        print(f"      🧪 RAW PTV '{ville}': {json.dumps(best)[:300]}")

        ref_pos = best.get("referencePosition", {})
        lat = ref_pos.get("lat") or ref_pos.get("latitude")
        lon = ref_pos.get("lon") or ref_pos.get("longitude")

        if lat is None or lon is None:
            print(f"      ⚠️ Structure inattendue: {ref_pos}")
            return None

        _geocache[key] = [lat, lon]
        sauvegarder_cache(_geocache)
        print(f"      📍 Géocodé '{ville}' → ({lat:.4f}, {lon:.4f})")
        return (lat, lon)

    except requests.RequestException as e:
        print(f"      ⚠️ Erreur géocodage PTV '{ville}' : {e}")
        return None


# ==========================================
# DISTANCE HAVERSINE
# ==========================================
def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat/2)**2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(d_lon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

# ==========================================
# FONCTION PRINCIPALE (remplace l'ancienne)
# ==========================================
def get_waypoints(origin: str, dest: str) -> list:
    routes = charger_routes()
    print(f"   🔍 Recherche route préférentielle : '{origin}' → '{dest}'")

    coords_origin = geocoder_ville(origin)
    coords_dest   = geocoder_ville(dest)
    norm_origin   = normalize(origin)
    norm_dest     = normalize(dest)

    # ── 1. Chercher route manuelle exacte ──
    for route in routes:
        origine_ref = route.get("origine", "")
        dest_ref    = route.get("destination", "")

        # Matching départ
        match_dep = False
        if coords_origin:
            coords_ref_dep = geocoder_ville(origine_ref)
            if coords_ref_dep:
                dist_dep = haversine(
                    coords_origin[0], coords_origin[1],
                    coords_ref_dep[0], coords_ref_dep[1]
                )
                match_dep = dist_dep <= RAYON_KM
            else:
                match_dep = normalize(origine_ref) == norm_origin
        else:
            match_dep = normalize(origine_ref) == norm_origin

        if not match_dep:
            continue

        # Matching arrivée
        match_arr = False
        if coords_dest:
            coords_ref_arr = geocoder_ville(dest_ref)
            if coords_ref_arr:
                dist_arr = haversine(
                    coords_dest[0], coords_dest[1],
                    coords_ref_arr[0], coords_ref_arr[1]
                )
                match_arr = dist_arr <= RAYON_KM
            else:
                match_arr = normalize(dest_ref) == norm_dest
        else:
            match_arr = normalize(dest_ref) == norm_dest

        if match_dep and match_arr:
            waypoints = route.get("waypoints", [])
            print(f"   ✅ Route manuelle trouvée : {origine_ref} → {dest_ref} "
                  f"({len(waypoints)} waypoints)")
            return waypoints

    # ── 2. Sinon : détection automatique villes-jalons ──
    if coords_origin and coords_dest:
        print(f"   🔄 Pas de route manuelle → détection villes-jalons...")
        jalons = detecter_villes_jalons(
            coords_origin[0], coords_origin[1],
            coords_dest[0], coords_dest[1]
        )
        if jalons:
            print(f"   ✅ {len(jalons)} villes-jalons détectées automatiquement")
            return jalons

    print(f"   📍 Aucune route préférentielle → PTV choisit le trajet")
    return []
