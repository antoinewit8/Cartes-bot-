"""
Serveur de cartes interactives.
Déployable sur Render.com (gratuit) → URL publique permanente.

Traversées maritimes / ferroviaires (ferries, navettes type Eurotunnel) :
  - chaque calcul renvoie `crossings` : les traversées que PTV a mises sur le trajet ;
  - GET /api/ferry_options liste les lignes disponibles autour d'un port
    (ou par nom de port) via la Data API PTV ;
  - /api/recalculate et /api/recalculate_drag acceptent `ferries` : liste de
    paramètres "combinedTransport=latA,lonA,latB,lonB" à imposer au trajet.
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
from typing import List, Optional
import uvicorn, uuid, json, os, re, math, httpx, base64
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

PTV_ROUTING_URL  = "https://api.myptv.com/routing/v1/routes"
PTV_GEOCODE_URL  = "https://api.myptv.com/geocoding/v1/locations/by-text"
PTV_CT_URL       = "https://api.myptv.com/data/v1/combined-transports"

# Rayon de recherche des lignes autour du port d'embarquement : 60 km couvre
# par exemple Calais + Dunkerque + le terminal Eurotunnel.
FERRY_RADIUS_KM_DEFAULT = 60
FERRY_RADIUS_KM_MAX     = 150
FERRY_OPTIONS_MAX       = 40

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
    traffic:        bool = True     # False = ignore fermetures/bouchons en temps réel
    alternatives:   bool = True     # variantes PTV (seulement départ → arrivée sans étape)
    super_pref:     bool = False
    # Envoyés par map.html : coordonnées exactes, prioritaires sur le géocodage
    origin_coords:  Optional[List[float]] = None
    dest_coords:    Optional[List[float]] = None
    via:            List[List[float]] = []
    # Traversées imposées : "combinedTransport=latA,lonA,latB,lonB"
    ferries:        List[str] = []

class WaypointItem(BaseModel):
    lat: float
    lng: float

class RecalcDragRequest(BaseModel):
    waypoints:      List[WaypointItem]
    avoid_tolls:    bool = False
    avoid_highways: bool = False
    traffic:        bool = True     # False = ignore fermetures/bouchons en temps réel
    alternatives:   bool = True     # variantes PTV (seulement départ → arrivée sans étape)
    super_pref:     bool = False
    route_id:       Optional[str] = None
    ferries:        List[str] = []

class SaveReferenceRequest(BaseModel):
    origin:    str
    dest:      str
    waypoints: List[WaypointItem]
    km:        float
    ferries:   List[str] = []


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

def _extract_toll_by_country(ptv: dict) -> list:
    """
    Ventilation du péage par pays : [{"country": "BE", "price": 43.35}, ...].

    Structure PTV Developer v1 (validée le 16/09/2026 sur Liège → Rotterdam) :
      toll.costs.countries[] = {countryCode, price: {price, currency},
                                convertedPrice: {price, currency}}
      toll.sections[]        = {countryCode, costs: [{price, currency, convertedPrice}]}
    1. toll.costs.countries (détail natif, somme = total PTV)
    2. repli : agrégation de toll.sections par countryCode
    Le prix converti (EUR) prime toujours.
    """
    def _montant(obj):
        if not isinstance(obj, dict):
            return None
        for cle in ("convertedPrice", "price"):
            v = obj.get(cle)
            if isinstance(v, dict):
                v = v.get("price")
            if isinstance(v, (int, float)):
                return float(v)
        return None

    toll = (ptv or {}).get("toll") or {}
    agrege = {}

    for c in (toll.get("costs") or {}).get("countries") or []:
        cc, m = c.get("countryCode"), _montant(c)
        if cc and m is not None:
            agrege[cc] = agrege.get(cc, 0.0) + m

    if not agrege:
        for sec in toll.get("sections") or []:
            cc = sec.get("countryCode")
            if not cc:
                continue
            for ligne in sec.get("costs") or []:
                m = _montant(ligne)
                if m is not None:
                    agrege[cc] = agrege.get(cc, 0.0) + m

    return sorted(({"country": k, "price": round(v, 2)} for k, v in agrege.items()),
                  key=lambda x: -x["price"])


def _extract_km_by_country(ptv: dict) -> list:
    """
    Kilomètres parcourus par pays : [{"country": "BE", "km": 168.2}, ...].

    PTV ne fournit pas ce total : on le reconstitue à partir des événements.
      - WAYPOINT_EVENTS : le premier événement (distanceFromStart = 0) porte le
        countryCode du départ.
      - BORDER_EVENTS   : chaque passage de frontière porte distanceFromStart
        et border.countryCode = pays dans lequel on entre.
    La distance totale est découpée entre ces jalons ; la somme des pays est
    donc égale à la distance de la route (aux arrondis près).
    """
    total_m = ptv.get("distance") or 0
    events = sorted((e for e in (ptv.get("events") or []) if isinstance(e, dict)),
                    key=lambda e: e.get("distanceFromStart") or 0)
    if not total_m or not events:
        return []   # pas d'événements reçus : ventilation inconnue

    def _pays_entree(e):
        b = e.get("border")
        if isinstance(b, dict):
            return b.get("countryCode") or e.get("countryCode")
        return None

    # Pays de départ : l'événement du premier waypoint en priorité (les
    # événements de traversée portent aussi un countryCode), sinon le premier
    # événement non-frontière, sinon la première section de péage.
    depart = None
    for e in events:
        if "waypoint" in e and e.get("countryCode"):
            depart = e["countryCode"]
            break
    if not depart:
        for e in events:
            if "border" not in e and e.get("countryCode"):
                depart = e["countryCode"]
                break
    if not depart:
        secs = ((ptv.get("toll") or {}).get("sections") or [])
        premiere_frontiere = next((e for e in events if "border" in e), None)
        if secs and not premiere_frontiere:
            depart = secs[0].get("countryCode")
    depart = depart or "??"

    agrege, pays, debut = {}, depart, 0
    for e in events:
        entree = _pays_entree(e)
        if not entree:
            continue
        d = e.get("distanceFromStart") or 0
        if d > debut:
            agrege[pays] = agrege.get(pays, 0) + (d - debut)
        pays, debut = entree, max(d, debut)
    if total_m > debut:
        agrege[pays] = agrege.get(pays, 0) + (total_m - debut)

    return sorted(({"country": k, "km": round(v / 1000, 1)} for k, v in agrege.items() if v > 0),
                  key=lambda x: -x["km"])


def _extract_crossings(ptv: dict) -> list:
    """
    Traversées présentes sur le trajet, à partir des COMBINED_TRANSPORT_EVENTS.
    PTV émet un événement ENTER (embarquement) et un EXIT (débarquement) ;
    relatedEventIndex relie les deux. Les noms de ports ne sont pas fournis
    ici : seule la liaison porte un nom.
    """
    events = ptv.get("events") or []
    ouverts, ordre, crossings = {}, [], []

    for idx, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        ct = e.get("combinedTransport")
        if not isinstance(ct, dict):
            continue
        acces = str(ct.get("accessType") or "").upper()
        if acces == "ENTER":
            ouverts[idx] = e
            ordre.append(idx)
            continue
        if acces != "EXIT":
            continue

        rel = ct.get("relatedEventIndex")
        if rel in ouverts:
            i_in = rel
        elif ordre:
            i_in = ordre[-1]
        else:
            continue
        entree = ouverts.pop(i_in)
        ordre.remove(i_in)
        ct_in = entree.get("combinedTransport") or {}

        if entree.get("latitude") is None or e.get("latitude") is None:
            continue
        d0 = entree.get("distanceFromStart") or 0
        d1 = e.get("distanceFromStart") or 0
        t0 = entree.get("travelTimeFromStart") or 0
        t1 = e.get("travelTimeFromStart") or 0

        crossings.append({
            "name": ct.get("name") or ct_in.get("name") or "",
            "type": str(ct.get("type") or ct_in.get("type") or "BOAT").upper(),
            "start": {"lat": entree.get("latitude"), "lng": entree.get("longitude"),
                      "cc": entree.get("countryCode")},
            "end":   {"lat": e.get("latitude"), "lng": e.get("longitude"),
                      "cc": e.get("countryCode")},
            "from_start_km": round(d0 / 1000, 1),
            "distance_km":   round(max(d1 - d0, 0) / 1000, 1),
            "duration_min":  round(max(t1 - t0, 0) / 60),
        })

    crossings.sort(key=lambda c: c["from_start_km"])
    return crossings


def _extract_ct_warnings(ptv: dict) -> list:
    """Avertissements PTV liés aux traversées imposées (ligne ignorée, ambiguë)."""
    out = []
    for w in (ptv or {}).get("warnings") or []:
        if not isinstance(w, dict):
            continue
        code = str(w.get("warningCode") or "")
        if code.startswith("ROUTING_COMBINED_TRANSPORT"):
            out.append({
                "code":        code,
                "description": w.get("description", ""),
                "details":     w.get("details") or {},
            })
    return out


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


def _texte_event(sub: dict) -> str:
    """Libellé lisible d'un événement trafic / restriction PTV."""
    for cle in ("description", "message", "text", "reason"):
        v = sub.get(cle)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):   # description localisée {"text": ...}
            t = v.get("text") or v.get("value")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return ""


