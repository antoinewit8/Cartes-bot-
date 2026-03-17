"""
Client HTTP vers le serveur de cartes.
Remplace generer_carte() (HTML local) par une URL publique partageable.
"""
import httpx
import time
import os
from dotenv import load_dotenv

load_dotenv()

MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")

MAX_RETRIES  = 3
RETRY_DELAY  = 10   # secondes entre chaque tentative
TIMEOUT      = 180  # secondes par tentative


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
    Retry automatique jusqu'à MAX_RETRIES fois en cas de timeout.
    """
    payload = {
        "origin":      origin_name,
        "dest":        dest_name,
        "distance_km": km,
        "duration_h":  duration_h,
        "polyline":    polyline,
        "prix_peage":  prix_peage,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt == 1:
                print(f"      🔍 Envoi carte → {MAP_SERVER_URL}/api/create_route")
            else:
                print(f"      🔄 Tentative {attempt}/{MAX_RETRIES}...")

            r = httpx.post(
                f"{MAP_SERVER_URL}/api/create_route",
                json=payload,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            url = r.json().get("url", "")
            print(f"      🌐 Carte publique créée : {url}")
            return url

        except httpx.HTTPStatusError as e:
            # Erreur HTTP (4xx/5xx) → pas la peine de retry
            print(f"      ⚠️ Serveur carte HTTP {e.response.status_code} : {e.response.text}")
            return ""

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            print(f"      ⏳ Timeout/connexion ({attempt}/{MAX_RETRIES}) : {e}")
            if attempt < MAX_RETRIES:
                print(f"      💤 Attente {RETRY_DELAY}s avant retry...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"      ⚠️ Serveur carte inaccessible après {MAX_RETRIES} tentatives")
                return ""

        except Exception as e:
            print(f"      ⚠️ Erreur inattendue : {e}")
            return ""

    return ""
