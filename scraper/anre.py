"""Parse ANRE (anre.md) "tarife în vigoare" HTML tables into normalized records.

Stdlib only, so the GitHub Actions job needs no dependency install.
"""
from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

# ---------------------------------------------------------------- HTML → grid


class Cell:
    __slots__ = ("text", "hrefs", "rowspan", "colspan", "bold")

    def __init__(self, rowspan: int, colspan: int):
        self.text = ""
        self.hrefs: list[str] = []
        self.rowspan = rowspan
        self.colspan = colspan
        self.bold = False

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Cell({self.text!r})"


class _TableParser(HTMLParser):
    """Collect the rows/cells of the first <table> on the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[Cell]] = []
        self._depth = 0  # table nesting depth
        self._done = False
        self._cell: Cell | None = None

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
        elif self._depth == 1 and tag == "tr":
            self.rows.append([])
        elif self._depth == 1 and tag in ("td", "th") and self.rows:
            self._cell = Cell(int(a.get("rowspan") or 1), int(a.get("colspan") or 1))
            self.rows[-1].append(self._cell)
        elif self._cell is not None:
            if tag == "a" and a.get("href"):
                self._cell.hrefs.append(a["href"])
            elif tag in ("strong", "b", "h4"):
                self._cell.bold = True
            elif tag == "br":
                self._cell.text += " "

    def handle_endtag(self, tag):
        if self._done:
            return
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self._done = True

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def parse_grid(html: str) -> list[list[Cell | None]]:
    """Return the first table as a rectangular grid with row/colspans expanded.

    A spanned cell appears (as the same object) in every grid slot it covers,
    so `grid[r][c] is grid[r+1][c]` tells you a cell spans rows.
    """
    p = _TableParser()
    p.feed(html)
    for row in p.rows:
        for cell in row:
            cell.text = _clean(cell.text)
    grid: list[list[Cell | None]] = []
    pending: dict[tuple[int, int], Cell] = {}
    for r, row in enumerate(p.rows):
        out: list[Cell | None] = []
        it = iter(row)
        c = 0
        while True:
            if (r, c) in pending:
                out.append(pending.pop((r, c)))
                c += 1
                continue
            cell = next(it, None)
            if cell is None:
                break
            for dc in range(cell.colspan):
                out.append(cell)
                for dr in range(1, cell.rowspan):
                    pending[(r + dr, c + dc)] = cell
            c += cell.colspan
        # flush trailing spanned cells
        while (r, c) in pending:
            out.append(pending.pop((r, c)))
            c += 1
        grid.append(out)
    width = max((len(r) for r in grid), default=0)
    for row in grid:
        row.extend([None] * (width - len(row)))
    return grid


def text(cell: Cell | None) -> str:
    return cell.text if cell else ""


# ---------------------------------------------------------------- value helpers

_NUM = re.compile(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_DECISION = re.compile(r"nr\.?\s*(\d+)\s*din\s*(\d{2})\.(\d{2})\.(\d{4})", re.I)
_DOC_ID = re.compile(r"doc_id=(\d+)")


def parse_number(s: str) -> float | None:
    """'18 641' → 18641, '80 723,40' → 80723.4, '32.66' → 32.66, '-' → None."""
    m = _NUM.search(s)
    if not m:
        return None
    return float(m.group(0).replace(" ", "").replace(",", "."))


def has_extra_text(s: str) -> bool:
    """True when a cell holds more than a bare number (e.g. a footnote)."""
    return bool(re.search(r"[^\d\s.,\-–]", s))


def parse_decision(s: str) -> dict | None:
    m = _DECISION.search(s)
    if not m:
        return None
    num, d, mth, y = m.groups()
    return {"number": f"{int(num)}/{y}", "date": f"{y}-{mth}-{d}", "legis_doc_id": None, "url": None}


def attach_link(decision: dict, hrefs: list[str]) -> bool:
    for h in hrefs:
        m = _DOC_ID.search(h)
        if m:
            decision["legis_doc_id"] = int(m.group(1))
            decision["url"] = f"https://www.legis.md/cautare/getResults?doc_id={m.group(1)}&lang=ro"
            return True
    return False


def fold(s: str) -> str:
    """Lowercase, strip diacritics (ș ş ț ţ ă â î → s t a i)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


_LEGAL_FORMS = {"SA", "SRL", "ICS", "IM", "IMDP", "DP", "FPC", "ISI", "IS", "SC", "IMDPAC"}


def slugify(name: str) -> str:
    s = fold(name)
    s = re.sub(r"[\"'„”“,]", " ", s)
    tokens = [t for t in s.split() if re.sub(r"[^a-z]", "", t).upper() not in _LEGAL_FORMS]
    s = "_".join(tokens)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:48].rstrip("_")


