#!/usr/bin/env python3
"""
Étape 2 : visite les sites officiels des bars (tag website d'OSM) pour y repérer des prix de bière.

    python3 scripts/2_sites.py           # reprend là où le dernier passage s'est arrêté
    python3 scripts/2_sites.py --tout    # repart de zéro

- Respecte robots.txt et s'identifie avec un User-Agent explicite.
- Politesse : 2 s minimum entre deux requêtes vers un MÊME site ; plusieurs sites différents sont lus en parallèle.
- Ne suit que les pages du même site dont le lien évoque la carte (carte, menu, boissons, bières, tarifs, happy hour).
- Lit aussi les cartes en PDF quand c'est possible (macOS : PDFKit via osascript).
- N'extrait AUCUN prix automatiquement : il produit des extraits de texte autour des mots "pinte", "50 cl",
  "blonde", "pils"... qui sont ensuite relus à la main et reportés (avec la source) dans
  scripts/prix_sources.csv. C'est volontaire : une carte mélange demis, pintes, IPA, cocktails...
  et un prix mal attribué serait pire qu'une absence de prix.

Entrée  : scripts/sortie/osm_bars.json
Sortie  : scripts/sortie/candidats_sites.json (enregistré au fur et à mesure)
"""
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

ICI = Path(__file__).resolve().parent
SORTIE = ICI / "sortie"
USER_AGENT = "pinte-pas-chere/0.1 (projet personnel ; lecture ponctuelle des cartes publiques)"
DELAI = 2.0             # secondes minimum entre deux requêtes vers un même site
SITES_EN_PARALLELE = 6  # sites différents lus en même temps
TAILLE_MAX = 3_000_000  # octets
PAGES_MAX = 4           # pages "carte" suivies par site
PDF_MAX = 3             # cartes PDF lues par site

# Plateformes à ne pas visiter : connexion obligatoire, CGU interdisant la collecte, ou simples annuaires
DOMAINES_IGNORES = ("facebook.com", "instagram.com", "linktr.ee", "tripadvisor.", "google.", "privateaser.com",
                    "schlouk-map.com", "mistergoodbeer.com", "thefork.", "lafourchette.", "untappd.com",
                    "ubereats.", "deliveroo.", "tiktok.com")

MOTS_LIEN = re.compile(r"carte|menu|boisson|drink|bi[eè]re|beer|tarif|prix|price|happy", re.I)
MOTS_PRIX = re.compile(r"pinte|pint\b|50\s?cl|blonde|pils|lager|pression|draft|happy\s?hour|demi\b|25\s?cl", re.I)
PRIX = re.compile(r"\d{1,2}(?:[,.]\d{1,2})?\s?(?:€|euros?)|€\s?\d{1,2}(?:[,.]\d{1,2})?|\b\d{1,2}[,.]\d{1,2}\b", re.I)

# Lecture des PDF avec PDFKit (macOS) via JavaScript for Automation, sans dépendance Python
SCRIPT_PDF = """ObjC.import('PDFKit');
function run(argv) {
  var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  return (!doc || doc.isNil()) ? "" : ObjC.unwrap(doc.string);
}"""
PDF_POSSIBLE = sys.platform == "darwin"


class Extracteur(HTMLParser):
    """Récupère le texte visible et les liens d'une page HTML."""

    def __init__(self):
        super().__init__()
        self.textes, self.liens, self._ignore, self._lien = [], [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._ignore += 1
        if tag == "a":
            self._lien = [dict(attrs).get("href") or "", ""]
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "td"):
            self.textes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._ignore:
            self._ignore -= 1
        if tag == "a" and self._lien:
            self.liens.append(tuple(self._lien))
            self._lien = None

    def handle_data(self, data):
        if self._ignore:
            return
        self.textes.append(data)
        if self._lien:
            self._lien[1] += data


# ---------------------------------------------------------------------------
# Politesse : robots.txt et délai par site
# ---------------------------------------------------------------------------
_robots, _derniere, _verrou = {}, {}, threading.Lock()


def hote(url):
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def attendre_tour(url):
    """Bloque jusqu'à ce que DELAI secondes se soient écoulées depuis la dernière requête vers ce site."""
    h = hote(url)
    while True:
        with _verrou:
            attente = DELAI - (time.time() - _derniere.get(h, 0))
            if attente <= 0:
                _derniere[h] = time.time()
                return
        time.sleep(attente)


def autorise(url):
    """Vérifie robots.txt (mis en cache par site). Lève ConnectionError si le site est injoignable."""
    racine = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if racine not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            contenu, _ = telecharger(racine + "/robots.txt", verifier_robots=False)
            rp.parse(contenu.decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            else:
                rp.allow_all = True
        except Exception:  # noqa: BLE001  domaine mort, DNS, TLS... : le site est injoignable
            rp = None
        _robots[racine] = rp
    if _robots[racine] is None:
        raise ConnectionError("site injoignable")
    return _robots[racine].can_fetch(USER_AGENT, url)


def telecharger(url, verifier_robots=True):
    """GET poli : robots.txt, délai par site, taille max. Renvoie (octets, type de contenu)."""
    if verifier_robots and not autorise(url):
        raise PermissionError("interdit par robots.txt")
    attendre_tour(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=20) as rep:
        return rep.read(TAILLE_MAX), rep.headers.get("Content-Type", "")


# ---------------------------------------------------------------------------
# Lecture des pages et des PDF
# ---------------------------------------------------------------------------
def texte_pdf(contenu):
    """Texte d'un PDF (macOS uniquement), ou "" si illisible."""
    if not PDF_POSSIBLE:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(contenu)
        f.flush()
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", SCRIPT_PDF, f.name],
                               capture_output=True, text=True, timeout=60)
            return r.stdout
        except Exception:  # noqa: BLE001
            return ""


