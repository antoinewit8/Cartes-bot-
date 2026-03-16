import os
import json
import polyline as polyline_lib

CARTES_DIR = "cartes_km"


def generer_carte(origin_name, dest_name, encoded_polyline):
    """Génère une carte HTML Leaflet avec le tracé de l'itinéraire."""
    if not encoded_polyline:
        return ""

    os.makedirs(CARTES_DIR, exist_ok=True)

    coords = []

    # Cas 1 : GeoJSON LineString {"type":"LineString","coordinates":[[lon,lat],...]}
    try:
        geojson = json.loads(encoded_polyline)
        if geojson.get("type") == "LineString":
            coords = [[c[1], c[0]] for c in geojson["coordinates"]]
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Cas 2 : Encoded polyline Google (fallback)
    if not coords:
        try:
            coords = polyline_lib.decode(encoded_polyline)
        except:
            return ""

    if not coords:
        return ""

    center_lat = sum(c[0] for c in coords) / len(coords)
    center_lon = sum(c[1] for c in coords) / len(coords)
    coords_js = str([[c[0], c[1]] for c in coords])

    safe_name = f"{origin_name}_to_{dest_name}".replace(" ", "_").replace(",", "")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "_-")[:80]
    filename = f"{safe_name}.html"
    filepath = os.path.join(CARTES_DIR, filename)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{origin_name} → {dest_name}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body{{margin:0}}
  #map{{height:100vh;width:100vw}}

  /* ---- Marqueur personnalisé A / B ---- */
  .marker-label {{
    width: 32px;
    height: 32px;
    border-radius: 50% 50% 50% 0;
    transform: rotate(-45deg);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: bold;
    font-size: 14px;
    color: white;
    border: 2px solid white;
    box-shadow: 0 2px 6px rgba(0,0,0,0.4);
  }}
  .marker-label span {{
    transform: rotate(45deg);
  }}
  .marker-A {{ background-color: #2ecc71; }}
  .marker-B {{ background-color: #e74c3c; }}
</style>
</head><body>
<div id="map"></div>
<script>
var coords = {coords_js};
var map = L.map('map').setView([{center_lat},{center_lon}], 7);

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors'
}}).addTo(map);

// Tracé de la route
var line = L.polyline(coords, {{color:'#0057A8', weight:4, opacity:0.85}}).addTo(map);
map.fitBounds(line.getBounds().pad(0.1));

// Icône A — Départ (vert)
var iconA = L.divIcon({{
  className: '',
  html: '<div class="marker-label marker-A"><span>A</span></div>',
  iconSize:   [32, 32],
  iconAnchor: [16, 32],
  popupAnchor:[0, -34]
}});

// Icône B — Arrivée (rouge)
var iconB = L.divIcon({{
  className: '',
  html: '<div class="marker-label marker-B"><span>B</span></div>',
  iconSize:   [32, 32],
  iconAnchor: [16, 32],
  popupAnchor:[0, -34]
}});

// Placement des marqueurs
L.marker(coords[0], {{icon: iconA}})
  .addTo(map)
  .bindPopup("<b>🟢 Départ</b><br>{origin_name}");

L.marker(coords[coords.length-1], {{icon: iconB}})
  .addTo(map)
  .bindPopup("<b>🔴 Arrivée</b><br>{dest_name}");

</script></body></html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    chemin_relatif = f"{CARTES_DIR}/{filename}"
    print(f"   🗺️  Carte générée : {chemin_relatif}")
    return chemin_relatif
