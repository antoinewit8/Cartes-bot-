"""
Villes-jalons automatiques pour forcer les axes routiers PL.
Si le trajet passe à moins de 35km d'une ville-jalon, elle devient waypoint.
"""

import math

# ==========================================
# VILLES-JALONS (lat, lon)
# ==========================================

VILLES_JALONS = {
    # Sud / Massif Central
    "Puy-en-Velay":     (45.0426,  3.8849),
    "Mende":            (44.5188,  3.4986),
    "Rodez":            (44.3516,  2.5735),

    # Ouest
    "Châteaubriant":    (47.7161, -1.3760),
    "Mayenne":          (48.3024, -0.6148),
    "Laval":            (48.0784, -0.7669),
    "Le Mans":          (48.0061,  0.1996),

    # Normandie / N12
    "Alençon":          (48.4322,  0.0913),
    "Argentan":         (48.7448, -0.0206),
    "Dreux":            (48.7356,  1.3662),

    # Nord
    "Amiens":           (49.8941,  2.2958),
    "Albert":           (50.0007,  2.6508),
    "Bapaume":          (50.1039,  2.8511),
    "Cambrai":          (50.1764,  3.2354),
    "Laon":             (49.5637,  3.6241),
    "Soissons":         (49.3817,  3.3236),

    # Ardennes
    "Vouziers":         (49.3956,  4.7014),

    # Est
    "Commercy":         (48.7618,  5.5915),
    "Nancy":            (48.6921,  6.1844),
    "Verdun":           (49.1598,  5.3823),
    "Épinal":           (48.1726,  6.4510),
    "Chaumont":         (48.1135,  5.1390),

    # Centre / Champagne
    "Troyes":           (48.2973,  4.0744),
    "Orléans":          (47.9029,  1.9039),
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

