#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
fn main() -> eframe::Result {
    let demo = std::env::args().any(|a| a == "--demo" || a == "--ui-qa");
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("LabDAQ · 数据采集与分析")
            .with_inner_size([1440.0, 900.0])
            .with_min_inner_size([1024.0, 680.0]),
        ..Default::default()
    };
    eframe::run_native(
        "LabDAQ",
        options,
        Box::new(move |cc| Ok(Box::new(labdaq_desktop::ui::LabDaqApp::new(cc, demo)))),
    )
}
