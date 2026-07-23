"""
eprel.py — Récupération automatique des données produit depuis EPREL.

EPREL = registre officiel de l'Union européenne pour l'étiquetage énergétique.
À partir d'une simple référence (« modelIdentifier »), on retrouve :
  - la marque, la classe énergie, la catégorie ;
  - les caractéristiques (volume, dimensions, bruit, consommation…) ;
  - l'étiquette énergie officielle (image PNG) ;
  - la fiche d'information produit officielle (PDF) et le n° d'enregistrement.

Aucune clé API n'est nécessaire : on interroge le même point d'accès public que
le site officiel eprel.ec.europa.eu (en-tête Referer + User-Agent de navigateur).
Le tout est renvoyé au format attendu par le générateur de fiches Bouresmau,
directement consommable par sa fonction JavaScript normalizeAiProduct().
"""

import base64
import difflib
import json
import os
import re
from urllib.parse import quote, urlparse

import requests

BASE = "https://eprel.ec.europa.eu"
ICECAT_BASE = "https://icecat.biz"
ICECAT_LIVE_API = "https://live.icecat.biz/api"
ICECAT_DEFAULT_USERNAME = "openIcecat-live"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------- #
# Correspondance catégorie Bouresmau  <->  groupe de produits EPREL
# --------------------------------------------------------------------------- #

# Catégorie de l'appli  ->  groupe EPREL (le plus récent).
CATEGORY_TO_GROUP = {
    "refrigerateur": "refrigeratingappliances2019",
    "congelateur": "refrigeratingappliances2019",
    "lave-linge": "washingmachines2019",
    "seche-linge": "tumbledryers20232534",
    "lave-vaisselle": "dishwashers2019",
    "four": "ovens",
    "hotte": "rangehoods",
    "climatiseur": "airconditioners",
    "tv": "electronicdisplays",
}

# Groupe EPREL  ->  (catégorie appli, type d'étiquette, type éco, règlement).
GROUP_META = {
    "refrigeratingappliances2019": ("refrigerateur", "fridge", "fridge", "2019/2016"),
    "washingmachines2019": ("lave-linge", "washing", "washing", "2019/2014"),
    "washerdriers2019": ("lave-linge", "washing", "washing", "2019/2014"),
    "dishwashers2019": ("lave-vaisselle", "dishwasher", "dishwasher", "2019/2017"),
    "tumbledryers20232534": ("seche-linge", "generic", "dryer", "2023/2534"),
    "tumbledriers": ("seche-linge", "generic", "dryer", ""),
    "ovens": ("four", "generic", "oven", "65/2014"),
    "rangehoods": ("hotte", "generic", "hood", "65/2014"),
    "airconditioners": ("climatiseur", "generic", "aircon", "626/2011"),
    "electronicdisplays": ("tv", "screen", "tv", "2019/2013"),
    "televisions": ("tv", "screen", "tv", ""),
    "smartphonestablets20231669": ("custom", "generic", "none", "2023/1669"),
    # Familles supplémentaires (étiquette officielle en image, champs génériques).
    "lightsources": ("custom", "generic", "none", "2019/2015"),
    "tyres": ("custom", "generic", "none", "2020/740"),
    "spaceheaters": ("custom", "generic", "none", "811/2013"),
    "waterheaters": ("custom", "generic", "none", "812/2013"),
    "localspaceheaters": ("custom", "generic", "none", "2015/1186"),
}

# Étiquette « intitulé » lisible par famille, pour le titre de l'affiche.
GROUP_LABEL = {
    "lightsources": "LAMPE / SOURCE LUMINEUSE",
    "tyres": "PNEU",
    "spaceheaters": "CHAUFFAGE",
    "waterheaters": "CHAUFFE-EAU",
    "localspaceheaters": "POÊLE / CHAUFFAGE D'APPOINT",
}

# Ordre de recherche quand la catégorie n'est pas connue (une réf. seule).
SEARCH_GROUPS = [
    "refrigeratingappliances2019",
    "washingmachines2019",
    "dishwashers2019",
    "washerdriers2019",
    "tumbledryers20232534",
    "ovens",
    "rangehoods",
    "airconditioners",
    "electronicdisplays",
    "smartphonestablets20231669",
    "lightsources",
    "tyres",
    "spaceheaters",
    "waterheaters",
    "localspaceheaters",
]


def _norm(s):
    """Minuscule sans accents ni séparateurs : sert à comparer des références."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _pretty_energy_class(cls):
    """EPREL encode A+++/A++/A+ en « APPP »/« APP »/« AP » : on rétablit les +."""
    cls = (cls or "").strip().upper()
    m = re.match(r"^([A-G])(P+)$", cls)
    return m.group(1) + "+" * len(m.group(2)) if m else cls


def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    return s


def _api_headers(group):
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE}/screen/product/{group}",
    }


def _search_group(session, group, query, limit=25):
    url = (f"{BASE}/api/products/{group}?_page=1&_limit={limit}"
           f"&genericField=MODEL_IDENTIFIER&modelIdentifier={quote(query)}"
           f"&applianceType=ANY&sort0=onMarketStartDateTS&order0=DESC")
    try:
        r = session.get(url, headers=_api_headers(group), timeout=25)
        if r.status_code == 200:
            return r.json().get("hits", []) or []
    except Exception:
        pass
    return []


def _brand_search_group(session, group, brand, limit=8):
    """Recherche EPREL par MARQUE dans une famille (pour « frigo bosch »…)."""
    url = (f"{BASE}/api/products/{group}?_page=1&_limit={limit}"
           f"&genericField=SUPPLIER_OR_TRADEMARK&supplierOrTrademark={quote(brand)}"
           f"&applianceType=ANY&sort0=onMarketStartDateTS&order0=DESC")
    try:
        r = session.get(url, headers=_api_headers(group), timeout=25)
        if r.status_code == 200:
            return r.json().get("hits", []) or []
    except Exception:
        pass
    return []


# Mots (français/anglais) qui désignent une famille EPREL, pour la recherche
# par NOM du type « réfrigérateur bosch », « lave-linge samsung ».
_CATEGORY_WORDS = {
    "refrigerateur": "refrigeratingappliances2019", "frigo": "refrigeratingappliances2019",
    "congelateur": "refrigeratingappliances2019", "congelo": "refrigeratingappliances2019",
    "refrigerateurcongelateur": "refrigeratingappliances2019",
    "lavelinge": "washingmachines2019", "machinealaver": "washingmachines2019",
    "lavevaisselle": "dishwashers2019",
    "sechelinge": "tumbledryers20232534",
    "four": "ovens", "cuisiniere": "ovens",
    "hotte": "rangehoods", "hottearpirante": "rangehoods",
    "climatiseur": "airconditioners", "clim": "airconditioners", "climatisation": "airconditioners",
    "televiseur": "electronicdisplays", "television": "electronicdisplays",
    "tele": "electronicdisplays", "tv": "electronicdisplays", "ecran": "electronicdisplays",
    "moniteur": "electronicdisplays",
    "lampe": "lightsources", "ampoule": "lightsources", "spot": "lightsources",
    "pneu": "tyres",
    "chauffage": "spaceheaters", "radiateur": "spaceheaters",
    "chauffeeau": "waterheaters", "ballon": "waterheaters",
    "poele": "localspaceheaters", "insert": "localspaceheaters",
}

# Mots à ignorer pour deviner la marque (descriptifs, pas une marque).
_BRAND_STOP = {
    "combine", "combi", "porte", "portes", "encastrable", "pose", "libre", "table",
    "litres", "litre", "cm", "noir", "blanc", "gris", "inox", "led", "energie",
    "classe", "avec", "sans", "pouces", "watt", "watts", "silver", "smart", "grand",
    "petit", "double", "simple", "froid", "ventile", "statique", "no", "frost",
}
_CATEGORY_TOKENS = {t for key in _CATEGORY_WORDS
                    for t in re.findall(r"[a-z]+", key)} | {
    "refrigerateur", "frigo", "congelateur", "lave", "linge", "vaisselle",
    "seche", "machine", "laver", "four", "hotte", "climatiseur", "clim",
    "televiseur", "television", "tele", "tv", "ecran", "moniteur", "lampe",
    "ampoule", "spot", "pneu", "chauffage", "radiateur", "chauffe", "eau",
    "ballon", "poele", "insert", "cuisiniere", "aspirante"}


def _detect_category_brand(query):
    """(groupe EPREL | None, marque devinée) à partir d'une saisie en toutes lettres."""
    dq = _deburr(query)
    compact = re.sub(r"[^a-z0-9]", "", dq)
    group = None
    # On prend le mot-clé de catégorie le plus long présent dans la saisie.
    for key in sorted(_CATEGORY_WORDS, key=len, reverse=True):
        if key in compact:
            group = _CATEGORY_WORDS[key]
            break
    tokens = [t for t in re.split(r"[^a-z0-9]+", dq) if len(t) >= 2]
    brand_tokens = [t for t in tokens
                    if t not in _CATEGORY_TOKENS and t not in _BRAND_STOP
                    and not t.isdigit()]
    return group, " ".join(brand_tokens).strip()


