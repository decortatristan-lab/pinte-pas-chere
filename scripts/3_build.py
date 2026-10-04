#!/usr/bin/env python3
"""
Étape 3 : fusionne les bars OSM et les prix sourcés, et produit data/bars.json pour le site.

    python3 scripts/3_build.py

Entrées :
  scripts/sortie/osm_bars.json   (étape 1)
  scripts/prix_sources.csv       (prix relevés à la main, UNE ligne = UNE source vérifiée)

Règles appliquées (aucun prix n'est inventé) :
  - un bar sans prix sourcé n'est pas exporté ;
  - source datée de plus de 24 mois : ligne rejetée ;
  - source de 12 à 24 mois : confiance plafonnée à "incertain" ;
  - prix hors de [1 € ; 15 €] : ligne rejetée (faute de frappe probable).
Bibliothèque standard Python uniquement.
"""
import csv
import datetime as dt
import importlib.util
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ICI = Path(__file__).resolve().parent
RACINE = ICI.parent
USER_AGENT = "pinte-pas-chere/0.1 (projet personnel)"
JOURS_FR = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
NIVEAUX = ["verifie", "estime", "incertain"]

# Réutilise le lecteur d'horaires OSM de l'étape 1
_spec = importlib.util.spec_from_file_location("osm", ICI / "1_osm.py")
osm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(osm)


def jours(texte):
    """'lun-ven' / 'sam,dim' / 'tous' -> ['lun', ...] (gère 'ven-lun')."""
    texte = texte.strip().lower()
    if texte in ("tous", "tlj", "tous les jours"):
        return list(JOURS_FR)
    res = []
    for morceau in texte.split(","):
        a, _, b = morceau.strip().partition("-")
        i, fin = JOURS_FR.index(a), JOURS_FR.index(b or a)
        while True:
            if JOURS_FR[i] not in res:
                res.append(JOURS_FR[i])
            if i == fin:
                break
            i = (i + 1) % 7
    return res


def happy_hours(texte):
    """'lun-ven 17:00-20:00 3.50 ; sam 15:00-19:00 4' -> [{jours, debut, fin, prix}, ...]"""
    res = []
    for plage in filter(None, (p.strip() for p in (texte or "").split(";"))):
        m = re.fullmatch(r"(\S+)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+(\d+(?:[.,]\d+)?)", plage)
        if not m:
            raise ValueError(f"happy hour illisible : « {plage} »")
        res.append({"jours": jours(m.group(1)), "debut": osm._heure(m.group(2)),
                    "fin": osm._heure(m.group(3)), "prix": float(m.group(4).replace(",", "."))})
    return res


def prix(texte):
    texte = (texte or "").strip().replace(",", ".")
    return float(texte) if texte else None


def geocoder(adresse, cache):
    """Adresse -> (lat, lng, libellé) via l'API Adresse (BAN). Utilisé pour les bars absents d'OSM."""
    if adresse in cache:
        return cache[adresse]
    url = "https://api-adresse.data.gouv.fr/search/?" + urllib.parse.urlencode({"q": adresse, "limit": 1})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    f = json.load(urllib.request.urlopen(req, timeout=20))["features"]
    time.sleep(0.2)
    if not f or f[0]["properties"]["score"] < 0.6:
        cache[adresse] = None
    else:
        p = f[0]["properties"]
        lng, lat = f[0]["geometry"]["coordinates"]
        cache[adresse] = (round(lat, 6), round(lng, 6), f"{p['name']}, {p['postcode']} Paris")
    return cache[adresse]


def simplifier(nom):
    """'Le Péché Mignon (13e)' -> 'peche mignon' : minuscules, sans accents, sans articles ni parenthèses."""
    nom = re.sub(r"\(.*?\)", " ", nom)
    nom = unicodedata.normalize("NFKD", nom.lower()).encode("ascii", "ignore").decode()
    mots = [m for m in re.split(r"[^a-z0-9]+", nom) if m and m not in ("le", "la", "les", "l", "the", "bar", "cafe", "au", "aux", "o")]
    return " ".join(mots)


def distance_m(lat1, lng1, lat2, lng2):
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