def _extract_alerts(ptv: dict) -> list:
    """
    Événements trafic (TRAFFIC_EVENTS) et restrictions enfreintes
    (VIOLATION_EVENTS) rencontrés SUR le tracé :
      [{"kind": "traffic"|"violation", "lat", "lng", "km", "title", "detail"}]
    Lecture tolérante : comme pour "border" ou "combinedTransport", PTV range
    le détail dans un sous-objet dont le nom contient traffic / violation.
    Les événements EXIT sont ignorés (doublon de l'ENTER).
    """
    alerts = []
    for e in ptv.get("events") or []:
        if not isinstance(e, dict):
            continue
        kind, sub = None, None
        for k, v in e.items():
            kl = k.lower()
            if isinstance(v, dict) and ("traffic" in kl or "violation" in kl):
                kind, sub = ("traffic" if "traffic" in kl else "violation"), v
                break
        if not kind:
            continue
        if str(sub.get("accessType") or e.get("accessType") or "").upper() == "EXIT":
            continue
        lat, lng = e.get("latitude"), e.get("longitude")
        if lat is None or lng is None:
            pos = e.get("position") or sub.get("position") or {}
            lat, lng = pos.get("latitude"), pos.get("longitude")
        if lat is None or lng is None:
            continue

        details = []
        if kind == "traffic":
            delay = sub.get("delay")
            if isinstance(delay, (int, float)) and delay > 0:
                details.append(f"retard {round(delay / 60)} min")
            length = sub.get("length")
            if isinstance(length, (int, float)) and length > 0:
                details.append(f"sur {length / 1000:.1f} km")
            titre = _texte_event(sub) or "Événement trafic"
        else:
            vtype = str(sub.get("type") or "").upper()
            titre = {
                "PROHIBITED":          "Passage interdit",
                "DELIVERY_ONLY":       "Accès desserte uniquement",
                "RESTRICTED_ACCESS":   "Accès restreint",
                "VEHICLE_PROPERTY":    "Restriction gabarit / poids",
                "COMBINED_TRANSPORT":  "Traversée non autorisée",
                "SCHEDULE":            "Horaire non respecté",
                "BLOCKED_ROAD_BY_INTERSECTION": "Route bloquée",
            }.get(vtype, "Restriction enfreinte")
            prop = sub.get("vehicleProperty") or sub.get("property")
            if prop:
                details.append(str(prop))
            txt = _texte_event(sub)
            if txt:
                details.append(txt)
        km = e.get("distanceFromStart")
        alerts.append({
            "kind":   kind,
            "lat":    float(lat),
            "lng":    float(lng),
            "km":     round(km / 1000, 1) if isinstance(km, (int, float)) else None,
            "title":  titre,
            "detail": " · ".join(details),
        })
    return alerts[:40]