def _score(query, hit):
    """
    Note de correspondance entre la référence saisie et un résultat EPREL.
    Les modelIdentifier EPREL contiennent souvent un code interne en plus
    (ex : « FRDN18ES3 923581310 ») : on compare donc à la chaîne complète ET
    à son premier mot, en privilégiant les préfixes communs.
    """
    q = _norm(query)
    if not q:
        return 0.0
    model = hit.get("modelIdentifier") or ""
    full = _norm(model)
    first = _norm(model.split()[0]) if model.split() else full
    if not full:
        return 0.0
    best = 0.0
    for cand in (full, first):
        if not cand:
            continue
        if cand == q:
            best = max(best, 1.0)
        elif cand.startswith(q) or q.startswith(cand):
            # préfixe : d'autant meilleur que la longueur commune couvre la réf.
            common = min(len(cand), len(q)) / max(len(cand), len(q))
            best = max(best, 0.80 + 0.20 * common)
        elif q in cand or cand in q:
            best = max(best, 0.70)
        best = max(best, difflib.SequenceMatcher(None, q, cand).ratio())
    return best


def find(reference, group=None, min_score=0.6):
    """
    Cherche la meilleure correspondance EPREL pour une référence.
    Renvoie (hit, group, score) ou (None, None, 0) si rien de convaincant.
    Si `group` est fourni (catégorie connue), on le teste en premier.
    """
    ref = (reference or "").strip()
    if not ref:
        return None, None, 0.0
    session = _session()
    groups = []
    if group and group in GROUP_META:
        groups.append(group)
    for g in SEARCH_GROUPS:
        if g not in groups:
            groups.append(g)

    # Requêtes successives : réf complète, puis préfixe alphanumérique.
    q_norm = _norm(ref)
    queries = [ref]
    prefix = re.match(r"[A-Za-z0-9]+", ref.replace(" ", ""))
    if prefix and prefix.group(0) != ref:
        queries.append(prefix.group(0))
    if len(q_norm) > 5:
        queries.append(ref[:5])

    best = (None, None, 0.0)
    for g in groups:
        seen_here = False
        for q in queries:
            hits = _search_group(session, g, q)
            if not hits:
                continue
            seen_here = True
            for h in hits:
                sc = _score(ref, h)
                if sc > best[2]:
                    best = (h, g, sc)
            if best[2] >= 0.999:
                break
        # Correspondance parfaite trouvée : inutile de continuer.
        if best[2] >= 0.999:
            break
        # Si on a une très bonne correspondance dans le groupe attendu, on garde.
        if seen_here and best[2] >= 0.9 and g == (group or groups[0]):
            break

    if best[0] is None or best[2] < min_score:
        return None, None, best[2]
    return best


def _fetch_bytes(session, url, referer):
    """Télécharge un fichier ; renvoie (contenu, type MIME) ou (None, None)."""
    try:
        r = session.get(url, headers={"Accept": "*/*", "Referer": referer},
                        timeout=30, allow_redirects=True)
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type", "application/octet-stream").split(";")[0]
            return r.content, ct
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------- #
# Logo de la marque (best-effort, en ligne)
# --------------------------------------------------------------------------- #
# EPREL n'héberge AUCUN logo : il ne donne que le nom de marque et le site du
# fabricant. On récupère donc un logo « au mieux » depuis le favicon haute
# définition du domaine de la marque (ou, à défaut, du site fabricant fourni
# par EPREL). C'est intégré en base64 côté serveur : la page l'affiche sans
# souci de CORS (un logo distant salirait le <canvas> et bloquerait l'export).
# Ce logo n'est qu'un FILET DE SÉCURITÉ : la page privilégie toujours sa propre
# bibliothèque locale (logos vectoriels propres) quand la marque y figure.

def _domain_of(url):
    """Extrait « exemple.com » d'une URL quelconque (sans le www.)."""
    try:
        net = urlparse(url if "//" in str(url) else "//" + str(url)).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _logo_candidates(brand, manufacturer_url, org_site):
    """Domaines à tester, du plus probable (la marque) au plus lointain."""
    slug = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
    domains = []
    if slug:
        for tld in ("com", "fr", "eu"):
            domains.append(f"{slug}.{tld}")
    for url in (manufacturer_url, org_site):
        d = _domain_of(url)
        if d and d not in domains:
            domains.append(d)
    return domains


def _fetch_brand_logo(session, brand, manufacturer_url="", org_site=""):
    """
    Renvoie une image de logo en data-URI (« data:image/…;base64,… ») ou None.
    On n'accepte qu'une réponse HTTP 200 : les fournisseurs de favicon renvoient
    un 404 (avec une image « globe » par défaut) quand ils n'ont rien — filtrer
    sur le code 200 élimine ces faux positifs.
    """
    for domain in _logo_candidates(brand, manufacturer_url, org_site):
        providers = (
            f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
            f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        )
        for url in providers:
            try:
                r = session.get(url, headers={"Accept": "image/*"},
                                timeout=6, allow_redirects=True)
            except Exception:
                continue
            ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if (r.status_code == 200 and r.content
                    and ct.startswith("image") and len(r.content) >= 200):
                return "data:%s;base64,%s" % (
                    ct, base64.b64encode(r.content).decode("ascii"))
    return None


# --------------------------------------------------------------------------- #
# Icecat (catalogue large : electromenager + petit electro, best-effort)
# --------------------------------------------------------------------------- #
# EPREL reste la source officielle pour l'etiquette energie UE. Icecat sert a
# trouver beaucoup plus de references, surtout les familles hors EPREL
# (micro-ondes, aspirateurs, machines a cafe, robots, petit electromenager...).
# Les identifiants peuvent etre fournis par variables d'environnement :
#   ICECAT_USERNAME, ICECAT_APP_KEY, ICECAT_API_TOKEN, ICECAT_CONTENT_TOKEN.

def _icecat_config():
    return {
        "username": (os.environ.get("ICECAT_USERNAME")
                     or os.environ.get("ICECAT_USER")
                     or ICECAT_DEFAULT_USERNAME),
        "app_key": os.environ.get("ICECAT_APP_KEY") or os.environ.get("ICECAT_API_KEY") or "",
        "api_token": os.environ.get("ICECAT_API_TOKEN") or "",
        "content_token": os.environ.get("ICECAT_CONTENT_TOKEN") or "",
        "lang": os.environ.get("ICECAT_LANGUAGE") or "fr",
    }


def _icecat_headers(accept="application/json, text/plain, */*"):
    cfg = _icecat_config()
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": f"{ICECAT_BASE}/",
    }
    if cfg["api_token"]:
        headers["api-token"] = cfg["api_token"]
    if cfg["content_token"]:
        headers["content-token"] = cfg["content_token"]
    return headers


def _abs_icecat_url(url):
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return ICECAT_BASE + url
    return url


def _icecat_brand_from_hit(hit):
    title = (hit.get("title") or "").strip()
    first = title.split()[0] if title else ""
    url = hit.get("url") or ""
    slug = ""
    m = re.search(r"/p/([^/]+)/", url)
    if m:
        slug = m.group(1).replace("+", " ").replace("-", " ").strip()
    if first and (not slug or _norm(first) == _norm(slug)):
        return first
    return first or slug.title()


