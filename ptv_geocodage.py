"""
Géocodage PTV pour la Carte Manuelle.

Trois routes, appelées par map.html :
    GET /api/ptv/suggest?q=lieg&cc=BE   → autocomplétion (suggestions/by-text), sans coordonnées
    GET /api/ptv/locate?q=Liège&cc=BE   → géocodage complet (locations/by-text), avec coordonnées
    GET /api/ptv/reverse?lat=..&lng=..  → adresse la plus proche (locations/by-position)

Ajout dans map_server_main.py (deux lignes, après la création de `app`) :
    from ptv_geocodage import router as ptv_geo_router
    app.include_router(ptv_geo_router)

La clé est lue dans les variables d'environnement Render (voir _ptv_key).
Chaque appel PTV est une transaction facturée : tout passe par un cache
mémoire de 24 h, donc une même saisie ne coûte qu'une fois par jour et par
instance. Ajouter ?debug=1 à une route renvoie la réponse PTV brute.
"""

import os
import threading
import time
from collections import OrderedDict

import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/ptv", tags=["géocodage PTV"])

PTV_GEO = "https://api.myptv.com/geocoding/v1"
TIMEOUT_S = 6
CACHE_TTL_S = 24 * 3600
CACHE_MAX = 5000

_http = requests.Session()
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_lock = threading.Lock()

ISO3_TO_2 = {
    "BEL": "BE", "FRA": "FR", "LUX": "LU", "NLD": "NL", "DEU": "DE", "ITA": "IT", "ESP": "ES",
    "PRT": "PT", "CHE": "CH", "AUT": "AT", "POL": "PL", "CZE": "CZ", "SVK": "SK", "HUN": "HU",
    "SVN": "SI", "HRV": "HR", "ROU": "RO", "BGR": "BG", "DNK": "DK", "SWE": "SE", "NOR": "NO",
    "FIN": "FI", "IRL": "IE", "GBR": "GB",
}

# locationType PTV → types reconnus par map.html (classement et pastille)
TYPE_MAP = {
    "LOCALITY": "city", "POSTAL_CODE": "locality", "DISTRICT": "district",
    "SUBDISTRICT": "district", "STREET": "street", "EXACT_ADDRESS": "house",
    "INTERPOLATED_ADDRESS": "house", "STATE": "state", "PROVINCE": "county", "COUNTRY": "country",
}


def _ptv_key() -> str:
    # Adaptez si votre clé porte un autre nom dans les variables Render.
    for name in ("PTV_API_KEY", "PTV_KEY", "API_KEY_PTV", "MYPTV_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip()
    raise HTTPException(500, "Clé PTV introuvable dans les variables d'environnement (PTV_API_KEY)")


def _cache_get(key: str):
    with _lock:
        hit = _cache.get(key)
        if not hit:
            return None
        if time.time() - hit[0] > CACHE_TTL_S:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return hit[1]


def _cache_set(key: str, value: dict):
    with _lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def _ptv(path: str, params: dict) -> dict:
    try:
        r = _http.get(f"{PTV_GEO}/{path}", params=params,
                      headers={"ApiKey": _ptv_key()}, timeout=TIMEOUT_S)
    except requests.Timeout:
        raise HTTPException(504, "PTV : délai dépassé")
    except requests.RequestException as e:
        raise HTTPException(502, f"PTV injoignable : {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"PTV {path} : {r.text[:200]}")
    return r.json()


def _cc(a: dict) -> str:
    code = (a.get("countryCodeIsoAlpha2") or a.get("countryCode")
            or a.get("countryCodeIsoAlpha3") or "").upper()
    return ISO3_TO_2.get(code, code if len(code) == 2 else "")


def _location(loc: dict) -> dict:
    a = loc.get("address") or {}
    pos = loc.get("referencePosition") or loc.get("roadAccessPosition") or {}
    t = TYPE_MAP.get(str(loc.get("locationType", "")).upper(), "other")
    street, num = a.get("street") or "", a.get("houseNumber") or ""
    city = a.get("city") or a.get("district") or ""
    name = street if t in ("street", "house") and street else (city or loc.get("formattedAddress") or "")
    return {
        "lat": pos.get("latitude"), "lng": pos.get("longitude"),
        "name": name, "label": loc.get("formattedAddress") or "",
        "street": street, "housenumber": num,
        "postcode": a.get("postalCode") or "", "city": city,
        "region": a.get("province") or a.get("state") or "",
        "country": a.get("countryName") or "", "cc": _cc(a), "type": t,
    }


def _suggestion(s: dict) -> dict:
    # Les champs varient selon la version de l'API : on prend ce qui existe.
    a = s.get("address") if isinstance(s.get("address"), dict) else s
    caption = s.get("caption") or s.get("text") or s.get("formattedAddress") or ""
    sub = s.get("subCaption") or s.get("secondaryCaption") or ""
    city = a.get("locality") or a.get("city") or ""
    country = a.get("countryName") or a.get("country") or ""
    if not sub:
        sub = ", ".join(x for x in (a.get("state") or "", country) if x and x != caption)
    return {
        "lat": None, "lng": None,
        "name": caption.split(" (")[0] if caption else city,
        "label": caption, "query": s.get("searchText") or caption,
        "street": a.get("street") or "", "housenumber": a.get("houseNumber") or "",
        "postcode": a.get("postalCode") or "", "city": "" if city == caption else city,
        "region": "", "country": sub or country, "cc": _cc(a), "type": "other",
    }


@router.get("/suggest")
def ptv_suggest(q: str = Query(..., min_length=2, max_length=200),
                cc: str = "", lang: str = "fr", debug: int = 0):
    key = f"s|{lang}|{cc.upper()}|{q.strip().lower()}"
    if not debug and (hit := _cache_get(key)) is not None:
        return hit
    params = {"searchText": q.strip(), "language": lang}
    if cc:
        params["countryFilter"] = cc.upper()
    raw = _ptv("suggestions/by-text", params)
    if debug:
        return raw
    out = {"results": [_suggestion(s) for s in (raw.get("suggestions") or [])[:10]]}
    _cache_set(key, out)
    return out


@router.get("/locate")
def ptv_locate(q: str = Query(..., min_length=2, max_length=300),
               cc: str = "", lang: str = "fr", debug: int = 0):
    key = f"l|{lang}|{cc.upper()}|{q.strip().lower()}"
    if not debug and (hit := _cache_get(key)) is not None:
        return hit
    params = {"searchText": q.strip(), "language": lang}
    if cc:
        params["countryFilter"] = cc.upper()
    raw = _ptv("locations/by-text", params)
    if debug:
        return raw
    out = {"results": [_location(l) for l in (raw.get("locations") or [])[:10]]}
    _cache_set(key, out)
    return out


@router.get("/reverse")
def ptv_reverse(lat: float = Query(..., ge=-90, le=90), lng: float = Query(..., ge=-180, le=180),
                lang: str = "fr", debug: int = 0):
    key = f"r|{lang}|{lat:.5f}|{lng:.5f}"
    if not debug and (hit := _cache_get(key)) is not None:
        return hit
    raw = _ptv(f"locations/by-position/{lat:.6f}/{lng:.6f}", {"language": lang})
    if debug:
        return raw
    out = {"results": [_location(l) for l in (raw.get("locations") or [])[:3]]}
    _cache_set(key, out)
    return out
