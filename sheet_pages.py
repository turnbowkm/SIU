import csv
import pymupdf

# ---------------- CONFIG ----------------
SITES_CSV     = "sites.csv"   # site code -> site name
SITE_CODE_COL = "site_code"   # key column in both CSVs
SITE_NAME_COL = "site_name"
TESTS_CSV     = "tests.csv"   # one row per site, one column per test
TRUE_VALUES   = {"x", "y", "yes", "1", "true", "t"}  # cell values that mean "required"
TITLE         = "Water Sample Sheet"

FIELD_PARAMS = ["Water Temp (C)", "DO (mg/L)", "pH (SU)", "Sp. Cond. (uS/cm)", "Turbidity (NTU)"]
TESTS = [  # (code, analyte group, container/preservative); TESTS_CSV headers match code or name
    ("NUT",   "Nutrients",          "250 mL, H2SO4, 4C"),
    ("MET",   "Total Metals",       "250 mL, HNO3"),
    ("DMET",  "Dissolved Metals",   "250 mL, filtered, HNO3"),
    ("CLSO4", "Chloride / Sulfate", "250 mL, 4C"),
    ("TSS",   "TSS / Solids",       "1 L, 4C"),
    ("ECOLI", "E. coli",            "100 mL sterile, 4C"),
    ("CHLA",  "Chlorophyll a",      "filter, frozen"),
]
QC_ITEMS = ["Field Blank", "Duplicate", "Equipment Blank", "MS/MSD"]

HDR_FILL = (0.92, 0.92, 0.92)   # set to None for no shading
ROW_H    = 26
LINE_W   = 0.6
PAGE = pymupdf.paper_rect("letter")
M = 50
FONT, BOLD = "helv", "hebo"
# ----------------------------------------

W = PAGE.width - 2 * M
U = W / 5   # one grid unit; every row spans 5 units

# style -> (font, size, align, fill)
STYLES = {
    "title":   (BOLD, 15,  "l", None),
    "section": (BOLD, 10,  "l", HDR_FILL),
    "head":    (BOLD, 9.5, "c", HDR_FILL),
    "param":   (FONT, 8.5, "c", HDR_FILL),
    "label":   (FONT, 10,  "l", None),
    "bold":    (BOLD, 11,  "l", None),
    "site":    (FONT, 12,  "l", None),
    "blank":   (FONT, 10,  "l", None),
    "cbox":    (FONT, 10,  "l", None),
    "qc":      (FONT, 10,  "l", None),
}


def read_tests(path):
    """site code -> set of required test codes."""
    lookup = {}
    for code, name in ((t[0], t[1]) for t in TESTS):
        lookup[code.upper()] = code.upper()
        lookup[name.upper()] = code.upper()
    required = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = {h: lookup.get(h.strip().upper()) for h in rd.fieldnames if h != SITE_CODE_COL}
        for h, c in cols.items():
            if c is None:
                print(f"warning: tests column '{h}' matches no test, ignored")
        for r in rd:
            key = r[SITE_CODE_COL].strip()
            required[key] = {c for h, c in cols.items()
                             if c and (r.get(h) or "").strip().lower() in TRUE_VALUES}
    return required


def cell(pg, x, y, w, h, text, style):
    font, size, align, fill = STYLES[style]
    r = pymupdf.Rect(x, y, x + w, y + h)
    pg.draw_rect(r, color=(0, 0, 0), fill=fill, width=LINE_W)
    cy = y + h / 2
    if style == "cbox":
        bx, by = x + w / 2 - 4.5, cy - 4.5
        pg.draw_rect(pymupdf.Rect(bx, by, bx + 9, by + 9), width=LINE_W)
        if text:  # checked
            pg.draw_polyline([(bx + 1.8, by + 4.8), (bx + 3.8, by + 7.2), (bx + 7.6, by + 1.6)],
                             color=(0, 0, 0), width=1.3)
        return
    if style == "qc":
        pg.draw_rect(pymupdf.Rect(x + 6, cy - 4, x + 14, cy + 4), width=LINE_W)
        x += 14
        w -= 14
    if not text:
        return
    if style == "site":  # wrap to max 2 lines, shrink only if needed
        while True:
            lines, cur = [], ""
            for word in text.split():
                t = (cur + " " + word).strip()
                if pymupdf.get_text_length(t, font, size) <= w - 10 or not cur:
                    cur = t
                else:
                    lines.append(cur); cur = word
            lines.append(cur)
            if len(lines) <= 2 or size <= 8:
                break
            size -= 0.5
        top = cy - (len(lines) * size * 1.15) / 2
        for i, ln in enumerate(lines):
            pg.insert_text((x + 5, top + size * (0.95 + 1.15 * i)), ln, fontname=font, fontsize=size)
        return
    while pymupdf.get_text_length(text, font, size) > w - 10 and size > 6:
        size -= 0.5
    tw = pymupdf.get_text_length(text, font, size)
    tx = x + (w - tw) / 2 if align == "c" else x + 5
    pg.insert_text((tx, cy + size * 0.35), text, fontname=font, fontsize=size)


