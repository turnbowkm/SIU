from shiny import App, render, ui, reactive
import pandas as pd
from datetime import date
import fitz
import tempfile
import os
import math
import io
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import qrcode

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
QR_MARGIN = 4                            # pt, around the QR on the QR label
QR_BOX_SIZE = 10                          # px per module in the PNG
QR_BORDER = 2                             # modules of quiet zone
QR_ERROR = qrcode.constants.ERROR_CORRECT_M   # L, M, Q or H
SCAN_ACCEPT_TAB = False      # True if scanner sends Tab instead of Enter
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
        fitz.Rect(x0, QR_MARGIN, x0 + side, QR_MARGIN + side),
        stream=make_qr_png(str(row["SampleID"])), keep_proportion=True)

    project_tests = get_project_tests(row["Project"]) or [row["TestType"]]
    max_tests = TEST_COLS * ((LABEL_HEIGHT - TEXT_TESTS_TOP - BOTTOM_MARGIN) // ROW_HEIGHT)
    if len(project_tests) > max_tests:
        print(f"WARNING: {len(project_tests)} tests but only {max_tests} fit on the label")

    page = doc.new_page(width=LABEL_WIDTH, height=LABEL_HEIGHT)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(2, 2, LABEL_WIDTH - 2, LABEL_HEIGHT - 2))

    sample_date_str = str(row["SampleDate"]) if pd.notna(row["SampleDate"]) else ""
    shape.insert_text((8, 14), f"ID: {row['SampleID']}", fontsize=10)
    shape.insert_text((8, 26), f"Sample Date: {sample_date_str}", fontsize=9)

    col_gap = 6
    col_width = (LABEL_WIDTH - 16 - col_gap * (TEST_COLS - 1)) / TEST_COLS
    for i, test in enumerate(project_tests):
        x = 8 + (i % TEST_COLS) * (col_width + col_gap)
        y = TEXT_TESTS_TOP + (i // TEST_COLS) * ROW_HEIGHT
        shape.draw_rect(fitz.Rect(x, y, x + BOX_SIZE, y + BOX_SIZE))
        shape.insert_text((x + BOX_SIZE + 3, y + BOX_SIZE - 2), test, fontsize=TEST_FONT)

    shape.finish(width=0.5)
    shape.commit()
    return bpage, page


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
    ui.card(
        ui.card_header("Test Records"),
        ui.output_data_frame("sample_table"),
    ),
    ui.card(
        ui.card_header("Label Preview"),
        ui.output_image("label_preview"),
    ),
    ui.download_button("download_label", "Download Selected Labels"),
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
        doc = fitz.open()
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

        doc = fitz.open()
        draw_label_page(doc, row)
        gap = 6
        combo = fitz.open()
        cp = combo.new_page(width=LABEL_WIDTH, height=2 * LABEL_HEIGHT + gap)
        cp.show_pdf_page(fitz.Rect(0, 0, LABEL_WIDTH, LABEL_HEIGHT), doc, 0)
        cp.show_pdf_page(fitz.Rect(0, LABEL_HEIGHT + gap, LABEL_WIDTH, 2 * LABEL_HEIGHT + gap), doc, 1)
        pix = cp.get_pixmap(matrix=fitz.Matrix(3, 3))
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

        doc = fitz.open()
        for _, row in label_rows.iterrows():
            draw_label_page(doc, row)

        pdf_bytes = doc.tobytes()
        doc.close()

        yield pdf_bytes

app = App(app_ui, server)
