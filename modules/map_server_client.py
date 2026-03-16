"""
Client HTTP vers le serveur de cartes.
Remplace generer_carte() (HTML local) par une URL publique partageable.
"""
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")


def create_route_url(
    origin_name: str,
    dest_name:   str,
    km:          float,
    duration_h:  float,
    polyline:    list,   # [[lat, lon], ...]
    prix_peage:  float = 0.0,
) -> str:
    """
    Envoie le trajet au serveur de cartes.
    Retourne l'URL publique cliquable (ex: https://ton-serveur.com/carte?id=abc123)
    En cas d'erreur → retourne "" (le reste du traitement continue)
    """
    payload = {
        "origin":      origin_name,
        "dest":        dest_name,
        "distance_km": km,
        "duration_h":  duration_h,
        "polyline":    polyline,
        "prix_peage":  prix_peage,
    }
    try:
        r = httpx.post(
            f"{MAP_SERVER_URL}/api/create_route",
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        url = r.json().get("url", "")
        print(f"      🌐 Carte publique créée : {url}")
        return url
    except httpx.HTTPStatusError as e:
        print(f"      ⚠️ Serveur carte HTTP {e.response.status_code} : {e.response.text}")
        return ""
    except Exception as e:
        print(f"      ⚠️ Serveur carte inaccessible : {e}")
        return ""
