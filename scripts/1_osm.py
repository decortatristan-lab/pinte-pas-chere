#!/usr/bin/env python3
"""
Étape 1 : récupère les bars et pubs d'arrondissements de Paris depuis OpenStreetMap (Overpass API).

    python3 scripts/1_osm.py                 # tout Paris : 20 arrondissements (par défaut)
    python3 scripts/1_osm.py 75111 75120     # seulement certains arrondissements (codes INSEE)

Sortie : scripts/sortie/osm_bars.json (une entrée par bar, sans prix).
Les adresses manquantes dans OSM sont complétées par géocodage inverse (api-adresse.data.gouv.fr).
Bibliothèque standard Python uniquement.
"""
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ICI = Path(__file__).resolve().parent
SORTIE = ICI / "sortie"
USER_AGENT = "pinte-pas-chere/0.1 (projet personnel, collecte ponctuelle)"
# serveur principal puis miroirs publics (Overpass est parfois saturé : erreurs 429/504)
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
REVERSE_CSV = "https://api-adresse.data.gouv.fr/reverse/csv/"

JOURS_OSM = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
JOURS_FR = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def http_get_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=200) as rep:
        return json.load(rep)


# ---------------------------------------------------------------------------
# Horaires d'ouverture OSM -> liste de plages {jours, debut, fin}
# On ne gère que la syntaxe courante ("Mo-Fr 17:00-02:00; Sa,Su 15:00-02:00; Mo off").
# Tout ce qui est plus exotique (mois, semaines, "17:00+", ...) => None (= horaires inconnus).
# ---------------------------------------------------------------------------
JOUR = r"(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)"
SELECTEUR_JOURS = rf"{JOUR}(?:\s*-\s*{JOUR})?(?:\s*,\s*{JOUR}(?:\s*-\s*{JOUR})?)*"
HORAIRE = r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}"
REGLE = re.compile(rf"(?P<jours>{SELECTEUR_JOURS})?\s*(?P<heures>{HORAIRE}(?:\s*,\s*{HORAIRE})*|off|closed)")


def _jours(selecteur):
    """'Mo-We,Fr,PH' -> ({0,1,2,4}, a_des_jours_feries=True). Gère 'Fr-Mo' (passage dimanche->lundi)."""
    jours, ferie = set(), False
    for morceau in re.split(r"\s*,\s*", selecteur):
        bornes = [b.strip() for b in morceau.split("-")]
        if any(b in ("PH", "SH") for b in bornes):
            ferie = True
            continue
        a = JOURS_OSM.index(bornes[0])
        b = JOURS_OSM.index(bornes[-1])
        i = a
        while True:
            jours.add(i)
            if i == b:
                break
            i = (i + 1) % 7
    return jours, ferie


def _heure(h):
    """'26:00' -> '02:00' ; '23:59' -> '24:00' (évite un trou d'une minute avant minuit)."""
    hh, mm = (int(x) for x in h.split(":"))
    if (hh, mm) == (23, 59):
        return "24:00"
    if hh > 24 or (hh == 24 and mm > 0):
        hh -= 24
    return f"{hh:02d}:{mm:02d}"


def parser_horaires(texte):
    """Renvoie une liste de plages {jours, debut, fin} ou None si le format n'est pas compris."""
    if not texte:
        return None
    texte = texte.strip()
    if texte == "24/7":
        return [{"jours": list(JOURS_FR), "debut": "00:00", "fin": "24:00"}]

    par_jour = {}  # index du jour -> liste de (debut, fin)
    for segment in texte.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        pos = 0
        for m in REGLE.finditer(segment):
            # entre deux règles, seuls des séparateurs sont admis
            if segment[pos:m.start()].strip(" ,"):
                return None
            pos = m.end()
            if m.group("jours"):
                jours, ferie = _jours(m.group("jours"))
                if not jours and ferie:
                    continue  # règle propre aux jours fériés : ignorée
            else:
                jours = set(range(7))
            if m.group("heures") in ("off", "closed"):
                plages = []
            else:
                plages = []
                for h in re.split(r"\s*,\s*", m.group("heures")):
                    debut, fin = (x.strip() for x in h.split("-"))
                    plages.append((_heure(debut), _heure(fin)))
            for j in jours:
                par_jour[j] = plages  # une règle plus loin remplace les précédentes pour ces jours
        if segment[pos:].strip(" ,"):
            return None

    if not par_jour:
        return None
    # Regroupe les jours qui ont exactement la même plage
    groupes = {}
    for j in sorted(par_jour):
        for debut, fin in par_jour[j]:
            groupes.setdefault((debut, fin), []).append(JOURS_FR[j])
    return [{"jours": jours, "debut": d, "fin": f} for (d, f), jours in groupes.items()]


