library(shiny)
library(bslib)
library(rhandsontable)
library(shinyjs)
library(qrcode)
library(base64enc)

# Load configuration
config <- read.csv("C:/Users/kevin.turnbow/Downloads/config.csv", stringsAsFactors = FALSE)

safe_as_date <- function(x) {
  if (inherits(x, "Date")) return(x)
  x <- as.character(x)
  x[x == "" | x == "null"] <- NA
  suppressWarnings(as.Date(x))
}

ui <- page_sidebar(
  title = "Lab Sample Tracker",
  useShinyjs(),
  tags$head(tags$style(HTML("
    @media print {
      body * { visibility: hidden; }
      #printable_label, #printable_label * { visibility: visible; }
      #printable_label { 
        position: absolute; left: 0; top: 0; 
        width: 3in; height: 1in; 
        margin: 0; padding: 0px;
        overflow: hidden;
      }
      @page { size: 3in 1in; margin: 0; }
      html, body { margin: 0; padding: 0; }
    }
  "))),
  sidebar = sidebar(
    selectInput("user", "User:", choices = unique(config$User)),
    selectInput("project", "Project:", choices = unique(config$Project)),
    dateInput("sample_date", "Sample Date:", value = Sys.Date()),
    dateInput("test_date", "Date Tests Performed:", value = NA),
    numericInput("start_suffix", "Starting Suffix:", value = 1, min = 1),
    numericInput("count", "Number of Samples:", value = 1, min = 1),
    actionButton("add_samples", "Add Samples", class = "btn-primary w-100 mb-2"),
    actionButton("print_label_ui", "Print Selected Label", class = "btn-success w-100")
  ),
  card(
    card_header("Test Records (Edit dates directly in table)"),
    rHandsontableOutput("hot_table")
  ),
  uiOutput("pdf_label_container"),
  tableOutput("debug_table")
)

server <- function(input, output, session) {
  
  samples <- reactiveVal(data.frame(
    SampleID = character(), Project = character(), SampleDate = as.Date(character()),
    TestDate = as.Date(character()), Test = character(), User = character(),
    stringsAsFactors = FALSE
  ))
  
  last_created_id <- reactiveVal(NULL)
  selected_override <- reactiveVal(NULL)
  
  observeEvent(input$add_samples, {
    req(input$project, input$user, input$sample_date, input$count)
    
    # 1. Get current data safely
    current_data <- samples()
    
    match_idx <- which(config$Project == input$project)
    default_tests_str <- if (length(match_idx) > 0) as.character(config$DefaultTests[match_idx[1]]) else ""
    if (is.na(default_tests_str)) default_tests_str <- ""
    test_list <- unlist(strsplit(default_tests_str, ", "))
    
    test_date_val <- if (length(input$test_date) == 0 || is.na(input$test_date)) NA else as.Date(input$test_date)
    
    new_entries <- list()
    last_id_in_batch <- NULL
    
    for (i in 0:(input$count - 1)) {
      suffix <- input$start_suffix + i
      new_id <- paste0(toupper(input$project), "-", format(as.Date(input$sample_date), "%Y%m%d"), "-", sprintf("%03d", suffix))
      last_id_in_batch <- new_id
      
      for (test in test_list) {
        new_entries[[length(new_entries) + 1]] <- data.frame(
          SampleID = new_id, Project = input$project, SampleDate = as.Date(input$sample_date),
          TestDate = test_date_val, Test = test, User = input$user, stringsAsFactors = FALSE
        )
      }
    }
    
    new_df <- do.call(rbind, new_entries)
    samples(rbind(current_data, new_df))
    last_created_id(last_id_in_batch)
  })
  
  output$hot_table <- renderRHandsontable({
    rhandsontable(samples(), stretchH = "all", selectCallback = TRUE) %>%
      hot_col("SampleDate", type = "date", dateFormat = "YYYY-MM-DD") %>%
      hot_col("TestDate", type = "date", dateFormat = "YYYY-MM-DD")
  })
  
  observeEvent(input$hot_table, {
    new_data <- hot_to_r(input$hot_table)
    # Only update if the data is different to prevent unnecessary re-renders
    if (!identical(new_data, samples())) {
      samples(new_data)
    }
  })
  
  observeEvent(input$hot_table_select$select$row, {
    row <- input$hot_table_select$select$row
    df <- samples()
    if (!is.null(row) && row >= 1 && row <= nrow(df)) selected_override(df$SampleID[row])
  })
  
  output$pdf_label_container <- renderUI({
    df <- samples()
    req(nrow(df) > 0)
    
    selected_id <- if (!is.null(selected_override())) selected_override() else last_created_id()
    if (is.null(selected_id)) selected_id <- df$SampleID[nrow(df)]
    
    subset_data <- df[df$SampleID == selected_id, ]
    
    # Generate QR Code
    #qr_obj <- qr_code(selected_id)
    #png_file <- tempfile(fileext = ".svg")
    #generate_svg(qr_obj, filename = png_file)
    #qr_b64 <- dataURI(file = png_file, mime = "image/svg+xml")
    
    tags$div(id = "printable_label", style = "font-family: sans-serif; font-size: 10px; display: flex; align-items: center;",
             tags$div(style = "width: 75px; padding-right: 5px;", 
                      #tags$img(src = qr_b64, width = "65px", height = "65px")
             ),
             tags$div(
               tags$div(style = "font-weight: bold; font-size: 12px;", selected_id),
               tags$div(paste("Collected:", subset_data$SampleDate[1])),
               tags$div(paste("Tested:", ifelse(is.na(subset_data$TestDate[1]), "Pending", as.character(subset_data$TestDate[1])))),
               tags$div(style = "display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px;",
                        lapply(subset_data$Test, function(t) {
                          tags$div(style = "display: flex; align-items: center;", 
                                   tags$input(type = "checkbox"), tags$span(t))
                        })
               )
             )
    )
  })
  
  observeEvent(input$print_label_ui, {
    runjs("window.print();")
  })
  output$debug_table <- renderTable({ samples() })
  
}

shinyApp(ui, server)