def _extract_alternatives(ptv: dict) -> list:
    """Variantes PTV (ALTERNATIVE_ROUTES) : [{distance_km, duration_h, polyline}]."""
    brut = ptv.get("alternativeRoutes") or ptv.get("alternatives") or []
    out = []
    for alt in brut if isinstance(brut, list) else []:
        if not isinstance(alt, dict):
            continue
        poly = _extract_polyline(alt)
        if len(poly) < 2:
            continue
        d_m, t_s = _extract_distance_duration(alt)
        out.append({"distance_km": round(d_m / 1000, 1),
                    "duration_h":  round(t_s / 3600, 2),
                    "polyline":    poly})
    return out[:3]


def _route_payload(ptv: dict) -> dict:
    """Champs communs renvoyés par les deux endpoints de calcul."""
    distance_m, duration_s = _extract_distance_duration(ptv)
    crossings = _extract_crossings(ptv)
    return {
        "alerts":          _extract_alerts(ptv),
        "violated":        bool(ptv.get("violated")),
        "alternatives":    _extract_alternatives(ptv),
        "distance_km":     round(distance_m / 1000, 1),
        "duration_h":      round(duration_s / 3600, 2),
        "prix_peage":      round(_extract_toll(ptv), 2),
        "toll_by_country": _extract_toll_by_country(ptv),
        "km_by_country":   _extract_km_by_country(ptv),
        "crossings":       crossings,
        "ferry_km":        round(sum(c["distance_km"] for c in crossings), 1),
        "ferry_warnings":  _extract_ct_warnings(ptv),
        "polyline":        _extract_polyline(ptv),
    }


