# Moldova utility tariffs

Regulated **household** electricity, natural gas and water/sewerage tariffs in the Republic of Moldova, scraped once a day from the ANRE "tarife în vigoare" pages and published as one static JSON file. Built for Home Assistant, usable by anything.

> **Status: proof of concept.** The schema (`v1`) may still change.

## Endpoint

```
https://raw.githubusercontent.com/R1DEN/moldova-utility-tariffs/main/data/v1/tariffs.json
```

- No server, no API key, no tracking: a GitHub Action runs once a day and commits only when a tariff actually changed.
- Git history of `data/v1/tariffs.json` = tariff history.
- If ANRE changes its page layout, sanity checks fail the run and the last good file keeps being served.

## Data shape

Only what an ordinary household pays. All prices exclude VAT.

```jsonc
{
  "schema_version": 1,
  "electricity": {                       // universal-service supplier, low voltage, MDL/kWh
    "premier_energy": {                  // center & south
      "name": "Premier Energy", "region": "center_south", "unit": "MDL/kWh", "vat_included": false,
      "current": {
        "decision": { "number": "222/2026", "effective_from": "2026-04-02", "url": "https://www.legis.md/…" },
        "price": { "flat": 3.56, "day": 3.75, "night": 2.94 }   // day/night only with hourly metering
      },
      "entries": [ … ]                   // newest first, up to 5, incl. not-yet-effective ones
    },
    "fee_nord": { "region": "north", … }
  },
  "gas": {
    "energocom": { "unit": "MDL/m3", "current": { "decision": { … }, "price": 24.202 } }
  },
  "water": {                             // household tariffs per operator, MDL/m3
    "apa_canal_chisinau": { "current": { "price": { "water": 14.03, "sewerage": 6.63 } } },
    "apa_canal_anenii_noi": { "current": { "price": { "technical_water": 17.85, "sewerage": 25.5 } } }
  }
}
```

- Water: a price key that's missing means ANRE lists no tariff, so don't treat it as zero. `technical_water` is a non-potable supply, used in towns that have no drinking-water tariff.
- `current` is computed when the job runs. A client that wants to switch exactly on the effective date takes the first item in `entries` whose `decision.effective_from` is `null` (unknown, treated as in force) or `<=` today.

## Home Assistant (no integration needed)

```yaml
rest:
  - resource: https://raw.githubusercontent.com/R1DEN/moldova-utility-tariffs/main/data/v1/tariffs.json
    scan_interval: 21600 # 6 h
    sensor:
      - name: Electricity tariff
        unique_id: md_tariff_electricity
        value_template: "{{ value_json.electricity.premier_energy.current.price.flat }}" # or fee_nord
        unit_of_measurement: MDL/kWh
      - name: Gas tariff
        unique_id: md_tariff_gas
        value_template: "{{ value_json.gas.energocom.current.price }}"
        unit_of_measurement: MDL/m³
      - name: Water tariff
        unique_id: md_tariff_water
        value_template: >-
          {% set p = value_json.water.apa_canal_chisinau.current.price %}
          {{ (p.water + p.sewerage) | round(2) }}
        unit_of_measurement: MDL/m³
```

Multiply by your VAT rate in the template if you want gross prices.

## Curation

The ANRE tables don't print effective dates, and the decision texts on legis.md sit behind a Cloudflare browser check, so effective dates are curated by hand in [`data/overrides.json`](data/overrides.json).

When a new decision number appears, the job publishes it right away with `effective_from: null` and opens an issue. Add the date from legis.md to the overrides file and it is applied on the next run.

## Development

```sh
python3 -m unittest discover -s tests          # parser + merge tests on saved fixtures
python3 scraper/scrape.py                      # live scrape → data/v1/tariffs.json
python3 scraper/scrape.py --fixtures tests/fixtures
```

Stdlib-only Python 3.9+.

## Sources

- Electricity: https://anre.md/energie-electrica-3-290
- Natural gas: https://anre.md/gaze-naturale-3-205
- Water/sewerage: https://anre.md/alimentare-cu-apa-si-canalizare-3-283

Data © ANRE (Agenția Națională pentru Reglementare în Energetică), public regulatory information. This project is not affiliated with ANRE or any supplier. Code: MIT.
