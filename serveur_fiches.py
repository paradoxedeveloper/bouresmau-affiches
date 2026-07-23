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
import hashlib
import hmac
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import eprel

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BOURESMAU_PORT", "8770"))
HOST = os.environ.get("BOURESMAU_HOST", "127.0.0.1")
PAGE = "fiches.html"
AUTH_USER = os.environ.get("BOURESMAU_USER", "bouresmau")
AUTH_PASSWORD = os.environ.get("BOURESMAU_PASSWORD", "")
AUTH_SECRET = os.environ.get("BOURESMAU_AUTH_SECRET", "") or secrets.token_hex(32)
SESSION_SECONDS = 12 * 60 * 60


def _session_token(expires):
    payload = f"{AUTH_USER}|{expires}"
    signature = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _valid_session(token):
    try:
        expires_text, signature = token.split(".", 1)
        expires = int(expires_text)
    except (ValueError, AttributeError):
        return False
    if expires < int(time.time()):
        return False
    expected = _session_token(expires).split(".", 1)[1]
    return hmac.compare_digest(signature, expected)


def _login_page(error=False):
    message = '<p class="error">Identifiant ou mot de passe incorrect.</p>' if error else ""
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connexion — Bouresmau</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f5f3;
font-family:Arial,sans-serif;color:#111}} .card{{width:min(410px,calc(100% - 32px));background:#fff;padding:42px;
border:1px solid #ddd;border-radius:22px;box-shadow:0 18px 55px #00000018}} .logo{{font-size:42px;font-weight:800;
letter-spacing:-2px;margin-bottom:5px}} .bar{{width:58px;height:8px;border-radius:8px;background:#d62b2f;margin-bottom:28px}}
h1{{font-size:22px;margin:0 0 7px}} .intro{{color:#666;margin:0 0 25px}} label{{display:block;font-weight:700;
font-size:14px;margin:16px 0 7px}} input{{width:100%;padding:13px 14px;border:1px solid #bbb;border-radius:9px;
font-size:16px}} input:focus{{outline:2px solid #111;border-color:#111}} button{{width:100%;margin-top:24px;padding:14px;
border:0;border-radius:9px;background:#111;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.error{{padding:10px 12px;background:#fff0f0;color:#b00020;border-radius:8px;font-size:14px}}
</style></head><body><main class="card"><div class="logo">Bouresmau</div><div class="bar"></div>
<h1>Accès au créateur d’affiches</h1><p class="intro">Connectez-vous pour continuer.</p>{message}
<form method="post" action="/login"><label for="user">Identifiant</label>
<input id="user" name="user" autocomplete="username" required autofocus>
<label for="password">Mot de passe</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Se connecter</button></form></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _authenticated(self):
        if not AUTH_PASSWORD:
            return True
        cookies = {}
        for item in self.headers.get("Cookie", "").split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value
        return _valid_session(cookies.get("bouresmau_session", ""))

    def _redirect(self, location, cookie=None):
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(303, b"", "text/plain; charset=utf-8", headers)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            params = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            user = (params.get("user") or [""])[0]
            password = (params.get("password") or [""])[0]
            user_ok = hmac.compare_digest(user, AUTH_USER)
            password_ok = bool(AUTH_PASSWORD) and hmac.compare_digest(password, AUTH_PASSWORD)
            if user_ok and password_ok:
                expires = int(time.time()) + SESSION_SECONDS
                cookie = (f"bouresmau_session={_session_token(expires)}; Max-Age={SESSION_SECONDS}; "
                          "Path=/; HttpOnly; Secure; SameSite=Lax")
                self._redirect("/", cookie)
            else:
                self._send(401, _login_page(error=True), "text/html; charset=utf-8")
            return
        if parsed.path == "/logout":
            self._redirect("/login", "bouresmau_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax")
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send(200, json.dumps({"status": "ok"}))
            return
        if parsed.path == "/login":
            if self._authenticated():
                self._redirect("/")
            else:
                self._send(200, _login_page(), "text/html; charset=utf-8")
            return
        if parsed.path == "/logout":
            self._redirect("/login", "bouresmau_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax")
            return
        if not self._authenticated():
            if parsed.path.startswith("/api/"):
                self._send(401, json.dumps({"error": "authentication required"}))
            else:
                self._redirect("/login")
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
                    page = fh.read()
                    if AUTH_PASSWORD:
                        logout = b"""<style>@media print{#bouresmau-logout{display:none!important}}</style>
<form id="bouresmau-logout" method="post" action="/logout" style="position:fixed;right:18px;bottom:18px;z-index:99999">
<button type="submit" style="border:0;border-radius:9px;background:#111;color:#fff;padding:10px 14px;font-weight:700;cursor:pointer;box-shadow:0 4px 16px #0003">Se deconnecter</button>
</form>"""
                        page = page.replace(b"</body>", logout + b"</body>", 1)
                    self._send(200, page, "text/html; charset=utf-8")
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