# ---------------------------------------------------------------------------
# Adresse
# ---------------------------------------------------------------------------
def adresse_osm(tags):
    rue = tags.get("addr:street")
    if not rue:
        return None
    num = tags.get("addr:housenumber", "")
    cp = tags.get("addr:postcode", "")
    return f"{num} {rue}, {cp} Paris".replace("  ", " ").replace(" ,", ",").strip()


def cle_position(lat, lng):
    return f"{lat:.6f},{lng:.6f}"


def adresses_inverses_par_lot(positions, cache, taille_lot=500):
    """
    Complète le cache {"lat,lng": adresse} pour toutes les positions inconnues, en envoyant
    des lots CSV à l'API Adresse (/reverse/csv/) : une requête pour 500 bars au lieu de 500 requêtes.
    """
    a_faire = sorted({cle_position(lat, lng) for lat, lng in positions} - set(cache))
    for i in range(0, len(a_faire), taille_lot):
        lot = a_faire[i:i + taille_lot]
        csv_lot = "lat,lon\n" + "\n".join(lot) + "\n"
        frontiere = "----pinte-pas-chere"
        corps = "".join(
            f'--{frontiere}\r\nContent-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'
            for nom, valeur in [("result_columns", "result_name"), ("result_columns", "result_postcode")]
        ) + (f'--{frontiere}\r\nContent-Disposition: form-data; name="data"; filename="lot.csv"\r\n'
             f"Content-Type: text/csv\r\n\r\n{csv_lot}\r\n--{frontiere}--\r\n")
        req = urllib.request.Request(REVERSE_CSV, data=corps.encode(), method="POST", headers={
            "User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={frontiere}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as rep:
                lignes = list(csv.DictReader(io.StringIO(rep.read().decode("utf-8"))))
        except Exception as e:  # noqa: BLE001
            print(f"  géocodage inverse par lot impossible ({e}) ; adresses laissées vides")
            return
        for l in lignes:
            if l.get("result_name") and l.get("result_postcode"):
                cache[cle_position(float(l["lat"]), float(l["lon"]))] = \
                    f"{l['result_name']}, {l['result_postcode']} Paris"  # "12 Rue Oberkampf, 75011 Paris"
        print(f"  {min(i + taille_lot, len(a_faire))}/{len(a_faire)} adresses complétées par lot", flush=True)
        time.sleep(1)


def overpass(requete):
    """Envoie une requête Overpass (avec réessais et miroirs). Renvoie la liste des éléments, ou None."""
    for essai in range(5):
        serveur = OVERPASS[essai % len(OVERPASS)]
        try:
            reponse = http_get_json(serveur, urllib.parse.urlencode({"data": requete}).encode())
            # Overpass peut répondre 200 avec une erreur dans "remark" et aucun élément : c'est un échec
            if reponse.get("remark") or not reponse.get("elements"):
                raise RuntimeError(reponse.get("remark") or "réponse vide")
            return reponse["elements"]
        except Exception as e:  # noqa: BLE001
            print(f"  {serveur} : {str(e)[:120]} ; nouvel essai dans {20 * (essai + 1)} s")
            time.sleep(20 * (essai + 1))
    return None


def stations(filtre_zone):
    """
    Stations de métro, tram et RER (pour choisir sa position sans GPS ni service d'adresses).
    Une entrée par nom ; position moyenne des quais. Écrit data/stations.json.
    """
    elements = overpass(
        "[out:json][timeout:120];"
        f"area{filtre_zone}->.zone;"
        '(nwr["railway"="station"]["station"~"subway|light_rail"](area.zone);'
        'nwr["railway"="station"]["network"~"RER"](area.zone););'
        "out center tags;"
    )
    if elements is None:
        print("Stations : Overpass ne répond pas, data/stations.json n'a pas été modifié.")
        return
    par_nom = {}
    for el in elements:
        nom = el.get("tags", {}).get("name")
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lng = el.get("lon", el.get("center", {}).get("lon"))
        if nom and lat:
            par_nom.setdefault(nom, []).append((lat, lng))
    liste = [{"nom": nom, "lat": round(sum(p[0] for p in pts) / len(pts), 5),
              "lng": round(sum(p[1] for p in pts) / len(pts), 5)} for nom, pts in sorted(par_nom.items())]
    (ICI.parent / "data" / "stations.json").write_text(json.dumps(liste, ensure_ascii=False))
    print(f"{len(liste)} stations de métro, tram et RER -> data/stations.json")


# ---------------------------------------------------------------------------
def main():
    codes = sys.argv[1:] or [f"751{i:02d}" for i in range(1, 21)]  # 75101 (1er) ... 75120 (20e)
    tout_paris = len(codes) == 20
    print("Zone : tout Paris" if tout_paris else f"Arrondissements (INSEE) : {', '.join(codes)}")
    # Pour tout Paris, une seule zone (la commune, INSEE 75056) : bien plus léger pour Overpass que 20 zones
    filtre_zone = '["ref:INSEE"="75056"]["admin_level"="8"]' if tout_paris \
        else f'["ref:INSEE"~"^({"|".join(codes)})$"]["boundary"="administrative"]'
    requete = (
        "[out:json][timeout:180];"
        f"area{filtre_zone}->.zone;"
        'nwr["amenity"~"^(bar|pub)$"](area.zone);'
        "out center tags;"
    )
    elements = overpass(requete)
    if elements is None:
        sys.exit("Overpass ne répond pas, réessayez plus tard (le fichier existant n'a pas été modifié).")
    print(f"{len(elements)} bars/pubs trouvés dans OSM")

    SORTIE.mkdir(exist_ok=True)
    chemin_cache = SORTIE / "cache_adresses.json"
    cache = json.loads(chemin_cache.read_text()) if chemin_cache.exists() else {}

    def position(el):
        return el.get("lat", el.get("center", {}).get("lat")), el.get("lon", el.get("center", {}).get("lon"))

    # Adresses absentes d'OSM : géocodage inverse groupé (API Adresse, format CSV)
    sans_adresse = [position(el) for el in elements
                    if el.get("tags", {}).get("name") and not re.match(r"^\d", adresse_osm(el["tags"]) or "")]
    adresses_inverses_par_lot(sans_adresse, cache)
    chemin_cache.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    bars, horaires_compris, adresses_inverses = [], 0, 0
    for el in elements:
        t = el.get("tags", {})
        if not t.get("name"):
            continue
        lat, lng = position(el)
        adresse = adresse_osm(t)
        if not adresse or not re.match(r"^\d", adresse):
            adresse = cache.get(cle_position(lat, lng)) or adresse
            adresses_inverses += 1
        horaires = parser_horaires(t.get("opening_hours"))
        horaires_compris += horaires is not None
        bars.append({
            "osm_id": f"{el['type']}/{el['id']}",
            "nom": t["name"],
            "type": t["amenity"],
            "adresse": adresse,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "horaires_ouverture": horaires,
            "horaires_brut": t.get("opening_hours"),
            "happy_hours_osm": t.get("happy_hours"),  # horaires seuls, sans prix : simple indice
            "site_web": t.get("website") or t.get("contact:website"),
            "menu_url": t.get("website:menu"),
            "instagram": t.get("contact:instagram"),
            "facebook": t.get("contact:facebook"),
        })

    bars.sort(key=lambda b: b["nom"].lower())
    (SORTIE / "osm_bars.json").write_text(json.dumps(
        {"genere_le": time.strftime("%Y-%m-%d"), "codes_insee": codes, "bars": bars},
        ensure_ascii=False, indent=1))
    print(f"{len(bars)} bars nommés · horaires compris : {horaires_compris} · "
          f"adresses complétées par géocodage inverse : {adresses_inverses} · "
          f"avec site web : {sum(1 for b in bars if b['site_web'])}")
    print(f"-> {SORTIE / 'osm_bars.json'}")
    stations(filtre_zone)


if __name__ == "__main__":
    main()