# ---------------------------------------------------------------- electricity

ELECTRICITY_POINTS = [
    ("iesire rt", "transport_exit"),
    ("tensiune inalta", "high_voltage"),
    ("tensiune medie", "medium_voltage"),
    ("tensiune joasa", "low_voltage"),
]


def _point_key(label: str, table: list[tuple[str, str]]) -> str:
    f = fold(label)
    for needle, key in table:
        if needle in f:
            return key
    return slugify(label) or "default"


def _electricity_category(section: str) -> str:
    f = fold(section)
    if "ultima optiune" in f:
        return "last_resort"
    if "serviciului universal" in f:
        return "universal_service"
    return slugify(section)


def parse_electricity(html: str) -> list[dict]:
    """One record per (category, licensee). Prices in MDL/kWh, excl. VAT.

    Columns: licensee | point | flat bani/kWh | day 07-23 | night 23-07 | decision | link
    """
    grid = parse_grid(html)
    if not grid:
        return []
    width = len(grid[0])
    category = "network_and_market"  # rows before the first section header
    series: dict[tuple[str, int], dict] = {}
    for row in grid:
        cells = {id(c) for c in row if c is not None}
        if len(cells) == 1 and row[0] is not None and row[0].colspan >= width - 1:
            category = _electricity_category(row[0].text)
            continue
        decision = parse_decision(text(row[5]))
        if decision is None:
            continue  # header rows
        flat, day, night = (parse_number(text(row[i])) for i in (2, 3, 4))
        if flat is None:
            continue
        key = (category, id(row[0]))
        rec = series.get(key)
        if rec is None:
            attach_link(decision, row[6].hrefs if row[6] else [])
            rec = series[key] = {
                "category": category,
                "licensee": text(row[0]).lstrip(". ").strip(),
                "decision": decision,
                "unit": "MDL/kWh",
                "source_unit": "bani/kWh",
                "vat_included": False,
                "prices": {},
            }
        price = {"flat": round(flat / 100, 5)}
        if day is not None:
            price["day"] = round(day / 100, 5)
        if night is not None:
            price["night"] = round(night / 100, 5)
        rec["prices"][_point_key(text(row[1]), ELECTRICITY_POINTS) if text(row[1]) else "default"] = price
    return list(series.values())


# ---------------------------------------------------------------- gas

GAS_POINTS = [
    # transport-tariff block (capacity tariffs, lei/1000 m³/day/year)
    ("punctelor de intrare", "entry_interconnection"),
    ("iesire din reteaua de transport al gazelor naturale spre retelele de transport", "exit_interconnection"),
    ("iesire din reteaua de transport al gazelor naturale spre retelele de distributie", "exit_domestic"),
    # price/distribution blocks
    ("intrare in retelele de transport", "transport_entry"),
    ("iesire din retelele de transport", "transport_exit"),
    ("inalta presiune", "distribution_high"),
    ("medie presiune", "distribution_medium"),
    ("joasa presiune", "distribution_low"),
]

GAS_LICENSEE_ALIASES = [
    ("la instalatiile de gaze ale consumatorilor finali", "Uniform distribution tariff — end consumers"),
    ("cu alti osd", "Uniform distribution tariff — between DSOs"),
]


def _gas_category(title: str) -> str:
    f = fold(title)
    if "ultima optiune" in f:
        return "last_resort"
    if "anumitor categorii de consumatori" in f:
        return "public_service_obligation"
    if "distributie" in f:
        return "distribution"
    if "transport" in f:
        return "transport"
    return slugify(title)