def _icecat_local_category(category_name="", title=""):
    text = (str(category_name or "") + " " + str(title or "")).lower()
    rules = [
        ("lave-vaisselle", ("dishwasher", "dish washer", "lave-vaisselle")),
        ("lave-linge", ("washing machine", "washer dryer", "washer-dryer", "lave-linge")),
        ("seche-linge", ("tumble dryer", "tumbledryer", "seche-linge", "dryer")),
        ("micro-ondes", ("microwave", "micro-ondes")),
        ("aspirateur", ("vacuum", "aspirateur")),
        ("climatiseur", ("air conditioner", "air-conditioner", "climatiseur")),
        ("tv", ("television", "tv ", "display", "monitor")),
        ("plaque", ("hob", "cooktop", "plaque")),
        ("hotte", ("hood", "extractor", "range hood", "hotte")),
        ("four", ("oven", "four")),
        ("congelateur", ("freezer", "congelateur")),
        ("refrigerateur", ("fridge", "refrigerator", "refrigerateur", "cooler")),
        ("petit", (
            "coffee", "espresso", "kettle", "toaster", "blender", "mixer",
            "food processor", "fryer", "iron", "steam generator", "hair dryer",
            "electric toothbrush", "shaver", "grill", "raclette", "robot",
        )),
    ]
    for category, needles in rules:
        if any(n in text for n in needles):
            return category
    if "domestic appliance" in text or "home appliance" in text:
        return "petit"
    return "custom"


def _icecat_eco_type(category, category_name="", title=""):
    text = (str(category_name or "") + " " + str(title or "")).lower()
    if category == "refrigerateur":
        return "fridge"
    if category == "congelateur":
        return "freezer"
    if category == "lave-linge":
        return "washing"
    if category == "seche-linge":
        return "dryer"
    if category == "lave-vaisselle":
        return "dishwasher"
    if category == "four":
        return "oven"
    if category == "plaque":
        return "hob"
    if category == "micro-ondes":
        return "microwave"
    if category == "hotte":
        return "hood"
    if category == "aspirateur":
        if "robot" in text:
            return "vacuum-robot"
        if "stick" in text or "balai" in text:
            return "vacuum-stick"
        if "hand" in text or "main" in text:
            return "vacuum-hand"
        return "vacuum-canister"
    if category == "climatiseur":
        return "aircon"
    if category == "tv":
        return "tv"
    if category == "petit":
        if "coffee" in text or "espresso" in text or "cafe" in text:
            return "coffeeAuto"
        if "kettle" in text or "bouilloire" in text:
            return "kettle"
        if "iron" in text or "steam" in text or "repass" in text:
            return "iron"
        if "grill" in text or "fryer" in text or "toaster" in text:
            return "smallcook"
        return "foodprep"
    return "none"


def _icecat_title(category, title, category_name):
    if title:
        return str(title).strip().upper()
    labels = {
        "refrigerateur": "REFRIGERATEUR",
        "congelateur": "CONGELATEUR",
        "lave-linge": "LAVE-LINGE",
        "seche-linge": "SECHE-LINGE",
        "lave-vaisselle": "LAVE-VAISSELLE",
        "four": "FOUR",
        "plaque": "PLAQUE DE CUISSON",
        "micro-ondes": "MICRO-ONDES",
        "hotte": "HOTTE",
        "aspirateur": "ASPIRATEUR",
        "climatiseur": "CLIMATISEUR",
        "petit": "PETIT ELECTROMENAGER",
        "tv": "TELEVISEUR / ECRAN",
    }
    return labels.get(category) or str(category_name or "PRODUIT").upper()


def _icecat_score(query, hit):
    q = _norm(query)
    if not q:
        return 0.0
    candidates = [
        hit.get("mpn"),
        hit.get("name"),
        hit.get("reference"),
        hit.get("title"),
        hit.get("modelIdentifier"),
    ]
    best = 0.0
    for cand in candidates:
        c = _norm(cand)
        if not c:
            continue
        if c == q:
            best = max(best, 1.0)
        elif c.startswith(q) or q.startswith(c):
            best = max(best, 0.78 + 0.20 * min(len(c), len(q)) / max(len(c), len(q)))
        elif q in c or c in q:
            best = max(best, 0.72)
        best = max(best, difflib.SequenceMatcher(None, q, c).ratio())
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}", str(query or "")):
        t = _norm(token)
        if any(t and t == _norm(c) for c in candidates):
            best = max(best, 0.96)
    return best


def _icecat_candidate_queries(reference):
    ref = (reference or "").strip()
    out = []
    def add(q):
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    add(ref)
    compact = re.sub(r"\s+", "", ref)
    if compact != ref:
        add(compact)
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}", ref):
        if any(ch.isdigit() for ch in token) or len(token) >= 7:
            add(token)
    return out[:5]


