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

PTV_API_KEY = os.environ.get("PTV_API_KEY", "")
MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")

FIREBASE_URL = os.environ.get("FIREBASE_URL", "").rstrip("/")

# ── Helpers stockage ──────────────────────────────────────────────────────────

def load_routes() -> dict:
    # 1. Si on est connecté à Firebase, on lit depuis le cloud
    if FIREBASE_URL:
        try:
            r = httpx.get(f"{FIREBASE_URL}/routes.json", timeout=10)
            if r.status_code == 200 and r.json():
                return r.json()
        except Exception as e:
            print(f"Erreur lecture Firebase : {e}")
        return {}
        
    # 2. Sinon, on lit le fichier local (pour tes tests)
    if not os.path.exists(ROUTES_FILE):
        return {}
    with open(ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_routes(data: dict):
    # 1. Si on est connecté à Firebase, on sauvegarde dans le cloud
    if FIREBASE_URL:
        try:
            # On utilise PATCH pour ajouter les nouveaux trajets sans écraser les anciens
            httpx.patch(f"{FIREBASE_URL}/routes.json", json=data, timeout=10)
        except Exception as e:
            print(f"Erreur écriture Firebase : {e}")
        return
        
    # 2. Sinon, on sauvegarde en local
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Modèles ───────────────────────────────────────────────────────────────────

class RouteCreate(BaseModel):
    origin:      str
    dest:        str
    distance_km: float
    duration_h:  float
    polyline:    list          # [[lat, lon], ...]
    prix_peage:  float = 0.0

class RouteRecalc(BaseModel):
    origin:         str
    dest:           str
    avoid_tolls:    bool = False
    avoid_highways: bool = False

class WaypointItem(BaseModel):
    lat: float
    lng: float

class RecalcDragRequest(BaseModel):
    waypoints:      List[WaypointItem]   # [origin, ...intermediaires, dest]
    avoid_tolls:    bool = False
    avoid_highways: bool = False
    route_id:       Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/create_route")
async def create_route(route: RouteCreate):
    """Reçoit un trajet calculé, le stocke, retourne l'URL publique."""
    route_id = uuid.uuid4().hex[:8]
    routes   = load_routes()
    routes[route_id] = route.dict()
    save_routes(routes)
    url = f"{MAP_SERVER_URL}/carte?id={route_id}"
    return {"url": url, "id": route_id}


@app.get("/carte")
async def show_map(request: Request, id: str):
    """Affiche la carte interactive pour un trajet donné."""
    routes = load_routes()
    if id not in routes:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    route = routes[id]
    return templates.TemplateResponse("map.html", {
        "request":      request,
        "route":        route,
        "route_id":     id,
        "server_url":   MAP_SERVER_URL,
    })


@app.post("/api/recalculate")
async def recalculate(data: RouteRecalc):
    """Recalcule un trajet avec nouvelles options (depuis le panel latéral)."""

    origin_coords = await _geocode(data.origin)
    dest_coords   = await _geocode(data.dest)

    if not origin_coords or not dest_coords:
        raise HTTPException(status_code=400, detail="Géocodage impossible")

    avoid = []
    if data.avoid_tolls:    avoid.append("TOLL_ROADS")
    if data.avoid_highways: avoid.append("HIGHWAYS")

    params = [
        ("waypoints", f"{origin_coords[0]},{origin_coords[1]}"),
        ("waypoints", f"{dest_coords[0]},{dest_coords[1]}"),
        ("profile", "EUR_TRAILER_TRUCK"),
        ("results", "POLYLINE,TOLL_COSTS"),
    ]
    if avoid:
        params.append(("options[avoid]", ",".join(avoid)))

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/routing/v1/routes",
            params=params,
            headers={"apiKey": PTV_API_KEY},
            timeout=30,
        )
    print(f"🔍 PTV status: {resp.status_code}")
    print(f"🔍 PTV URL: {resp.url}")
    print(f"🔍 PTV response: {resp.text[:500]}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"PTV error: {resp.text}")

    ptv = resp.json()

    legs = ptv.get("legs", [])
    if legs:
        distance_m = sum(leg.get("distance", 0) for leg in legs)
        duration_s = sum(leg.get("travelTime", 0) for leg in legs)
    else:
        distance_m = ptv.get("distance", 0)
        duration_s = ptv.get("travelTime", 0)

    toll_costs = ptv.get("toll", {}).get("costs", [])
    prix_peage = sum(c.get("price", {}).get("amount", 0) for c in toll_costs)

    polyline_raw = ptv.get("polyline", "")
    encoded = (polyline_raw.get("encodedPolyline", "")
               if isinstance(polyline_raw, dict) else polyline_raw)
    coords = _decode_polyline(encoded)

    return {
        "distance_km": round(distance_m / 1000, 1),
        "duration_h":  round(duration_s / 3600, 2),
        "prix_peage":  round(prix_peage, 2),
        "polyline":    coords,
        "origin":      data.origin,
        "dest":        data.dest,
    }


@app.post("/api/recalculate_drag")
async def recalculate_drag(data: RecalcDragRequest):
    """
    Recalcule via PTV avec N waypoints (drag & drop sur la carte).
    Reçoit : [origin, ...points_intermédiaires_draggés, dest]
    """

    if len(data.waypoints) < 2:
        raise HTTPException(400, "Il faut au minimum 2 waypoints")

    # Construire les waypoints PTV : "lat,lon"
    params = [("waypoints", f"{wp.lat},{wp.lng}") for wp in data.waypoints]
    params.append(("profile", "EUR_TRAILER_TRUCK"))
    params.append(("results", "POLYLINE,TOLL_COSTS"))

    # Options évitement
    avoid = []
    if data.avoid_tolls:    avoid.append("TOLL_ROADS")
    if data.avoid_highways: avoid.append("HIGHWAYS")
    if avoid:
        params.append(("options[avoid]", ",".join(avoid)))

    # Appel PTV
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/routing/v1/routes",
            params=params,
            headers={"apiKey": PTV_API_KEY},
            timeout=30,
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"PTV error {resp.status_code}: {resp.text[:500]}")

    ptv = resp.json()

    # Distance & durée
    legs = ptv.get("legs", [])
    if legs:
        distance_m = sum(leg.get("distance", 0) for leg in legs)
        duration_s = sum(leg.get("travelTime", 0) for leg in legs)
    else:
        distance_m = ptv.get("distance", 0)
        duration_s = ptv.get("travelTime", 0)

    # Péages
    toll_costs = ptv.get("toll", {}).get("costs", [])
    prix_peage = sum(c.get("price", {}).get("amount", 0) for c in toll_costs)

    # Polyline
    polyline_raw = ptv.get("polyline", "")
    if isinstance(polyline_raw, dict):
        encoded = polyline_raw.get("encodedPolyline", "")
    else:
        encoded = polyline_raw
    coords = _decode_polyline(encoded)

    # Sauvegarder si route_id fourni
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


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _geocode(address: str):
    """Géocodage PTV async."""
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


def _decode_polyline(encoded: str) -> list:
    """Décode Google encoded polyline → [[lat, lon], ...]"""
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


if __name__ == "__main__":
    uvicorn.run("map_server_main:app", host="0.0.0.0", port=8000, reload=True)