def row_(pg, y, h, cells):
    assert abs(sum(c[0] for c in cells) - 5) < 1e-6, cells
    x = M
    for units, text, style in cells:
        cell(pg, x, y, units * U, h, text, style)
        x += units * U
    return y + h


def load_lookups():
    """Call once per download: (site code -> name, site code -> set of required test codes)."""
    with open(SITES_CSV, newline="", encoding="utf-8-sig") as f:
        names = {r[SITE_CODE_COL].strip(): r[SITE_NAME_COL].strip() for r in csv.DictReader(f)}
    return names, read_tests(TESTS_CSV)


def draw_sample_sheet(doc, row, lookups):
    """Append one grid sample sheet for a samples_data row (needs Site, SampleID, SampleDate)."""
    names, required = lookups
    code = str(row["Site"]).strip()
    if code not in names:
        print(f"warning: site {code} not in {SITES_CSV}")
    if code not in required:
        print(f"warning: site {code} not in {TESTS_CSV}, no tests pre-checked")
    name, tests = names.get(code, ""), required.get(code, set())
    d = row["SampleDate"]
    date_str = "" if d is None or d != d else str(d)

    pg = doc.new_page(width=PAGE.width, height=PAGE.height)
    y = M

    y = row_(pg, y, 32, [(5, TITLE, "title")])
    y = row_(pg, y, 30, [(1, "Site Code", "bold"), (1, code, "site"),
                         (1, "Site Name", "bold"), (2, name, "site")])
    y = row_(pg, y, ROW_H, [(1, "Sample ID", "bold"), (4, str(row["SampleID"]), "site")])

    y = row_(pg, y, 18, [(5, "Collection", "section")])
    y = row_(pg, y, ROW_H, [(1, "Date", "label"), (1, date_str, "blank"), (1, "Time (24h)", "label"), (2, "", "blank")])
    y = row_(pg, y, ROW_H, [(1, "Collector(s)", "label"), (4, "", "blank")])
    y = row_(pg, y, ROW_H, [(1, "Weather", "label"), (1, "", "blank"), (1, "Stage / Flow", "label"), (2, "", "blank")])

    y = row_(pg, y, 18, [(5, "Field Parameters", "section")])
    n = len(FIELD_PARAMS)
    y = row_(pg, y, 20, [(5 / n, p, "param") for p in FIELD_PARAMS])
    y = row_(pg, y, 40, [(5 / n, "", "blank") for _ in FIELD_PARAMS])

    y = row_(pg, y, 18, [(5, "Samples", "section")])
    y = row_(pg, y, 20, [(0.6, "Required", "head"), (0.6, "Collected", "head"), (1.1, "Analyte Group", "head"),
                         (1.5, "Container / Preservative", "head"), (1.2, "Bottle ID / Notes", "head")])
    for tcode, grp, cont in TESTS:
        y = row_(pg, y, 22, [(0.6, "X" if tcode.upper() in tests else "", "cbox"), (0.6, "", "cbox"),
                             (1.1, grp, "label"), (1.5, cont, "label"), (1.2, "", "blank")])
    q = 4 / len(QC_ITEMS)
    y = row_(pg, y, ROW_H, [(1, "QC Samples", "label")] + [(q, t, "qc") for t in QC_ITEMS])

    y = row_(pg, y, 18, [(5, "Field Notes", "section")])
    y = row_(pg, y, 80, [(5, "", "blank")])

    y = row_(pg, y, 18, [(5, "Chain of Custody", "section")])
    y = row_(pg, y, ROW_H, [(1, "Relinquished by", "label"), (1.5, "", "blank"), (1, "Date/Time", "label"), (1.5, "", "blank")])
    y = row_(pg, y, ROW_H, [(1, "Received by (lab)", "label"), (1.5, "", "blank"), (1, "Date/Time", "label"), (1.5, "", "blank")])
    y = row_(pg, y, ROW_H, [(1, "Temp on receipt", "label"), (4, "", "blank")])
    return pg