def _icecat_search_once(session, query, limit=8):
    try:
        r = session.get(
            f"{ICECAT_BASE}/search/rest/sug-two",
            params={"query": query, "tmp": ""},
            headers=_icecat_headers(),
            timeout=12,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("prod") or []:
        product_id = item.get("product_id") or item.get("id")
        mpn = (item.get("mpn") or item.get("name") or "").strip()
        title = (item.get("title") or "").strip()
        if not product_id or not (mpn or title):
            continue
        url = _abs_icecat_url(item.get("url") or "")
        brand = _icecat_brand_from_hit({"url": url, "title": title})
        category = item.get("category_name") or item.get("categoryName") or ""
        out.append({
            "product_id": int(product_id),
            "mpn": mpn,
            "name": (item.get("name") or "").strip(),
            "title": title,
            "brand": brand,
            "categoryName": category,
            "url": url,
        })
        if len(out) >= limit:
            break
    return out


def _icecat_find(reference, limit=6, min_score=0.48):
    ref = (reference or "").strip()
    if not ref or len(_norm(ref)) < 3:
        return None, 0.0
    session = _session()
    found = []
    seen = set()
    for q in _icecat_candidate_queries(ref):
        for hit in _icecat_search_once(session, q, limit=limit):
            key = hit.get("product_id")
            if key in seen:
                continue
            seen.add(key)
            hit["score"] = _icecat_score(ref, hit)
            found.append(hit)
    if not found:
        return None, 0.0
    found.sort(key=lambda h: h.get("score", 0), reverse=True)
    best = found[0]
    score = best.get("score", 0.0)
    if score < min_score:
        return None, score
    return best, score


def _icecat_public_product_from_html(html_text):
    parts = []
    for m in re.finditer(r"self\.__next_f\.push\(\[1,\"(.*?)\"\]\)</script>",
                         html_text or "", re.S):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            continue
    stream = "".join(parts)
    for m in re.finditer(r"\"value\":(\{.*?\}),\"children\"", stream, re.S):
        blob = m.group(1)
        if '"brandName"' not in blob or '"mpn"' not in blob:
            continue
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if obj.get("id") and (obj.get("mpn") or obj.get("title")):
            return obj
    return None


def _icecat_fetch_public_product(session, hit):
    url = _abs_icecat_url((hit or {}).get("url") or "")
    if not url:
        return None
    try:
        r = session.get(url, headers=_icecat_headers("text/html,*/*"),
                        timeout=20, allow_redirects=True)
        if r.status_code == 200 and r.text:
            obj = _icecat_public_product_from_html(r.text)
            if obj:
                obj["_url"] = r.url
                return obj
    except Exception:
        pass
    return None


def _icecat_fetch_json_product(session, hit):
    cfg = _icecat_config()
    params = {
        "shopname": cfg["username"],
        "lang": cfg["lang"],
        "content": "",
    }
    if hit.get("product_id"):
        params["icecat_id"] = str(hit["product_id"])
    elif hit.get("gtin"):
        params["GTIN"] = hit["gtin"]
    else:
        if hit.get("brand"):
            params["Brand"] = hit["brand"]
        if hit.get("mpn"):
            params["ProductCode"] = hit["mpn"]
    if cfg["app_key"]:
        params["app_key"] = cfg["app_key"]
    try:
        r = session.get(ICECAT_LIVE_API, params=params,
                        headers=_icecat_headers(), timeout=25)
        if r.status_code == 200:
            data = r.json()
            if (data.get("msg") or "").upper() == "OK" and data.get("data"):
                return data, None
            return None, data.get("message") or data.get("Message") or data.get("msg")
        try:
            return None, r.json().get("Message")
        except Exception:
            return None, f"HTTP {r.status_code}"
    except Exception as exc:
        return None, str(exc)


def _icecat_text(value):
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(filter(None, (_icecat_text(v) for v in value)))
    if isinstance(value, dict):
        for key in ("Value", "LocalValue", "PresentationValue", "RawValue",
                    "LongDesc", "LongProductName", "ShortSummaryDescription",
                    "LongSummaryDescription", "_", "Name"):
            txt = _icecat_text(value.get(key))
            if txt:
                return txt
        return ""
    return re.sub(r"<[^>]+>", " ", str(value)).strip()


def _icecat_feature_specs(data, max_specs=28):
    specs, seen = [], set()

    def add(label, value):
        label = _icecat_text(label)
        value = _icecat_text(value)
        if not label or not value:
            return
        label = re.sub(r"\s+", " ", label).strip(" :")
        value = re.sub(r"\s+", " ", value).strip()
        key = (_norm(label), _norm(value))
        if key in seen:
            return
        seen.add(key)
        specs.append([label, value])

    gi = data.get("GeneralInfo") or {}
    category = (gi.get("Category") or {}).get("Name") if isinstance(gi.get("Category"), dict) else ""
    add("Catégorie Icecat", category)
    if gi.get("ProductFamily"):
        add("Gamme", gi.get("ProductFamily"))
    if gi.get("ProductSeries"):
        add("Série", gi.get("ProductSeries"))
    gtins = gi.get("GTIN") or gi.get("GTINs")
    if gtins:
        add("EAN / GTIN", gtins)
    desc = gi.get("Description") or {}
    if isinstance(desc, dict) and desc.get("WarrantyInfo"):
        add("Garantie fabricant", desc.get("WarrantyInfo"))

    for group in data.get("FeaturesGroups") or []:
        features = group.get("Features") or group.get("Feature") or []
        if isinstance(features, dict):
            features = [features]
        for item in features:
            feature = item.get("Feature") or {}
            name = ((feature.get("Name") or {}).get("Value")
                    if isinstance(feature.get("Name"), dict) else feature.get("Name"))
            value = (item.get("PresentationValue") or item.get("LocalValue")
                     or item.get("Value") or item.get("RawValue"))
            unit = ""
            measure = feature.get("Measure") or {}
            if isinstance(measure, dict):
                signs = measure.get("Signs") or {}
                unit = signs.get("_") if isinstance(signs, dict) else ""
                unit = unit or measure.get("Sign") or ""
            value_text = _icecat_text(value)
            if unit and value_text and unit not in value_text:
                value_text = f"{value_text} {unit}"
            add(name, value_text)
            if len(specs) >= max_specs:
                return specs
    return specs


def _icecat_bullets(data, max_n=6):
    """Points forts marketing pour l'affiche « Seconde Vie ».

    Puise d'abord dans le résumé Icecat (SummaryDescription), puis dans les
    fonctionnalités booléennes « Oui » des FeaturesGroups. Renvoie une liste de
    courtes puces prêtes à afficher.
    """
    bullets, seen = [], set()

    def add(text):
        text = re.sub(r"<[^>]+>", " ", str(text or ""))
        text = re.sub(r"\s+", " ", text).strip(" -•·–\t")
        if len(text) < 4 or len(text) > 90:
            return
        key = _norm(text)
        if not key or key in seen:
            return
        seen.add(key)
        bullets.append(text)

    gi = data.get("GeneralInfo") or {}
    summary = gi.get("SummaryDescription") or {}
    if isinstance(summary, dict):
        for field in ("LongSummaryDescription", "ShortSummaryDescription"):
            raw = _icecat_text(summary.get(field))
            if raw:
                for part in re.split(r"(?:•|•|▪|–\s|∙|\n|\r|;|·|\.\s)", raw):
                    add(part)
                if bullets:
                    break

    if len(bullets) < max_n:
        for group in data.get("FeaturesGroups") or []:
            features = group.get("Features") or group.get("Feature") or []
            if isinstance(features, dict):
                features = [features]
            for item in features:
                feature = item.get("Feature") or {}
                name = ((feature.get("Name") or {}).get("Value")
                        if isinstance(feature.get("Name"), dict) else feature.get("Name"))
                value = _norm(_icecat_text(item.get("PresentationValue")
                                           or item.get("LocalValue")
                                           or item.get("Value") or item.get("RawValue")))
                if value in ("oui", "yes", "y", "true", "1"):
                    add(_icecat_text(name))
                if len(bullets) >= max_n:
                    break
            if len(bullets) >= max_n:
                break
    return bullets[:max_n]


# --------------------------------------------------------------------------- #
# Indice de réparabilité (auto) et pertinence des suggestions Icecat
# --------------------------------------------------------------------------- #

# Un libellé de caractéristique qui désigne l'indice de réparabilité / durabilité.
_REPAIR_LABEL_RE = re.compile(
    r"r[ée]parabilit|repairabilit|indice de r[ée]par|reparation index|"
    r"durabilit[ée].*indice|indice.*durabilit", re.I)


def _deburr(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _parse_repair_value(text):
    """Extrait une note de réparabilité sur 10 depuis un texte (« 7,5 / 10 »)."""
    t = str(text or "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    v = float(m.group(1))
    if v < 0 or v > 10:          # l'indice français est sur 10
        return None
    return round(v, 1)


def _repair_from_specs(specs):
    """Cherche l'indice de réparabilité dans une liste [label, valeur]."""
    for row in specs or []:
        try:
            label, value = row[0], row[1]
        except (IndexError, TypeError):
            continue
        if _REPAIR_LABEL_RE.search(str(label)):
            v = _parse_repair_value(value)
            if v is not None:
                return v
    return None


def _icecat_repair_from_data(data):
    """Parcourt toutes les caractéristiques Icecat pour l'indice de réparabilité."""
    for group in data.get("FeaturesGroups") or []:
        features = group.get("Features") or group.get("Feature") or []
        if isinstance(features, dict):
            features = [features]
        for item in features:
            feature = item.get("Feature") or {}
            name = ((feature.get("Name") or {}).get("Value")
                    if isinstance(feature.get("Name"), dict) else feature.get("Name"))
            if _REPAIR_LABEL_RE.search(_icecat_text(name)):
                val = _icecat_text(item.get("PresentationValue") or item.get("LocalValue")
                                   or item.get("Value") or item.get("RawValue"))
                v = _parse_repair_value(val)
                if v is not None:
                    return v
    return None


# Catégories Icecat sans intérêt pour une affiche produit (accessoires, pièces…).
_ICECAT_JUNK_RE = re.compile(
    r"accessor|\bparts\b|protector|holder|\btoy|role play|parfum|perfum|"
    r"\bcase\b|\bcover\b|\bbag\b|supplies|spare|\bmount\b|\bstand\b|cable|"
    r"adapter|adaptor|sticker|\bskin\b|side & end|pan & pot|bracket|strap|filter",
    re.I)


def _icecat_is_relevant(category_name):
    return not _ICECAT_JUNK_RE.search(str(category_name or ""))


def _icecat_rank(query, hits):
    """Classe les suggestions : marque tapée en tête, accessoires en dernier."""
    toks = [t for t in re.split(r"[^a-z0-9]+", _deburr(query)) if len(t) >= 2]

    def score(h):
        title = _deburr(h.get("title"))
        brand = _deburr(h.get("brand"))
        s = 0.0
        for t in toks:
            if t in brand:
                s += 2.0        # la marque tapée compte double
            if t in title:
                s += 1.0
        if not _icecat_is_relevant(h.get("categoryName")):
            s -= 5.0            # accessoire / pièce : tout en bas
        return s

    return sorted(hits, key=score, reverse=True)


def _icecat_docs_from_api(data):
    docs = []
    gi = data.get("GeneralInfo") or {}
    desc = gi.get("Description") or {}
    if isinstance(desc, dict):
        if desc.get("ManualPDFURL"):
            docs.append(("manualPdf", "manuel-icecat.pdf", desc.get("ManualPDFURL")))
        if desc.get("LeafletPDFURL"):
            docs.append(("technicalSheetPdf", "fiche-technique-icecat.pdf", desc.get("LeafletPDFURL")))
    for section in ("Multimedia", "Gallery"):
        values = data.get(section) or []
        if isinstance(values, dict):
            values = [values]
        for item in values:
            url = item.get("URL") or item.get("Url") or item.get("Pic") or item.get("HighPic")
            ctype = (item.get("ContentType") or item.get("contentType") or "").lower()
            descr = (item.get("Description") or item.get("Type") or "").lower()
            if not url:
                continue
            if "pdf" in ctype or str(url).lower().endswith(".pdf"):
                role = "manualPdf" if "manual" in descr else "technicalSheetPdf"
                docs.append((role, f"{role}-icecat.pdf", url))
            elif "image" in ctype or re.search(r"\.(png|jpe?g|webp)(?:\?|$)", str(url), re.I):
                docs.append(("productImage", "image-produit-icecat.png", url))
    return docs


def _icecat_docs_from_public(obj):
    docs = []
    for item in obj.get("manuals") or []:
        url = item.get("url")
        if not url or item.get("isPrivate"):
            continue
        kind = ((item.get("type") or "") + " " + (item.get("description") or "")).lower()
        if "energy" in kind and "label" in kind:
            docs.append(("energyLabelPdf", "etiquette-energie-icecat.pdf", url))
        elif "fiche" in kind or "data-sheet" in kind or "datasheet" in kind:
            docs.append(("productSheetPdf", "fiche-information-icecat.pdf", url))
        elif "manual" in kind or "notice" in kind:
            docs.append(("manualPdf", "manuel-icecat.pdf", url))
        else:
            docs.append(("technicalSheetPdf", "document-icecat.pdf", url))
    for item in obj.get("gallery") or []:
        url = item.get("url") or item.get("pic") or item.get("thumb")
        if url:
            docs.append(("productImage", "image-produit-icecat.png", url))
    return docs


def _icecat_fetch_data_uri(session, url, referer=""):
    data, ct = _fetch_bytes(session, url, referer or ICECAT_BASE)
    if not data:
        return None
    if not (ct or "").startswith("image"):
        return None
    return "data:%s;base64,%s" % (ct, base64.b64encode(data).decode("ascii"))


def _icecat_embed_docs(session, docs, referer):
    embedded = {}
    for role, name, url in docs:
        if role in embedded:
            continue
        data, ct = _fetch_bytes(session, _abs_icecat_url(url), referer or ICECAT_BASE)
        if not data:
            continue
        if role.endswith("Pdf") and "pdf" not in (ct or "").lower():
            continue
        if role == "productImage" and not (ct or "").startswith("image"):
            continue
        embedded[role] = {
            "name": name,
            "mimeType": ct or ("application/pdf" if role.endswith("Pdf") else "image/png"),
            "data": base64.b64encode(data).decode("ascii"),
        }
    return embedded


def _icecat_base_product(reference, brand, title, mpn, category_name, source_url):
    category = _icecat_local_category(category_name, title)
    eco_type = _icecat_eco_type(category, category_name, title)
    product = {
        "category": category,
        "productTitle": _icecat_title(category, title, category_name),
        "reference": mpn or reference,
        "brandName": brand or "",
        "energy": "Non affichée",
        "specColumns": "2",
        "specs": [],
        "ecoType": eco_type,
        "ecoEnabled": eco_type != "none",
        "ecoAuto": True,
        "energyLabelEnabled": False,
        "energyLabelMode": "editable",
        "energyLabelType": {
            "refrigerateur": "fridge",
            "congelateur": "fridge",
            "lave-linge": "washing",
            "lave-vaisselle": "dishwasher",
            "tv": "screen",
        }.get(category, "generic"),
        "icecatUrl": source_url or "",
    }
    return product


def _icecat_public_to_product(obj, reference, session=None, with_files=True):
    session = session or _session()
    source_url = obj.get("_url") or _abs_icecat_url(obj.get("url") or obj.get("link") or "")
    brand = obj.get("brandName") or obj.get("brand") or ""
    mpn = obj.get("mpn") or obj.get("name") or reference
    title = obj.get("title") or ""
    category_name = obj.get("categoryName") or ""
    product = _icecat_base_product(reference, brand, title, mpn, category_name, source_url)
    specs = []
    if category_name:
        specs.append(["Catégorie Icecat", category_name])
    if obj.get("familyName"):
        specs.append(["Gamme", obj.get("familyName")])
    if obj.get("gtins"):
        specs.append(["EAN / GTIN", ", ".join(map(str, obj.get("gtins") or []))])
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*L\b", title or "", re.I)
    if m:
        specs.append(["Capacité / volume", m.group(1).replace(".", ",") + " L"])
    colors = {
        "stainless steel": "Inox",
        "silver": "Argent",
        "white": "Blanc",
        "black": "Noir",
        "grey": "Gris",
        "gray": "Gris",
        "red": "Rouge",
    }
    low_title = (title or "").lower()
    for needle, value in colors.items():
        if needle in low_title:
            specs.append(["Couleur / finition", value])
            break
    product["specs"] = specs or [["Source", "Icecat"]]
    repair = _repair_from_specs(product["specs"])
    if repair is not None:
        product["repairEnabled"] = True
        product["repairScore"] = str(repair)
    if obj.get("brandLogo"):
        logo = _icecat_fetch_data_uri(session, obj.get("brandLogo"), source_url)
        if logo:
            product["brandLogoAuto"] = logo
    if obj.get("manuals"):
        for role, _name, url in _icecat_docs_from_public(obj):
            if role == "energyLabelPdf":
                product["officialEnergyLabelUrl"] = _abs_icecat_url(url)
            elif role == "productSheetPdf":
                product["officialProductSheetUrl"] = _abs_icecat_url(url)
            elif role in ("technicalSheetPdf", "manualPdf"):
                product.setdefault("officialTechnicalSheetUrl", _abs_icecat_url(url))
    embedded = _icecat_embed_docs(session, _icecat_docs_from_public(obj), source_url) if with_files else {}
    meta = {
        "source": "icecat",
        "sourceLabel": "Icecat",
        "icecatId": obj.get("id"),
        "icecatUrl": source_url,
        "icecatAccess": "public-summary" if not obj.get("isFullAccess") else "public",
        "matchedBrand": brand,
        "modelIdentifier": mpn,
    }
    if not obj.get("isFullAccess"):
        meta["warning"] = ("Fiche Icecat trouvee, mais les specifications detaillees "
                           "peuvent necessiter un compte Full Icecat/app_key.")
    return product, meta, embedded


def _icecat_api_to_product(payload, reference, hit, session=None, with_files=True):
    session = session or _session()
    data = payload.get("data") or {}
    gi = data.get("GeneralInfo") or {}
    brand = gi.get("Brand") or hit.get("brand") or ""
    mpn = gi.get("BrandPartCode") or hit.get("mpn") or reference
    title = gi.get("Title") or hit.get("title") or ""
    cat = gi.get("Category") or {}
    category_name = _icecat_text(cat.get("Name") if isinstance(cat, dict) else cat)
    source_url = _abs_icecat_url(hit.get("url") or "")
    product = _icecat_base_product(reference, brand, title, mpn, category_name, source_url)
    product["specs"] = _icecat_feature_specs(data) or product["specs"]
    bullets = _icecat_bullets(data)
    if bullets:
        product["bullets"] = bullets

    # Indice de réparabilité rempli tout seul s'il figure dans les données.
    repair = _icecat_repair_from_data(data)
    if repair is None:
        repair = _repair_from_specs(product["specs"])
    if repair is not None:
        product["repairEnabled"] = True
        product["repairScore"] = str(repair)

    brand_logo = gi.get("BrandLogo") or ((gi.get("BrandInfo") or {}).get("BrandLogo")
                                         if isinstance(gi.get("BrandInfo"), dict) else "")
    if brand_logo:
        logo = _icecat_fetch_data_uri(session, brand_logo, source_url)
        if logo:
            product["brandLogoAuto"] = logo

    image = data.get("Image") or {}
    image_url = ""
    if isinstance(image, dict):
        image_url = image.get("HighPic") or image.get("Pic") or image.get("ThumbPic") or ""
    if image_url and with_files:
        embedded_img = _icecat_embed_docs(session, [("productImage", "image-produit-icecat.png", image_url)], source_url)
    else:
        embedded_img = {}

    desc = gi.get("Description") or {}
    if isinstance(desc, dict):
        product["manufacturerPageUrl"] = desc.get("URL") or ""
        if desc.get("LeafletPDFURL"):
            product["officialTechnicalSheetUrl"] = desc.get("LeafletPDFURL")
    embedded = embedded_img
    if with_files:
        embedded.update(_icecat_embed_docs(session, _icecat_docs_from_api(data), source_url))

    meta = {
        "source": "icecat",
        "sourceLabel": "Icecat",
        "icecatId": gi.get("IcecatId") or hit.get("product_id"),
        "icecatUrl": source_url,
        "icecatAccess": "json-api",
        "matchedBrand": brand,
        "modelIdentifier": mpn,
    }
    return product, meta, embedded


def _merge_icecat_product(product, meta, embedded, icecat_result):
    if not icecat_result or not icecat_result.get("found"):
        return product, meta, embedded
    ip = icecat_result.get("product") or {}
    imeta = icecat_result.get("meta") or {}
    product.setdefault("icecatUrl", ip.get("icecatUrl") or imeta.get("icecatUrl") or "")
    for key in ("brandLogoAuto", "officialTechnicalSheetUrl", "manufacturerPageUrl"):
        if not product.get(key) and ip.get(key):
            product[key] = ip[key]
    if ip.get("bullets") and not product.get("bullets"):
        product["bullets"] = ip["bullets"]
    existing = {(_norm(a), _norm(b)) for a, b in product.get("specs", [])}
    for spec in ip.get("specs") or []:
        if isinstance(spec, dict):
            row = [spec.get("label"), spec.get("value")]
        else:
            row = list(spec[:2]) if isinstance(spec, (list, tuple)) else []
        if len(row) < 2 or not row[0] or not row[1]:
            continue
        key = (_norm(row[0]), _norm(row[1]))
        if key not in existing:
            product.setdefault("specs", []).append(row)
            existing.add(key)
    for role, record in (icecat_result.get("embeddedFiles") or {}).items():
        embedded.setdefault(role, record)
    meta["icecat"] = imeta
    meta["sourceLabel"] = "EPREL + Icecat"
    return product, meta, embedded


def icecat_lookup(reference, group=None, with_files=True):
    session = _session()
    hit = None
    score = 0.0
    if group and str(group).startswith("icecat:"):
        try:
            product_id = int(str(group).split(":", 1)[1])
            hit = {"product_id": product_id, "mpn": reference, "reference": reference}
            score = 1.0
            full_hit, _ = _icecat_find(reference)
            if full_hit and full_hit.get("product_id") == product_id:
                hit.update(full_hit)
        except (TypeError, ValueError):
            hit = None
    if hit is None:
        hit, score = _icecat_find(reference)
    if hit is None:
        return {
            "found": False,
            "reference": reference,
            "score": round(score, 3),
            "source": "icecat",
            "sourceLabel": "Icecat",
            "message": "Aucune correspondance Icecat fiable pour cette reference.",
        }

    api_payload, api_error = _icecat_fetch_json_product(session, hit)
    public_obj = _icecat_fetch_public_product(session, hit)
    if api_payload:
        product, meta, embedded = _icecat_api_to_product(
            api_payload, reference, hit, session=session, with_files=with_files)
        if public_obj:
            public_product, public_meta, public_embedded = _icecat_public_to_product(
                public_obj, reference, session=session, with_files=with_files)
            product.setdefault("icecatUrl", public_product.get("icecatUrl") or "")
            for key in ("officialEnergyLabelUrl", "officialProductSheetUrl",
                        "officialTechnicalSheetUrl", "brandLogoAuto"):
                if not product.get(key) and public_product.get(key):
                    product[key] = public_product[key]
            for role, rec in public_embedded.items():
                embedded.setdefault(role, rec)
            meta["public"] = public_meta
    elif public_obj:
        product, meta, embedded = _icecat_public_to_product(
            public_obj, reference, session=session, with_files=with_files)
        if api_error:
            meta["apiMessage"] = api_error
    else:
        product = _icecat_base_product(
            reference, hit.get("brand"), hit.get("title"),
            hit.get("mpn"), hit.get("categoryName"), hit.get("url"))
        product["specs"] = [["Catégorie Icecat", hit.get("categoryName") or ""]]
        meta = {
            "source": "icecat",
            "sourceLabel": "Icecat",
            "icecatId": hit.get("product_id"),
            "icecatUrl": hit.get("url") or "",
            "icecatAccess": "search-summary",
            "apiMessage": api_error or "",
        }
        embedded = {}

    return {
        "found": True,
        "reference": reference,
        "score": round(score, 3),
        "source": "icecat",
        "sourceLabel": meta.get("sourceLabel") or "Icecat",
        "product": product,
        "meta": meta,
        "embeddedFiles": embedded,
    }


def _cm(value, in_mm):
    """Convertit une dimension EPREL en cm lisibles. Selon le groupe, la valeur
    est en mm (frigo : 1772) ou déjà en cm (lave-linge : 85)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    cm = v / 10.0 if in_mm else v
    txt = f"{cm:.1f}".rstrip("0").rstrip(".")
    return txt.replace(".", ",")


def _dims(hit):
    raw = [hit.get("dimensionHeight"), hit.get("dimensionWidth"),
           hit.get("dimensionDepth")]
    nums = []
    for v in raw:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            nums.append(None)
    if any(n is None for n in nums):
        return ""
    # Détection d'unité : au-delà de 300, la valeur est en millimètres.
    in_mm = max(nums) > 300
    h, w, d = (_cm(n, in_mm) for n in nums)
    return f"{h} × {w} × {d} cm"


def _specs_for(group, hit):
    """Caractéristiques pertinentes selon le groupe de produits."""
    def g(*keys):
        for k in keys:
            v = hit.get(k)
            if v not in (None, "", []):
                return v
        return None

    rows = []
    noise = g("noise")
    noise_class = g("noiseClass")

    if group == "refrigeratingappliances2019":
        energy = g("energyConsAnnualV2", "energyConsAnnual", "consolidatedEnergyConsAnnual")
        rows = [
            ["Volume total", f"{g('totalVolume')} litres" if g("totalVolume") else ""],
            ["Volume réfrigérateur", f"{g('capRefrNet')} L" if g("capRefrNet") else ""],
            ["Volume congélateur", f"{g('capFreezeNet')} L" if g("capFreezeNet") else ""],
            ["Consommation", f"{energy} kWh/an" if energy else ""],
            ["Indice d'efficacité (IEE)", str(g("energyEfficiencyIndex")) if g("energyEfficiencyIndex") else ""],
            ["Niveau sonore", f"{noise} dB" if noise else ""],
            ["Classe d'émission sonore", noise_class or ""],
            ["Pose", "Encastrable" if hit.get("designType") == "BUILT_IN" else "Pose libre"],
            ["Dimensions (H × L × P)", _dims(hit)],
        ]
    elif group in ("washingmachines2019", "washerdriers2019"):
        energy = g("energyConsPer100Cycle", "energyCons100")
        spin = g("spinSpeedRated", "spinSpeed", "maxSpinSpeed")
        dur = g("programmeDurationRated", "programmeDuration")
        rows = [
            ["Capacité", f"{g('ratedCapacity','capacity')} kg" if g("ratedCapacity", "capacity") else ""],
            ["Essorage", f"{spin} tr/min" if spin else ""],
            ["Classe d'essorage", g("spinClass") or ""],
            ["Consommation", f"{energy} kWh/100 cycles" if energy else ""],
            ["Consommation d'eau", f"{g('waterCons','waterConsumption')} L/cycle" if g("waterCons", "waterConsumption") else ""],
            ["Durée du programme éco", f"{dur} min" if dur else ""],
            ["Niveau sonore (essorage)", f"{noise} dB" if noise else ""],
            ["Classe d'émission sonore", noise_class or ""],
            ["Dimensions (H × L × P)", _dims(hit)],
        ]
    elif group == "dishwashers2019":
        energy = g("energyCons100", "energyConsPer100Cycle")
        dur = g("programmeDuration", "programmeDurationRated")
        rows = [
            ["Capacité", f"{g('ratedCapacity','placeSettings')} couverts" if g("ratedCapacity", "placeSettings") else ""],
            ["Consommation", f"{energy} kWh/100 cycles" if energy else ""],
            ["Consommation d'eau", f"{g('waterCons','waterConsumption')} L/cycle" if g("waterCons", "waterConsumption") else ""],
            ["Durée du programme éco", f"{dur} min" if dur else ""],
            ["Niveau sonore", f"{noise} dB" if noise else ""],
            ["Classe d'émission sonore", noise_class or ""],
            ["Dimensions (H × L × P)", _dims(hit)],
        ]
    elif group == "electronicdisplays":
        diag = g("diagonalCm", "screenDiagonalCm")
        rows = [
            ["Diagonale", f"{diag} cm" if diag else ""],
            ["Résolution", f"{g('horizontalPixels')} × {g('verticalPixels')} px" if g("horizontalPixels") else ""],
            ["Consommation (SDR)", f"{g('energyConsSDR')} kWh/1000 h" if g("energyConsSDR") else ""],
            ["Consommation (HDR)", f"{g('energyConsHDR')} kWh/1000 h" if g("energyConsHDR") else ""],
            ["Dimensions (H × L × P)", _dims(hit)],
        ]
    elif group == "lightsources":
        watt = g("powerOnMode", "energyConsOnMode")
        lumen = g("luminousFlux")
        cri = g("colourRenderingIndex")
        kmin, kmax = g("correlatedColourTempMin"), g("correlatedColourTempMax")
        temp = (f"{kmin} K" if kmin == kmax or not kmax
                else f"{kmin}–{kmax} K") if kmin else None
        rows = [
            ["Technologie", g("lightingTechnology") or ""],
            ["Flux lumineux", f"{lumen} lm" if lumen else ""],
            ["Puissance", f"{watt} W" if watt else ""],
            ["Consommation", f"{g('energyConsOnMode')} kWh/1000 h" if g("energyConsOnMode") else ""],
            ["Température de couleur", temp or ""],
            ["Rendu des couleurs (IRC)", str(cri) if cri else ""],
            ["Variable (dimmable)", {"YES": "Oui", "NO": "Non"}.get(str(g("dimmable")), "")],
        ]
    elif group == "tyres":
        rows = [
            ["Efficacité en carburant", g("energyClass") or ""],
            ["Adhérence sur sol mouillé", g("wetGripClass") or ""],
            ["Bruit de roulement", f"{g('externalRollingNoiseValue')} dB ({g('externalRollingNoiseClass')})"
                if g("externalRollingNoiseValue") else (g("externalRollingNoiseClass") or "")],
            ["Indice de charge", str(g("loadCapacityIndex")) if g("loadCapacityIndex") else ""],
            ["Pneu neige", "Oui" if g("snowTyre") else ""],
            ["Pneu glace", "Oui" if hit.get("iceTyre") else ""],
        ]
    elif group in ("spaceheaters", "localspaceheaters"):
        energy = g("energyAnnualKwh", "energyAnnualColdKwh")
        rows = [
            ["Puissance thermique", f"{g('directHeatOutput')} kW" if g("directHeatOutput") else ""],
            ["Efficacité énergétique saisonnière", f"{g('energyEfficiencyIndex')} %" if g("energyEfficiencyIndex") else ""],
            ["Consommation annuelle", f"{energy} kWh/an" if energy else ""],
            ["Niveau sonore", f"{noise} dB" if noise else ""],
        ]
    elif group == "waterheaters":
        rows = [
            ["Profil de soutirage", g("declaredLoadProfileType") or ""],
            ["Efficacité (chauffage eau)", f"{g('declaredLoadProfileWaterHeatingEfficiency')} %"
                if g("declaredLoadProfileWaterHeatingEfficiency") else ""],
            ["Consommation annuelle", f"{g('declaredLoadProfileWaterHeatingAnnualElectricityCons')} kWh/an"
                if g("declaredLoadProfileWaterHeatingAnnualElectricityCons") else ""],
            ["Type", g("complexType") or ""],
            ["Niveau sonore", f"{noise} dB" if noise else ""],
        ]
    else:
        energy = g("energyConsAnnualV2", "energyConsAnnual")
        rows = [
            ["Consommation", f"{energy} kWh/an" if energy else ""],
            ["Niveau sonore", f"{noise} dB" if noise else ""],
            ["Dimensions (H × L × P)", _dims(hit)],
        ]
    return [[a, str(b).strip()] for a, b in rows if b and str(b).strip()]


def _title_for(category, hit):
    labels = {
        "refrigerateur": "RÉFRIGÉRATEUR",
        "congelateur": "CONGÉLATEUR",
        "lave-linge": "LAVE-LINGE",
        "seche-linge": "SÈCHE-LINGE",
        "lave-vaisselle": "LAVE-VAISSELLE",
        "four": "FOUR",
        "hotte": "HOTTE",
        "climatiseur": "CLIMATISEUR",
        "tv": "TÉLÉVISEUR",
    }
    base = labels.get(category, "PRODUIT")
    vol = hit.get("totalVolume")
    if category == "refrigerateur" and vol:
        return f"{base} {vol} L"
    diag = hit.get("diagonalCm") or hit.get("screenDiagonalCm")
    if category == "tv" and diag:
        try:
            return f"{base} {round(float(diag) / 2.54)} POUCES"
        except (TypeError, ValueError):
            pass
    return base


def to_product(hit, group, reference, session=None, with_files=True):
    """
    Transforme un résultat EPREL en objet produit prêt pour le générateur.
    Les clés correspondent à ce que comprend normalizeAiProduct() côté page.
    Renvoie (product, meta, embedded_files).
    """
    session = session or _session()
    category, label_type, eco_type, regulation = GROUP_META.get(
        group, ("custom", "generic", "none", ""))
    reg = str(hit.get("eprelRegistrationNumber") or "").strip()
    model = (hit.get("modelIdentifier") or reference or "").split()[0]
    brand = (hit.get("supplierOrTrademark") or hit.get("trademarkOwner")
             or (hit.get("organisation") or {}).get("organisationName") or "")
    energy = _pretty_energy_class(hit.get("energyClass"))
    referer = f"{BASE}/screen/product/{group}/{reg}"

    product = {
        "category": category,
        "productTitle": GROUP_LABEL.get(group) or _title_for(category, hit),
        "reference": reference or model,
        "brandName": brand,
        "energy": energy or "Non affichée",
        "specColumns": "2",
        "specs": _specs_for(group, hit),
        "ecoType": eco_type,
        "ecoEnabled": eco_type != "none",
        "ecoAuto": True,
        "energyLabelEnabled": True,
        "energyLabelType": label_type,
        "labelClass": energy or "A",
        "labelBrand": brand,
        "labelModel": model,
        "labelRegulation": regulation,
        "eprelUrl": f"{BASE}/screen/product/{group}/{reg}" if reg else "",
        "manufacturerPageUrl": hit.get("webLinkManufacturer") or "",
        "officialEnergyLabelUrl": f"{BASE}/labels/{group}/Label_{reg}.pdf" if reg else "",
        "officialProductSheetUrl": f"{BASE}/fiches/{group}/Fiche_{reg}_FR.pdf" if reg else "",
    }

    # Consommation / capacité / eau / durée pour l'étiquette énergie éditable,
    # avec les unités propres à chaque groupe de produits.
    if group == "refrigeratingappliances2019":
        ev = (hit.get("energyConsAnnualV2") or hit.get("energyConsAnnual")
              or hit.get("consolidatedEnergyConsAnnual"))
        if ev:
            product["labelEnergy"], product["labelEnergyUnit"] = ev, "kWh/an"
        if hit.get("totalVolume"):
            product["labelCapacity"] = f"{hit['totalVolume']} L"
    elif group in ("washingmachines2019", "washerdriers2019"):
        ev = hit.get("energyConsPer100Cycle") or hit.get("energyCons100")
        if ev:
            product["labelEnergy"], product["labelEnergyUnit"] = ev, "kWh/100 cycles"
        if hit.get("ratedCapacity"):
            product["labelCapacity"] = f"{hit['ratedCapacity']} kg"
        if hit.get("waterCons"):
            product["labelWater"] = f"{hit['waterCons']} L"
        dur = hit.get("programmeDurationRated") or hit.get("programmeDuration")
        if dur:
            product["labelDuration"] = f"{dur} min"
    elif group == "dishwashers2019":
        ev = hit.get("energyCons100") or hit.get("energyConsPer100Cycle")
        if ev:
            product["labelEnergy"], product["labelEnergyUnit"] = ev, "kWh/100 cycles"
        if hit.get("ratedCapacity"):
            product["labelCapacity"] = f"{hit['ratedCapacity']} couverts"
        if hit.get("waterCons"):
            product["labelWater"] = f"{hit['waterCons']} L"
        dur = hit.get("programmeDuration") or hit.get("programmeDurationRated")
        if dur:
            product["labelDuration"] = f"{dur} min"

    if hit.get("noise"):
        product["labelNoise"] = f"{hit['noise']} dB"
    if hit.get("noiseClass"):
        product["labelNoiseClass"] = hit["noiseClass"]

    meta = {
        "eprelRegistrationNumber": reg,
        "group": group,
        "modelIdentifier": hit.get("modelIdentifier"),
        "matchedBrand": brand,
    }

    # Logo de la marque récupéré en ligne (filet de sécurité, best-effort) :
    # la page ne l'utilise que si la marque manque à sa bibliothèque locale.
    if with_files and brand:
        try:
            logo = _fetch_brand_logo(
                session, brand,
                manufacturer_url=hit.get("webLinkManufacturer") or "",
                org_site=(hit.get("organisation") or {}).get("website") or "")
            if logo:
                product["brandLogoAuto"] = logo
        except Exception:
            pass

    embedded = {}
    if with_files and reg:
        # Étiquette énergie officielle (PNG) -> intégrée au kit + mode officiel.
        label_png = f"{BASE}/labels/{group}/Label_{reg}.png"
        png, ct = _fetch_bytes(session, label_png, referer)
        if png:
            embedded["energyLabelImage"] = {
                "name": f"etiquette-energie-officielle-{reg}.png",
                "mimeType": ct or "image/png",
                "data": base64.b64encode(png).decode("ascii"),
            }
            product["energyLabelMode"] = "official"
            # Copie directe pour l'affichage immédiat sur l'affiche.
            product["officialLabelImage"] = f"data:{ct or 'image/png'};base64," + \
                embedded["energyLabelImage"]["data"]
        # Fiche d'information produit officielle (PDF) -> intégrée au kit.
        fiche_pdf = f"{BASE}/fiches/{group}/Fiche_{reg}_FR.pdf"
        pdf, ct = _fetch_bytes(session, fiche_pdf, referer)
        if pdf:
            embedded["productSheetPdf"] = {
                "name": f"fiche-information-produit-{reg}.pdf",
                "mimeType": ct or "application/pdf",
                "data": base64.b64encode(pdf).decode("ascii"),
            }
            meta["productSheetBytes"] = len(pdf)

    return product, meta, embedded


def lookup(reference, group=None, with_files=True):
    """
    Point d'entrée principal : référence -> dict résultat complet.
    Renvoie {found: bool, ...}. Utilisé par serveur_fiches.py.
    """
    # `group` peut être une catégorie de l'appli (« refrigerateur ») ou un
    # groupe EPREL (« refrigeratingappliances2019 ») : on tolère les deux.
    if group and str(group).startswith("icecat:"):
        return icecat_lookup(reference, group=group, with_files=with_files)
    if group and group not in GROUP_META:
        group = CATEGORY_TO_GROUP.get(group)
    hit, matched_group, score = find(reference, group=group)
    if hit is None:
        icecat = icecat_lookup(reference, group=None, with_files=with_files)
        if icecat.get("found"):
            return icecat
        return {
            "found": False,
            "reference": reference,
            "score": round(score, 3),
            "message": ("Aucune correspondance EPREL ou Icecat fiable pour cette référence. "
                        "Vérifiez la référence ou complétez la fiche à la main."),
        }
    session = _session()
    session.get(f"{BASE}/screen/product/{matched_group}", timeout=25)
    product, meta, embedded = to_product(hit, matched_group, reference,
                                         session=session, with_files=with_files)
    try:
        icecat = icecat_lookup(reference, group=None, with_files=with_files)
        product, meta, embedded = _merge_icecat_product(product, meta, embedded, icecat)
    except Exception:
        pass
    return {
        "found": True,
        "reference": reference,
        "score": round(score, 3),
        "source": "eprel",
        "sourceLabel": meta.get("sourceLabel") or "EPREL",
        "product": product,
        "meta": meta,
        "embeddedFiles": embedded,
    }


def search(query, limit=8):
    """
    Autocomplétion : à partir d'une saisie partielle (référence ou modèle, même
    au milieu du code), renvoie une liste de suggestions triées par pertinence.
    Chaque suggestion : {reference, modelIdentifier, brand, energy, group,
    category, label}. Toutes les familles EPREL sont interrogées en parallèle.
    """
    from concurrent.futures import ThreadPoolExecutor
    q = (query or "").strip()
    if len(q) < 3:
        return []
    session = _session()
    seen, out = set(), []

    def add_eprel(h, g):
        model = (h.get("modelIdentifier") or "").strip()
        if not model:
            return
        ref = model.split()[0]
        brand = (h.get("supplierOrTrademark") or "").strip()
        key = (_norm(brand), _norm(ref))
        if key in seen:
            return
        seen.add(key)
        out.append({
            "reference": ref,
            "modelIdentifier": model,
            "brand": brand,
            "energy": _pretty_energy_class(h.get("energyClass")),
            "group": g,
            "category": GROUP_META.get(g, ("custom",))[0],
            "source": "EPREL",
            "label": (brand + " " + ref).strip(),
        })

    # (1) Recherche par NOM « catégorie + marque » (ex. « réfrigérateur bosch »,
    #     « lave-linge samsung ») : données officielles EPREL, prioritaires.
    cat_group, brand_q = _detect_category_brand(q)
    if cat_group and len(_norm(brand_q)) >= 2:
        try:
            for h in _brand_search_group(session, cat_group, brand_q, limit=min(limit, 8)):
                add_eprel(h, cat_group)
                if len(out) >= limit:
                    break
        except Exception:
            pass

    # (2) Recherche par CODE modèle (préfixe ou milieu) sur toutes les familles.
    if len(out) < limit:
        def one(g):
            try:
                return [(g, h) for h in _search_group(session, g, q, limit=6)]
            except Exception:
                return []
        pairs = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for part in ex.map(one, SEARCH_GROUPS):
                pairs.extend(part)
        pairs.sort(key=lambda gh: _score(q, gh[1]), reverse=True)
        for g, h in pairs:
            add_eprel(h, g)
            if len(out) >= limit:
                break
    try:
        # On récupère large puis on classe (marque tapée d'abord) et on écarte
        # les accessoires : la recherche par NOM devient utilisable.
        raw = _icecat_search_once(session, q, limit=max(limit * 3, 18))
        for h in _icecat_rank(q, raw):
            ref = (h.get("mpn") or h.get("name") or "").strip()
            brand = (h.get("brand") or "").strip()
            if not ref:
                continue
            key = (_norm(brand), _norm(ref))
            if key in seen:
                continue
            seen.add(key)
            category_name = h.get("categoryName") or ""
            title = h.get("title") or ref
            out.append({
                "reference": ref,
                "modelIdentifier": title,
                "title": title,
                "brand": brand,
                "energy": "",
                "group": f"icecat:{h.get('product_id')}",
                "category": _icecat_local_category(category_name, title),
                "categoryName": category_name,
                "relevant": _icecat_is_relevant(category_name),
                "source": "Icecat",
                "label": (brand + " " + ref).strip(),
            })
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import json
    import sys
    ref = sys.argv[1] if len(sys.argv) > 1 else "FRDN18ES3"
    res = lookup(ref, with_files=False)
    print(json.dumps(res, ensure_ascii=False, indent=2))
