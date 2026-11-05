from shiny import App, render, ui, reactive
import pandas as pd
from datetime import date
import pymupdf
import tempfile
import os
import math
import sqlite3
import threading
import time
from contextlib import closing
import io
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import qrcode
try:
    from sheet_pages import load_lookups, draw_sample_sheet
    HAVE_SHEETS = True
except ImportError as e:
    print(f"sheet_pages not available, sample sheets disabled: {e}")
    HAVE_SHEETS = False

# ---------------- CONFIG ----------------
CONFIG_FILE = "config.csv"                # User, Project
SITES_TESTS_FILE = "sites_tests.csv"      # Project, site_code, site_name, then one column per test (any non-blank = test applies)
SITE_META_COLS = ("Project", "site_code", "site_name")
DATA_FILE = "samples_data.csv"            # legacy CSV, imported once if samples.db is empty
DB_FILE = "samples.db"
DB_BACKUP = "samples.db.bak"              # refreshed at every app start
EXPORT_FILE = "samples_export.csv"        # Excel-friendly copy of the whole database, rewritten in the background
EXPORT_DELAY = 2                          # s, wait after a change so bursts become one export
EXPORT_RETRY = 30                         # s, retry interval if the file is locked (e.g. open in Excel)
TABLE_DEFAULT_VIEW = "recent"             # records table: "incomplete", "recent" or "all"
TABLE_RECENT_DAYS = 30                    # window for the "recent" view
TABLE_MAX_ROWS = 2000                     # newest N matching rows are sent to the browser
DATA_COLUMNS = ["SampleID", "User", "Project", "Site", "SampleDate", "TestDate", "TestType",
                "CompletedDate", "CompletedBy", "Comment"]
DATE_COLUMNS = ("SampleDate", "TestDate", "CompletedDate")
TEXT_COLUMNS = ("CompletedDate", "CompletedBy", "Comment")

SUPPLIES_FILE = "supplies.csv"            # you enter Barcode,Name,Lot,Expiration (YYYY-MM-DD); every scan appends a new row with UsedAt + Test (sample ID)
SUPPLY_COLUMNS = ["Barcode", "Name", "Lot", "Expiration", "UsedAt", "Test"]
EXPIRY_WARN_DAYS = 14                     # flag "expiring soon" within this many days

MANAGER_EMAIL = ""                        # empty = no email, log + on-screen warning only
SMTP_HOST = "smtp.example.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = SMTP_USER

RELEASE_CODE = "RELEASE"                  # scan this code to release the active sample

LABEL_WIDTH = 216                         # 3 in, in pt
LABEL_HEIGHT = 72                         # 1 in, in pt
ROW_HEIGHT = 10                           # test checkbox rows beside the QR
BOTTOM_MARGIN = 4
BOX_SIZE = 8
TEST_COLS = 2
TEST_FONT = 7
TEXT_TESTS_TOP = 28                       # pt, where checkbox rows start
QR_MARGIN = 4                             # pt, around the QR on the QR label
QR_BOX_SIZE = 10                          # px per module in the PNG
QR_BORDER = 2                             # modules of quiet zone
QR_ERROR = qrcode.constants.ERROR_CORRECT_M   # L, M, Q or H
SCAN_ACCEPT_TAB = False                   # True if scanner sends Tab instead of Enter
SCAN_ADDS_SAMPLES = True                  # scanning a valid label whose sample isn't in the database adds it

PRINT_PAGE_W = 792                        # letter landscape, pt
PRINT_PAGE_H = 612
PRINT_MARGIN = 36
PRINT_FONT = 8
PRINT_ROW_H = 14                          # pt per sample row
PRINT_BOX = 8                             # pt, checkbox size
PRINT_ID_W = 180                          # pt, Sample ID column
PRINT_DATE_W = 72                         # pt, Sample Date column
PRINT_USER_W = 70                         # pt, User column
PRINT_TEST_COL_MAX = 60                   # pt, max width of each test column (shrinks to fit the page)
PRINT_DONE_LABEL = "Complete"             # text shown in a test cell once that test is done
# ----------------------------------------

config_df = pd.read_csv(CONFIG_FILE)
st_df = pd.read_csv(SITES_TESTS_FILE, dtype=str).fillna("")
for _c in ("Project", "site_code"):
    st_df[_c] = st_df[_c].str.strip()
TEST_COLUMNS = [c for c in st_df.columns if c not in SITE_META_COLS]
_last_preview_path = None  # module-level, not reactive.Value


def _prep(df):
    for c in DATA_COLUMNS:
        if c not in df.columns:
            df[c] = None
    for c in TEXT_COLUMNS:
        df[c] = df[c].astype(object)
    for c in DATE_COLUMNS:
        df[c] = pd.to_datetime(df[c], errors="coerce").dt.date.astype(object)
    return df[DATA_COLUMNS]


