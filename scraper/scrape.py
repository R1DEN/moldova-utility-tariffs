#!/usr/bin/env python3
"""Fetch ANRE tariff pages → data/v1/tariffs.json.

Usage:
  scrape.py [--fixtures DIR] [--today YYYY-MM-DD]

Exit codes: 0 ok (file may or may not have changed), 1 fetch/parse/sanity failure
(the previous JSON is left untouched, so consumers keep the last good data).

Writes the list of newly seen ANRE decisions without an override (effective
date etc.) to new_decisions.txt so the workflow can open an issue.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import anre  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "v1" / "tariffs.json"
OVERRIDES = ROOT / "data" / "overrides.json"
NEW_DECISIONS = ROOT / "new_decisions.txt"

SCHEMA_VERSION = 1
HISTORY_KEEP = 5  # entries per series, newest first
USER_AGENT = "moldova-utility-tariffs/0.1 (+https://github.com/R1DEN/moldova-utility-tariffs)"

PAGES = {
    "electricity": "https://anre.md/energie-electrica-3-290",
    "gas": "https://anre.md/gaze-naturale-3-205",
    "water": "https://anre.md/alimentare-cu-apa-si-canalizare-3-283",
}


def fetch(url: str) -> str:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ro"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


# ---------------------------------------------------------------- build


def entry_from(rec: dict, today: str, overrides: dict) -> dict:
    e = {}
    if "decision" in rec:
        d = dict(rec["decision"])
        ov = overrides.get("decisions", {}).get(d["number"], {})
        d["effective_from"] = ov.get("effective_from")
        if ov.get("note"):
            d["note"] = ov["note"]
        e["decision"] = d
    e["price"] = rec["price"]
    e["first_seen"] = today
    return e


def same_entry(a: dict, b: dict) -> bool:
    if "decision" in a or "decision" in b:
        return a.get("decision", {}).get("number") == b.get("decision", {}).get("number")
    return a["price"] == b["price"]


def merge(prev_series: dict | None, new_entry: dict, overrides: dict) -> list[dict]:
    """Return entries newest-first. A new decision (or new water prices) is
    prepended; the old ones stay so clients can honour future effective dates."""
    entries = copy.deepcopy(prev_series["entries"]) if prev_series else []
    if entries and same_entry(entries[0], new_entry):
        head = entries[0]
        head["price"] = new_entry["price"]  # same decision, refresh values
        if "decision" in new_entry:
            head["decision"] = new_entry["decision"]
    else:
        entries.insert(0, new_entry)
    # re-apply overrides to older entries too (effective dates get filled in later)
    for e in entries:
        if "decision" in e:
            ov = overrides.get("decisions", {}).get(e["decision"]["number"], {})
            if "effective_from" in ov:
                e["decision"]["effective_from"] = ov["effective_from"]
    return entries[:HISTORY_KEEP]


def active_entry(entries: list[dict], today: str) -> dict:
    """Newest entry already in force. Unknown effective date = in force (the
    ANRE page is titled 'tarife în vigoare')."""
    for e in entries:
        eff = e.get("decision", {}).get("effective_from")
        if eff is None or eff <= today:
            return e
    return entries[-1]


# ---------------------------------------------------------------- household selection

# Household VAT (Fiscal Code): electricity and water/sewerage are exempt with the
# right to deduction (art. 104 lit. b)), i.e. 0 for the consumer; natural gas has
# the reduced 8% rate (art. 96). ANRE prices are always excl. VAT.
VAT_RATE = {"electricity": 0.0, "gas": 0.08, "water": 0.0}


def with_vat(price, rate: float):
    if isinstance(price, dict):
        return {k: with_vat(v, rate) for k, v in price.items()}
    return round(price * (1 + rate), 4)


# universal-service suppliers (regulated household electricity), by licence area
ELECTRICITY_SUPPLIERS = {
    "premier_energy": {"name": "Premier Energy", "region": "center_south"},
    "furnizarea_energiei_electrice_nord": {"id": "fee_nord", "name": "FEE Nord", "region": "north"},
}
GAS_SUPPLIERS = {"energocom": {"name": "Energocom"}}


def household(parsed: dict) -> dict:
    """Reduce the full ANRE tables to what an ordinary household pays."""
    out: dict = {"electricity": {}, "gas": {}, "water": {}}
    for rec in parsed["electricity"]:
        meta = ELECTRICITY_SUPPLIERS.get(anre.slugify(rec["licensee"]))
        if rec["category"] != "universal_service" or not meta or "low_voltage" not in rec["prices"]:
            continue
        sid = meta.get("id", anre.slugify(rec["licensee"]))
        out["electricity"][sid] = {
            "name": meta["name"], "region": meta["region"], "unit": "MDL/kWh",
            "decision": rec["decision"], "price": rec["prices"]["low_voltage"],
        }
    for rec in parsed["gas"]:
        sid = anre.slugify(rec["licensee"])
        meta = GAS_SUPPLIERS.get(sid)
        if rec["category"] != "public_service_obligation" or not meta or "distribution_low" not in rec["prices"]:
            continue
        out["gas"][sid] = {
            "name": meta["name"], "unit": "MDL/m3",
            "decision": rec["decision"], "price": rec["prices"]["distribution_low"],
        }
    for rec in parsed["water"]:
        p = rec["prices"]
        price = {}
        if "water_household" in p:
            price["water"] = p["water_household"]
        elif "technical_water_household" in p or "technical_water" in p:
            # no drinking-water tariff in this town: non-potable supply
            price["technical_water"] = p.get("technical_water_household", p.get("technical_water"))
        if "sewerage_household" in p:
            price["sewerage"] = p["sewerage_household"]
        if price:
            out["water"][anre.slugify(rec["operator"])] = {
                "name": rec["operator"], "unit": "MDL/m3", "price": price,
            }
    return out


def build(pages: dict[str, str], prev: dict, overrides: dict, today: str) -> tuple[dict, list[str]]:
    parsed = {
        "electricity": anre.parse_electricity(pages["electricity"]),
        "gas": anre.parse_gas(pages["gas"]),
        "water": anre.parse_water(pages["water"]),
    }
    sanity(parsed)
    hh = household(parsed)
    missing = [k for k in ("premier_energy", "fee_nord") if k not in hh["electricity"]]
    missing += [k for k in GAS_SUPPLIERS if k not in hh["gas"]]
    if missing:
        raise SystemExit(f"SANITY CHECK FAILED: household suppliers not found: {missing}")

    prev_decisions = {
        e["decision"]["number"]
        for util in ("electricity", "gas")
        for s in prev.get(util, {}).values()
        for e in s.get("entries", [])
        if "decision" in e
    }
    out: dict = {}
    new_decisions: list[str] = []
    for util in ("electricity", "gas", "water"):
        out[util] = {}
        for sid, rec in hh[util].items():
            entries = merge(prev.get(util, {}).get(sid), entry_from(rec, today, overrides), overrides)
            if "decision" in rec:
                num = rec["decision"]["number"]
                if num not in prev_decisions and num not in overrides.get("decisions", {}) and num not in new_decisions:
                    new_decisions.append(num)
            for e in entries:
                e["price_final"] = with_vat(e["price"], VAT_RATE[util])
            act = active_entry(entries, today)
            meta = {k: v for k, v in rec.items() if k not in ("decision", "price")}
            out[util][sid] = {
                **meta,
                "vat_rate": VAT_RATE[util],
                "current": {k: act[k] for k in ("decision", "price", "price_final") if k in act},
                "entries": entries,
            }

    # No timestamps in the document: a git diff means a real tariff change.
    doc = {
        "schema_version": SCHEMA_VERSION,
        "source": dict(PAGES),
        "notes": overrides.get("notes", {}),
        **out,
    }
    return doc, new_decisions


def sanity(parsed: dict) -> None:
    """Fail loudly (→ no commit, red workflow, e-mail) instead of publishing garbage."""
    errs = []
    us = [r for r in parsed["electricity"] if r["category"] == "universal_service"]
    lv = [r["prices"].get("low_voltage", {}).get("flat") for r in us]
    if len(us) < 2 or not all(v and 1 <= v <= 20 for v in lv):
        errs.append(f"electricity universal_service low_voltage looks wrong: {lv}")
    pso = [r for r in parsed["gas"] if r["category"] == "public_service_obligation"]
    gl = [r["prices"].get("distribution_low") for r in pso]
    if not pso or not all(v and 5 <= v <= 100 for v in gl):
        errs.append(f"gas PSO distribution_low looks wrong: {gl}")
    w = parsed["water"]
    if len(w) < 20:
        errs.append(f"only {len(w)} water operators parsed")
    bad = [r["operator"] for r in w for v in r["prices"].values() if not (0 < v < 200)]
    if bad:
        errs.append(f"water prices out of range: {bad}")
    if errs:
        raise SystemExit("SANITY CHECK FAILED:\n  " + "\n  ".join(errs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=pathlib.Path, help="read <util>.html from here instead of fetching")
    ap.add_argument("--today", default=dt.datetime.now(dt.timezone(dt.timedelta(hours=2))).date().isoformat())
    a = ap.parse_args()

    if a.fixtures:
        pages = {k: (a.fixtures / f"{k}.html").read_text(encoding="utf-8") for k in PAGES}
    else:
        pages = {k: fetch(u) for k, u in PAGES.items()}

    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    doc, new = build(pages, prev, overrides, a.today)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    NEW_DECISIONS.write_text("".join(f"{n}\n" for n in new), encoding="utf-8")
    print(f"ok: {len(doc['electricity'])} electricity, {len(doc['gas'])} gas, {len(doc['water'])} water; "
          f"new decisions: {new or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
