from shiny import App, render, ui, reactive
import pandas as pd
from datetime import date
import pymupdf
import tempfile
import os
import math
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
CONFIG_FILE = "config.csv"
DATA_FILE = "samples_data.csv"
BACKUP_FILE = "samples_data.csv.bak"
CORRUPT_FILE = "samples_data.csv.corrupt"
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
ROW_HEIGHT = 13                           # test checkbox rows on the text label
BOTTOM_MARGIN = 4
BOX_SIZE = 11
TEST_COLS = 3
TEST_FONT = 8
TEXT_TESTS_TOP = 32                       # pt, where checkbox rows start on the text label
QR_MARGIN = 4                             # pt, around the QR on the QR label
QR_BOX_SIZE = 10                          # px per module in the PNG
QR_BORDER = 2                             # modules of quiet zone
QR_ERROR = qrcode.constants.ERROR_CORRECT_M   # L, M, Q or H
SCAN_ACCEPT_TAB = False                   # True if scanner sends Tab instead of Enter

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


def load_samples():
    if os.path.exists(DATA_FILE):
        try:
            return _prep(pd.read_csv(DATA_FILE))
        except Exception:
            try:
                os.replace(DATA_FILE, CORRUPT_FILE)
            except OSError:
                pass

    if os.path.exists(BACKUP_FILE):
        try:
            return _prep(pd.read_csv(BACKUP_FILE))
        except Exception:
            pass
    return _prep(pd.DataFrame(columns=DATA_COLUMNS))


def save_samples(df):
    tmp_path = DATA_FILE + ".tmp"
    df.to_csv(tmp_path, index=False)
    with open(tmp_path, "rb+") as f:
        f.flush()
        os.fsync(f.fileno())

    if os.path.exists(DATA_FILE):
        try:
            os.replace(DATA_FILE, BACKUP_FILE)
        except OSError:
            pass
    os.replace(tmp_path, DATA_FILE)


def get_project_tests(project):
    matches = config_df.loc[config_df["Project"] == project, "DefaultTests"]
    if matches.empty:
        return []
    return [t.strip() for t in matches.values[0].split(",")]


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


