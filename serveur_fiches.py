"""
serveur_fiches.py — Générateur de fiches Bouresmau, remplissage automatique.

Petit serveur local (bibliothèque standard uniquement) qui :
  - sert la page  fiches.html  (le générateur d'affiches) ;
  - expose une API  /api/fiche?ref=...  qui interroge EPREL + Icecat (via eprel.py) et
    renvoie, à partir d'une simple référence, toutes les données produit :
    marque, classe énergie, catégorie, caractéristiques, étiquette énergie
    officielle (image) et fiche produit officielle (PDF).

Le navigateur ne peut pas interroger EPREL/Icecat lui-même proprement (blocage
CORS, clés côté serveur) : ce serveur s'en charge, puis la page web remplit la
fiche automatiquement.

Lancement :  python serveur_fiches.py     (ou double-clic sur lancer_fiches.bat)
Puis ouvrir :  http://localhost:8770
"""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import eprel

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BOURESMAU_PORT", "8770"))
HOST = os.environ.get("BOURESMAU_HOST", "127.0.0.1")
PAGE = "fiches.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send(200, json.dumps({"status": "ok"}))
            return

        # ---- API de remplissage automatique depuis une référence ---------
        if parsed.path == "/api/fiche":
            params = parse_qs(parsed.query)
            ref = (params.get("ref") or [""])[0].strip()
            group = (params.get("group") or [""])[0].strip() or None
            want_files = (params.get("files") or ["1"])[0] != "0"
            if not ref:
                self._send(400, json.dumps({"found": False,
                                            "message": "Référence vide."}))
                return
            try:
                result = eprel.lookup(ref, group=group, with_files=want_files)
                self._send(200, json.dumps(result, ensure_ascii=False))
            except Exception as exc:  # pragma: no cover
                self._send(500, json.dumps({"found": False,
                                            "message": f"Erreur serveur : {exc}"}))
            return

        # ---- Autocomplétion : suggestions de références EPREL + Icecat ---
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            q = (params.get("q") or [""])[0].strip()
            try:
                results = eprel.search(q, limit=8)
                self._send(200, json.dumps({"results": results},
                                           ensure_ascii=False))
            except Exception as exc:  # pragma: no cover
                self._send(500, json.dumps({"results": [],
                                            "message": f"Erreur serveur : {exc}"}))
            return

        # ---- Logo d'une marque (indépendant d'EPREL) ---------------------
        # Permet à la page de récupérer un logo pour N'IMPORTE QUELLE marque
        # saisie à la main (machines à café, petit électroménager… absents
        # d'EPREL). Renvoie un data-URI base64 (pas de souci de CORS canvas).
        if parsed.path == "/api/logo":
            params = parse_qs(parsed.query)
            brand = (params.get("brand") or [""])[0].strip()
            site = (params.get("site") or [""])[0].strip()
            if not brand:
                self._send(400, json.dumps({"found": False}))
                return
            try:
                session = eprel._session()
                uri = eprel._fetch_brand_logo(session, brand, manufacturer_url=site)
                self._send(200, json.dumps({"found": bool(uri),
                                            "dataUri": uri or ""},
                                           ensure_ascii=False))
            except Exception as exc:  # pragma: no cover
                self._send(500, json.dumps({"found": False,
                                            "message": f"Erreur serveur : {exc}"}))
            return

        # ---- Assets visuels extraits des maquettes fournies ---------------
        visual_assets = {
            "/assets/reparabilite-officielle.png": "reparabilite-officielle.png",
            "/assets/service-delivery.png": "service-delivery.png",
            "/assets/service-installation.png": "service-installation.png",
            "/assets/service-warranty.png": "service-warranty.png",
        }
        if parsed.path in visual_assets:
            asset = os.path.join(HERE, visual_assets[parsed.path])
            try:
                with open(asset, "rb") as fh:
                    self._send(200, fh.read(), "image/png")
            except FileNotFoundError:
                self._send(404, "Asset introuvable", "text/plain")
            return

        # ---- Page web ----------------------------------------------------
        if parsed.path in ("/", "/" + PAGE, "/index.html"):
            try:
                with open(os.path.join(HERE, PAGE), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, f"{PAGE} introuvable", "text/plain")
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Si l'application tourne déjà, un nouveau double-clic rouvre la page.
        webbrowser.open(url)
        return
    print(f"Générateur de fiches Bouresmau en ligne sur  {url}")
    print("Tapez une référence dans la page : la fiche se remplit toute seule.")
    print("Fermez cette fenêtre pour arrêter le serveur.")
    if HOST in ("127.0.0.1", "localhost"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