def parse_gas(html: str) -> list[dict]:
    """The gas page stacks several sub-tables, each with a 2-row header:

        title (colspan=2 rowspan=2) | unit (colspan=n)
                                    | point labels...
        licensee (rowspan=2) | 'HCA nr. X din dd.mm.yyyy' | values (rowspan=2)...
                             | legis.md link
    """
    grid = parse_grid(html)
    if not grid:
        return []
    out: list[dict] = []
    block = None
    r = 0
    while r < len(grid):
        row = grid[r]
        c0 = row[0]
        if c0 is not None and c0.colspan == 2 and c0.rowspan == 2 and (r == 0 or grid[r - 1][0] is not c0):
            unit_txt = " ".join(dict.fromkeys(text(c) for c in row[2:] if c and c.text))
            labels = [text(grid[r + 1][i]) for i in range(2, len(row))]
            per_m3 = "1000 m" in unit_txt and "zi" not in fold(unit_txt)
            block = {
                "category": _gas_category(c0.text),
                "labels": labels,
                "per_m3": per_m3,
                "unit": "MDL/m3" if per_m3 else "MDL/1000m3/day/year",
                "source_unit": re.sub(r"^(Preț|Tarif)\s*,\s*", "", unit_txt),
                "vat_included": False if "fara tva" in fold(unit_txt) else None,
                "last": None,
            }
            r += 2
            continue
        if block is None or c0 is None or c0.colspan > 2 or not c0.text:
            r += 1
            continue
        decision = parse_decision(text(row[1]))
        hrefs = row[1].hrefs if row[1] else []
        prev = block["last"]
        if prev is not None and prev["_cell"] is c0:
            # continuation row of a rowspan=2 licensee: holds the link
            if prev["decision"]["legis_doc_id"] is None:
                attach_link(prev["decision"], hrefs)
            r += 1
            continue
        if decision is None and prev is not None:
            # e.g. 2nd uniform-distribution row shares the decision above; its
            # decision cell holds the link instead.
            if prev["decision"]["legis_doc_id"] is None:
                attach_link(prev["decision"], hrefs)
            decision = dict(prev["decision"])
        if decision is None:
            r += 1
            continue
        attach_link(decision, hrefs)
        prices = {}
        for i, label in enumerate(block["labels"], start=2):
            cell = row[i]
            if not label or cell is None or cell is c0:
                continue
            v = parse_number(cell.text)
            if v is None:
                continue
            prices[_point_key(label, GAS_POINTS)] = round(v / 1000, 6) if block["per_m3"] else v
        licensee = c0.text
        for needle, alias in GAS_LICENSEE_ALIASES:
            if needle in fold(licensee):
                licensee = alias
        rec = {
            "category": block["category"],
            "licensee": licensee,
            "decision": decision,
            "unit": block["unit"],
            "source_unit": block["source_unit"],
            "vat_included": block["vat_included"],
            "prices": prices,
            "_cell": c0,
        }
        block["last"] = rec
        out.append(rec)
        r += 1
    for rec in out:
        rec.pop("_cell")
    # Second uniform-distribution row got a copy of the decision before the link
    # was attached; re-sync by number.
    links = {d["decision"]["number"]: d["decision"] for d in out if d["decision"]["legis_doc_id"]}
    for rec in out:
        if rec["decision"]["legis_doc_id"] is None and rec["decision"]["number"] in links:
            rec["decision"] = dict(links[rec["decision"]["number"]])
    return out


# ---------------------------------------------------------------- water

WATER_COLUMNS = [
    # (needles in folded header path, key) — first match wins
    (("medii", "apa potabila"), "avg_water"),
    (("medii", "canalizare"), "avg_sewerage"),
    (("alimentare cu apa", "noncasnici"), "water_non_household"),
    (("alimentare cu apa", "casnici"), "water_household"),
    (("canalizare", "noncasnici"), "sewerage_non_household"),
    (("canalizare", "casnici"), "sewerage_household"),
    (("apa tehnologica",), "technical_water"),
    (("producerea",), "bulk_water_production_transport"),
    (("epurare a apelor uzate",), "wastewater_treatment"),
]


_HH_SPLIT = re.compile(r"\bcasnici\W*(\d+(?:[.,]\d+)?).*noncasnici\W*(\d+(?:[.,]\d+)?)")


def parse_water(html: str) -> list[dict]:
    """Prices presumed MDL/m³ excl. VAT — the ANRE page prints neither."""
    grid = parse_grid(html)
    if not grid:
        return []
    header_rows = 0
    while header_rows < len(grid) and not text(grid[header_rows][0]).isdigit():
        header_rows += 1
    width = len(grid[0])
    keys: list[str | None] = [None] * width
    for c in range(width):
        path = " ".join(fold(text(grid[r][c])) for r in range(header_rows))
        for needles, key in WATER_COLUMNS:
            if all(n in path for n in needles):
                keys[c] = key
                break
    out = []
    for row in grid[header_rows:]:
        if not text(row[0]).isdigit() or not text(row[1]):
            continue
        prices, notes = {}, {}
        for c, key in enumerate(keys):
            if key is None or row[c] is None:
                continue
            raw = row[c].text
            split = _HH_SPLIT.search(fold(raw))
            if split:
                # "Consumatori casnici – 17,85 Consumatori noncasnici – 36,93"
                prices[f"{key}_household"] = parse_number(split.group(1))
                prices[f"{key}_non_household"] = parse_number(split.group(2))
                continue
            v = parse_number(raw)
            if v is None:
                continue
            prices[key] = v
            if has_extra_text(raw):
                notes[key] = raw
        rec = {"operator": row[1].text, "prices": prices}
        if notes:
            rec["notes"] = notes
        out.append(rec)
    return out
