/*
 * Tests de la logique horaire / happy hour d'app.js sur le jeu de données FICTIF.
 * Lancer : node scripts/test_logique.js
 */
const assert = require("assert");
const path = require("path");
const app = require(path.join(__dirname, "..", "app.js"));
const { bars } = require(path.join(__dirname, "..", "data", "bars.fictif.json"));

const POSITION = { lat: 48.8650, lng: 2.3740 }; // Oberkampf
const t = (heure) => {
  const n = app.maintenantParis(new Date(), heure);
  return n.jour * 1440 + n.minutes;
};
const ids = (liste) => liste.map((x) => x.bar.id);
const parId = (analyses, id) => analyses.find((x) => x.bar.id === id);

let ok = 0;
function test(nom, fn) {
  try { fn(); ok++; console.log("  ok  " + nom); }
  catch (e) { console.log("ÉCHEC " + nom + "\n      " + e.message); process.exitCode = 1; }
}

console.log("Fuseau Europe/Paris");
test("15:50 UTC un dimanche d'octobre = dimanche 17h50 à Paris (heure d'été)", () => {
  assert.deepStrictEqual(app.maintenantParis(new Date("2026-10-04T15:50:00Z")), { jour: 6, minutes: 17 * 60 + 50, simule: false });
});
test("31/12 23:30 UTC = vendredi 1er janvier 0h30 à Paris (heure d'hiver)", () => {
  assert.deepStrictEqual(app.maintenantParis(new Date("2026-12-31T23:30:00Z")), { jour: 4, minutes: 30, simule: false });
});
test("heure simulée ?heure=2026-10-05T09:05 = lundi 9h05", () => {
  assert.deepStrictEqual(app.maintenantParis(new Date(), "2026-10-05T09:05"), { jour: 0, minutes: 545, simule: true });
});

console.log("Dimanche 17h45");
{
  const a = app.analyserBars(bars, POSITION, t("2026-10-04T17:45"));
  const top = app.selectionnerPasChers(a);
  test("le bar fermé le dimanche est exclu", () => assert(!parId(a, "fictif-04")));
  test("Le Noctambule (ouvre à 18h) est exclu", () => assert(!parId(a, "fictif-03")));
  test("happy hour 17h-20h active : 3,50 €", () => {
    const x = parId(a, "fictif-02");
    assert.strictEqual(x.prix, 3.5);
    assert.strictEqual(x.happyHour.debut, "17:00");
  });
  test("top 3 = 3 bars <= 4 €, triés par distance", () => {
    assert.strictEqual(top.length, 3);
    top.forEach((x) => assert(x.prix <= 4));
    assert(top[0].marche <= top[1].marche && top[1].marche <= top[2].marche);
  });
  test("le bar cher n'est pas dans le top", () => assert(!ids(top).includes("fictif-07")));
  test("bar aux horaires inconnus gardé (ouvert = null)", () => assert.strictEqual(parId(a, "fictif-06").ouvert, null));
  test("happy hour de 18h signalée 'bientôt' (dans 15 min), hors top 3", () => {
    const b = app.selectionnerBientot(a);
    assert.deepStrictEqual(ids(b), ["fictif-10"]);
    assert.strictEqual(b[0].bientot.delai, 15);
    assert(!ids(top).includes("fictif-10"));
  });
}

console.log("Plages qui passent minuit");
test("dimanche 0h30 : le bar 'fermé le dimanche' est encore ouvert (suite du samedi 11h-1h)", () => {
  assert(parId(app.analyserBars(bars, POSITION, t("2026-10-04T00:30")), "fictif-04"));
});
test("dimanche 0h30 : happy hour du samedi 22h-2h active au Noctambule (4 €)", () => {
  const x = parId(app.analyserBars(bars, POSITION, t("2026-10-04T00:30")), "fictif-03");
  assert.strictEqual(x.prix, 4);
  assert(x.happyHour);
});
test("lundi 0h30 : happy hour dim 23h -> lun 1h active (tour de la semaine)", () => {
  const x = parId(app.analyserBars(bars, POSITION, t("2026-10-05T00:30")), "fictif-11");
  assert.strictEqual(x.prix, 3);
});
test("lundi 1h00 : cette happy hour est terminée (fin exclue)", () => {
  const x = parId(app.analyserBars(bars, POSITION, t("2026-10-05T01:00")), "fictif-11");
  assert.strictEqual(x.prix, 6);
});
test("vendredi 3h : Le Noctambule fermé (jeudi 18h-2h)", () => {
  assert(!parId(app.analyserBars(bars, POSITION, t("2026-10-09T03:00")), "fictif-03"));
});
test("samedi 3h : Le Noctambule ouvert (vendredi 18h-5h) mais happy hour finie : 7 €", () => {
  assert.strictEqual(parId(app.analyserBars(bars, POSITION, t("2026-10-10T03:00")), "fictif-03").prix, 7);
});