def draw_label_page(doc, row):
    """Two 3x1 in labels per sample: page 1 = QR only, page 2 = readable text + test boxes."""
    bpage = doc.new_page(width=LABEL_WIDTH, height=LABEL_HEIGHT)
    side = LABEL_HEIGHT - 2 * QR_MARGIN
    x0 = (LABEL_WIDTH - side) / 2
    bpage.insert_image(
        pymupdf.Rect(x0, QR_MARGIN, x0 + side, QR_MARGIN + side),
        stream=make_qr_png(str(row["SampleID"])), keep_proportion=True)

    project_tests = get_project_tests(row["Project"]) or [row["TestType"]]
    max_tests = TEST_COLS * ((LABEL_HEIGHT - TEXT_TESTS_TOP - BOTTOM_MARGIN) // ROW_HEIGHT)
    if len(project_tests) > max_tests:
        print(f"WARNING: {len(project_tests)} tests but only {max_tests} fit on the label")

    page = doc.new_page(width=LABEL_WIDTH, height=LABEL_HEIGHT)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(2, 2, LABEL_WIDTH - 2, LABEL_HEIGHT - 2))

    sample_date_str = str(row["SampleDate"]) if pd.notna(row["SampleDate"]) else ""
    shape.insert_text((8, 14), f"ID: {row['SampleID']}", fontsize=10)
    shape.insert_text((8, 26), f"Sample Date: {sample_date_str}", fontsize=9)

    col_gap = 6
    col_width = (LABEL_WIDTH - 16 - col_gap * (TEST_COLS - 1)) / TEST_COLS
    for i, test in enumerate(project_tests):
        x = 8 + (i % TEST_COLS) * (col_width + col_gap)
        y = TEXT_TESTS_TOP + (i // TEST_COLS) * ROW_HEIGHT
        shape.draw_rect(pymupdf.Rect(x, y, x + BOX_SIZE, y + BOX_SIZE))
        shape.insert_text((x + BOX_SIZE + 3, y + BOX_SIZE - 2), test, fontsize=TEST_FONT)

    shape.finish(width=0.5)
    shape.commit()
    return bpage, page


def all_config_tests():
    seen = []
    for v in config_df["DefaultTests"].dropna():
        for t in str(v).split(","):
            t = t.strip()
            if t and t not in seen:
                seen.append(t)
    return seen


def incomplete_rows(df):
    return df.loc[df["CompletedDate"].map(_blank)]


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


def get_next_suffix(df, project, site, sample_date):
    if df.empty or not project or not site or not sample_date:
        return 1
    date_str = sample_date.strftime('%Y%m%d')
    prefix = f"{project}-{site}-{date_str}-"
    matching = df[df["SampleID"].astype(str).str.startswith(prefix)]
    if matching.empty:
        return 1
    max_s = 0
    for sid in matching["SampleID"].dropna().astype(str):
        try:
            s_val = int(sid.split("-")[-1])
            if s_val > max_s:
                max_s = s_val
        except ValueError:
            pass
    return max_s + 1


SCAN_KEYS = "['Enter','Tab']" if SCAN_ACCEPT_TAB else "['Enter']"

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_text("scan", "Scan:", placeholder="Sample or chemical code..."),
        ui.output_text("active_label"),
        ui.input_action_button("release", "Release Active Sample", class_="btn-outline-secondary w-100"),
        ui.input_select("user", "User:", choices=sorted(config_df["User"].unique().tolist())),
        ui.input_select("project", "Project:", choices=sorted(config_df["Project"].unique().tolist())),
        ui.output_ui("site_selector"),
        ui.input_date("sample_date", "Sample Date:", value=date.today()),
        ui.input_date("test_date", "Test Date:", value=None),
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
                    ui.card_header("Test Records"),
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

    @render.ui
    def site_selector():
        project = input.project()
        raw_values = config_df.loc[config_df["Project"] == project, "Site"].dropna().tolist()
        sites = sorted({s.strip() for v in raw_values for s in str(v).split(",") if s.strip()})
        return ui.input_select("site", "Site:", choices=sites)

    @reactive.Effect
    def _():
        proj = input.project()
        try:
            site = input.site()
        except Exception:
            site = None
        sdate = input.sample_date()
        df = samples.get()
        if proj and site and sdate:
            next_s = get_next_suffix(df, proj, site, sdate)
            ui.update_numeric("start_suffix", value=next_s)

    @reactive.Effect
    @reactive.event(input.add_samples)
    def _():
        project_data = config_df.loc[config_df["Project"] == input.project()]
        if project_data.empty: return
        test_list = [t.strip() for t in project_data.iloc[0]["DefaultTests"].split(",")]

        new_data = []
        for i in range(input.count()):
            suffix = input.start_suffix() + i
            sample_id = f"{input.project()}-{input.site()}-{input.sample_date().strftime('%Y%m%d')}-{suffix:03d}"
            for test in test_list:
                new_data.append({
                    "SampleID": sample_id, "User": input.user(), "Project": input.project(),
                    "Site": input.site(), "SampleDate": input.sample_date(),
                    "TestDate": input.test_date(), "TestType": test,
                })
        updated_df = _prep(pd.concat([samples.get(), pd.DataFrame(new_data)], ignore_index=True))
        samples.set(updated_df)
        save_samples(updated_df)

    @render.data_frame
    def sample_table():
        return render.DataGrid(samples.get(), editable=True, selection_mode="rows")

    @reactive.Effect
    @reactive.event(input.sample_table_cell_edit)
    def _():
        edit = input.sample_table_cell_edit()
        df = samples.get()
        df.iat[edit["row"], edit["col"]] = edit["value"]
        samples.set(df)
        save_samples(df)

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
        df.loc[m, "CompletedBy"] = input.cl_by()
        samples.set(df)
        save_samples(df)
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

    @reactive.Effect
    @reactive.event(input.release)
    def _():
        scanned_id.set(None)
        completed_user.set(None)
        ui.notification_show("Active sample released.", duration=3)

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
        df.loc[tmask, "CompletedDate"] = date.today()
        df.loc[tmask, "CompletedBy"] = input.completed_by() or completed_user.get()
        comment = (input.scan_comment() or "").strip()
        if comment:
            df.loc[mask, "Comment"] = comment
        samples.set(df)
        save_samples(df)
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
        row = samples.get().iloc[list(selected_indices)[0]]

        doc = pymupdf.open()
        draw_label_page(doc, row)
        gap = 6
        combo = pymupdf.open()
        cp = combo.new_page(width=LABEL_WIDTH, height=2 * LABEL_HEIGHT + gap)
        cp.show_pdf_page(pymupdf.Rect(0, 0, LABEL_WIDTH, LABEL_HEIGHT), doc, 0)
        cp.show_pdf_page(pymupdf.Rect(0, LABEL_HEIGHT + gap, LABEL_WIDTH, 2 * LABEL_HEIGHT + gap), doc, 1)
        pix = cp.get_pixmap(matrix=pymupdf.Matrix(3, 3))
        combo.close()

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
        label_rows = samples.get().iloc[list(selected_indices)]

        doc = pymupdf.open()
        for _, row in label_rows.iterrows():
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
        rows = samples.get().iloc[list(selected_indices)].drop_duplicates("SampleID")
        lookups = load_lookups()

        doc = pymupdf.open()
        for _, row in rows.iterrows():
            draw_sample_sheet(doc, row, lookups)
        pdf_bytes = doc.tobytes()
        doc.close()

        yield pdf_bytes


app = App(app_ui, server)