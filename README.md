# Pinte pas chère

Site statique (HTML/CSS/JS, sans framework ni build) qui donne les 3 bars les plus proches où la pinte de blonde coûte 4 € ou moins à l'heure actuelle, happy hours comprises.

Les données couvrent **tout Paris** (20 arrondissements).

**Prix retenu** : pour chaque bar, la pinte (50 cl) de pils ou blonde pression **la moins chère** de la carte. IPA, blanches, ambrées et bières belges sont ignorées.

## Lancer le site en local

```bash
cd pinte-pas-chere
python3 -m http.server 8000
```

Ouvrir ensuite http://localhost:8000. Un serveur est nécessaire : ouvert directement en `file://`, le navigateur bloque la lecture de `data/bars.json`. La géolocalisation fonctionne sur `localhost` et en HTTPS.

Options de test dans l'URL :
- `?heure=2026-10-04T17:45` simule une heure (heure de Paris) ;
- `?donnees=fictif` charge le jeu de test fictif (`data/bars.fictif.json`).

Tests de la logique horaire (fuseau, plages qui passent minuit, happy hours à venir) :

```bash
node scripts/test_logique.js
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `index.html`, `style.css`, `app.js` | le site |
| `data/bars.json` | données réelles lues par le site (générées par `scripts/3_build.py`) |
| `data/stations.json` | stations de métro, tram et RER (générées par `scripts/1_osm.py`) : recherche par station, même sans réseau |
| `data/bars.fictif.json` | jeu de test **fictif** (bars inventés, préfixés `[FICTIF]`) |
| `scripts/1_osm.py` | liste des bars et pubs (OpenStreetMap / Overpass) |
| `scripts/2_sites.py` | lecture des sites officiels des bars, qui en extrait des passages à relire |
| `scripts/prix_sources.csv` | **les prix relevés, une ligne par bar, avec leur source** |
| `scripts/3_build.py` | fusion OSM + prix, puis écriture de `data/bars.json` |

## Relancer la collecte

Python 3.9+ suffit, sans aucune dépendance.

```bash
python3 scripts/1_osm.py                # bars de tout Paris (codes INSEE en argument pour certains arrondissements seulement)
python3 scripts/2_sites.py              # sites officiels (6 en parallèle, 2 s entre requêtes sur un même site, robots.txt respecté) ; reprend où il s'était arrêté, --tout pour repartir de zéro
# relire scripts/sortie/candidats_sites.json et compléter scripts/prix_sources.csv à la main
python3 scripts/3_build.py              # écrit data/bars.json et affiche le bilan
```

### Pourquoi une étape manuelle ?

Aucune API ne donne le prix des pintes. Une carte de bar mélange demis, pintes, IPA et cocktails, et une extraction automatique produirait des prix faux. Le script 2 se contente donc de repérer les passages pertinents. Chaque prix retenu est ensuite relu et noté dans `prix_sources.csv` avec son URL et sa date.

### Format de `prix_sources.csv`

| Colonne | Contenu |
|---|---|
| `osm_id` | ex. `node/2387389298` ; vide si le bar n'est pas dans OSM (l'adresse est alors géocodée) |
| `nom`, `adresse` | `adresse` est obligatoire si `osm_id` est vide ; le bar est alors rattaché automatiquement à un bar OSM du même nom à moins de 150 m |
| `prix_pinte_normal` | prix hors happy hour, en euros (`3.5`) ; vide si inconnu |
| `happy_hours` | `lun-ven 17:00-20:00 3.5 ; sam,dim 15:00-19:00 4` (`tous` = tous les jours) |
| `horaires` | horaires d'ouverture au format OSM, seulement si OSM ne les a pas |
| `source_url`, `date_source` | page consultée et date à laquelle elle atteste le prix (AAAA-MM-JJ) |
| `confiance` | `verifie` (site ou carte du bar), `estime` (presse récente, demi doublé…), `incertain` |
| `note` | précision affichée sur le site (ex. « demi à 2,50 € doublé ») |

Règles appliquées par `3_build.py` :
- aucun prix n'est inventé ;
- une source de plus de 24 mois est rejetée ;
- une source de 12 à 24 mois passe en `incertain`.

### Bilan de la collecte du 4 octobre 2026 (tout Paris)

- OSM recense **2 038 bars et pubs** à Paris, dont 514 avec un site web. Sur ces 514 sites, 350 ont pu être lus ; les autres étaient des réseaux sociaux, des domaines morts ou des sites qui bloquent les robots.
- **101 bars** ont un prix sourcé :
  - 64 prix viennent du site du bar, 37 de la presse (Le Bonbon, 10h26, Paris Zigzag, Paris-Berlin, Sortir à Paris) ;
  - 48 sont `verifie`, 39 `estime` et 14 `incertain`.
- **40 bars** servent une pinte à 4 € ou moins à un moment de la semaine. Seuls 4 de ces prix sont confirmés par le bar lui-même (Ernest, Le Maximilien, La Mutinerie, La Cantine de Belleville, tous en happy hour) ; les autres viennent de la presse.
- Les bars qui publient leur carte en ligne sont plutôt chers : 5 à 10 € la pinte hors happy hour.

### Sources écartées

| Source | Motif |
|---|---|
| Mister Good Beer / Schlouk Map | leurs CGU interdisent d'« extraire ou collecter des informations relatives aux bars » |
| Google Maps, TripAdvisor, TheFork, Untappd, Instagram, Facebook | CGU ou connexion obligatoire |
| Privateaser | CGU non vérifiables |
