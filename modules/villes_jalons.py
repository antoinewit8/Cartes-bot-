"""
Villes-jalons automatiques pour forcer les axes routiers PL.
Si le trajet passe à moins de 35km d'une ville-jalon, elle devient waypoint.
"""

import math

# ==========================================
# VILLES-JALONS (lat, lon)
# ==========================================

VILLES_JALONS = {
    # Sud / Massif Central (points sur N88, A75, RN)
    "Puy-en-Velay":     (45.0540,  3.8530),   # N88 sortie sud
    "Mende":            (44.5060,  3.4710),   # N88 contournement
    "Rodez":            (44.3660,  2.6050),   # RN88 échangeur nord

    # Ouest (points sur N171, N162, A81)
    "Châteaubriant":    (47.7200, -1.3550),   # N171 contournement
    "Mayenne":          (48.2980, -0.6330),   # N162 sortie
    "Laval":            (48.0850, -0.7340),   # A81 échangeur
    "Le Mans":          (48.0250,  0.2200),   # A28/A81 échangeur

    # Normandie / N12
    "Alençon":          (48.4450,  0.1150),   # N12 contournement
    "Argentan":         (48.7380, -0.0050),   # N26 échangeur
    "Dreux":            (48.7250,  1.3900),   # N12 contournement sud

    # Nord (points sur A1, A26, N2, N44)
    "Amiens":           (49.8700,  2.3350),   # A29/A16 échangeur
    "Albert":           (50.0050,  2.6350),   # D929/N29 contournement
    "Bapaume":          (50.1100,  2.8550),   # A1 sortie Bapaume
    "Cambrai":          (50.1900,  3.2100),   # A26 échangeur
    "Laon":             (49.5780,  3.6450),   # N2 contournement est
    "Soissons":         (49.3700,  3.3450),   # N2 contournement sud

    # Ardennes
    "Vouziers":         (49.3850,  4.6850),   # D946 contournement

    # Est (points sur A31, A4, N4)
    "Commercy":         (48.7550,  5.5700),   # N4 contournement
    "Nancy":            (48.7100,  6.2100),   # A31 échangeur sud
    "Verdun":           (49.1750,  5.3650),   # N3/A4 échangeur
    "Épinal":           (48.1850,  6.4350),   # N57 contournement
    "Chaumont":         (48.1050,  5.1200),   # N67 contournement

    # Centre / Champagne
    "Troyes":           (48.3100,  4.1000),   # A5/A26 échangeur
    "Orléans":          (47.9200,  1.9300),   # A10 échangeur nord
}


# ==========================================
# AXES STRATÉGIQUES
# ==========================================

AXE_N12 = ["Dreux", "Alençon", "Mayenne", "Laval"]
AXE_N2  = ["Soissons", "Laon", "Cambrai"]

RAYON_DETECTION_KM = 35  # réduit de 50 à 35 pour éviter les faux positifs


# ==========================================
# HAVERSINE
# ==========================================
def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ==========================================
# DISTANCE POINT → SEGMENT
# ==========================================
def _distance_point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return _haversine(px, py, ax, ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    proj_lat = ax + t * dx
    proj_lon = ay + t * dy
    return _haversine(px, py, proj_lat, proj_lon)


# ==========================================
# DÉTECTION AXES
# ==========================================
def _is_east_west(lat_start, lon_start, lat_end, lon_end) -> bool:
    lat_moy = (lat_start + lat_end) / 2
    delta_lon = abs(lon_end - lon_start)
    delta_lat = abs(lat_end - lat_start)
    return (47.5 <= lat_moy <= 49.5
            and delta_lon > 2.0
            and delta_lon > delta_lat * 1.5)


def _is_north_axis(lat_start, lon_start, lat_end, lon_end) -> bool:
    lat_max = max(lat_start, lat_end)
    delta_lon = abs(lon_end - lon_start)
    return lat_max >= 49.0 and delta_lon > 2.0


# ==========================================
# FONCTION PRINCIPALE
# ==========================================
def detecter_villes_jalons(lat_start, lon_start, lat_end, lon_end) -> list:
    print(f"      🧭 Jalons: ({lat_start:.4f}, {lon_start:.4f}) → ({lat_end:.4f}, {lon_end:.4f})")
    villes_proches = []

    # 1. Détection par proximité au segment direct
    for ville, (vlat, vlon) in VILLES_JALONS.items():
        dist = _distance_point_to_segment(
            vlat, vlon,
            lat_start, lon_start,
            lat_end, lon_end
        )
        if dist <= RAYON_DETECTION_KM:
            dist_from_start = _haversine(lat_start, lon_start, vlat, vlon)
            villes_proches.append((ville, vlat, vlon, dist_from_start, dist))

    # 2. Axes forcés — TOUJOURS vérifier la proximité au segment
    if _is_east_west(lat_start, lon_start, lat_end, lon_end):
        for ville in AXE_N12:
            if not any(v[0] == ville for v in villes_proches):
                vlat, vlon = VILLES_JALONS[ville]
                dist_seg = _distance_point_to_segment(
                    vlat, vlon,
                    lat_start, lon_start,
                    lat_end, lon_end
                )
                if dist_seg <= RAYON_DETECTION_KM:
                    dist_from_start = _haversine(lat_start, lon_start, vlat, vlon)
                    villes_proches.append((ville, vlat, vlon, dist_from_start, dist_seg))
                    print(f"      🛣️  N12 forcé : {ville} ({dist_seg:.0f}km du segment)")
                else:
                    print(f"      🛣️  N12 ignoré : {ville} ({dist_seg:.0f}km trop loin)")

    if _is_north_axis(lat_start, lon_start, lat_end, lon_end):
        for ville in AXE_N2:
            if not any(v[0] == ville for v in villes_proches):
                vlat, vlon = VILLES_JALONS[ville]
                dist_seg = _distance_point_to_segment(
                    vlat, vlon,
                    lat_start, lon_start,
                    lat_end, lon_end
                )
                if dist_seg <= RAYON_DETECTION_KM:
                    dist_from_start = _haversine(lat_start, lon_start, vlat, vlon)
                    villes_proches.append((ville, vlat, vlon, dist_from_start, dist_seg))
                    print(f"      🛣️  N2 forcé : {ville} ({dist_seg:.0f}km du segment)")
                else:
                    print(f"      🛣️  N2 ignoré : {ville} ({dist_seg:.0f}km trop loin)")

    # 3. Tri par distance depuis le départ
    villes_proches.sort(key=lambda x: x[3])

    # 4. Conversion en strings "lat, lon"
    waypoints = []
    for ville, vlat, vlon, dist_start, dist_seg in villes_proches:
        waypoints.append(f"{vlat}, {vlon}")
        print(f"      📌 Jalon auto : {ville} ({dist_seg:.0f}km du trajet, {dist_start:.0f}km du départ)")

    return waypoints