def meme_nom(a, b):
    """Vrai si tous les mots significatifs d'un nom figurent dans l'autre ('Liebe' / 'Liebe Bar')."""
    mots_a, mots_b = set(simplifier(a).split()), set(simplifier(b).split())
    petit, grand = sorted([mots_a, mots_b], key=len)
    return bool(petit - {"paris"}) and petit <= grand


def rapprocher_osm(nom, lat, lng, bars_osm, rayon=150):
    """Bar OSM du même nom à moins de `rayon` mètres, sinon None."""
    candidats = [b for b in bars_osm if distance_m(lat, lng, b["lat"], b["lng"]) <= rayon and meme_nom(nom, b["nom"])]
    return min(candidats, key=lambda b: distance_m(lat, lng, b["lat"], b["lng"])) if candidats else None


def perimetre(codes):
    """['75110', '75111'] -> 'Paris 10e, 11e' ; les 20 arrondissements -> 'Paris'."""
    if len(codes) >= 20:
        return "Paris"
    noms = [("1er" if int(c[-2:]) == 1 else f"{int(c[-2:])}e") for c in sorted(codes)]
    return "Paris " + ", ".join(noms)


def main():
    aujourd_hui = dt.date.today()
    osm_data = json.loads((ICI / "sortie" / "osm_bars.json").read_text())
    par_id = {b["osm_id"]: b for b in osm_data["bars"]}
    chemin_cache = ICI / "sortie" / "cache_geocodage.json"
    cache = {k: tuple(v) if v else None for k, v in json.loads(chemin_cache.read_text()).items()} \
        if chemin_cache.exists() else {}

    bars, rejets, rapproches = {}, [], []
    with open(ICI / "prix_sources.csv", newline="", encoding="utf-8") as f:
        lignes = [l for l in csv.DictReader(f) if l["nom"].strip() and not l["nom"].startswith("#")]

    for n, l in enumerate(lignes, 2):
        nom = l["nom"].strip()
        try:
            # --- âge de la source -> confiance
            date_source = dt.date.fromisoformat(l["date_source"].strip())
            age_mois = (aujourd_hui - date_source).days / 30.4
            confiance = l["confiance"].strip()
            if confiance not in NIVEAUX:
                raise ValueError(f"confiance inconnue « {confiance} »")
            if age_mois > 24:
                raise ValueError(f"source trop ancienne ({date_source})")
            if age_mois > 12:
                confiance = "incertain"

            # --- prix
            p_normal = prix(l["prix_pinte_normal"])
            hh = happy_hours(l["happy_hours"])
            tous_prix = [p for p in [p_normal] + [h["prix"] for h in hh] if p is not None]
            if not tous_prix:
                raise ValueError("aucun prix")
            if any(not 1 <= p <= 15 for p in tous_prix):
                raise ValueError(f"prix improbable {tous_prix}")

            # --- position, adresse, horaires
            base = par_id.get(l["osm_id"].strip()) if l["osm_id"].strip() else None
            if l["osm_id"].strip() and not base:
                raise ValueError(f"osm_id introuvable {l['osm_id']}")
            if not base:
                # Bar cité par la presse sans osm_id : on géocode son adresse puis on cherche
                # dans OSM un bar du même nom à moins de 150 m (pour récupérer ses horaires).
                geo = geocoder(l["adresse"].strip(), cache)
                if not geo:
                    raise ValueError(f"adresse introuvable « {l['adresse']} »")
                base = rapprocher_osm(nom, geo[0], geo[1], osm_data["bars"])
                if base:
                    rapproches.append(f"{nom} -> {base['nom']} ({base['osm_id']})")
            if base:
                lat, lng = base["lat"], base["lng"]
                adresse = l["adresse"].strip() or base["adresse"]
                horaires = base["horaires_ouverture"]
                ident = "osm-" + base["osm_id"].replace("/", "-")
            else:
                lat, lng, adresse = geo
                horaires = None
                sans_accents = unicodedata.normalize("NFKD", nom.lower()).encode("ascii", "ignore").decode()
                ident = "src-" + re.sub(r"[^a-z0-9]+", "-", sans_accents).strip("-")
            if l["horaires"].strip():
                horaires = osm.parser_horaires(l["horaires"].strip())
                if horaires is None:
                    raise ValueError(f"horaires illisibles « {l['horaires']} »")
        except (ValueError, KeyError) as e:
            rejets.append(f"ligne {n} ({nom}) : {e}")
            continue

        if ident in bars:
            # Deuxième source pour le même bar (ex. prix normal sur le site du bar + happy hour dans la presse) :
            # on complète sans écraser, et on garde la confiance la plus faible et la date la plus ancienne.
            b = bars[ident]
            if p_normal is not None and b["prix_pinte_normal"] is not None and p_normal != b["prix_pinte_normal"]:
                rejets.append(f"ligne {n} ({nom}) : prix normal contradictoire avec une autre source, ligne ignorée")
                continue
            b["prix_pinte_normal"] = b["prix_pinte_normal"] if b["prix_pinte_normal"] is not None else p_normal
            b["happy_hours"] += hh
            b["autres_sources"].append(l["source_url"].strip())
            b["confiance"] = max(b["confiance"], confiance, key=NIVEAUX.index)
            b["date_verification"] = min(b["date_verification"], date_source.isoformat())
            if l["note"].strip():
                b["note_prix"] = " ".join(filter(None, [b["note_prix"], l["note"].strip()]))
            continue
        bars[ident] = {
            "id": ident,
            "nom": nom,
            "adresse": adresse,
            "lat": lat,
            "lng": lng,
            "horaires_ouverture": horaires,
            "prix_pinte_normal": p_normal,
            "happy_hours": hh,
            "source_url": l["source_url"].strip(),
            "autres_sources": [],
            "date_verification": date_source.isoformat(),  # date à laquelle la source atteste le prix
            "confiance": confiance,
            "note_prix": l["note"].strip() or None,
        }

    chemin_cache.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    sortie = {
        "meta": {
            "fictif": False,
            "genere_le": aujourd_hui.isoformat(),
            "perimetre": perimetre(osm_data["codes_insee"]),
            "bars_osm_dans_le_perimetre": len(par_id),
            "description": "Prix relevés dans des sources publiques (sites des bars, presse) ; voir source_url de chaque bar.",
        },
        "bars": sorted(bars.values(), key=lambda b: b["nom"].lower()),
    }
    (RACINE / "data" / "bars.json").write_text(json.dumps(sortie, ensure_ascii=False, indent=1))

    # --- bilan
    nb_osm = sum(1 for i in bars if i.startswith("osm-"))
    print(f"{len(bars)} bars avec un prix sourcé -> data/bars.json")
    print(f"  dont {nb_osm} des {len(par_id)} bars OSM du périmètre ({nb_osm / max(1, len(par_id)):.0%})")
    print("  par confiance :", dict(Counter(b["confiance"] for b in bars.values())))
    presse = ("lebonbon.fr", "pariszigzag.fr", "10h26.com", "magazine-paris-berlin.com", "sortiraparis.com")
    de_presse = lambda b: any(p in u for u in [b["source_url"]] + b["autres_sources"] for p in presse)  # noqa: E731
    print(f"  par source    : site officiel du bar {sum(1 for b in bars.values() if not de_presse(b))}, "
          f"presse {sum(1 for b in bars.values() if de_presse(b))} "
          f"({', '.join(f'{d} {n}' for d, n in Counter(urllib.parse.urlsplit(b['source_url']).netloc.removeprefix('www.') for b in bars.values() if de_presse(b)).most_common())})")
    peu_cher = [b for b in bars.values() if min([b["prix_pinte_normal"] or 99] + [h["prix"] for h in b["happy_hours"]]) <= 4]
    print(f"  dont pinte <= 4 € à un moment : {len(peu_cher)} "
          f"(confiance : {dict(Counter(b['confiance'] for b in peu_cher))})")
    print(f"  dont {len(bars) - nb_osm} absents d'OSM en bar/pub (adresse de la source géocodée)")
    if rapproches:
        print(f"\n{len(rapproches)} bar(s) de la presse rattaché(s) à OSM :")
        for r in rapproches:
            print("  -", r)
    if rejets:
        print(f"\n{len(rejets)} ligne(s) rejetée(s) :")
        for r in rejets:
            print("  -", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