console.log("Happy hours à venir");
test("dimanche 22h30 : happy hour de 23h signalée (dans 30 min)", () => {
  const b = app.selectionnerBientot(app.analyserBars(bars, POSITION, t("2026-10-04T22:30")));
  assert(ids(b).includes("fictif-11"));
});
test("vendredi 21h30 : happy hour de 22h à 4 € pile signalée", () => {
  const b = app.selectionnerBientot(app.analyserBars(bars, POSITION, t("2026-10-09T21:30")));
  assert(ids(b).includes("fictif-03"));
});
test("vendredi 20h30 : happy hour de 22h trop lointaine (90 min), pas signalée", () => {
  const b = app.selectionnerBientot(app.analyserBars(bars, POSITION, t("2026-10-09T20:30")));
  assert(!ids(b).includes("fictif-03"));
});
test("happy hour qui commence alors que le bar sera fermé : pas signalée", () => {
  const bar = {
    id: "x", nom: "x", adresse: "x", lat: 48.865, lng: 2.374, prix_pinte_normal: 6,
    horaires_ouverture: [{ jours: ["lun"], debut: "19:00", fin: "23:00" }],
    happy_hours: [{ jours: ["lun"], debut: "18:00", fin: "20:00", prix: 3 }],
  };
  assert.strictEqual(app.happyHourBientot(bar, t("2026-10-05T17:30")), null);
});

test("bar fermé qui ouvre à 18h avec happy hour : signalé 'bientôt' à 17h30, absent du top", () => {
  const bar = {
    id: "y", nom: "y", adresse: "y", lat: 48.865, lng: 2.374, prix_pinte_normal: 7,
    horaires_ouverture: [{ jours: ["ven"], debut: "18:00", fin: "02:00" }],
    happy_hours: [{ jours: ["ven"], debut: "18:00", fin: "20:00", prix: 4 }],
  };
  const a = app.analyserBars([bar], POSITION, t("2026-10-09T17:30"));
  assert.deepStrictEqual(ids(app.selectionnerBientot(a)), ["y"]);
  assert.deepStrictEqual(ids(app.selectionnerPasChers(a)), []);
  assert.deepStrictEqual(ids(app.selectionnerMoinsChers(a)), []);
});

test("happy hour imminente mais à plus de 1,5 km : pas signalée", () => {
  const loin = { lat: 48.8800, lng: 2.3740 }; // ~1,6 km au nord, soit ~2 km à pied
  const a = app.analyserBars(bars, loin, t("2026-10-04T17:45"));
  assert(!ids(app.selectionnerBientot(a)).includes("fictif-10"));
});

console.log("Repli et cas vides");
test("lundi 9h : aucun bar ouvert à 4 € ou moins", () => {
  const top = app.selectionnerPasChers(app.analyserBars(bars, POSITION, t("2026-10-05T09:00")));
  assert.deepStrictEqual(ids(top), []);
});
test("'les moins chers' : triés par prix croissant", () => {
  const m = app.selectionnerMoinsChers(app.analyserBars(bars, POSITION, t("2026-10-05T09:00")));
  for (let i = 1; i < m.length; i++) assert(m[i - 1].prix <= m[i].prix);
  assert.strictEqual(m[0].bar.id, "fictif-06"); // 4,20 €, horaires inconnus
});

console.log("Mise en forme");
test("formatJours", () => {
  assert.strictEqual(app.formatJours(["lun", "mar", "mer", "jeu", "ven"]), "lun-ven");
  assert.strictEqual(app.formatJours(["ven", "sam"]), "ven, sam");
  assert.strictEqual(app.formatJours(["lun", "mer", "jeu", "ven", "dim"]), "lun, mer-ven, dim");
  assert.strictEqual(app.formatJours(["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]), "tous les jours");
});
test("formatHeure / formatPrix", () => {
  assert.strictEqual(app.formatHeure("17:00"), "17h");
  assert.strictEqual(app.formatHeure(1530), "1h30"); // 25h30 -> 1h30
  assert.strictEqual(app.formatPrix(3.5).replace(/\s/g, " "), "3,50 €");
  assert.strictEqual(app.formatPrix(4).replace(/\s/g, " "), "4 €");
});

console.log(`\n${ok} test(s) réussi(s)${process.exitCode ? ", des échecs ci-dessus" : ""}.`);