# Pays couverts par le géocodeur PTV (l'ancien filtre à 7 pays rendait
# introuvables le Danemark, l'Italie, la Pologne...)
COUNTRY_FILTER = ("FR,BE,LU,DE,ES,NL,GB,IT,CH,AT,PT,IE,"
                  "DK,SE,NO,FI,PL,CZ,SK,HU,SI,HR,RO,BG,GR,EE,LV,LT")

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")

_NUM = r"(-?\d+(?:\.\d+)?)"
_CT_RE = re.compile(
    rf"^\s*combinedTransport\s*=\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*$")


def _parse_coords(texte: str) -> Optional[list]:
    """'50.6326,5.5797' -> [50.6326, 5.5797]. None si ce n'est pas un couple lat,lon.
    Sans ce contrôle, PTV géocodait les coordonnées comme une adresse :
    Liège finissait vers Thionville, Rotterdam en banlieue parisienne."""
    m = _COORD_RE.match(texte or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return [lat, lng]
    return None


def _valid_pair(v) -> Optional[list]:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            lat, lng = float(v[0]), float(v[1])
        except (TypeError, ValueError):
            return None
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return [lat, lng]
    return None


def _parse_ferry(param: str) -> Optional[tuple]:
    """'combinedTransport=latA,lonA,latB,lonB' -> ((latA, lonA), (latB, lonB)).
    Tout ce qui ne correspond pas exactement à ce format est rejeté : la
    valeur part telle quelle dans la requête PTV."""
    m = _CT_RE.match(param or "")
    if not m:
        return None
    a_lat, a_lng, b_lat, b_lng = (float(m.group(i)) for i in range(1, 5))
    for lat, lng in ((a_lat, a_lng), (b_lat, b_lng)):
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
    return (a_lat, a_lng), (b_lat, b_lng)


def _ferry_param(start: tuple, dest: tuple) -> str:
    return (f"combinedTransport={start[0]:.6f},{start[1]:.6f},"
            f"{dest[0]:.6f},{dest[1]:.6f}")


def _dist_km(a, b) -> float:
    """Distance à vol d'oiseau entre deux (lat, lng)."""
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def _insert_ferries(points: List[str], ferries: List[str]) -> List[str]:
    """
    Place chaque traversée imposée dans la séquence de points.

    PTV exige que le waypoint combinedTransport soit entre les bons points :
    ni en premier, ni en dernier, et du bon côté des étapes. On l'insère là où
    il ajoute le moins de détour à vol d'oiseau :
        d(point avant, port A) + d(port B, point après) - d(point avant, point après)
    """
    noeuds = []
    for p in points:
        lat, lng = (float(x.strip()) for x in p.split(","))
        noeuds.append({"param": p, "entree": (lat, lng), "sortie": (lat, lng)})

    vus = set()
    for f in ferries or []:
        parsed = _parse_ferry(f)
        if not parsed:
            print(f"Traversée ignorée (format invalide) : {f!r}")
            continue
        a, b = parsed
        param = _ferry_param(a, b)
        if param in vus:
            continue
        vus.add(param)

        meilleur, cout_min = None, float("inf")
        for i in range(len(noeuds) - 1):
            avant, apres = noeuds[i]["sortie"], noeuds[i + 1]["entree"]
            cout = _dist_km(avant, a) + _dist_km(b, apres) - _dist_km(avant, apres)
            if cout < cout_min:
                meilleur, cout_min = i, cout
        if meilleur is None:
            continue
        noeuds.insert(meilleur + 1, {"param": param, "entree": a, "sortie": b})

    return [n["param"] for n in noeuds]


async def _geocode(address: str) -> Optional[list]:
    coords = _parse_coords(address)
    if coords:
        return coords
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            PTV_GEOCODE_URL,
            headers={"apiKey": PTV_API_KEY},
            params={"searchText": address, "countryFilter": COUNTRY_FILTER},
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
                    super_pref: bool = False, via_radius: Optional[int] = 5000,
                    traffic: bool = True, alternatives: bool = False) -> dict:
    """
    waypoints_list : "lat,lng" pour les points classiques,
                     "combinedTransport=..." pour une traversée imposée
                     (jamais en première ni en dernière position).
    via_radius     : rayon (m) des étapes intermédiaires. Avec un rayon, PTV
                     n'a qu'à passer à moins de X m du point (étape de
                     manipulation) : bien pour les villes-jalons automatiques,
                     mauvais pour une étape posée à la main sur une route
                     précise. None = étape exacte, la route passe par le point.
    traffic        : True = trafic temps réel PTV (fermetures, chantiers, bouchons).
                     False = trafic moyen (trafficMode=AVERAGE) : seules les
                     restrictions permanentes et le profil véhicule comptent.
    alternatives   : demande jusqu'à 3 variantes. PTV ne les calcule que pour
                     un trajet départ → arrivée sans étape ni traversée imposée.
    En plus du tracé, PTV renvoie les événements trafic et les restrictions
    enfreintes rencontrés sur la route. Si PTV refuse ces résultats en plus,
    on refait l'appel avec les résultats de base pour ne jamais bloquer le calcul.
    """
    base = ["POLYLINE", "TOLL_COSTS", "TOLL_SECTIONS", "BORDER_EVENTS",
            "WAYPOINT_EVENTS", "COMBINED_TRANSPORT_EVENTS"]
    extra = ["TRAFFIC_EVENTS", "VIOLATION_EVENTS"]
    if alternatives and len(waypoints_list) == 2:
        extra.append("ALTERNATIVE_ROUTES")
    query_params = [
        ("profile", "EUR_TRAILER_TRUCK"),
        ("options[currency]", "EUR"),
    ]
    dernier = len(waypoints_list) - 1
    for i, wp_str in enumerate(waypoints_list):
        if wp_str.startswith("combinedTransport="):
            if 0 < i < dernier:
                query_params.append(("waypoints", wp_str))
            continue
        parts = wp_str.split(",")
        lat, lng = float(parts[0].strip()), float(parts[1].strip())
        if 0 < i < dernier and via_radius:
            query_params.append(("waypoints", f"{lat},{lng};radius={int(via_radius)}"))
        else:
            query_params.append(("waypoints", f"{lat},{lng}"))
    avoid = []
    if avoid_tolls or super_pref: avoid.append("TOLL")
    if avoid_highways:            avoid.append("HIGHWAYS")
    if avoid:
        query_params.append(("options[avoid]", ",".join(avoid)))
    if not traffic:
        query_params.append(("options[trafficMode]", "AVERAGE"))
    async def _get(results: list):
        params = query_params + [("results", ",".join(results))]
        print(f"PTV QUERY: {params}")
        async with httpx.AsyncClient() as client:
            return await client.get(
                PTV_ROUTING_URL,
                headers={"apiKey": PTV_API_KEY},
                params=params,
                timeout=30,
            )

    resp = await _get(base + extra)
    if resp.status_code == 400:
        print(f"PTV 400 avec {extra}, nouvel essai sans : {resp.text[:300]}")
        resp = await _get(base)
    if resp.status_code != 200:
        print(f"PTV ERROR {resp.status_code}: {resp.text[:1000]}")
        raise HTTPException(502, f"PTV error {resp.status_code}: {resp.text[:500]}")
    ptv = resp.json()
    # Trace de contrôle : forme réelle des événements trafic / restriction
    # et des variantes, pour vérifier la lecture dans les logs Render.
    autres = [e for e in (ptv.get("events") or []) if isinstance(e, dict)
              and not ({"border", "waypoint", "combinedTransport"} & set(e))]
    if autres:
        print(f"PTV EVENT (exemple sur {len(autres)}) : {json.dumps(autres[0])[:600]}")
    if ptv.get("alternativeRoutes"):
        print(f"PTV ALTERNATIVES : {len(ptv['alternativeRoutes'])} variante(s), "
              f"clés {list(ptv['alternativeRoutes'][0])[:12]}")
    return ptv


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Géocodage (recherche adresse depuis la carte) ────────────────────────────
@app.get("/api/geocode")
async def api_geocode(q: str, country: str = ""):
    """`country` optionnel : codes ISO séparés par des virgules (ex. "NL,DK")."""
    if not q or len(q) < 3:
        raise HTTPException(400, "Requête trop courte")
    coords = _parse_coords(q)
    if coords:
        return {"lat": coords[0], "lng": coords[1], "label": q}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            PTV_GEOCODE_URL,
            headers={"apiKey": PTV_API_KEY},
            params={"searchText": q, "countryFilter": country.strip() or COUNTRY_FILTER},
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


