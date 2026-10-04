/*
 * Pinte pas chère — logique du site.
 *
 * 1. Charge data/bars.json
 * 2. Obtient la position (géolocalisation ou adresse géocodée)
 * 3. Calcule pour chaque bar : ouvert ?, prix effectif maintenant (happy hour ou normal), distance
 * 4. Affiche les 3 bars les plus proches à 4 € ou moins
 *
 * Toutes les heures sont calculées dans le fuseau Europe/Paris, quel que soit celui de l'appareil.
 * Astuces de test : ajouter ?heure=2026-10-04T17:45 à l'URL pour simuler une heure (heure de Paris),
 * et ?donnees=fictif pour utiliser le jeu de données de test.
 */

/* ===================== Réglages ===================== */

const PRIX_MAX = 4;            // € : seuil d'une "pinte pas chère"
const NB_RESULTATS = 3;
const FENETRE_BIENTOT = 60;    // minutes : on signale une happy hour qui commence dans moins de 60 min
const DISTANCE_BIENTOT = 1500; // mètres à pied : au-delà, une happy hour imminente n'est pas signalée
const FACTEUR_MARCHE = 1.3;    // la marche réelle est ~30 % plus longue que la ligne droite
const VITESSE_MARCHE = 80;     // mètres par minute (~4,8 km/h)
const PARIS = { lat: 48.8566, lng: 2.3522 };

const JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]; // index 0 = lundi
const JOURS_LONGS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
const MINUTES_SEMAINE = 7 * 1440;

/* ===================== Heure de Paris ===================== */

/**
 * Renvoie l'instant présent à Paris sous la forme { jour: 0..6 (0 = lundi), minutes: 0..1439 }.
 * Si l'URL contient ?heure=AAAA-MM-JJTHH:MM, cette heure (de Paris) est utilisée à la place.
 */
function maintenantParis(date = new Date(), heureSimulee = null) {
  if (heureSimulee) {
    const m = heureSimulee.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{1,2}):(\d{2})$/);
    if (m) {
      const [, a, mo, j, h, mi] = m.map(Number);
      const jourJs = new Date(Date.UTC(a, mo - 1, j)).getUTCDay(); // 0 = dimanche
      return { jour: (jourJs + 6) % 7, minutes: h * 60 + mi, simule: true };
    }
  }
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Paris", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date);
  const val = (type) => parts.find((p) => p.type === type).value;
  const jour = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(val("weekday"));
  return { jour, minutes: Number(val("hour")) * 60 + Number(val("minute")), simule: false };
}

