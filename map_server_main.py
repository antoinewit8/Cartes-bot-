"""
Serveur de cartes interactives.
Déployable sur Render.com (gratuit) → URL publique permanente.
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
from typing import List, Optional
import uvicorn, uuid, json, os, httpx, base64
from datetime import date
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(title="CB Route Map Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

ROUTES_FILE = "data/routes.json"
os.makedirs("data", exist_ok=True)

PTV_API_KEY    = os.environ.get("PTV_API_KEY", "")
MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")
FIREBASE_URL   = os.environ.get("FIREBASE_URL", "").rstrip("/")

# ── GitHub API ────────────────────────────────────────────────────────────────
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "antoinewit8/hub")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
LEARNED_FILE  = "transport_hub/tools/km_calcul/routes_apprises.json"


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES PRÉFÉRENTIELLES
# ══════════════════════════════════════════════════════════════════════════════

PREF_ROUTES_FILE = "routes_preferentielles.json"

def load_pref_routes() -> list:
    if not os.path.exists(PREF_ROUTES_FILE):
        return []
    with open(PREF_ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def find_pref_waypoints(origin: str, dest: str, super_mode: bool = False) -> list:
    prefs = load_pref_routes()
    o = origin.strip().lower()
    d = dest.strip().lower()
    for route in prefs:
        if (route["origine"].strip().lower() == o
                and route["destination"].strip().lower() == d):
            key = "super_waypoints" if super_mode and "super_waypoints" in route else "waypoints"
            wps = []
            for wp in route.get(key, []):
                parts = wp.split(",")
                if len(parts) == 2:
                    wps.append({"lat": float(parts[0].strip()), "lng": float(parts[1].strip())})
            return wps
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  STOCKAGE Firebase / fichier local
# ══════════════════════════════════════════════════════════════════════════════

def save_route_firebase(route_id: str, route_data: dict):
    """PUT individuel sur /routes/{route_id} — plus fiable que PATCH racine."""
    try:
        r = httpx.put(
            f"{FIREBASE_URL}/routes/{route_id}.json",
            json=route_data,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            print(f"Firebase PUT erreur {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Erreur écriture Firebase route {route_id} : {e}")

def get_route(route_id: str) -> dict:
    if FIREBASE_URL:
        try:
            r = httpx.get(f"{FIREBASE_URL}/routes/{route_id}.json", timeout=20)
            if r.status_code == 200 and r.json():
                return r.json()
        except Exception as e:
            print(f"Erreur lecture Firebase route {route_id} : {e}")
        return None
    # Fallback local
    if not os.path.exists(ROUTES_FILE):
        return None
    with open(ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get(route_id)


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
    super_pref:     bool = False

class WaypointItem(BaseModel):
    lat: float
    lng: float

class RecalcDragRequest(BaseModel):
    waypoints:      List[WaypointItem]
    avoid_tolls:    bool = False
    avoid_highways: bool = False
    super_pref:     bool = False
    route_id:       Optional[str] = None

class SaveReferenceRequest(BaseModel):
    origin:    str
    dest:      str
    waypoints: List[WaypointItem]
    km:        float


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS PTV
# ══════════════════════════════════════════════════════════════════════════════

def _extract_polyline(ptv: dict) -> list:
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
    legs = ptv.get("legs", [])
    if legs:
        return (sum(l.get("distance", 0) for l in legs),
                sum(l.get("travelTime", 0) for l in legs))
    return ptv.get("distance", 0), ptv.get("travelTime", 0)

def _extract_toll(ptv: dict) -> float:
    toll_data = ptv.get("toll", {}).get("costs", {})
    if isinstance(toll_data, dict):
        return toll_data.get("convertedPrice", {}).get("price", 0)
    return 0

def _decode_polyline(encoded: str) -> list:
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
            if is_lng: lng += delta
            else:      lat += delta
        coords.append([lat / 1e5, lng / 1e5])
    return coords

async def _geocode(address: str) -> Optional[list]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/geocoding/v1/locations/by-text",
            headers={"apiKey": PTV_API_KEY},
            params={"searchText": address, "countryFilter": "FR,BE,LU,DE,ES,NL,GB"},
            timeout=15,
        )
    if resp.status_code != 200:
        return None
    results = resp.json().get("locations", [])
    if not results:
        return None
    loc = results[0]["referencePosition"]
    return [loc["latitude"], loc["longitude"]]

async def _call_ptv(waypoints_list: list, avoid_tolls: bool, avoid_highways: bool,
                    super_pref: bool = False) -> dict:
    query_params = [
        ("profile", "EUR_TRAILER_TRUCK"),
        ("results", "POLYLINE,TOLL_COSTS"),
        ("options[currency]", "EUR"),
    ]
    for i, wp_str in enumerate(waypoints_list):
        parts = wp_str.split(",")
        lat, lng = float(parts[0].strip()), float(parts[1].strip())
        if 0 < i < len(waypoints_list) - 1:
            query_params.append(("waypoints", f"{lat},{lng};radius=5000"))
        else:
            query_params.append(("waypoints", f"{lat},{lng}"))
    avoid = []
    if avoid_tolls or super_pref: avoid.append("TOLL")
    if avoid_highways:            avoid.append("HIGHWAYS")
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


# ── Géocodage (recherche adresse depuis la carte) ────────────────────────────
@app.get("/api/geocode")
async def api_geocode(q: str):
    if not q or len(q) < 3:
        raise HTTPException(400, "Requête trop courte")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.myptv.com/geocoding/v1/locations/by-text",
            headers={"apiKey": PTV_API_KEY},
            params={"searchText": q, "countryFilter": "FR,BE,LU,DE,ES,NL,GB"},
            timeout=15,
        )
    if resp.status_code != 200:
        raise HTTPException(500, "Erreur API PTV geocode")
    results = resp.json().get("locations", [])
    if not results:
        raise HTTPException(404, "Adresse introuvable")
    loc   = results[0]["referencePosition"]
    label = results[0].get("address", {}).get("formattedAddress", q)
    return {"lat": loc["latitude"], "lng": loc["longitude"], "label": label}


# ── Créer une route ──────────────────────────────────────────────────────────
@app.post("/api/create_route")
async def create_route(route: RouteCreate):
    route_id   = uuid.uuid4().hex[:8]
    route_data = route.dict()

    # Sauvegarder les valeurs originales (jamais écrasées)
    route_data["polyline_original"]    = route.polyline
    route_data["polyline_current"]     = route.polyline
    route_data["distance_km_original"] = route.distance_km
    route_data["duration_h_original"]  = route.duration_h
    route_data["prix_peage_original"]  = route.prix_peage

    if FIREBASE_URL:
        save_route_firebase(route_id, route_data)
    else:
        routes = {}
        if os.path.exists(ROUTES_FILE):
            with open(ROUTES_FILE, "r", encoding="utf-8") as f:
                routes = json.load(f)
        routes[route_id] = route_data
        with open(ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False, indent=2)

    url = f"{MAP_SERVER_URL}/carte?id={route_id}"
    return {"url": url, "id": route_id}


# ── Afficher la carte ────────────────────────────────────────────────────────
@app.get("/carte")
async def show_map(request: Request, id: str):
    route = get_route(id)
    if not route:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    if "polyline_original" not in route:
        route["polyline_original"] = route.get("polyline", [])
        route["polyline_current"]  = route.get("polyline", [])
    return templates.TemplateResponse("map.html", {
        "request":    request,
        "route":      route,
        "route_id":   id,
        "server_url": MAP_SERVER_URL,
    })


# ── Recalcul standard (texte) ────────────────────────────────────────────────
@app.post("/api/recalculate")
async def recalculate(data: RouteRecalc):
    origin_coords = await _geocode(data.origin)
    dest_coords   = await _geocode(data.dest)
    if not origin_coords or not dest_coords:
        raise HTTPException(400, "Géocodage impossible")
    pref_wps = find_pref_waypoints(data.origin, data.dest, super_mode=data.super_pref)
    waypoints_list = [f"{origin_coords[0]},{origin_coords[1]}"]
    for wp in pref_wps:
        waypoints_list.append(f"{wp['lat']},{wp['lng']}")
    waypoints_list.append(f"{dest_coords[0]},{dest_coords[1]}")
    ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways, data.super_pref)
    distance_m, duration_s = _extract_distance_duration(ptv)
    return {
        "distance_km":    round(distance_m / 1000, 1),
        "duration_h":     round(duration_s / 3600, 2),
        "prix_peage":     round(_extract_toll(ptv), 2),
        "polyline":       _extract_polyline(ptv),
        "origin":         data.origin,
        "dest":           data.dest,
        "pref_waypoints": pref_wps,
    }


# ── Recalcul drag ────────────────────────────────────────────────────────────
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur interne: {e}")

    distance_m, duration_s = _extract_distance_duration(ptv)
    prix_peage = _extract_toll(ptv)
    coords     = _extract_polyline(ptv)
    print(f"RÉSULTAT PTV : {round(distance_m/1000,1)}km, {len(coords)} points")

    # Mise à jour Firebase — polyline_current uniquement, originaux préservés
    if data.route_id and FIREBASE_URL:
        update_data = {
            "polyline_current": coords,
            "distance_km":      round(distance_m / 1000, 1),
            "duration_h":       round(duration_s / 3600, 2),
            "prix_peage":       round(prix_peage, 2),
        }
        try:
            httpx.patch(
                f"{FIREBASE_URL}/routes/{data.route_id}.json",
                json=update_data, timeout=10
            )
        except Exception as e:
            print(f"Erreur maj Firebase: {e}")

    return {
        "distance_km": round(distance_m / 1000, 1),
        "duration_h":  round(duration_s / 3600, 2),
        "prix_peage":  round(prix_peage, 2),
        "polyline":    coords,
    }


# ── Reset route ───────────────────────────────────────────────────────────────
@app.post("/api/reset_route/{route_id}")
async def reset_route(route_id: str):
    if not FIREBASE_URL:
        raise HTTPException(400, "Firebase non configuré")
    try:
        r = httpx.get(f"{FIREBASE_URL}/routes/{route_id}.json", timeout=10)
        if r.status_code != 200 or not r.json():
            raise HTTPException(404, "Route introuvable")
        route = r.json()
        original_poly = route.get("polyline_original")
        if not original_poly:
            raise HTTPException(404, "polyline_original introuvable")
        reset_data = {
            "polyline_current": original_poly,
            "distance_km":      route.get("distance_km_original", route.get("distance_km")),
            "duration_h":       route.get("duration_h_original",  route.get("duration_h")),
            "prix_peage":       route.get("prix_peage_original",  route.get("prix_peage")),
        }
        httpx.patch(f"{FIREBASE_URL}/routes/{route_id}.json", json=reset_data, timeout=10)
        return {
            "status":      "reset",
            "points":      len(original_poly),
            "distance_km": reset_data["distance_km"],
            "duration_h":  reset_data["duration_h"],
            "prix_peage":  reset_data["prix_peage"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur reset: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB API — routes_apprises.json
# ══════════════════════════════════════════════════════════════════════════════

GITHUB_API = "https://api.github.com"

def _github_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def _github_read_learned() -> tuple:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{LEARNED_FILE}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_github_headers(),
                             params={"ref": GITHUB_BRANCH}, timeout=15)
    if r.status_code == 404:
        return [], None
    if r.status_code != 200:
        raise HTTPException(500, f"GitHub read error {r.status_code}: {r.text}")
    data    = r.json()
    sha     = data["sha"]
    content = base64.b64decode(data["content"]).decode("utf-8")
    try:
        routes = json.loads(content)
    except json.JSONDecodeError:
        routes = []
    return routes, sha

async def _github_write_learned(routes: list, sha, commit_msg: str):
    if not GITHUB_TOKEN:
        raise HTTPException(500, "GITHUB_TOKEN non configuré")
    url     = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{LEARNED_FILE}"
    content = base64.b64encode(
        json.dumps(routes, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    body = {"message": commit_msg, "content": content, "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    async with httpx.AsyncClient() as client:
        r = await client.put(url, headers=_github_headers(), json=body, timeout=20)
    if r.status_code not in (200, 201):
        raise HTTPException(500, f"GitHub write error {r.status_code}: {r.text}")
    return r.json()


# ── Sauvegarde référence → GitHub ─────────────────────────────────────────────
@app.post("/api/save_reference")
async def save_reference(data: SaveReferenceRequest):
    try:
        routes, sha = await _github_read_learned()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur lecture GitHub : {e}")

    o_norm = data.origin.strip().lower()
    d_norm = data.dest.strip().lower()
    existing_idx = None
    for i, r in enumerate(routes):
        if (r.get("origine", "").strip().lower() == o_norm
                and r.get("destination", "").strip().lower() == d_norm):
            existing_idx = i
            break

    wp_strings = [f"{wp.lat:.6f}, {wp.lng:.6f}" for wp in data.waypoints]
    today = date.today().isoformat()

    if existing_idx is not None:
        new_confiance = routes[existing_idx].get("confiance", 1) + 1
        routes[existing_idx] = {
            "origine":      data.origin.strip(),
            "destination":  data.dest.strip(),
            "waypoints":    wp_strings,
            "km_reference": round(data.km, 1),
            "source":       "carte_manuelle",
            "date":         today,
            "confiance":    new_confiance,
        }
        action = f"update ({new_confiance}x validé)"
    else:
        routes.append({
            "origine":      data.origin.strip(),
            "destination":  data.dest.strip(),
            "waypoints":    wp_strings,
            "km_reference": round(data.km, 1),
            "source":       "carte_manuelle",
            "date":         today,
            "confiance":    1,
        })
        action = "ajout"

    commit_msg = f"feat(routes): {action} {data.origin} → {data.dest} ({round(data.km)}km)"
    try:
        await _github_write_learned(routes, sha, commit_msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur écriture GitHub : {e}")

    return {
        "status":        "ok",
        "action":        action,
        "origin":        data.origin,
        "dest":          data.dest,
        "waypoints":     len(wp_strings),
        "km":            round(data.km, 1),
        "total_learned": len(routes),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("map_server_main:app", host="0.0.0.0", port=8000, reload=False)
