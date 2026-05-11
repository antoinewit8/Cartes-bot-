"""
Serveur de cartes interactives.
Déployable sur Render.com (gratuit) → URL publique permanente.
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import uvicorn, uuid, json, os, httpx, pathlib
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = pathlib.Path(__file__).resolve().parent

app = FastAPI(title="Arcelor Route Map Server")

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


ROUTES_FILE = "data/routes.json"
os.makedirs("data", exist_ok=True)

PTV_API_KEY    = os.environ.get("PTV_API_KEY", "")
MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")
FIREBASE_URL   = os.environ.get("FIREBASE_URL", "").rstrip("/")


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES PRÉFÉRENTIELLES
# ══════════════════════════════════════════════════════════════════════════════

PREF_ROUTES_FILE = "routes_preferentielles.json"

def load_pref_routes() -> list:
    """Charge le fichier JSON des routes préférentielles."""
    if not os.path.exists(PREF_ROUTES_FILE):
        return []
    with open(PREF_ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def find_pref_waypoints(origin: str, dest: str) -> list:
    """Retourne les waypoints préférentiels pour un trajet, ou []."""
    prefs = load_pref_routes()
    o = origin.strip().lower()
    d = dest.strip().lower()
    for route in prefs:
        if (route["origine"].strip().lower() == o
                and route["destination"].strip().lower() == d):
            wps = []
            for wp in route.get("waypoints", []):
                parts = wp.split(",")
                if len(parts) == 2:
                    wps.append({
                        "lat": float(parts[0].strip()),
                        "lng": float(parts[1].strip()),
                    })
            return wps
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  STOCKAGE (Firebase ou fichier local)
# ══════════════════════════════════════════════════════════════════════════════

def load_routes() -> dict:
    """Charge toutes les routes depuis Firebase ou fichier local."""
    if FIREBASE_URL:
        try:
            r = httpx.get(f"{FIREBASE_URL}/routes.json", timeout=30)
            if r.status_code == 200 and r.json():
                return r.json()
        except Exception as e:
            print(f"Erreur lecture Firebase : {e}")
        return {}

    if not os.path.exists(ROUTES_FILE):
        return {}
    with open(ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_routes(data: dict):
    """Sauvegarde toutes les routes vers Firebase ou fichier local."""
    if FIREBASE_URL:
        try:
            httpx.patch(f"{FIREBASE_URL}/routes.json", json=data, timeout=30)
        except Exception as e:
            print(f"Erreur écriture Firebase : {e}")
        return

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_route(route_id: str) -> dict:
    """Télécharge une seule route depuis Firebase (ultra rapide)."""
    if FIREBASE_URL:
        try:
            r = httpx.get(f"{FIREBASE_URL}/routes/{route_id}.json", timeout=20)
            if r.status_code == 200 and r.json():
                return r.json()
        except Exception as e:
            print(f"Erreur lecture Firebase pour la route {route_id} : {e}")
        return None

    # Fallback local
    routes = load_routes()
    return routes.get(route_id)


# ══════════════════════════════════════════════════════════════════════════════
#  MODÈLES PYDANTIC
# ══════════════════════════════════════════════════════════════════════════════

class RouteCreate(BaseModel):
    origin:         str
    dest:           str
    distance_km:    float
    duration_h:     float
    polyline:       list
    prix_peage:     float = 0.0
    pref_waypoints: list  = []

class RouteRecalc(BaseModel):
    origin:         str
    dest:           str
    avoid_tolls:    bool = False
    avoid_highways: bool = False

class WaypointItem(BaseModel):
    lat: float
    lng: float

class RecalcDragRequest(BaseModel):
    waypoints:      List[WaypointItem]
    avoid_tolls:    bool = False
    avoid_highways: bool = False
    route_id:       Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS PTV
# ══════════════════════════════════════════════════════════════════════════════

def _extract_polyline(ptv: dict) -> list:
    """Extrait les coordonnées [[lat, lon], ...] depuis la réponse PTV."""
    polyline_raw = ptv.get("polyline", "")

    if isinstance(polyline_raw, dict):
        if polyline_raw.get("type") == "LineString":
            return [[c[1], c[0]] for c in polyline_raw.get("coordinates", [])]
        if "plain" in polyline_raw:
            raw = polyline_raw["plain"].get("pointsByCoordinates", [])
            return [[raw[i + 1], raw[i]] for i in range(0, len(raw) - 1, 2)]
        if "encodedPolyline" in polyline_raw:
            return _decode_polyline(polyline_raw["encodedPolyline"])
        return []

    if isinstance(polyline_raw, str) and polyline_raw:
        try:
            parsed = json.loads(polyline_raw)
            if isinstance(parsed, dict) and parsed.get("type") == "LineString":
                return [[c[1], c[0]] for c in parsed.get("coordinates", [])]
        except (json.JSONDecodeError, TypeError):
            pass
        return _decode_polyline(polyline_raw)

    return []

def _extract_distance_duration(ptv: dict):
    """Retourne (distance_m, duration_s) depuis la réponse PTV."""
    legs = ptv.get("legs", [])
    if legs:
        distance_m = sum(leg.get("distance", 0) for leg in legs)
        duration_s = sum(leg.get("travelTime", 0) for leg in legs)
    else:
        distance_m = ptv.get("distance", 0)
        duration_s = ptv.get("travelTime", 0)
    return distance_m, duration_s

def _extract_toll(ptv: dict) -> float:
    """Extrait le prix de péage depuis la réponse PTV."""
    toll_data = ptv.get("toll", {}).get("costs", {})
    if isinstance(toll_data, dict):
        return toll_data.get("convertedPrice", {}).get("price", 0)
    return 0

def _decode_polyline(encoded: str) -> list:
    """Décode Google encoded polyline → [[lat, lon], ...]."""
    coords, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for is_lng in [False, True]:
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        coords.append([lat / 1e5, lng / 1e5])
    return coords

async def _geocode(address: str) -> Optional[list]:
    """Géocode une adresse via PTV → [lat, lng]."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/geocoding/v1/locations/by-text",
            headers={"apiKey": PTV_API_KEY},
            params={"searchText": address, "countryFilter": "FRA,BEL,LUX,DEU,ESP"},
            timeout=15,
        )
    if resp.status_code != 200:
        return None
    results = resp.json().get("locations", [])
    if not results:
        return None
    loc = results[0]["referencePosition"]
    return [loc["latitude"], loc["longitude"]]

