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
DATA_COLUMNS = ["SampleID", "User", "Project", "SampleDate", "TestDate", "TestType"]


def load_samples():
    if os.path.exists(DATA_FILE):
        try:
            df = pd.read_csv(DATA_FILE)
            for col in ("SampleDate", "TestDate"):
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            return df
        except Exception:
            # Main file is unreadable/corrupted (e.g. disk fault unrelated
            # to our own write, since writes are atomic). Preserve the bad
            # file for inspection instead of silently deleting it, then try
            # the backup before giving up and starting fresh.
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
    # Power-loss-safe write for kiosk use: write to a temp file in the same
    # directory, fsync it to force bytes onto disk (not just OS buffers),
    # then atomically replace the real file. This guarantees DATA_FILE is
    # always either the previous complete version or the new complete
    # version - never a partial/corrupted write, even if power cuts out
    # mid-save.
    tmp_path = DATA_FILE + ".tmp"
    df.to_csv(tmp_path, index=False)
    with open(tmp_path, "rb+") as f:
        f.flush()
        os.fsync(f.fileno())

    # Rotate current good file to .bak before replacing it, so there's
    # always a fallback copy even if something outside this code damages
    # the live file later.
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
    """Draws one label page for a single sample/test record row.
    Shared by both the download PDF and the live preview so they can
    never drift out of sync with each other.
    """
    label_width = 300
    top_margin = 44  # space reserved for ID / Test Date lines
    row_height = 20  # vertical space per test checkbox line
    bottom_margin = 8

    project_tests = get_project_tests(row["Project"]) or [row["TestType"]]
    num_tests = len(project_tests)
    rows_needed = math.ceil(num_tests / 2)  # 2 columns
    label_height = max(72, top_margin + rows_needed * row_height + bottom_margin)

    page = doc.new_page(width=label_width, height=label_height)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(2, 2, label_width - 2, label_height - 2))

    test_date_str = row["TestDate"] if pd.notna(row["TestDate"]) else ""

    shape.insert_text((10, 20), f"ID: {row['SampleID']}", fontsize=15)
    shape.insert_text((10, 34), f"Test Date: {test_date_str}", fontsize=12)

    # Two-column layout for checkboxes
    col_gap = 10
    col_width = (label_width - 20 - col_gap) / 2  # 20 = left/right margins
    col_x = [10, 10 + col_width + col_gap]

    for i, test in enumerate(project_tests):
        col = i % 2
        row_idx = i // 2
        x = col_x[col]
        y = top_margin + row_idx * row_height

        box = fitz.Rect(x, y, x + 8, y + 8)
        shape.draw_rect(box)
        #if test == row["TestType"]:
        #    shape.draw_line(box.top_left, box.bottom_right)
        #    shape.draw_line(box.bottom_left, box.top_right)
        shape.insert_text((x + 12, y + 7), test, fontsize=8)

    shape.finish(width=0.5)
    shape.commit()
    return page

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_select("user", "User:", choices=sorted(config_df["User"].unique().tolist())),
        ui.input_select("project", "Project:", choices=sorted(config_df["Project"].unique().tolist())),
        ui.input_date("sample_date", "Sample Date:", value=date.today()),
        ui.input_date("test_date", "Test Date:", value=None),  # blank by default
        ui.input_numeric("start_suffix", "Starting Suffix:", value=1),
        ui.input_numeric("count", "Number of Samples:", value=1),
        ui.input_action_button("add_samples", "Add Samples", class_="btn-primary w-100"),
    ),
    ui.card(
        ui.card_header("Test Records (One record per test)"),
        ui.output_data_frame("sample_table"),
    ),
    ui.card(
        ui.card_header("Label Preview (select a row to preview its label)"),
        ui.output_image("label_preview"),
    ),
    ui.download_button("download_label", "Download Selected Labels"),
)


def server(input, output, session):
    samples = reactive.Value(load_samples())

    @reactive.Effect
    @reactive.event(input.add_samples)
    def _():
        matches = config_df.loc[config_df["Project"] == input.project(), "DefaultTests"]
        if matches.empty:
            return
        test_list = [t.strip() for t in matches.values[0].split(",")]

        new_data = []
        for i in range(input.count()):
            suffix = input.start_suffix() + i
            sample_id = f"{input.project()}-{input.sample_date().strftime('%Y%m%d')}-{suffix:03d}"

            for test in test_list:
                new_data.append(
                    {
                        "SampleID": sample_id,
                        "User": input.user(),
                        "Project": input.project(),
                        "SampleDate": input.sample_date(),
                        "TestDate": input.test_date(),  # may be None
                        "TestType": test,
                    }
                )

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
        selection = sample_table.cell_selection()
        selected_indices = selection["rows"]
        if not selected_indices:
            return None

        df = samples.get()
        # Preview just the first selected row - previewing every selected
        # row would mean re-rendering an image on every click for
        # potentially dozens of rows, which is wasted work for a sanity
        # check. The download still exports all selected rows.
        row = df.iloc[list(selected_indices)[0]]

        doc = fitz.open()
        page = draw_label_page(doc, row)
        # Render at a higher zoom than the label's actual point size so
        # small text is legible on screen.
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            preview_path = tmp.name
        pix.save(preview_path)
        doc.close()

        return {"src": preview_path, "width": pix.width // 1, "height": pix.height // 1}

    @render.download(filename="labels.pdf")
    def download_label():
        selection = sample_table.cell_selection()
        selected_indices = selection["rows"]
        if not selected_indices:
            return

        df = samples.get()
        # One label per selected row. If the same SampleID appears on multiple
        # rows (e.g. one row per test), selecting all of them prints one
        # duplicate label per record, as intended.
        label_rows = df.iloc[list(selected_indices)]

        doc = fitz.open()
        for _, row in label_rows.iterrows():
            draw_label_page(doc, row)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            output_path = tmp.name
        doc.save(output_path)
        doc.close()
        return output_path


app = App(app_ui, server)
