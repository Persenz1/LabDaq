//! Opt-in real-window rendering check. It always runs against isolated simulation.
use super::*;
use std::time::Instant;
pub(super) struct Run {
    directory: PathBuf,
    stage: u8,
    started: Instant,
    last: Instant,
    recording: PathBuf,
}
impl Run {
    pub(super) fn from_args() -> Option<Self> {
        let args: Vec<_> = std::env::args().collect();
        let i = args.iter().position(|a| a == "--ui-qa")?;
        let directory = PathBuf::from(args.get(i + 1)?);
        std::fs::create_dir_all(&directory).ok()?;
        Some(Self {
            recording: directory.join("recording"),
            directory,
            stage: 0,
            started: Instant::now(),
            last: Instant::now(),
        })
    }
    fn capture(&mut self, app: &mut LabDaqApp, ctx: &egui::Context, page: Page, name: &str) {
        app.page = page;
        app.last_capture = Some(self.directory.join(name));
        ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
        self.last = Instant::now();
    }
    pub(super) fn tick(&mut self, app: &mut LabDaqApp, ctx: &egui::Context) {
        if self.started.elapsed() > Duration::from_secs(60) {
            let _ = std::fs::write(
                self.directory.join("result.json"),
                json!({"ok":false,"stage":self.stage,"error":app.error}).to_string(),
            );
            self.finish(app, ctx);
            return;
        }
        if self.last.elapsed() < Duration::from_millis(400) || app.last_capture.is_some() {
            return;
        }
        match self.stage {
            0 if app.live.samples.first().is_some_and(|v| v.len() > 500) => {
                self.capture(app, ctx, Page::Live, "01-live.png");
                app.command(json!({"op":"start_recording","path":self.recording,"channels":app.settings.channels,"experiment":"UI 模拟验证","recording_options":{"duration_seconds":1.0,"group_number":1}}));
                self.stage = 1;
            }
            1 if app.state["last_recording"] == json!(self.recording)
                && app.state["recording"] == false =>
            {
                app.open_recording(self.recording.to_string_lossy().into());
                self.stage = 2;
            }
            2 if !app.replay.samples.is_empty() && app.job.is_none() => {
                self.capture(app, ctx, Page::Library, "02-library.png");
                let request = json!({"task":"analysis","samples":app.replay.samples,"sample_rate":app.replay.sample_rate,"nfft":1024,"mode":"amplitude","window":"hann"});
                app.start_job("频域分析", request);
                self.stage = 3;
            }
            3 if !app.analysis_result.is_null() && app.job.is_none() => {
                self.capture(app, ctx, Page::Analysis, "03-analysis.png");
                app.start_job("比较记录",json!({"op":"compare","sources":[{"path":self.recording},{"path":self.recording,"shift_seconds":0.02}],"axis":"relative","window":{"start":0.0,"span":2.0},"values":"engineering"}));
                self.stage = 4;
            }
            4 if app.comparison_result["traces"].is_array() && app.job.is_none() => {
                self.capture(app, ctx, Page::Compare, "04-comparison.png");
                app.command(
                    json!({"op":"preview_output","settings":app.settings.output,"count":12000}),
                );
                self.stage = 5;
            }
            5 if !app.output_preview.samples.is_empty() => {
                self.capture(app, ctx, Page::Output, "05-output.png");
                self.stage = 6;
            }
            6 => {
                self.capture(app, ctx, Page::Settings, "06-settings.png");
                self.stage = 7;
            }
            7 => {
                let ok = [
                    "01-live.png",
                    "02-library.png",
                    "03-analysis.png",
                    "04-comparison.png",
                    "05-output.png",
                    "06-settings.png",
                ]
                .iter()
                .all(|n| {
                    self.directory
                        .join(n)
                        .metadata()
                        .is_ok_and(|m| m.len() > 5000)
                });
                let _ = std::fs::write(
                    self.directory.join("result.json"),
                    json!({"ok":ok,"pages":6,"error":app.error}).to_string(),
                );
                self.finish(app, ctx);
            }
            _ => {}
        }
    }
    fn finish(&mut self, app: &mut LabDaqApp, ctx: &egui::Context) {
        if let Ok(c) = CoreClient::from_endpoint(&user_directory().join("runtime/core.json")) {
            let _ = c.request(json!({"op":"stop"}));
            let _ = c.request(json!({"op":"stop_output"}));
            let _ = c.request(json!({"op":"shutdown"}));
        }
        app.settings.auto_connect = false;
        self.stage = 255;
        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
    }
}
