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
    ville_clean = ville
    ville_clean = re.sub(r'\bST\b', 'SAINT', ville_clean, flags=re.IGNORECASE)
    ville_clean = re.sub(r'\bSTE\b', 'SAINTE', ville_clean, flags=re.IGNORECASE)
    
    key = normalize(ville_clean)

    if key in _geocache:
        coords = _geocache[key]
        print(f"      📦 Cache hit '{ville}' → ({coords[0]:.4f}, {coords[1]:.4f})")
        return (coords[0], coords[1])

    if not PTV_API_KEY:
        print("⚠️ PTV_API_KEY manquante")
        return None

    try:
        # ── Construire les paramètres PTV ──
        parts = [p.strip() for p in ville_clean.split(',')]

        if len(parts) >= 2:
            city_name = parts[0]
            cp = parts[1] if parts[1].strip().isdigit() else ""
            pays = parts[-1].strip() if len(parts) >= 3 else ""
            
            # Construire searchText avec ville + CP
            search = city_name
            if cp:
                search = f"{city_name}, {cp}"
            params = {"searchText": search}
            
            country_filter = ""
            if pays:
                country_map = {
                    "france": "FR", "belgium": "BE", "germany": "DE",
                    "netherlands": "NL", "luxembourg": "LU", "italy": "IT",
                    "spain": "ES", "switzerland": "CH", "austria": "AT",
                }
                country_filter = country_map.get(pays.lower(), "")
            if country_filter:
                params["countryFilter"] = country_filter
        else:
            params = {"searchText": ville_clean, "countryFilter": "FR"}


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

        ville_nom = ville.split(",")[0].strip()
        ville_nom_norm = normalize(ville_nom)
        best = None

        # 1) Match ville exact, sans rue parasite
        for loc in locations:
            addr = loc.get("address", {})
            city_norm = normalize(addr.get("city", ""))
            street = addr.get("street", "")
            street_norm = normalize(street)
            if ville_nom_norm in city_norm or city_norm in ville_nom_norm:
                # Rejeter si c'est juste une "Rue de Saint-Quentin" dans une autre ville
                if ville_nom_norm not in street_norm:
                    best = loc
                    break

        # 2) Match ville même avec rue
        if not best:
            for loc in locations:
                addr = loc.get("address", {})
                city_norm = normalize(addr.get("city", ""))
                if ville_nom_norm in city_norm or city_norm in ville_nom_norm:
                    best = loc
                    break

        # 3) Résultat sans rue
        if not best:
            for loc in locations:
                if not loc.get("address", {}).get("street"):
                    best = loc
                    break

        # 4) Dernier fallback
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
        norm_orig_ref = normalize(origine_ref)
        norm_dest_ref = normalize(dest_ref)

        match_dep = (norm_orig_ref == norm_origin)
        match_arr = (norm_dest_ref == norm_dest)

        # ── Match parfait texte → retourner direct ──
        if match_dep and match_arr:
            waypoints = route.get("waypoints", [])
            print(f"   ✅ Route manuelle trouvée : {origine_ref} → {dest_ref} "
                  f"({len(waypoints)} waypoints)")
            return waypoints

        # ── Skip si aucun match texte et pas de coords pour comparer ──
        if not match_dep and not coords_origin:
            continue
        if not match_arr and not coords_dest:
            continue

        # ── Géocoder seulement si nécessaire ──
        if not match_dep:
            coords_ref_dep = geocoder_ville(origine_ref)
            if not coords_ref_dep:
                continue
            if haversine(coords_origin[0], coords_origin[1],
                        coords_ref_dep[0], coords_ref_dep[1]) > RAYON_KM:
                continue
            match_dep = True

        if not match_arr:
            coords_ref_arr = geocoder_ville(dest_ref)
            if not coords_ref_arr:
                continue
            if haversine(coords_dest[0], coords_dest[1],
                        coords_ref_arr[0], coords_ref_arr[1]) > RAYON_KM:
                continue
            match_arr = True

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