async def _call_ptv(waypoints_list: list, avoid_tolls: bool, avoid_highways: bool) -> dict:
    """Appel PTV routing v1 GET — waypoints répétés en query string."""
    query_params = [
        ("profile", "EUR_TRAILER_TRUCK"),
        ("results", "POLYLINE,TOLL_COSTS"),
        ("options[currency]", "EUR"),
    ]

    for i, wp_str in enumerate(waypoints_list):
        parts = wp_str.split(",")
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
        if 0 < i < len(waypoints_list) - 1:
            query_params.append(("waypoints", f"{lat},{lng};radius=5000"))
        else:
            query_params.append(("waypoints", f"{lat},{lng}"))

    avoid = []
    if avoid_tolls:    avoid.append("TOLL_ROADS")
    if avoid_highways: avoid.append("HIGHWAYS")
    if avoid:
        query_params.append(("options[avoid]", ",".join(avoid)))

    print(f"PTV QUERY: {query_params}")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/routing/v1/routes",
            headers={"apiKey": PTV_API_KEY},
            params=query_params,
            timeout=30,
        )

    if resp.status_code != 200:
        print(f"PTV ERROR {resp.status_code}: {resp.text[:1000]}")
        raise HTTPException(502, f"PTV error {resp.status_code}: {resp.text[:500]}")

    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Créer une route ──────────────────────────────────────────────────────────
@app.post("/api/create_route")
async def create_route(route: RouteCreate):
    route_id = uuid.uuid4().hex[:8]

    # ✅ CORRIGÉ : route_data créé AVANT d'être utilisé
    route_data = route.dict()
    route_data["polyline_original"] = route.polyline  # immuable, jamais écrasé
    route_data["polyline_current"]  = route.polyline 
    route_data["distance_km_original"] = route.distance_km
    route_data["duration_h_original"]  = route.duration_h
    route_data["prix_peage_original"]  = route.prix_peage 

    routes = {route_id: route_data}
    save_routes(routes)

    url = f"{MAP_SERVER_URL}/carte?id={route_id}"
    return {"url": url, "id": route_id}


# ── Afficher la carte ────────────────────────────────────────────────────────
@app.get("/carte")
async def show_map(request: Request, id: str):
    route = get_route(id)
    if not route:
        raise HTTPException(status_code=404, detail="Trajet introuvable")

    # ✅ Rétrocompat : anciennes routes sans polyline_original
    if "polyline_original" not in route:
        route["polyline_original"] = route.get("polyline", [])
        route["polyline_current"]  = route.get("polyline", [])

    return templates.TemplateResponse("map.html", {
        "request":    request,
        "route":      route,
        "route_id":   id,
        "server_url": MAP_SERVER_URL,
    })


# ── Recalcul standard (origine / destination texte) ─────────────────────────
@app.post("/api/recalculate")
async def recalculate(data: RouteRecalc):
    origin_coords = await _geocode(data.origin)
    dest_coords   = await _geocode(data.dest)

    if not origin_coords or not dest_coords:
        raise HTTPException(status_code=400, detail="Géocodage impossible")

    pref_wps = find_pref_waypoints(data.origin, data.dest)

    waypoints_list = [f"{origin_coords[0]},{origin_coords[1]}"]
    for wp in pref_wps:
        waypoints_list.append(f"{wp['lat']},{wp['lng']}")
    waypoints_list.append(f"{dest_coords[0]},{dest_coords[1]}")

    ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways)

    distance_m, duration_s = _extract_distance_duration(ptv)
    prix_peage = _extract_toll(ptv)
    coords     = _extract_polyline(ptv)

    return {
        "distance_km":    round(distance_m / 1000, 1),
        "duration_h":     round(duration_s / 3600, 2),
        "prix_peage":     round(prix_peage, 2),
        "polyline":       coords,
        "origin":         data.origin,
        "dest":           data.dest,
        "pref_waypoints": pref_wps,
    }


