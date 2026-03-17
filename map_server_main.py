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
import uvicorn, uuid, json, os, httpx
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Arcelor Route Map Server")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    """Retourne les waypoints préférentiels pour un trajet, ou []"""
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
        
    # Sécurité si on est en local
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
    
    # ── Cas 1 : dict ──
    if isinstance(polyline_raw, dict):
        if polyline_raw.get("type") == "LineString":
            return [[c[1], c[0]] for c in polyline_raw.get("coordinates", [])]
        if "plain" in polyline_raw:
            raw = polyline_raw["plain"].get("pointsByCoordinates", [])
            return [[raw[i + 1], raw[i]] for i in range(0, len(raw) - 1, 2)]
        if "encodedPolyline" in polyline_raw:
            return _decode_polyline(polyline_raw["encodedPolyline"])
        return []

    # ── Cas 2 : string ──
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

async def _geocode(address: str):
    """Géocode une adresse via PTV → (lat, lon) ou None."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.myptv.com/geocoding/v1/locations/searchText",
                params={"searchText": address, "language": "fr"},
                headers={"apiKey": PTV_API_KEY},
                timeout=10,
            )
            data = r.json()
            loc  = data.get("locations", [{}])[0]
            ref  = loc.get("referencePosition", {})
            lat  = ref.get("lat") or ref.get("latitude")
            lon  = ref.get("lon") or ref.get("longitude")
            return (lat, lon) if lat and lon else None
    except Exception:
        return None

async def _call_ptv(waypoints_list: list, avoid_tolls: bool, avoid_highways: bool) -> dict:
    """Appel PTV routing avec waypoints, options d'évitement → dict réponse."""
    params = [("waypoints", w) for w in waypoints_list]
    params.append(("profile", "EUR_TRAILER_TRUCK"))
    params.append(("results", "POLYLINE,TOLL_COSTS"))

    avoid = []
    if avoid_tolls:    avoid.append("TOLL_ROADS")
    if avoid_highways: avoid.append("HIGHWAYS")
    if avoid:
        params.append(("options[avoid]", ",".join(avoid)))

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/routing/v1/routes",
            params=params,
            headers={"apiKey": PTV_API_KEY},
            timeout=30,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"PTV error {resp.status_code}: {resp.text[:500]}")

    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Health check (warm-up Render) ────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Créer une route (appelé par main_km.py) ──────────────────────────────────
@app.post("/api/create_route")
async def create_route(route: RouteCreate):
    route_id = uuid.uuid4().hex[:8]
    routes   = {}  # <--- MAGIE POUR NE PAS FAIRE PLANTER RENDER
    routes[route_id] = route.dict()
    save_routes(routes)
    url = f"{MAP_SERVER_URL}/carte?id={route_id}"
    return {"url": url, "id": route_id}


# ── Afficher la carte ────────────────────────────────────────────────────────
# ── Afficher la carte ────────────────────────────────────────────────────────
@app.get("/carte")
async def show_map(request: Request, id: str):
    route = get_route(id)        # <--- ON UTILISE LA NOUVELLE FONCTION MAGIQUE
    if not route:                # <--- ON VÉRIFIE SI ELLE A ÉTÉ TROUVÉE
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    
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

    ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways)

    distance_m, duration_s = _extract_distance_duration(ptv)
    prix_peage = _extract_toll(ptv)
    coords     = _extract_polyline(ptv)

    if data.route_id:
        routes = load_routes()
        if data.route_id in routes:
            routes[data.route_id]["polyline"]    = coords
            routes[data.route_id]["distance_km"] = round(distance_m / 1000, 1)
            routes[data.route_id]["duration_h"]  = round(duration_s / 3600, 2)
            routes[data.route_id]["prix_peage"]  = round(prix_peage, 2)
            save_routes(routes)

    return {
        "distance_km": round(distance_m / 1000, 1),
        "duration_h":  round(duration_s / 3600, 2),
        "prix_peage":  round(prix_peage, 2),
        "polyline":    coords,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("map_server_main:app", host="0.0.0.0", port=8000, reload=False)