# ── Lignes de ferry / navettes disponibles ───────────────────────────────────
@app.get("/api/ferry_options")
async def ferry_options(
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_km: float = FERRY_RADIUS_KM_DEFAULT,
    q: str = "",
    dest_lat: Optional[float] = None,
    dest_lng: Optional[float] = None,
    dest_cc: str = "",
):
    """
    Lignes au départ d'un port, via la Data API PTV (getCombinedTransports).
      - par position : lat/lng du port d'embarquement + radius_km ;
      - par texte    : q = nom de port ou de liaison ("Dunkerque", "Rosslare").
    dest_* (optionnels) : débarquement actuel, pour classer en tête les lignes
    qui arrivent dans le même pays et au plus près.
    Seules les lignes ouvertes aux camions sont renvoyées.
    """
    q = (q or "").strip()
    if q:
        if len(q) < 2:
            raise HTTPException(400, "Recherche trop courte")
        params = {"text[query]": q}
    elif lat is not None and lng is not None:
        rayon = min(max(radius_km, 1), FERRY_RADIUS_KM_MAX)
        params = {
            "position[latitude]":  lat,
            "position[longitude]": lng,
            "position[radius]":    int(rayon * 1000),
        }
    else:
        raise HTTPException(400, "Donner q, ou lat et lng")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            PTV_CT_URL,
            headers={"apiKey": PTV_API_KEY},
            params=params,
            timeout=20,
        )
    if resp.status_code != 200:
        print(f"PTV DATA ERROR {resp.status_code}: {resp.text[:500]}")
        raise HTTPException(502, f"PTV Data API {resp.status_code}: {resp.text[:300]}")

    items = resp.json().get("combinedTransports") or []
    options, vus = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        autorises = it.get("allowedFor")
        if autorises is not None and "TRUCK" not in str(autorises).upper():
            continue
        s, d = it.get("start") or {}, it.get("destination") or {}
        try:
            a = (float(s["latitude"]), float(s["longitude"]))
            b = (float(d["latitude"]), float(d["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        wp = _ferry_param(a, b)
        if wp in vus:
            continue
        vus.add(wp)
        duree = it.get("duration")
        options.append({
            "wp":           wp,
            "name":         it.get("name") or "",
            "type":         str(it.get("type") or "BOAT").upper(),
            "duration_min": round(duree / 60) if isinstance(duree, (int, float)) else None,
            "start": {"lat": a[0], "lng": a[1], "name": s.get("name") or "",
                      "cc": (s.get("countryCode") or "").upper()},
            "dest":  {"lat": b[0], "lng": b[1], "name": d.get("name") or "",
                      "cc": (d.get("countryCode") or "").upper()},
        })

    # PTV trie par distance au point de recherche. Si on connaît le
    # débarquement actuel, les lignes comparables passent devant.
    if dest_lat is not None and dest_lng is not None:
        cc = (dest_cc or "").upper()
        options.sort(key=lambda o: (
            0 if cc and o["dest"]["cc"] == cc else 1,
            _dist_km((dest_lat, dest_lng), (o["dest"]["lat"], o["dest"]["lng"])),
            o["duration_min"] or 0,
        ))

    return {
        "options":   options[:FERRY_OPTIONS_MAX],
        "truncated": len(options) > FERRY_OPTIONS_MAX,
    }


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
    origin_coords = _valid_pair(data.origin_coords) or await _geocode(data.origin)
    dest_coords   = _valid_pair(data.dest_coords)   or await _geocode(data.dest)
    if not origin_coords or not dest_coords:
        raise HTTPException(400, "Géocodage impossible")

    # Étapes posées sur la carte : elles priment sur les jalons préférentiels
    via = [p for p in (_valid_pair(v) for v in data.via) if p]
    if via:
        pref_wps = [{"lat": p[0], "lng": p[1]} for p in via]
    else:
        pref_wps = find_pref_waypoints(data.origin, data.dest, super_mode=data.super_pref)

    waypoints_list = [f"{origin_coords[0]},{origin_coords[1]}"]
    for wp in pref_wps:
        waypoints_list.append(f"{wp['lat']},{wp['lng']}")
    waypoints_list.append(f"{dest_coords[0]},{dest_coords[1]}")
    waypoints_list = _insert_ferries(waypoints_list, data.ferries)

    # Étapes posées à la main : exactes. Jalons automatiques : rayon 5 km.
    ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways, data.super_pref,
                          via_radius=None if via else 5000, traffic=data.traffic,
                          alternatives=data.alternatives)
    payload = _route_payload(ptv)
    payload.update({
        "origin":         data.origin,
        "dest":           data.dest,
        "pref_waypoints": pref_wps,
        "ferries_sent":   [w for w in waypoints_list if w.startswith("combinedTransport=")],
    })
    return payload


# ── Recalcul drag ────────────────────────────────────────────────────────────
@app.post("/api/recalculate_drag")
async def recalculate_drag(data: RecalcDragRequest):
    if len(data.waypoints) < 2:
        raise HTTPException(400, "Il faut au minimum 2 waypoints")
    waypoints_list = _insert_ferries([f"{wp.lat},{wp.lng}" for wp in data.waypoints],
                                     data.ferries)
    print("="*60)
    print(f"DRAG RECALC — {len(waypoints_list)} waypoints")
    for i, wp in enumerate(waypoints_list):
        print(f"  [{i}] {wp}")
    print("="*60)
    try:
        # Points déplacés ou ajoutés sur la carte : la route doit passer dessus.
        ptv = await _call_ptv(waypoints_list, data.avoid_tolls, data.avoid_highways,
                              via_radius=None, traffic=data.traffic,
                              alternatives=data.alternatives)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erreur interne: {e}")

    payload = _route_payload(ptv)
    payload["ferries_sent"] = [w for w in waypoints_list if w.startswith("combinedTransport=")]
    print(f"RÉSULTAT PTV : {payload['distance_km']}km, {len(payload['polyline'])} points, "
          f"{len(payload['crossings'])} traversée(s)")

    # Mise à jour Firebase — polyline_current uniquement, originaux préservés
    if data.route_id and FIREBASE_URL:
        update_data = {
            "polyline_current": payload["polyline"],
            "distance_km":      payload["distance_km"],
            "duration_h":       payload["duration_h"],
            "prix_peage":       payload["prix_peage"],
            "toll_by_country":  payload["toll_by_country"],
            "km_by_country":    payload["km_by_country"],
            "crossings":        payload["crossings"],
        }
        try:
            httpx.patch(
                f"{FIREBASE_URL}/routes/{data.route_id}.json",
                json=update_data, timeout=10
            )
        except Exception as e:
            print(f"Erreur maj Firebase: {e}")

    return payload


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
    ferries = []
    for f in data.ferries:
        parsed = _parse_ferry(f)
        if parsed:
            ferries.append(_ferry_param(*parsed))
    today = date.today().isoformat()

    entree = {
        "origine":      data.origin.strip(),
        "destination":  data.dest.strip(),
        "waypoints":    wp_strings,
        "km_reference": round(data.km, 1),
        "source":       "carte_manuelle",
        "date":         today,
    }
    if ferries:
        entree["ferries"] = ferries

    if existing_idx is not None:
        new_confiance = routes[existing_idx].get("confiance", 1) + 1
        entree["confiance"] = new_confiance
        routes[existing_idx] = entree
        action = f"update ({new_confiance}x validé)"
    else:
        entree["confiance"] = 1
        routes.append(entree)
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
        "ferries":       len(ferries),
        "km":            round(data.km, 1),
        "total_learned": len(routes),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("map_server_main:app", host="0.0.0.0", port=8000, reload=False)