/** "17:30" -> 1050 ; "24:00" -> 1440 */
function enMinutes(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

/** Index des jours d'une plage : ["lun","mar"] -> [0, 1] */
function indexJours(jours) {
  return jours.map((j) => JOURS.indexOf(j)).filter((i) => i >= 0);
}

/**
 * Une plage { jours, debut, fin } commence chaque jour listé à `debut`.
 * Si fin <= debut, elle déborde sur le lendemain (ex. ven 22:00 -> sam 02:00).
 * Renvoie la liste des intervalles [début, fin[ en minutes depuis lundi 00:00.
 */
function intervallesSemaine(plage) {
  const debut = enMinutes(plage.debut);
  let fin = enMinutes(plage.fin);
  if (fin <= debut) fin += 1440; // passe minuit (ou 24 h si debut == fin)
  return indexJours(plage.jours).map((j) => [j * 1440 + debut, j * 1440 + fin]);
}

/** Vrai si l'instant `t` (minutes depuis lundi 00:00) tombe dans la plage, en gérant le tour de semaine. */
function dansPlage(plage, t) {
  return intervallesSemaine(plage).some(([d, f]) =>
    (t >= d && t < f) || (t + MINUTES_SEMAINE >= d && t + MINUTES_SEMAINE < f) // dim 23h -> lun 2h
  );
}

/** Minutes avant le prochain début de la plage (0 < délai <= 7 jours). */
function minutesAvantDebut(plage, t) {
  const debuts = intervallesSemaine(plage).map(([d]) => (d - t + MINUTES_SEMAINE) % MINUTES_SEMAINE);
  const futurs = debuts.filter((d) => d > 0);
  return futurs.length ? Math.min(...futurs) : null;
}

/** Fin de la plage active à l'instant t, en minutes depuis lundi (pour afficher "jusqu'à 20h"). */
function finPlageActive(plage, t) {
  for (const [d, f] of intervallesSemaine(plage)) {
    if (t >= d && t < f) return f;
    if (t + MINUTES_SEMAINE >= d && t + MINUTES_SEMAINE < f) return f - MINUTES_SEMAINE;
  }
  return null;
}

/**
 * Le bar est-il ouvert à l'instant t ?
 * Renvoie true / false, ou null si les horaires sont inconnus.
 */
function estOuvert(bar, t) {
  if (!Array.isArray(bar.horaires_ouverture) || bar.horaires_ouverture.length === 0) return null;
  return bar.horaires_ouverture.some((p) => dansPlage(p, t));
}

/**
 * Prix effectif d'un bar à l'instant t.
 * Renvoie { prix, happyHour (plage active ou null), finHH (minutes depuis lundi ou null) }.
 * Si plusieurs happy hours se chevauchent, on prend la moins chère.
 */
function prixEffectif(bar, t) {
  let meilleur = { prix: bar.prix_pinte_normal, happyHour: null, finHH: null };
  for (const hh of bar.happy_hours || []) {
    if (dansPlage(hh, t) && (meilleur.prix == null || hh.prix < meilleur.prix)) {
      meilleur = { prix: hh.prix, happyHour: hh, finHH: finPlageActive(hh, t) };
    }
  }
  return meilleur;
}

/**
 * Happy hour qui commence dans les FENETRE_BIENTOT prochaines minutes avec un prix <= PRIX_MAX,
 * à condition que le bar soit ouvert (ou horaires inconnus) à ce moment-là. Sinon null.
 */
function happyHourBientot(bar, t) {
  let retenue = null;
  for (const hh of bar.happy_hours || []) {
    if (hh.prix > PRIX_MAX || dansPlage(hh, t)) continue;
    const delai = minutesAvantDebut(hh, t);
    if (delai == null || delai > FENETRE_BIENTOT) continue;
    const tDebut = (t + delai) % MINUTES_SEMAINE;
    if (estOuvert(bar, tDebut) === false) continue;
    if (!retenue || delai < retenue.delai) retenue = { hh, delai, tDebut };
  }
  return retenue;
}

/* ===================== Distance ===================== */

/** Distance à vol d'oiseau en mètres (formule de haversine). */
function distanceMetres(a, b) {
  const R = 6371000;
  const rad = (x) => (x * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

/* ===================== Sélection des bars ===================== */

/**
 * Calcule l'état de chaque bar à l'instant t pour une position donnée.
 * On garde les bars ouverts (ou aux horaires inconnus) qui ont un prix maintenant,
 * plus ceux, même fermés, dont la happy hour commence bientôt (ex. ouverture à 18h avec happy hour).
 */
function analyserBars(bars, position, t) {
  return bars
    .filter((bar) => typeof bar.prix_pinte_normal === "number" || (bar.happy_hours || []).length)
    .map((bar) => {
      const ouvert = estOuvert(bar, t);
      const { prix, happyHour, finHH } = prixEffectif(bar, t);
      const marche = distanceMetres(position, bar) * FACTEUR_MARCHE;
      return { bar, ouvert, prix, happyHour, finHH, marche, bientot: happyHourBientot(bar, t) };
    })
    .filter((x) => (x.ouvert !== false && typeof x.prix === "number") || x.bientot)
    .sort((a, b) => a.marche - b.marche);
}

/** Les NB_RESULTATS bars les plus proches avec un prix effectif <= PRIX_MAX. */
function selectionnerPasChers(analyses) {
  return analyses.filter((x) => x.ouvert !== false && typeof x.prix === "number" && x.prix <= PRIX_MAX).slice(0, NB_RESULTATS);
}

/**
 * Bars hors top (trop chers ou fermés maintenant) dont la happy hour (<= 4 €) commence bientôt,
 * à distance raisonnable : 1,5 km à pied, ou jusqu'au plus lointain des bars du top s'il est plus loin.
 */
function selectionnerBientot(analyses, top = []) {
  const horsTop = (x) => x.ouvert === false || typeof x.prix !== "number" || x.prix > PRIX_MAX;
  const limite = Math.max(DISTANCE_BIENTOT, ...top.map((x) => x.marche));
  return analyses.filter((x) => horsTop(x) && x.bientot && x.marche <= limite).slice(0, NB_RESULTATS);
}

/** Repli : parmi les 10 bars ouverts les plus proches, les moins chers. */
function selectionnerMoinsChers(analyses) {
  return analyses.filter((x) => x.ouvert !== false && typeof x.prix === "number").slice(0, 10).sort((a, b) => a.prix - b.prix || a.marche - b.marche).slice(0, NB_RESULTATS);
}

/* ===================== Mise en forme ===================== */

function formatPrix(p) {
  return new Intl.NumberFormat("fr-FR", {
    style: "currency", currency: "EUR", minimumFractionDigits: Number.isInteger(p) ? 0 : 2,
  }).format(p);
}

/** 1050 -> "17h30" ; 1020 -> "17h" ; accepte aussi "17:30" */
function formatHeure(x) {
  const min = typeof x === "string" ? enMinutes(x) : x;
  const m = ((min % 1440) + 1440) % 1440;
  const h = Math.floor(m / 60), mn = m % 60;
  return mn ? `${h}h${String(mn).padStart(2, "0")}` : `${h}h`;
}

/** ["lun","mar","mer","jeu","ven"] -> "lun-ven" ; 7 jours -> "tous les jours" */
function formatJours(jours) {
  const idx = [...new Set(indexJours(jours))].sort((a, b) => a - b);
  if (idx.length === 7) return "tous les jours";
  const blocs = [];
  for (const i of idx) {
    const dernier = blocs[blocs.length - 1];
    if (dernier && i === dernier[1] + 1) dernier[1] = i;
    else blocs.push([i, i]);
  }
  return blocs
    .map(([a, b]) => (a === b ? JOURS[a] : b === a + 1 ? `${JOURS[a]}, ${JOURS[b]}` : `${JOURS[a]}-${JOURS[b]}`))
    .join(", ");
}

function formatPlage(p) {
  return `${formatJours(p.jours)} ${formatHeure(p.debut)}-${formatHeure(p.fin)}`;
}

function formatDistance(m) {
  const arrondi = Math.round(m / 10) * 10;
  const txt = arrondi < 1000 ? `${arrondi} m` : `${(m / 1000).toFixed(1).replace(".", ",")} km`;
  const minutes = Math.max(1, Math.round(m / VITESSE_MARCHE));
  return minutes > 90 ? `~${txt} à pied` : `~${txt} · ${minutes} min à pied`;
}

function formatDate(iso) {
  if (!iso) return "date inconnue";
  const [a, m, j] = iso.split("-");
  return `${j}/${m}/${a}`;
}

const LIBELLES_CONFIANCE = {
  verifie: "Prix vérifié",
  estime: "Prix estimé",
  incertain: "Prix incertain",
};

/** Petit utilitaire pour créer un élément DOM. */
function el(tag, props = {}, ...enfants) {
  const n = document.createElement(tag);
  Object.assign(n, props);
  for (const e of enfants) if (e != null) n.append(e);
  return n;
}

function liensItineraire(bar) {
  const dest = `${bar.nom}, ${bar.adresse}`;
  return {
    google: `https://www.google.com/maps/dir/?api=1&travelmode=walking&destination=${encodeURIComponent(dest)}`,
    apple: `https://maps.apple.com/?dirflg=w&daddr=${encodeURIComponent(bar.adresse)}&q=${encodeURIComponent(bar.nom)}`,
  };
}

/** Construit la carte d'un bar. */
function carteBar(x) {
  const { bar } = x;
  const liens = liensItineraire(bar);
  const etiquettes = el("div", { className: "etiquettes" });

  if (x.happyHour) {
    etiquettes.append(el("span", { className: "etiquette hh", textContent: `Happy hour jusqu'à ${formatHeure(x.finHH)}` }));
  }
  const conf = bar.confiance in LIBELLES_CONFIANCE ? bar.confiance : "incertain";
  etiquettes.append(el("span", { className: `etiquette ${conf}`, textContent: LIBELLES_CONFIANCE[conf] }));
  if (x.ouvert === null) etiquettes.append(el("span", { className: "etiquette", textContent: "Horaires non vérifiés" }));

  const details = el("div", { className: "bar-details" });
  if (x.happyHour) {
    const normal = typeof bar.prix_pinte_normal === "number" ? ` (sinon ${formatPrix(bar.prix_pinte_normal)})` : "";
    details.append(el("p", { textContent: `Happy hour ${formatPlage(x.happyHour)}${normal}` }));
  } else if (x.bientot) {
    details.append(el("p", {
      textContent: `Happy hour à ${formatHeure(x.bientot.hh.debut)} : pinte à ${formatPrix(x.bientot.hh.prix)}`,
    }));
  }
  if (bar.note_prix) details.append(el("p", { textContent: bar.note_prix }));
  // Liens vers la ou les sources du prix, nommées par leur domaine (ex. "lebonbon.fr")
  const urls = [bar.source_url, ...(bar.autres_sources || [])].filter(Boolean);
  const liensSources = urls.flatMap((url, i) => {
    let nom = "source";
    try { nom = new URL(url).hostname.replace(/^www\./, ""); } catch { /* lien invalide */ }
    return [i ? ", " : "", el("a", { href: url, target: "_blank", rel: "noopener", textContent: nom })];
  });
  details.append(el("p", {}, `Dernière vérification : ${formatDate(bar.date_verification)}`,
    ...(liensSources.length ? [` · source${urls.length > 1 ? "s" : ""} : `, ...liensSources] : [])));

  return el("li", { className: "bar" },
    el("div", { className: "bar-haut" },
      el("div", {},
        el("h3", { className: "bar-nom", textContent: bar.nom }),
        el("p", { className: "bar-adresse", textContent: bar.adresse }),
      ),
      el("div", { className: "bar-prix" },
        el("span", { className: "montant", textContent: formatPrix(x.prix) }),
        el("span", { className: "unite", textContent: "la pinte" }),
      ),
    ),
    el("p", { className: "bar-distance", textContent: formatDistance(x.marche) }),
    etiquettes,
    details,
    el("div", { className: "bar-actions" },
      el("a", { className: "aller", href: liens.google, target: "_blank", rel: "noopener", textContent: "Y aller" }),
      el("a", { className: "apple", href: liens.apple, target: "_blank", rel: "noopener", textContent: "Plans (Apple)" }),
    ),
  );
}

/* ===================== Interface ===================== */

if (typeof document !== "undefined") {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const heureSimulee = params.get("heure");
  // ?donnees=fictif charge le jeu de test (data/bars.fictif.json) au lieu des vraies données
  const fichierDonnees = params.get("donnees") === "fictif" ? "data/bars.fictif.json" : "data/bars.json";
  let donnees = null;          // contenu de bars.json
  let derniereAnalyse = null;  // pour le bouton "voir les moins chers"

  /** Affiche un message d'état (ou le masque si texte vide). */
  function message(texte, erreur = false) {
    const m = $("message");
    m.hidden = !texte;
    m.textContent = texte || "";
    m.classList.toggle("erreur", erreur);
  }

  /** Charge bars.json une seule fois. */
  async function chargerDonnees() {
    if (donnees) return donnees;
    try {
      const rep = await fetch(fichierDonnees, { cache: "no-cache" });
      if (!rep.ok) throw new Error(rep.status);
      donnees = await rep.json();
    } catch (e) {
      throw new Error(location.protocol === "file:"
        ? "Impossible de lire les données en ouvrant le fichier directement. Lancez le site avec un petit serveur local (voir README)."
        : "Impossible de charger la liste des bars. Réessayez dans un instant.");
    }
    chargerStations(); // en parallèle, sans bloquer : sert aux suggestions du champ de recherche
    $("bandeau-fictif").hidden = !donnees.meta?.fictif;
    const meta = donnees.meta || {};
    $("info-donnees").textContent =
      `${(donnees.bars || []).length} bars avec un prix connu` +
      (meta.perimetre ? ` · zone couverte : ${meta.perimetre}` : "") +
      (meta.genere_le ? ` · données mises à jour le ${formatDate(meta.genere_le.slice(0, 10))}` : "");
    return donnees;
  }

  /**
   * Stations de métro, tram et RER (data/stations.json) : proposées pendant la saisie et
   * reconnues sans appel réseau, donc utilisables même si le service d'adresses ne répond pas.
   */
  let stations = [];
  async function chargerStations() {
    try {
      stations = await (await fetch("data/stations.json")).json();
      $("liste-stations").replaceChildren(...stations.map((s) => el("option", { value: `Métro ${s.nom}` })));
    } catch { /* pas de suggestions : la recherche d'adresse reste disponible */ }
  }

  /** "métro Bastille", "Bastille", "station bastille" -> la station Bastille (ou null). */
  function trouverStation(q) {
    const norm = (x) => x.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/^(metro|station|rer|tram)\s+/, "").replace(/[^a-z0-9]+/g, " ").trim();
    const cible = norm(q);
    return stations.find((s) => norm(s.nom) === cible) || null;
  }

  /** Lance la recherche à partir d'une position { lat, lng } et d'un libellé. */
  async function rechercher(position, libelle) {
    try {
      const { bars } = await chargerDonnees();
      const now = maintenantParis(new Date(), heureSimulee);
      const t = now.jour * 1440 + now.minutes;
      const analyses = analyserBars(bars, position, t);
      derniereAnalyse = analyses;
      afficher(analyses, libelle, now);
      message("");
    } catch (e) {
      message(e.message, true);
    }
  }

  function afficher(analyses, libelle, now) {
    const top = selectionnerPasChers(analyses);
    const bientot = selectionnerBientot(analyses, top);

    $("resultats").hidden = false;
    $("contexte").textContent =
      `Autour de ${libelle} · ${JOURS_LONGS[now.jour]} ${formatHeure(now.minutes)}` +
      (now.simule ? " (heure simulée)" : "") +
      (top.length && top.length < NB_RESULTATS ? ` · seulement ${top.length} bar${top.length > 1 ? "s" : ""} trouvé${top.length > 1 ? "s" : ""}` : "");

    $("liste-bars").replaceChildren(...top.map(carteBar));
    $("aucun-resultat").hidden = top.length > 0;
    $("btn-plus-chers").hidden = top.length > 0 || selectionnerMoinsChers(analyses).length === 0;
    $("bloc-plus-chers").hidden = true;

    // Prévenir si l'utilisateur est loin de la zone couverte par les données
    if (top.length && top[0].marche > 3000) {
      $("contexte").textContent += ` · attention, le plus proche est loin : la base ne couvre que ${donnees.meta?.perimetre || "une partie de Paris"}`;
    }

    // Happy hours qui commencent bientôt
    $("bloc-bientot").hidden = bientot.length === 0;
    $("liste-bientot").replaceChildren(...bientot.map((x) =>
      el("li", {},
        el("strong", { textContent: `${x.bar.nom} — happy hour à ${formatHeure(x.bientot.hh.debut)} : pinte à ${formatPrix(x.bientot.hh.prix)}` }),
        el("span", { className: "detail", textContent: `Dans ${x.bientot.delai} min · ${formatDistance(x.marche)} · ${x.bar.adresse}` +
          (x.ouvert === false ? " · fermé pour l'instant"
            : typeof x.prix === "number" ? ` · actuellement ${formatPrix(x.prix)}` : "") }),
      )
    ));

    $("resultats").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* --- Bouton "Me localiser" --- */
  $("btn-localiser").addEventListener("click", () => {
    if (!("geolocation" in navigator) || !window.isSecureContext) {
      message("La géolocalisation n'est pas disponible sur ce navigateur. Saisissez une adresse ci-dessous.", true);
      $("champ-adresse").focus();
      return;
    }
    const btn = $("btn-localiser");
    btn.disabled = true;
    message("Localisation en cours…");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        btn.disabled = false;
        rechercher({ lat: pos.coords.latitude, lng: pos.coords.longitude }, "votre position");
      },
      (err) => {
        btn.disabled = false;
        const raisons = {
          1: "La géolocalisation est refusée ou bloquée sur cette page.",
          2: "Votre position est introuvable pour le moment.",
          3: "La localisation a pris trop de temps.",
        };
        message(`${raisons[err.code] || "La géolocalisation a échoué."} Saisissez plutôt une adresse ou une station de métro.`, true);
        $("champ-adresse").focus();
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
    );
  });

  /* --- Champ adresse --- */
  $("form-adresse").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("champ-adresse").value.trim();
    if (q.length < 3 && !trouverStation(q)) {
      message("Saisissez une adresse ou une station un peu plus précise.", true);
      return;
    }
    const station = trouverStation(q);
    if (station) {
      rechercher(station, `la station ${station.nom}`);
      return;
    }
    message("Recherche de l'adresse…");
    try {
      const lieu = await geocoder(q);
      if (!lieu) {
        message("Adresse introuvable dans Paris. Essayez avec le numéro, la rue et l'arrondissement.", true);
        return;
      }
      rechercher(lieu, lieu.libelle);
    } catch {
      message("Le service d'adresses ne répond pas. Choisissez plutôt une station de métro dans la liste proposée.", true);
    }
  });

  /**
   * Géocode une adresse : API Adresse (BAN) d'abord, son miroir Géoplateforme ensuite,
   * puis Nominatim (OpenStreetMap) en secours. Renvoie { lat, lng, libelle } ou null.
   */
  async function geocoder(q) {
    // Le site ne couvre que Paris : on ne garde que les résultats dans Paris (codes INSEE 75101 à 75120)
    const params = `q=${encodeURIComponent(q)}&limit=5&lat=${PARIS.lat}&lon=${PARIS.lng}`;
    const sources = [
      `https://api-adresse.data.gouv.fr/search/?${params}`,
      `https://data.geopf.fr/geocodage/search?${params}`,
    ];
    let joignable = false;
    for (const url of sources) {
      try {
        const rep = await fetch(url);
        if (!rep.ok) continue;
        joignable = true;
        const f = (await rep.json()).features?.find((x) => x.properties.citycode?.startsWith("751") && x.properties.score > 0.3);
        if (f) {
          const [lng, lat] = f.geometry.coordinates;
          return { lat, lng, libelle: f.properties.label };
        }
        break; // le service répond mais ne trouve rien dans Paris : on passe à Nominatim
      } catch { /* service suivant */ }
    }
    try {
      const rep = await fetch(
        `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=fr&accept-language=fr` +
        `&viewbox=2.22,48.91,2.47,48.81&bounded=1&q=${encodeURIComponent(q)}`
      );
      if (rep.ok) {
        joignable = true;
        const r = (await rep.json())[0];
        if (r) return { lat: Number(r.lat), lng: Number(r.lon), libelle: r.display_name.split(",").slice(0, 3).join(",") };
      }
    } catch { /* aucun service */ }
    if (!joignable) throw new Error("géocodage indisponible");
    return null;
  }

  /* --- Bouton "Voir les moins chers même au-dessus de 4 €" --- */
  $("btn-plus-chers").addEventListener("click", () => {
    if (!derniereAnalyse) return;
    $("liste-plus-chers").replaceChildren(...selectionnerMoinsChers(derniereAnalyse).map(carteBar));
    $("bloc-plus-chers").hidden = false;
    $("btn-plus-chers").hidden = true;
  });

  // Pré-charge les données (affiche aussi le bandeau "fictif" et la date de mise à jour)
  chargerDonnees().catch((e) => message(e.message, true));
}

/* Export pour les tests en Node (scripts/test_logique.js) ; ignoré par le navigateur. */
if (typeof module !== "undefined") {
  module.exports = {
    maintenantParis, dansPlage, estOuvert, prixEffectif, happyHourBientot, minutesAvantDebut,
    analyserBars, selectionnerPasChers, selectionnerBientot, selectionnerMoinsChers,
    formatJours, formatHeure, formatPrix, distanceMetres, PRIX_MAX,
  };
}