def lire_page(url):
    """Renvoie (texte, liens, est_pdf)."""
    contenu, type_contenu = telecharger(url)
    if "pdf" in type_contenu or contenu[:5] == b"%PDF-":
        return texte_pdf(contenu), [], True
    m = re.search(r"charset=([\w-]+)", type_contenu)
    html = contenu.decode(m.group(1) if m else "utf-8", "replace")
    ex = Extracteur()
    ex.feed(html)
    texte = re.sub(r"[ \t\xa0]+", " ", "".join(ex.textes))
    texte = re.sub(r"\s*\n\s*", "\n", texte)
    return texte, ex.liens, False


def extraits(texte):
    """Morceaux de texte qui contiennent à la fois un mot-clé bière et un prix."""
    res = []
    for m in MOTS_PRIX.finditer(texte):
        debut, fin = max(0, m.start() - 160), min(len(texte), m.end() + 160)
        morceau = texte[debut:fin]
        if PRIX.search(morceau):
            morceau = " ".join(morceau.split())
            if not any(morceau[:60] in r for r in res):
                res.append(morceau)
    return res[:15]


def explorer(bar):
    sites = [u for u in (bar.get("menu_url"), bar.get("site_web")) if u]
    sites = [u if u.startswith("http") else "https://" + u for u in sites]
    resultat = {"osm_id": bar["osm_id"], "nom": bar["nom"], "adresse": bar["adresse"],
                "pages": [], "extraits": [], "pdf": [], "erreurs": []}
    a_visiter = [u for u in sites if not any(d in u for d in DOMAINES_IGNORES)]
    if not a_visiter:
        resultat["erreurs"].append("site ignoré (réseau social / plateforme)")
        return resultat

    vues, pdf_lus = set(), 0
    while a_visiter and len(vues) < PAGES_MAX + 1 + PDF_MAX:
        url = a_visiter.pop(0)
        if url in vues:
            continue
        vues.add(url)
        try:
            texte, liens, est_pdf = lire_page(url)
        except Exception as e:  # noqa: BLE001
            resultat["erreurs"].append(f"{url} : {e}")
            continue
        (resultat["pdf"] if est_pdf else resultat["pages"]).append(url)
        pdf_lus += est_pdf
        for morceau in extraits(texte):
            resultat["extraits"].append({"url": url, "texte": morceau})
        # liens vers la carte, sur le même site (pages HTML et PDF)
        for href, txt in liens:
            cible = urllib.parse.urljoin(url, href.strip()).split("#")[0]
            if not cible.startswith("http") or hote(cible) != hote(url) or cible in vues or cible in a_visiter:
                continue
            if MOTS_LIEN.search(href) or MOTS_LIEN.search(txt):
                if cible.lower().endswith(".pdf"):
                    if pdf_lus + sum(u.lower().endswith(".pdf") for u in a_visiter) < PDF_MAX:
                        a_visiter.append(cible)
                elif not any(cible.lower().endswith(x) for x in (".jpg", ".jpeg", ".png", ".webp")):
                    a_visiter.append(cible)
    return resultat


def main():
    bars = json.loads((SORTIE / "osm_bars.json").read_text())["bars"]
    avec_site = [b for b in bars if b.get("site_web") or b.get("menu_url")]
    chemin = SORTIE / "candidats_sites.json"
    deja = {} if "--tout" in sys.argv or not chemin.exists() else \
        {r["osm_id"]: r for r in json.loads(chemin.read_text())}
    a_faire = [b for b in avec_site if b["osm_id"] not in deja]
    print(f"{len(avec_site)} bars avec un site web sur {len(bars)} ; déjà traités : {len(deja)} ; "
          f"à visiter : {len(a_faire)}" + ("" if PDF_POSSIBLE else " (lecture des PDF indisponible hors macOS)"),
          flush=True)

    resultats = dict(deja)
    with ThreadPoolExecutor(SITES_EN_PARALLELE) as pool:
        futurs = {pool.submit(explorer, b): b for b in a_faire}
        for i, f in enumerate(as_completed(futurs), 1):
            bar = futurs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                r = {"osm_id": bar["osm_id"], "nom": bar["nom"], "adresse": bar["adresse"],
                     "pages": [], "extraits": [], "pdf": [], "erreurs": [f"erreur inattendue : {e}"]}
            resultats[r["osm_id"]] = r
            etat = f"{len(r['extraits'])} extrait(s), {len(r['pdf'])} pdf" if r["pages"] or r["pdf"] else \
                (r["erreurs"][0][-60:] if r["erreurs"] else "rien")
            print(f"[{i}/{len(a_faire)}] {r['nom']} : {etat}", flush=True)
            if i % 10 == 0:  # sauvegarde régulière : on peut interrompre et reprendre
                chemin.write_text(json.dumps(list(resultats.values()), ensure_ascii=False, indent=1))
    chemin.write_text(json.dumps(list(resultats.values()), ensure_ascii=False, indent=1))

    tous = list(resultats.values())
    print(f"\nSites lus : {sum(1 for r in tous if r['pages'] or r['pdf'])} · avec extraits prix : "
          f"{sum(1 for r in tous if r['extraits'])} · avec PDF : {sum(1 for r in tous if r['pdf'])}")
    print(f"-> {chemin} (à relire, puis reporter dans scripts/prix_sources.csv)")


if __name__ == "__main__":
    main()