def _db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _to_db(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _coerce(col, v):
    """Cell-edit text -> value for memory/DB (blank -> None, date columns -> date or None)."""
    v = "" if v is None else str(v).strip()
    if not v:
        return None
    if col in DATE_COLUMNS:
        d = pd.to_datetime(v, errors="coerce")
        return None if pd.isna(d) else d.date()
    return v


def insert_samples(df):
    df = _prep(df.copy())
    rows = [tuple(_to_db(v) for v in r) for r in df[DATA_COLUMNS].itertuples(index=False, name=None)]
    q = f"INSERT INTO samples ({', '.join(DATA_COLUMNS)}) VALUES ({', '.join('?' * len(DATA_COLUMNS))})"
    with closing(_db()) as con, con:
        con.executemany(q, rows)
    request_export()


def update_samples(ids, **fields):
    """Set columns on the given row ids (the DataFrame index is the DB id)."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = [_to_db(v) for v in fields.values()]
    with closing(_db()) as con, con:
        con.executemany(f"UPDATE samples SET {sets} WHERE id = ?", [(*vals, i) for i in ids])
    request_export()


def load_samples():
    with closing(_db()) as con:
        df = pd.read_sql_query("SELECT * FROM samples ORDER BY id", con, index_col="id")
    return _prep(df)


def export_csv_bytes():
    """Whole database as CSV (UTF-8 with BOM so Excel opens it cleanly)."""
    with closing(_db()) as con:
        df = pd.read_sql_query(f"SELECT {', '.join(DATA_COLUMNS)} FROM samples ORDER BY id", con)
    return df.to_csv(index=False).encode("utf-8-sig")


_export_wanted = threading.Event()


def request_export():
    _export_wanted.set()


def _export_worker():
    while True:
        _export_wanted.wait()
        time.sleep(EXPORT_DELAY)
        _export_wanted.clear()
        try:
            tmp = EXPORT_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(export_csv_bytes())
            os.replace(tmp, EXPORT_FILE)
        except Exception as e:
            print(f"WARNING: could not write {EXPORT_FILE} (open in Excel?), retrying in {EXPORT_RETRY}s: {e}")
            time.sleep(EXPORT_RETRY)
            _export_wanted.set()


threading.Thread(target=_export_worker, daemon=True).start()


def init_db():
    cols = ", ".join(f"{c} TEXT" for c in DATA_COLUMNS)
    with closing(_db()) as con, con:
        con.execute(f"CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        con.execute("CREATE INDEX IF NOT EXISTS ix_samples_sampleid ON samples (SampleID)")
        n = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    if n == 0 and os.path.exists(DATA_FILE):
        try:
            insert_samples(pd.read_csv(DATA_FILE))
            print(f"Imported {DATA_FILE} into {DB_FILE}")
        except Exception as e:
            print(f"WARNING: could not import {DATA_FILE}: {e}")


def backup_db():
    with closing(_db()) as src, closing(sqlite3.connect(DB_BACKUP)) as dst:
        src.backup(dst)


init_db()
backup_db()
request_export()


def _tests_for(rows):
    return [t for t in TEST_COLUMNS if (rows[t].str.strip() != "").any()]


def get_project_tests(project):
    return _tests_for(st_df.loc[st_df["Project"] == project])


def get_site_tests(project, site):
    return _tests_for(st_df.loc[(st_df["Project"] == project) & (st_df["site_code"] == site)])


def _norm(v):
    # tolerant barcode compare: ignore whitespace, case and leading zeros (Excel strips them)
    return str(v).strip().lstrip("0").upper()


def _blank(v):
    return pd.isna(v) or str(v).strip() == ""


def load_supplies():
    """All rows: inventory rows (UsedAt blank) plus one appended row per scan."""
    if os.path.exists(SUPPLIES_FILE):
        df = pd.read_csv(SUPPLIES_FILE, dtype=str)
    else:
        df = pd.DataFrame(columns=SUPPLY_COLUMNS)
    for c in SUPPLY_COLUMNS:
        if c not in df.columns:
            df[c] = None
        df[c] = df[c].astype(object)
    for c in ("Barcode", "Name", "Lot"):
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["Expiration"] = pd.to_datetime(df["Expiration"], errors="coerce").dt.date.astype(object)
    return df[SUPPLY_COLUMNS]


def save_supplies(df):
    tmp_path = SUPPLIES_FILE + ".tmp"
    df.to_csv(tmp_path, index=False)
    os.replace(tmp_path, SUPPLIES_FILE)


def is_expired(exp, on=None):
    # missing/unparseable expiration is treated as expired
    return pd.isna(exp) or exp < (on or date.today())


def record_supply_use(code, sample_id, user):
    """Look up a scanned chemical barcode. Returns None if unknown, else an info dict.
    If sample_id is set, appends a new row to supplies.csv for this scan."""
    sup = load_supplies()
    inv = sup.loc[sup["UsedAt"].map(_blank) & (sup["Barcode"].map(_norm) == _norm(code))]
    if inv.empty:
        return None
    r = inv.iloc[0]
    exp = r["Expiration"]
    info = {"name": r["Name"], "lot": r["Lot"], "barcode": r["Barcode"],
            "exp": None if pd.isna(exp) else exp, "expired": bool(is_expired(exp)),
            "soon": (not is_expired(exp)) and exp <= date.today() + timedelta(days=EXPIRY_WARN_DAYS),
            "logged": False, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if not sample_id:
        return info

    new_row = {"Barcode": r["Barcode"], "Name": r["Name"], "Lot": r["Lot"], "Expiration": exp,
               "UsedAt": info["time"], "Test": sample_id}
    save_supplies(pd.concat([sup, pd.DataFrame([new_row])], ignore_index=True))
    info["logged"] = True
    return info


def send_alert(subject, body):
    if not (MANAGER_EMAIL and SMTP_HOST and SMTP_USER):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM or SMTP_USER, MANAGER_EMAIL
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def make_qr_png(text):
    qr = qrcode.QRCode(error_correction=QR_ERROR, box_size=QR_BOX_SIZE, border=QR_BORDER)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def _fit(text, fontsize, max_w, min_size=5):
    while fontsize > min_size and pymupdf.get_text_length(text, fontname="helv", fontsize=fontsize) > max_w:
        fontsize -= 0.5
    return fontsize


def draw_label_page(doc, row):
    """One 3x1 in label per sample: QR on the left, text + test boxes on the right."""
    page = doc.new_page(width=LABEL_WIDTH, height=LABEL_HEIGHT)
    side = LABEL_HEIGHT - 2 * QR_MARGIN
    page.insert_image(
        pymupdf.Rect(QR_MARGIN, QR_MARGIN, QR_MARGIN + side, QR_MARGIN + side),
        stream=make_qr_png(str(row["SampleID"])), keep_proportion=True)

    project_tests = get_site_tests(row["Project"], row["Site"]) or [row["TestType"]]
    max_tests = TEST_COLS * ((LABEL_HEIGHT - TEXT_TESTS_TOP - BOTTOM_MARGIN) // ROW_HEIGHT)
    if len(project_tests) > max_tests:
        print(f"WARNING: {len(project_tests)} tests but only {max_tests} fit on the label")

    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(2, 2, LABEL_WIDTH - 2, LABEL_HEIGHT - 2))

    tx = 2 * QR_MARGIN + side                 # left edge of the text area
    tw = LABEL_WIDTH - 6 - tx
    sample_date_str = str(row["SampleDate"]) if pd.notna(row["SampleDate"]) else ""
    id_txt, date_txt = f"ID: {row['SampleID']}", f"Sample Date: {sample_date_str}"
    shape.insert_text((tx, 13), id_txt, fontsize=_fit(id_txt, 10, tw))
    shape.insert_text((tx, 23), date_txt, fontsize=_fit(date_txt, 8, tw))

    col_gap = 6
    col_width = (tw - col_gap * (TEST_COLS - 1)) / TEST_COLS
    for i, test in enumerate(project_tests):
        x = tx + (i % TEST_COLS) * (col_width + col_gap)
        y = TEXT_TESTS_TOP + (i // TEST_COLS) * ROW_HEIGHT
        shape.draw_rect(pymupdf.Rect(x, y, x + BOX_SIZE, y + BOX_SIZE))
        shape.insert_text((x + BOX_SIZE + 3, y + BOX_SIZE - 1.5), test,
                          fontsize=_fit(test, TEST_FONT, col_width - BOX_SIZE - 3))

    shape.finish(width=0.5)
    shape.commit()
    return page


def all_config_tests():
    return [t for t in TEST_COLUMNS if (st_df[t].str.strip() != "").any()]


def incomplete_rows(df):
    return df.loc[df["CompletedDate"].map(_blank)]


def filter_samples(df, mode):
    if mode == "incomplete":
        return incomplete_rows(df)
    if mode == "recent":
        sd = pd.to_datetime(df["SampleDate"], errors="coerce")
        return df.loc[sd >= pd.Timestamp(date.today() - timedelta(days=TABLE_RECENT_DAYS))]
    return df


def incomplete_summary(df):
    """[(project, DataFrame[TestType, Count, Oldest, Newest], n_samples_pending), ...]"""
    inc = incomplete_rows(df).copy()
    inc["SampleDate"] = pd.to_datetime(inc["SampleDate"], errors="coerce")
    inc["Project"] = inc["Project"].fillna("(none)").astype(str)
    g = inc.groupby(["Project", "TestType"]).agg(Count=("SampleID", "nunique"),
                                                 Oldest=("SampleDate", "min"),
                                                 Newest=("SampleDate", "max"))
    projects = list(dict.fromkeys(config_df["Project"].dropna().astype(str)))
    projects += sorted(set(inc["Project"]) - set(projects))
    out = []
    for p in projects:
        cfg_tests = get_project_tests(p)
        tests = cfg_tests + [t for (pp, t) in g.index if pp == p and t not in cfg_tests]
        sub = g.reindex(pd.MultiIndex.from_product([[p], tests], names=["Project", "TestType"]))
        sub["Count"] = sub["Count"].fillna(0).astype(int)
        sub = sub.reset_index().drop(columns="Project").sort_values(["Count", "TestType"], ascending=[False, True])
        out.append((p, sub, int(inc.loc[inc["Project"] == p, "SampleID"].nunique())))
    return out


def draw_incomplete_list(doc, df):
    M, W, H = PRINT_MARGIN, PRINT_PAGE_W, PRINT_PAGE_H
    inc = incomplete_rows(df)
    cfg = all_config_tests()
    found = set(inc["TestType"].astype(str).str.strip())
    tests = [t for t in cfg if t in found] + sorted(found - set(cfg))
    done_rows = df.loc[~df["CompletedDate"].map(_blank)]
    done_by = {str(k): set(g["TestType"].astype(str).str.strip()) for k, g in done_rows.groupby("SampleID")}
    entries = []
    for sid, g in inc.groupby("SampleID", sort=False):
        r = g.iloc[0]
        entries.append((str(r["SampleDate"]), str(sid), "" if _blank(r["User"]) else str(r["User"]),
                        set(g["TestType"].astype(str).str.strip()), done_by.get(str(sid), set())))
    entries.sort(key=lambda e: e[:3])
    fixed = (M, M + PRINT_ID_W, M + PRINT_ID_W + PRINT_DATE_W)   # ID, sample date, user
    x0 = M + PRINT_ID_W + PRINT_DATE_W + PRINT_USER_W            # first test column
    colw = min(PRINT_TEST_COL_MAX, (W - M - x0) / max(len(tests), 1))

    def new_page():
        p = doc.new_page(width=W, height=H)
        p.insert_text((M, M), "Incomplete Tests", fontsize=13)
        p.insert_text((M, M + 14), f"Printed {datetime.now():%Y-%m-%d %H:%M}  |  {len(entries)} samples, {len(inc)} tests pending",
                      fontsize=PRINT_FONT)
        y = M + 32
        for x, h in zip(fixed, ("Sample ID", "Sample Date", "User")):
            p.insert_text((x, y), h, fontsize=PRINT_FONT + 1, fontname="hebo")
        for i, t in enumerate(tests):
            fs = PRINT_FONT + 1
            while fs > 4 and pymupdf.get_text_length(t, fontname="hebo", fontsize=fs) > colw - 2:
                fs -= 0.5
            tw = pymupdf.get_text_length(t, fontname="hebo", fontsize=fs)
            p.insert_text((x0 + i * colw + (colw - tw) / 2, y), t, fontsize=fs, fontname="hebo")
        p.draw_line((M, y + 3), (W - M, y + 3), width=0.5)
        return p, y + PRINT_ROW_H

    page, y = new_page()
    if not entries:
        page.insert_text((M, y + 4), "Nothing pending.", fontsize=PRINT_FONT + 2)
        return
    for sample_date, sid, user, pend, done in entries:
        if y + PRINT_ROW_H > H - M:
            page, y = new_page()
        for x, txt in zip(fixed, (sid, sample_date, user)):
            page.insert_text((x, y), txt, fontsize=PRINT_FONT)
        for i, t in enumerate(tests):
            if t in pend:
                bx = x0 + i * colw + (colw - PRINT_BOX) / 2
                page.draw_rect(pymupdf.Rect(bx, y - PRINT_BOX, bx + PRINT_BOX, y), width=0.5)
            elif t in done:
                fs = PRINT_FONT - 1
                while fs > 4 and pymupdf.get_text_length(PRINT_DONE_LABEL, fontname="helv", fontsize=fs) > colw - 2:
                    fs -= 0.5
                tw = pymupdf.get_text_length(PRINT_DONE_LABEL, fontname="helv", fontsize=fs)
                page.insert_text((x0 + i * colw + (colw - tw) / 2, y), PRINT_DONE_LABEL,
                                 fontsize=fs, color=(0.35, 0.35, 0.35))
        page.draw_line((M, y + 4), (W - M, y + 4), color=(0.85, 0.85, 0.85), width=0.3)
        y += PRINT_ROW_H


def draw_test_list(doc, df, test):
    """Printable checklist of pending samples for one test (df = already-filtered pending rows)."""
    M, W, H = PRINT_MARGIN, PRINT_PAGE_W, PRINT_PAGE_H
    rows = sorted((str(r.SampleDate), str(r.SampleID), "" if _blank(r.User) else str(r.User))
                  for r in df.itertuples())
    x_id = M + 14
    x_date = x_id + PRINT_ID_W
    x_user = x_date + PRINT_DATE_W

    def new_page():
        p = doc.new_page(width=W, height=H)
        p.insert_text((M, M), f"{test}: pending", fontsize=13)
        p.insert_text((M, M + 14), f"Printed {datetime.now():%Y-%m-%d %H:%M}  |  {len(rows)} samples",
                      fontsize=PRINT_FONT)
        y = M + 32
        for x, h in ((x_id, "Sample ID"), (x_date, "Sample Date"), (x_user, "User")):
            p.insert_text((x, y), h, fontsize=PRINT_FONT + 1, fontname="hebo")
        p.draw_line((M, y + 3), (W - M, y + 3), width=0.5)
        return p, y + PRINT_ROW_H

    page, y = new_page()
    if not rows:
        page.insert_text((M, y + 4), "Nothing pending.", fontsize=PRINT_FONT + 2)
        return
    for sample_date, sid, user in rows:
        if y + PRINT_ROW_H > H - M:
            page, y = new_page()
        page.draw_rect(pymupdf.Rect(M, y - PRINT_BOX, M + PRINT_BOX, y), width=0.5)
        for x, txt in ((x_id, sid), (x_date, sample_date), (x_user, user)):
            page.insert_text((x, y), txt, fontsize=PRINT_FONT)
        page.draw_line((M, y + 4), (W - M, y + 4), color=(0.85, 0.85, 0.85), width=0.3)
        y += PRINT_ROW_H


def parse_sample_id(code):
    """'Project-Site-YYYYMMDD-NNN' -> (project, site, date, suffix) if it matches a configured project/site, else None."""
    parts = str(code).strip().rsplit("-", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    head, ds, suffix = parts
    try:
        d = datetime.strptime(ds, "%Y%m%d").date()
    except ValueError:
        return None
    for r in st_df[["Project", "site_code"]].drop_duplicates().itertuples(index=False):
        if f"{r.Project}-{r.site_code}".upper() == head.upper():
            return r.Project, r.site_code, d, suffix
    return None


def get_next_suffix(df, project):
    """Next sample number for the project, continuing across all sites and dates."""
    if df.empty or not project:
        return 1
    ids = df.loc[df["Project"] == project, "SampleID"].dropna().astype(str)
    nums = pd.to_numeric(ids.str.rsplit("-", n=1).str[-1], errors="coerce")
    return int(nums.max()) + 1 if nums.notna().any() else 1


SCAN_KEYS = "['Enter','Tab']" if SCAN_ACCEPT_TAB else "['Enter']"

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_text("scan", "Scan:", placeholder="Sample or chemical code..."),
        ui.output_text("active_label"),
        ui.download_button("download_checked_labels", "Print Labels for Checked Sites", class_="btn-outline-secondary w-100"),
        ui.input_select("user", "User:", choices=sorted(config_df["User"].unique().tolist())),
        ui.input_select("project", "Project:", choices=sorted(config_df["Project"].unique().tolist())),
        ui.output_ui("site_selector"),
        ui.input_date("sample_date", "Sample Date:", value=date.today()),
        ui.input_numeric("start_suffix", "Starting Suffix:", value=1),
        ui.input_numeric("count", "Number of Samples:", value=1),
        ui.input_action_button("add_samples", "Add Samples", class_="btn-primary w-100"),
    ),
    ui.tags.script("""
document.addEventListener('keydown', function(e) {
  if (""" + SCAN_KEYS + """.includes(e.key) && (e.target.id === 'scan' || e.target.id === 'scan_modal')) {
    e.preventDefault();
    var v = e.target.value.trim();
    e.target.value = '';
    if (v) Shiny.setInputValue('scan_event', {code: v, t: Date.now()}, {priority: 'event'});
  }
});
function focusScan() {
  var el = document.getElementById('scan_modal') || document.getElementById('scan');
  if (el) el.focus();
}
document.addEventListener('keydown', function(e) {
  var t = e.target, tag = (t.tagName || '').toLowerCase();
  var editable = tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable;
  if (!editable && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) focusScan();
}, true);
Shiny.addCustomMessageHandler('focus_scan', function(m) { focusScan(); });
document.addEventListener('shown.bs.modal', focusScan);
document.addEventListener('hidden.bs.modal', function() {
  var el = document.getElementById('scan'); if (el) el.focus();
});
$(document).on('shiny:connected', focusScan);
"""),
    ui.navset_tab(
        ui.nav_panel(
            "Records",
            ui.div(
                ui.card(
                    ui.card_header(ui.div(
                        ui.span("Test Records"),
                        ui.input_select("view_mode", None, width="180px", selected=TABLE_DEFAULT_VIEW,
                                        choices={"incomplete": "Incomplete only",
                                                 "recent": f"Last {TABLE_RECENT_DAYS} days",
                                                 "all": "All rows"}),
                        ui.output_text("table_note"),
                        class_="d-flex align-items-center gap-3 flex-wrap")),
                    ui.output_data_frame("sample_table"),
                ),
                ui.card(
                    ui.card_header("Label Preview"),
                    ui.output_image("label_preview"),
                ),
                ui.div(
                    ui.download_button("download_label", "Download Selected Labels"),
                    *([ui.download_button("download_sheets", "Download Sample Sheets")] if HAVE_SHEETS else []),
                    class_="d-flex gap-2",
                ),
                class_="pt-3",
            ),
        ),
        ui.nav_panel(
            "Incomplete Tests",
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.div(
                            ui.span("Click to Mark Samples as Tested", class_="me-4"),
                            ui.div(
                                ui.input_action_button("cl_open", "Complete Tests", class_="btn-sm btn-primary"),
                                ui.download_button("download_incomplete", "Print Incomplete List",
                                                   class_="btn-sm btn-outline-primary"),
                                class_="d-flex gap-2",
                            ),
                            class_="d-flex justify-content-between align-items-center flex-wrap gap-4",
                        )),
                    ui.output_ui("incomplete_chart"),
                ),
                class_="pt-3",
            ),
        ),
        id="main_tabs",
    ),
)


def server(input, output, session):
    samples = reactive.Value(load_samples())
    scanned_id = reactive.Value(None)
    completed_user = reactive.Value(None)

    def _project_sites():
        sub = st_df.loc[st_df["Project"] == input.project()].drop_duplicates("site_code").sort_values("site_code")
        return {r.site_code: f"{r.site_code} - {r.site_name}" for r in sub.itertuples()}

    @render.ui
    def site_selector():
        return ui.div(
            ui.tags.label("Sites:", class_="form-label"),
            ui.div(ui.input_action_button("sites_all", "Select all", class_="btn-sm btn-outline-secondary mb-2")),
            ui.input_checkbox_group("sites", None, choices=_project_sites()),
            style="max-height:40vh;overflow-y:auto;")

    @reactive.Effect
    @reactive.event(input.sites_all)
    def _():
        keys = list(_project_sites().keys())
        all_on = bool(keys) and set(input.sites() or []) == set(keys)
        ui.update_checkbox_group("sites", selected=[] if all_on else keys)

    @reactive.Effect
    def _():
        keys = set(_project_sites().keys())
        all_on = bool(keys) and set(input.sites() or []) == keys
        ui.update_action_button("sites_all", label="Deselect all" if all_on else "Select all")

    @reactive.Effect
    def _():
        proj = input.project()
        if proj:
            ui.update_numeric("start_suffix", value=get_next_suffix(samples.get(), proj))

    def _planned_rows(sites):
        """Sample rows for the checked sites from the sidebar settings. Nothing is saved here."""
        n = input.start_suffix()
        out = []
        for site in sites:
            test_list = get_site_tests(input.project(), site)
            if not test_list:
                ui.notification_show(f"No tests configured for {site}, skipped.", type="warning")
                continue
            for _ in range(input.count()):
                sample_id = f"{input.project()}-{site}-{input.sample_date().strftime('%Y%m%d')}-{n:03d}"
                n += 1
                for test in test_list:
                    out.append({
                        "SampleID": sample_id, "User": input.user(), "Project": input.project(),
                        "Site": site, "SampleDate": input.sample_date(),
                        "TestType": test,
                    })
        return out

    @reactive.Effect
    @reactive.event(input.add_samples)
    def _():
        sites = list(input.sites() or [])
        if not sites:
            ui.notification_show("Check at least one site.", type="warning")
            return
        new_data = _planned_rows(sites)
        if not new_data:
            return
        insert_samples(pd.DataFrame(new_data))
        samples.set(load_samples())

    @reactive.Calc
    def _filtered():
        return filter_samples(samples.get(), input.view_mode())

    @reactive.Calc
    def view_df():
        return _filtered().tail(TABLE_MAX_ROWS)

    @render.text
    def table_note():
        n, total = len(_filtered()), len(samples.get())
        shown = min(n, TABLE_MAX_ROWS)
        return f"Showing {shown} of {total} rows" + (f" (newest {TABLE_MAX_ROWS} of {n} matching)" if n > shown else "")

    @render.data_frame
    def sample_table():
        return render.DataGrid(view_df(), editable=True, selection_mode="rows", filters=True)

    @reactive.Effect
    @reactive.event(input.sample_table_cell_edit)
    def _():
        edit = input.sample_table_cell_edit()
        rid = view_df().index[edit["row"]]          # DataFrame index = DB id
        col = DATA_COLUMNS[edit["col"]]
        val = _coerce(col, edit["value"])
        if val is None and col in DATE_COLUMNS and str(edit["value"] or "").strip():
            ui.notification_show(f"Invalid date in {col}, cleared.", type="warning")
        samples.get().at[rid, col] = val
        update_samples([rid], **{col: val})

    # ---------- complete-tests window ----------
    def _get_complete_projects(df):
        inc = incomplete_rows(df)
        projs = sorted(set(inc["Project"].dropna().astype(str)) | set(config_df["Project"].dropna().astype(str)))
        return ["(All)"] + projs

    def _pending_mask(df, test, project=None):
        m = (df["TestType"].astype(str).str.strip().str.upper() == str(test).strip().upper()) \
            & df["CompletedDate"].map(_blank)
        if project and project != "(All)":
            m = m & (df["Project"].astype(str) == str(project))
        return m

    def _pending_choices(test, project=None):
        df = samples.get()
        pend = df.loc[_pending_mask(df, test, project)].sort_values(["SampleDate", "SampleID"])
        return {str(r.SampleID): f"{r.SampleID}  ({r.SampleDate})" for r in pend.itertuples()}

    @reactive.Effect
    @reactive.event(input.cl_open)
    def _():
        df = samples.get()
        inc = incomplete_rows(df)
        projects = _get_complete_projects(df)
        default_proj = input.project() if input.project() in projects else "(All)"
        
        cfg = all_config_tests()
        tests = cfg + sorted(set(inc["TestType"].dropna().astype(str).str.strip()) - set(cfg))
        if not tests:
            ui.notification_show("No tests configured.", duration=3)
            return
        counts = {t: int(_pending_mask(df, t).sum()) for t in tests}
        first = max(tests, key=lambda t: counts[t])
        users = sorted(set(config_df["User"].unique().tolist()) | {input.user()})
        
        ui.modal_show(ui.modal(
            ui.div(
                ui.tags.label("Project:", class_="form-label fw-bold small text-muted mb-1"),
                ui.input_radio_buttons("cl_project", None, choices=projects, selected=default_proj, inline=True),
                class_="mb-3",
            ),
            ui.div(
                ui.tags.label("Test:", class_="form-label fw-bold small text-muted mb-1"),
                ui.input_radio_buttons("cl_test", None, choices={t: f"{t} ({counts[t]})" for t in tests}, selected=first, inline=True),
                class_="mb-3",
            ),
            ui.div(
                ui.input_action_button("cl_all", "Select all", class_="btn-sm btn-outline-secondary"),
                ui.download_button("cl_print", "Print list", class_="btn-sm btn-outline-primary"),
                class_="d-flex gap-2 mb-2",
            ),
            ui.output_ui("cl_list"),
            ui.input_date("cl_date", "Completed date:", value=date.today()),
            ui.input_select("cl_by", "Completed by:", choices=users, selected=input.user()),
            title="Complete tests",
            footer=ui.TagList(ui.modal_button("Cancel"),
                              ui.input_action_button("cl_save", "Save", class_="btn-primary")),
            size="l", easy_close=True,
        ))

    @reactive.Effect
    @reactive.event(input.cl_project)
    def _():
        proj = input.cl_project()
        df = samples.get()
        inc = incomplete_rows(df)
        if proj and proj != "(All)":
            cfg = get_project_tests(proj)
            tests = cfg + sorted(set(inc.loc[inc["Project"].astype(str) == proj, "TestType"].dropna().astype(str).str.strip()) - set(cfg))
            counts = {t: int(_pending_mask(df, t, proj).sum()) for t in tests}
        else:
            cfg = all_config_tests()
            tests = cfg + sorted(set(inc["TestType"].dropna().astype(str).str.strip()) - set(cfg))
            counts = {t: int(_pending_mask(df, t).sum()) for t in tests}
        
        choices = {t: f"{t} ({counts.get(t, 0)})" for t in tests}
        first = max(tests, key=lambda t: counts.get(t, 0)) if tests else None
        ui.update_radio_buttons("cl_test", choices=choices, selected=first)

    @render.ui
    def cl_list():
        test = input.cl_test()
        proj = input.cl_project() if "cl_project" in input else "(All)"
        choices = _pending_choices(test, proj)
        if not choices:
            return ui.p(f"No {test} pending.", class_="text-muted")
        return ui.div(ui.input_checkbox_group("cl_sel", None, choices=choices),
                      style="max-height:50vh;overflow-y:auto;padding:6px 10px;")

    @reactive.Effect
    @reactive.event(input.cl_all)
    def _():
        test = input.cl_test()
        proj = input.cl_project() if "cl_project" in input else "(All)"
        choices = _pending_choices(test, proj)
        if not choices:
            return
        
        all_keys = list(choices.keys()) if isinstance(choices, dict) else list(choices)
        current_sel = input.cl_sel() or []
        
        if set(current_sel) == set(all_keys):
            ui.update_checkbox_group("cl_sel", selected=[])
            ui.update_action_button("cl_all", label="Select all")
        else:
            ui.update_checkbox_group("cl_sel", selected=all_keys)
            ui.update_action_button("cl_all", label="Deselect all")

    @reactive.Effect
    @reactive.event(input.cl_test)
    def _():
        test = input.cl_test()
        proj = input.cl_project() if "cl_project" in input else "(All)"
        ui.update_action_button("cl_all", label="Select all")
        ui.update_checkbox_group("cl_sel", selected=list(_pending_choices(test, proj).keys()))

    @render.download(filename=lambda: f"{input.cl_project()}_{input.cl_test()}_pending.pdf")
    def cl_print():
        test = input.cl_test()
        proj = input.cl_project() if "cl_project" in input else "(All)"
        df = samples.get()
        doc = pymupdf.open()
        draw_test_list(doc, df.loc[_pending_mask(df, test, proj)], test)
        pdf_bytes = doc.tobytes()
        doc.close()
        yield pdf_bytes

    @reactive.Effect
    @reactive.event(input.cl_save)
    def _():
        test = input.cl_test()
        proj = input.cl_project() if "cl_project" in input else "(All)"
        sel = list(input.cl_sel() or [])
        if not sel:
            ui.notification_show("Nothing checked.", duration=3)
            return
        df = samples.get().copy()
        m = df["SampleID"].astype(str).isin(sel) & _pending_mask(df, test, proj)
        df.loc[m, "CompletedDate"] = input.cl_date()
        df.loc[m, "TestDate"] = input.cl_date()
        df.loc[m, "CompletedBy"] = input.cl_by()
        samples.set(df)
        update_samples(df.index[m.to_numpy()], CompletedDate=input.cl_date(), TestDate=input.cl_date(), CompletedBy=input.cl_by())
        ui.modal_remove()
        ui.notification_show(f"{int(m.sum())} {test} marked complete.", duration=3)
    # -------------------------------------------

    @render.ui
    def incomplete_chart():
        groups = incomplete_summary(samples.get())
        if not groups:
            return ui.p("No projects configured.")
        maxc = max(max((int(g["Count"].max()) for _, g, _ in groups if not g.empty), default=1), 1)
        grid = "display:grid;grid-template-columns:130px 1fr 220px;gap:10px;align-items:center;margin:3px 0;"
        track = "position:relative;height:14px;background:#eee;border-radius:3px;"
        rows = [ui.div(
            ui.div("Test", style="font-weight:600;"),
            ui.div("Incomplete samples", style="font-weight:600;"),
            ui.div("Oldest - newest (age)", style="font-weight:600;"),
            style=grid)]
        today = pd.Timestamp(date.today())
        for proj, g, n_samples in groups:
            rows.append(ui.div(
                ui.span(proj, style="font-weight:700;"),
                ui.span(f"  {n_samples} sample{'s' if n_samples != 1 else ''} pending", style="font-size:.85em;color:#666;"),
                style="margin-top:12px;padding-bottom:2px;border-bottom:1px solid #ccc;"))
            for _, r in g.iterrows():
                n = int(r["Count"])
                pct = 100 * n / maxc
                if pct >= 12:                                   # inside the bar, white
                    pos, color = "left:6px;", "#fff"
                else:                                           # just past the bar, dark red (grey if zero)
                    pos, color = f"left:calc({pct:.1f}% + 6px);", "#a94442" if n else "#888"
                bar = ui.div(
                    ui.div(style=f"width:{pct:.1f}%;height:100%;background:#d9534f;border-radius:3px;"),
                    ui.span(str(n), style=f"position:absolute;{pos}top:0;line-height:14px;"
                                          f"font-size:.8em;font-weight:600;color:{color};"),
                    style=track)
                if n and pd.notna(r["Oldest"]):
                    txt = f"{r['Oldest'].date()} - {r['Newest'].date()} ({(today - r['Oldest']).days}d)"
                else:
                    txt = "-"
                rows.append(ui.div(ui.div(r["TestType"], style="padding-left:10px;"), bar,
                                   ui.div(txt, style="font-size:.85em;"), style=grid))
        return ui.div(*rows)

    @render.download(filename="incomplete_tests.pdf")
    def download_incomplete():
        doc = pymupdf.open()
        draw_incomplete_list(doc, samples.get())
        pdf_bytes = doc.tobytes()
        doc.close()
        yield pdf_bytes

    # ---------- scan flow ----------
    usage_tick = reactive.Value(0)

    @reactive.Effect
    @reactive.event(input.scan_event)
    async def _():
        await session.send_custom_message("focus_scan", {})
        code = ((input.scan_event() or {}).get("code") or "").strip()
        print(f"scan_event: {code!r}")  # debug: remove once scanning works
        if not code:
            return
        if code == RELEASE_CODE:
            scanned_id.set(None)
            ui.notification_show("Active sample released.", duration=3)
            return

        info = record_supply_use(code, scanned_id.get(), completed_user.get() or input.user())
        if info is not None:
            label = f"{info['name']} (Lot {info['lot']})"
            if info["expired"]:
                exp_txt = f"exp {info['exp']}" if info["exp"] else "no expiration date"
                if info["logged"]:
                    sid = scanned_id.get()
                    sent = send_alert(
                        f"Expired supply used: {label}",
                        f"Sample: {sid}\nSupply: {label}\nBarcode: {code}\nExpiration: {info['exp'] or 'none'}\n"
                        f"User: {completed_user.get() or input.user()}\nTime: {info['time']}")
                    ui.notification_show(
                        f"EXPIRED: {label}, {exp_txt}. Logged to {sid}; "
                        + ("manager emailed." if sent else "manager email NOT sent (check SMTP config)."),
                        type="error", duration=None)
                else:
                    ui.notification_show(f"EXPIRED: {label}, {exp_txt}. NOT logged: scan a sample ID first.",
                                         type="error", duration=None)
            elif not info["logged"]:
                ui.notification_show(f"{label} NOT logged: scan a sample ID first.", type="error", duration=10)
            else:
                msg = f"Logged {label} to {scanned_id.get()}."
                if info["soon"]:
                    msg += f" Expires {info['exp']}."
                ui.notification_show(msg, type="warning" if info["soon"] else "message", duration=5)
            usage_tick.set(usage_tick.get() + 1)
            return

        rows = samples.get().loc[lambda d: d["SampleID"].astype(str).str.upper() == code.upper()]
        if rows.empty and SCAN_ADDS_SAMPLES:
            samples.set(load_samples())          # may have been added from another session
            rows = samples.get().loc[lambda d: d["SampleID"].astype(str).str.upper() == code.upper()]
            parsed = parse_sample_id(code)
            tests = get_site_tests(parsed[0], parsed[1]) if parsed and rows.empty else []
            if tests:
                proj, site, sdate, suffix = parsed
                sid = f"{proj}-{site}-{sdate:%Y%m%d}-{suffix}"
                insert_samples(pd.DataFrame([{
                    "SampleID": sid, "User": input.user(), "Project": proj, "Site": site,
                    "SampleDate": sdate, "TestType": t} for t in tests]))
                samples.set(load_samples())
                rows = samples.get().loc[lambda d: d["SampleID"] == sid]
                ui.notification_show(f"Added {sid} to the database.", duration=4)
        if rows.empty:
            ui.notification_show(f"Unknown code: {code}", type="warning")
            return
        code = str(rows.iloc[0]["SampleID"])  # canonical casing
        scanned_id.set(code)
        pending = rows.loc[rows["CompletedDate"].isna(), "TestType"].tolist()
        prior = rows["Comment"].dropna()
        owners = rows["User"].dropna()
        sample_user = owners.iloc[0] if len(owners) else input.user()
        completed_user.set(sample_user)
        users = sorted(set(config_df["User"].unique().tolist()) | {sample_user})
        ui.modal_show(ui.modal(
            ui.input_text("scan_modal", "Scan chemicals:", placeholder="Chemical barcode..."),
            ui.h6("Supplies used"),
            ui.output_ui("sample_supplies"),
            ui.input_checkbox_group("done_tests", "Tests completed:", choices=pending),
            ui.input_select("completed_by", "Completed by:", choices=users, selected=sample_user),
            ui.input_text_area("scan_comment", "Comment:", value=prior.iloc[0] if len(prior) else ""),
            ui.download_button("download_dup", "Print Duplicate Label"),
            title=code,
            footer=ui.TagList(ui.modal_button("Cancel"),
                              ui.input_action_button("scan_save", "Save", class_="btn-primary")),
        ))

    @reactive.Effect
    @reactive.event(input.completed_by)
    def _():
        completed_user.set(input.completed_by())

    @render.text
    def active_label():
        return f"Active sample: {scanned_id.get()}" if scanned_id.get() else "Active sample: none"

    @render.ui
    def sample_supplies():
        usage_tick.get()
        sid = scanned_id.get()
        sup = load_supplies()
        used = sup.loc[~sup["UsedAt"].map(_blank) & (sup["Test"] == sid)] if sid else sup.iloc[0:0]
        if used.empty:
            return ui.p("None yet.")
        items = []
        for _, r in used.iterrows():
            bad = bool(is_expired(r["Expiration"], pd.to_datetime(r["UsedAt"]).date()))
            items.append(ui.tags.li(f"{r['Name']} (Lot {r['Lot']}) at {r['UsedAt']}" + (" - EXPIRED" if bad else ""),
                                    class_="text-danger" if bad else None))
        return ui.tags.ul(*items)

    @reactive.Effect
    @reactive.event(input.scan_save)
    def _():
        sid = scanned_id.get()
        if not sid:
            return
        df = samples.get().copy()
        mask = df["SampleID"] == sid
        tmask = mask & df["TestType"].isin(list(input.done_tests() or []))
        by = input.completed_by() or completed_user.get()
        df.loc[tmask, "CompletedDate"] = date.today()
        df.loc[tmask, "TestDate"] = date.today()
        df.loc[tmask, "CompletedBy"] = by
        update_samples(df.index[tmask.to_numpy()], CompletedDate=date.today(), TestDate=date.today(), CompletedBy=by)
        comment = (input.scan_comment() or "").strip()
        if comment:
            df.loc[mask, "Comment"] = comment
            update_samples(df.index[mask.to_numpy()], Comment=comment)
        samples.set(df)
        ui.modal_remove()
        ui.notification_show(f"Saved {sid}.", duration=3)

    @render.download(filename=lambda: f"{scanned_id.get()}.pdf")
    def download_dup():
        rows = samples.get().loc[lambda d: d["SampleID"] == scanned_id.get()]
        if rows.empty:
            return
        doc = pymupdf.open()
        draw_label_page(doc, rows.iloc[0])
        pdf_bytes = doc.tobytes()
        doc.close()
        yield pdf_bytes
    # -------------------------------

    @render.image
    def label_preview():
        global _last_preview_path
        selected_indices = input.sample_table_selected_rows()
        if not selected_indices:
            return None
        row = view_df().iloc[list(selected_indices)[0]]

        doc = pymupdf.open()
        draw_label_page(doc, row)
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3))

        if _last_preview_path and os.path.exists(_last_preview_path):
            try:
                os.remove(_last_preview_path)
            except OSError:
                pass

        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        pix.save(path)
        doc.close()

        _last_preview_path = path
        return {"src": path, "width": pix.width, "height": pix.height}

    @render.download(filename="labels.pdf")
    def download_label():
        selected_indices = input.sample_table_selected_rows()
        if not selected_indices:
            return
        label_rows = view_df().iloc[list(selected_indices)]

        doc = pymupdf.open()
        for _, row in label_rows.iterrows():
            draw_label_page(doc, row)

        pdf_bytes = doc.tobytes()
        doc.close()

        yield pdf_bytes

    @render.download(filename=lambda: f"{input.project()}_{input.sample_date()}_labels.pdf")
    def download_checked_labels():
        sites = list(input.sites() or [])
        if not sites:
            return
        planned = _planned_rows(sites)           # same IDs Add Samples would create; not saved
        if not planned:
            return
        rows = pd.DataFrame(planned).drop_duplicates("SampleID")

        doc = pymupdf.open()
        for _, row in rows.iterrows():
            draw_label_page(doc, row)
        pdf_bytes = doc.tobytes()
        doc.close()
        yield pdf_bytes

    @render.download(filename="sample_sheets.pdf")
    def download_sheets():
        if not HAVE_SHEETS:
            return
        selected_indices = input.sample_table_selected_rows()
        if not selected_indices:
            return
        rows = view_df().iloc[list(selected_indices)].drop_duplicates("SampleID")
        lookups = load_lookups()

        doc = pymupdf.open()
        for _, row in rows.iterrows():
            draw_sample_sheet(doc, row, lookups)
        pdf_bytes = doc.tobytes()
        doc.close()

        yield pdf_bytes


app = App(app_ui, server)