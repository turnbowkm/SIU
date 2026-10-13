from shiny import App, render, ui, reactive
import pandas as pd
from datetime import date
import fitz
import tempfile
import os
import math
import io
from barcode import Code128
from barcode.writer import ImageWriter

# ---------------- CONFIG ----------------
CONFIG_FILE = "config.csv"
DATA_FILE = "samples_data.csv"
BACKUP_FILE = "samples_data.csv.bak"
CORRUPT_FILE = "samples_data.csv.corrupt"
DATA_COLUMNS = ["SampleID", "User", "Project", "Site", "SampleDate", "TestDate", "TestType",
                "CompletedDate", "CompletedBy", "Comment"]
DATE_COLUMNS = ("SampleDate", "TestDate", "CompletedDate")
TEXT_COLUMNS = ("CompletedDate", "CompletedBy", "Comment")

LABEL_WIDTH = 300
ROW_HEIGHT = 26
BOTTOM_MARGIN = 8
BOX_SIZE = 26
BARCODE_HEIGHT = 30   # pt
BARCODE_TOP = 40      # pt, below the date line
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


def make_barcode_png(text):
    buf = io.BytesIO()
    Code128(text, writer=ImageWriter()).write(
        buf, options={"write_text": False, "module_height": 8, "module_width": 0.25,
                      "quiet_zone": 2, "dpi": 300})
    return buf.getvalue()


def draw_label_page(doc, row):
    top_margin = BARCODE_TOP + BARCODE_HEIGHT + 6

    project_tests = get_project_tests(row["Project"]) or [row["TestType"]]
    rows_needed = math.ceil(len(project_tests) / 2)
    label_height = max(72, top_margin + rows_needed * ROW_HEIGHT + BOTTOM_MARGIN)

    page = doc.new_page(width=LABEL_WIDTH, height=label_height)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(2, 2, LABEL_WIDTH - 2, label_height - 2))

    sample_date_str = str(row["SampleDate"]) if pd.notna(row["SampleDate"]) else ""
    shape.insert_text((10, 18), f"ID: {row['SampleID']}", fontsize=15)
    shape.insert_text((10, 34), f"Sample Date: {sample_date_str}", fontsize=12)

    col_gap = 10
    col_width = (LABEL_WIDTH - 20 - col_gap) / 2
    col_x = [10, 10 + col_width + col_gap]

    for i, test in enumerate(project_tests):
        x = col_x[i % 2]
        y = top_margin + (i // 2) * ROW_HEIGHT
        shape.draw_rect(fitz.Rect(x, y, x + BOX_SIZE, y + BOX_SIZE))
        shape.insert_text((x + BOX_SIZE + 4, y + BOX_SIZE - 2), test, fontsize=11)

    shape.finish(width=0.5)
    shape.commit()
    page.insert_image(
        fitz.Rect(10, BARCODE_TOP, LABEL_WIDTH - 10, BARCODE_TOP + BARCODE_HEIGHT),
        stream=make_barcode_png(str(row["SampleID"])), keep_proportion=False)
    return page


app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_text("scan", "Scan Sample ID:", placeholder="Scan barcode...", update_on="blur"),
        ui.input_select("user", "User:", choices=sorted(config_df["User"].unique().tolist())),
        ui.input_select("project", "Project:", choices=sorted(config_df["Project"].unique().tolist())),
        ui.output_ui("site_selector"),
        ui.input_date("sample_date", "Sample Date:", value=date.today()),
        ui.input_date("test_date", "Test Date:", value=None),
        ui.input_numeric("start_suffix", "Starting Suffix:", value=1),
        ui.input_numeric("count", "Number of Samples:", value=1),
        ui.input_action_button("add_samples", "Add Samples", class_="btn-primary w-100"),
    ),
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
    @reactive.Effect
    @reactive.event(input.scan)
    def _():
        sid = (input.scan() or "").strip()
        if not sid:
            return
        rows = samples.get().loc[lambda d: d["SampleID"] == sid]
        ui.update_text("scan", value="")
        if rows.empty:
            ui.notification_show(f"Unknown ID: {sid}", type="warning")
            return
        scanned_id.set(sid)
        pending = rows.loc[rows["CompletedDate"].isna(), "TestType"].tolist()
        prior = rows["Comment"].dropna()
        ui.modal_show(ui.modal(
            ui.input_checkbox_group("done_tests", "Tests completed:", choices=pending),
            ui.input_text_area("scan_comment", "Comment:", value=prior.iloc[0] if len(prior) else ""),
            ui.download_button("download_dup", "Print Duplicate Label"),
            title=sid,
            footer=ui.TagList(ui.modal_button("Cancel"),
                              ui.input_action_button("scan_save", "Save", class_="btn-primary")),
        ))

    @reactive.Effect
    @reactive.event(input.scan_save)
    def _():
        sid = scanned_id.get()
        df = samples.get().copy()
        mask = df["SampleID"] == sid
        tmask = mask & df["TestType"].isin(list(input.done_tests() or []))
        df.loc[tmask, "CompletedDate"] = date.today()
        df.loc[tmask, "CompletedBy"] = input.user()
        comment = (input.scan_comment() or "").strip()
        if comment:
            df.loc[mask, "Comment"] = comment
        samples.set(df)
        save_samples(df)
        ui.modal_remove()

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
        page = draw_label_page(doc, row)
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))

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
