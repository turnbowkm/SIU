from shiny import App, render, ui, reactive
import pandas as pd
from datetime import date
import fitz
import tempfile
import os
import math

# Load configuration
config_df = pd.read_csv("config.csv")

DATA_FILE = "samples_data.csv"
BACKUP_FILE = "samples_data.csv.bak"
CORRUPT_FILE = "samples_data.csv.corrupt"
DATA_COLUMNS = ["SampleID", "User", "Project", "Site", "SampleDate", "TestDate", "TestType"]
_last_preview_path = None  # module-level, not reactive.Value

def load_samples():
    if os.path.exists(DATA_FILE):
        try:
            df = pd.read_csv(DATA_FILE)
            for col in ("SampleDate", "TestDate"):
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            return df
        except Exception:
            try:
                os.replace(DATA_FILE, CORRUPT_FILE)
            except OSError:
                pass

    if os.path.exists(BACKUP_FILE):
        try:
            df = pd.read_csv(BACKUP_FILE)
            for col in ("SampleDate", "TestDate"):
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            return df
        except Exception:
            pass
    return pd.DataFrame(columns=DATA_COLUMNS)

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

def draw_label_page(doc, row):
    label_width = 300
    top_margin = 48
    row_height = 26
    bottom_margin = 8

    project_tests = get_project_tests(row["Project"]) or [row["TestType"]]
    num_tests = len(project_tests)
    rows_needed = math.ceil(num_tests / 2)
    label_height = max(72, top_margin + rows_needed * row_height + bottom_margin)

    page = doc.new_page(width=label_width, height=label_height)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(2, 2, label_width - 2, label_height - 2))

    sample_date_str = str(row["SampleDate"]) if pd.notna(row["SampleDate"]) else ""
    shape.insert_text((10, 15), f"ID: {row['SampleID']}", fontsize=15)
    shape.insert_text((10, 34), f"Sample Date: {sample_date_str}", fontsize=12)

    col_gap = 10
    col_width = (label_width - 20 - col_gap) / 2
    col_x = [10, 10 + col_width + col_gap]
    box_size = 26

    for i, test in enumerate(project_tests):
        col = i % 2
        row_idx = i // 2
        x = col_x[col]
        y = top_margin + row_idx * row_height
        box = fitz.Rect(x, y, x + box_size, y + box_size)
        shape.draw_rect(box)
        shape.insert_text((x + box_size + 4, y + box_size - 2), test, fontsize=11)

    shape.finish(width=0.5)
    shape.commit()
    return page

app_ui = ui.page_sidebar(
    ui.sidebar(
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
        updated_df = pd.concat([samples.get(), pd.DataFrame(new_data)], ignore_index=True)
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