# ── Recalcul drag (waypoints coordonnées) ───────────────────────────────────
@app.post("/api/recalculate_drag")
async def recalculate_drag(data: RecalcDragRequest):
    if len(data.waypoints) < 2:
        raise HTTPException(400, "Il faut au minimum 2 waypoints")

    waypoints_list = [f"{wp.lat},{wp.lng}" for wp in data.waypoints]

    print("="*60)
    print(f"DRAG RECALC — {len(waypoints_list)} waypoints")
    for i, wp in enumerate(waypoints_list):
        print(f"  [{i}] {wp}")
    print("="*60)

    try:
        ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways)
    except HTTPException as e:
        print(f"PTV a planté : {e.detail}")
        raise
    except Exception as e:
        print(f"ERREUR INATTENDUE : {type(e).__name__}: {e}")
        raise HTTPException(500, f"Erreur interne: {e}")

    distance_m, duration_s = _extract_distance_duration(ptv)
    prix_peage = _extract_toll(ptv)
    coords     = _extract_polyline(ptv)

    print(f"RÉSULTAT PTV : dist={distance_m}m, dur={duration_s}s, peage={prix_peage}, coords={len(coords)} points")

    if data.route_id and FIREBASE_URL:
        # ── 1) Lire la route existante pour préserver les originaux ──
        try:
            existing = httpx.get(
                f"{FIREBASE_URL}/routes/{data.route_id}.json",
                timeout=10
            ).json() or {}
        except Exception as e:
            print(f"Erreur lecture Firebase: {e}")
            existing = {}

        # ── 2) Sauvegarder les originaux s'ils n'existent pas encore ──
        originals_patch = {}
        if "distance_km_original" not in existing:
            originals_patch["distance_km_original"] = existing.get("distance_km")
        if "duration_h_original" not in existing:
            originals_patch["duration_h_original"] = existing.get("duration_h")
        if "prix_peage_original" not in existing:
            originals_patch["prix_peage_original"] = existing.get("prix_peage")

        # ── 3) Écrire originaux + nouvelles valeurs en un seul patch ──
        update_data = {
            **originals_patch,
            "polyline_current": coords,
            "distance_km":      round(distance_m / 1000, 1),
            "duration_h":       round(duration_s / 3600, 2),
            "prix_peage":       round(prix_peage, 2),
        }
        try:
            httpx.patch(
                f"{FIREBASE_URL}/routes/{data.route_id}.json",
                json=update_data,
                timeout=10
            )
        except Exception as e:
            print(f"Erreur maj Firebase: {e}")

    return {
        "distance_km": round(distance_m / 1000, 1),
        "duration_h":  round(duration_s / 3600, 2),
        "prix_peage":  round(prix_peage, 2),
        "polyline":    coords,
    }



# ── Reset route → retour à l'itinéraire original ─────────────────────────────
@app.post("/api/reset_route/{route_id}")
async def reset_route(route_id: str):
    if not FIREBASE_URL:
        raise HTTPException(400, "Firebase non configuré")

    try:
        r = httpx.get(
            f"{FIREBASE_URL}/routes/{route_id}.json",
            timeout=10
        )
        if r.status_code != 200 or not r.json():
            raise HTTPException(404, "Route introuvable")

        route = r.json()
        original_poly = route.get("polyline_original")
        if not original_poly:
            raise HTTPException(404, "polyline_original introuvable")

        # ✅ Restaurer polyline ET stats
        reset_data = {
            "polyline_current": original_poly,
            "distance_km":      route.get("distance_km_original", route.get("distance_km")),
            "duration_h":       route.get("duration_h_original",  route.get("duration_h")),
            "prix_peage":       route.get("prix_peage_original",  route.get("prix_peage")),
        }

        httpx.patch(
            f"{FIREBASE_URL}/routes/{route_id}.json",
            json=reset_data,
            timeout=10
        )
        return {
            "status": "reset",
            "points": len(original_poly),
            "distance_km": reset_data["distance_km"],
            "duration_h":  reset_data["duration_h"],
            "prix_peage":  reset_data["prix_peage"],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur reset: {e}")
    
@app.get("/api/geocode")
async def geocode(q: str):
    if not q or len(q) < 3:
        raise HTTPException(400, "Requête trop courte")
    coords = await _geocode(q)
    if not coords:
        raise HTTPException(404, "Adresse introuvable")
    return {"lat": coords[0], "lng": coords[1], "label": q}




# ══════════════════════════════════════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("map_server_main:app", host="0.0.0.0", port=8000, reload=False)
