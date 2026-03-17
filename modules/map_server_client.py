# map_server_client.py

import httpx
import time
import os
import threading
from dotenv import load_dotenv

load_dotenv()

MAP_SERVER_URL = os.environ.get("MAP_SERVER_URL", "http://localhost:8000")

MAX_RETRIES  = 3
RETRY_DELAY  = 5
TIMEOUT      = 60   # ↑ augmenté car Render peut être lent

# 1 seule requête carte à la fois (Render free tier)
_map_semaphore = threading.Semaphore(1)


def warm_up_server() -> bool:
    print(f"\n🔌 Réveil du serveur de cartes...")
    for attempt in range(1, 7):
        try:
            r = httpx.get(f"{MAP_SERVER_URL}/health", timeout=10)
            if r.status_code == 200:
                print(f"   ✅ Serveur prêt ({attempt * 10}s)")
                return True
        except (httpx.TimeoutException, httpx.ConnectError):
            pass
        print(f"   ⏳ Pas encore prêt... ({attempt}/6)")
        time.sleep(10)
    print(f"   ❌ Serveur inaccessible après 60s")
    return False


def create_route_url(
    origin_name: str,
    dest_name:   str,
    km:          float,
    duration_h:  float,
    polyline:    list,
    prix_peage:  float = 0.0,
) -> str:
    payload = {
        "origin":      origin_name,
        "dest":        dest_name,
        "distance_km": km,
        "duration_h":  duration_h,
        "polyline":    polyline,
        "prix_peage":  prix_peage,
    }

    with _map_semaphore:   # ← 1 seul thread entre ici à la fois
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if attempt > 1:
                    print(f"      🔄 Tentative {attempt}/{MAX_RETRIES}...")

                r = httpx.post(
                    f"{MAP_SERVER_URL}/api/create_route",
                    json=payload,
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                url = r.json().get("url", "")
                print(f"      🌐 {url}")
                return url

            except httpx.HTTPStatusError as e:
                print(f"      ⚠️ HTTP {e.response.status_code}")
                return ""

            except (httpx.TimeoutException, httpx.ConnectError):
                print(f"      ⏳ Timeout ({attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"      ⚠️ Carte échouée")
                    return ""

            except Exception as e:
                print(f"      ⚠️ Erreur : {e}")
                return ""

    return ""
