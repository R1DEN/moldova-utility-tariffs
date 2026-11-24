import copy
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
import anre  # noqa: E402
import scrape  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
PAGES = {k: (FIX / f"{k}.html").read_text(encoding="utf-8") for k in ("electricity", "gas", "water")}
OV = {"decisions": {"578/2026": {"effective_from": "2026-10-01"}}}


class Parse(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(anre.parse_number("18 641"), 18641)
        self.assertEqual(anre.parse_number("80 723,40"), 80723.4)
        self.assertEqual(anre.parse_number(" 32.66 "), 32.66)
        self.assertIsNone(anre.parse_number(" - "))

    def test_slug(self):
        self.assertEqual(anre.slugify("Î.C.S. „Premier Energy” S.R.L."), "premier_energy")
        self.assertEqual(anre.slugify('S.A. "Apă-Canal Chişinău"'), "apa_canal_chisinau")

    def test_electricity(self):
        e = {(r["category"], anre.slugify(r["licensee"])): r for r in anre.parse_electricity(PAGES["electricity"])}
        pe = e[("universal_service", "premier_energy")]
        self.assertEqual(pe["prices"]["low_voltage"], {"flat": 3.56, "day": 3.75, "night": 2.94})
        self.assertEqual(pe["decision"]["number"], "222/2026")
        self.assertEqual(pe["decision"]["legis_doc_id"], 153764)
        self.assertEqual(e[("last_resort", "premier_energy")]["prices"]["low_voltage"]["flat"], 3.92)

    def test_gas(self):
        g = {(r["category"], anre.slugify(r["licensee"])): r for r in anre.parse_gas(PAGES["gas"])}
        en = g[("public_service_obligation", "energocom")]
        self.assertEqual(en["prices"]["distribution_low"], 24.202)
        self.assertEqual(en["decision"]["legis_doc_id"], 156575)
        self.assertEqual(g[("last_resort", "energocom")]["prices"]["distribution_low"], 26.255)
        dist = [r for r in g.values() if r["category"] == "distribution"]
        self.assertEqual(len(dist), 2)
        self.assertTrue(all(r["decision"]["legis_doc_id"] == 153388 for r in dist))

    def test_water(self):
        w = {anre.slugify(r["operator"]): r for r in anre.parse_water(PAGES["water"])}
        self.assertEqual(len(w), 44)
        self.assertEqual(w["apa_canal_chisinau"]["prices"]["water_household"], 14.03)
        self.assertNotIn("sewerage_household", w["regia_apa_canal_balti"]["prices"])
        self.assertEqual(w["apa_canal_anenii_noi"]["prices"]["technical_water_non_household"], 36.93)


class Build(unittest.TestCase):
    def test_household_shape(self):
        doc, new = scrape.build(PAGES, {}, OV, "2026-11-24")
        self.assertEqual(set(doc["electricity"]), {"premier_energy", "fee_nord"})
        self.assertEqual(doc["electricity"]["premier_energy"]["current"]["price"], {"flat": 3.56, "day": 3.75, "night": 2.94})
        self.assertEqual(doc["electricity"]["fee_nord"]["current"]["price"]["flat"], 3.95)
        self.assertEqual(doc["electricity"]["fee_nord"]["region"], "north")
        self.assertEqual(set(doc["gas"]), {"energocom"})
        self.assertEqual(doc["gas"]["energocom"]["current"]["price"], 24.202)
        self.assertEqual(doc["water"]["apa_canal_chisinau"]["current"]["price"], {"water": 14.03, "sewerage": 6.63})
        self.assertEqual(doc["water"]["regia_apa_canal_balti"]["current"]["price"], {"water": 27.4})
        self.assertEqual(doc["water"]["apa_canal_anenii_noi"]["current"]["price"], {"technical_water": 17.85, "sewerage": 25.5})
        self.assertNotIn("acva_nord_soroca", doc["water"])  # bulk-only operator, no household tariff
        self.assertEqual(sorted(new), ["222/2026", "224/2026"])  # 578 has an override

    def test_new_decision_keeps_history_and_honours_future_date(self):
        doc1, _ = scrape.build(PAGES, {}, OV, "2026-11-24")
        gas = PAGES["gas"].replace("HCA nr. 578 din 28.09.2026", "HCA nr. 700 din 20.12.2026").replace("24 202", "25 000")
        ov = copy.deepcopy(OV)
        doc2, new2 = scrape.build({**PAGES, "gas": gas}, doc1, ov, "2026-12-21")
        self.assertEqual(new2, ["700/2026"])
        s = doc2["gas"]["energocom"]
        self.assertEqual([e["decision"]["number"] for e in s["entries"]], ["700/2026", "578/2026"])
        self.assertEqual(s["current"]["price"], 25.0)  # unknown date → in force
        ov["decisions"]["700/2026"] = {"effective_from": "2027-01-01"}
        doc3, _ = scrape.build({**PAGES, "gas": gas}, doc2, ov, "2026-12-22")
        self.assertEqual(doc3["gas"]["energocom"]["current"]["price"], 24.202)  # not yet in force
        doc4, _ = scrape.build({**PAGES, "gas": gas}, doc3, ov, "2027-01-01")
        self.assertEqual(doc4["gas"]["energocom"]["current"]["price"], 25.0)

    def test_water_price_change_adds_entry(self):
        doc1, _ = scrape.build(PAGES, {}, OV, "2026-11-24")
        water = PAGES["water"].replace(">14,03<", ">15,00<", 1)
        doc2, _ = scrape.build({**PAGES, "water": water}, doc1, OV, "2026-11-25")
        s = doc2["water"]["apa_canal_chisinau"]
        self.assertEqual(s["current"]["price"]["water"], 15.0)
        self.assertEqual(len(s["entries"]), 2)

    def test_idempotent(self):
        doc1, _ = scrape.build(PAGES, {}, OV, "2026-11-24")
        doc2, new = scrape.build(PAGES, json.loads(json.dumps(doc1)), OV, "2026-11-25")
        self.assertEqual(doc1, doc2)
        self.assertEqual(new, [])

    def test_sanity_rejects_garbage(self):
        with self.assertRaises(SystemExit):
            scrape.build({**PAGES, "water": "<table></table>"}, {}, OV, "2026-11-24")


if __name__ == "__main__":
    unittest.main()
