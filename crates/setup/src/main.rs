#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod driver;
use eframe::egui;
use std::{
    fs,
    io::Cursor,
    path::{Path, PathBuf},
    sync::mpsc,
};
static PAYLOAD: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/payload.zip"));
fn main() {
    let args: Vec<_> = std::env::args().collect();
    if let Some(i) = args
        .iter()
        .position(|a| a == "--extract-only" || a == "--install")
    {
        let result = args
            .get(i + 1)
            .ok_or_else(|| "Missing destination".to_owned())
            .and_then(|p| {
                install(
                    Path::new(p),
                    args.iter().any(|a| a == "--usb"),
                    args[i] == "--install",
                )
            });
        if let Err(error) = result {
            eprintln!("{error}");
            if let Some(dir) = args.get(i + 1) {
                let _ = fs::write(Path::new(dir).join("installation-error.txt"), error);
            }
            std::process::exit(1);
        }
        return;
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([680.0, 430.0])
            .with_title("LabDAQ 安装"),
        ..Default::default()
    };
    let _ = eframe::run_native(
        "LabDAQ 安装",
        options,
        Box::new(|cc| {
            let mut fonts = egui::FontDefinitions::default();
            if let Ok(bytes) = fs::read("C:/Windows/Fonts/msyh.ttc") {
                fonts
                    .font_data
                    .insert("chinese".into(), egui::FontData::from_owned(bytes).into());
                fonts
                    .families
                    .get_mut(&egui::FontFamily::Proportional)
                    .unwrap()
                    .insert(0, "chinese".into());
            }
            cc.egui_ctx.set_fonts(fonts);
            cc.egui_ctx.set_visuals(egui::Visuals::light());
            Ok(Box::new(Setup::new()))
        }),
    );
}
fn has_driver() -> bool {
    zip::ZipArchive::new(Cursor::new(PAYLOAD))
        .ok()
        .is_some_and(|mut z| z.by_name("driver/labdaq-winusb.cat").is_ok())
}
fn extract(bytes: &[u8], destination: &Path) -> Result<(), String> {
    let mut archive =
        zip::ZipArchive::new(Cursor::new(bytes)).map_err(|e| format!("安装资源不可用：{e}"))?;
    if archive.by_name("LabDAQ.exe").is_err() || archive.by_name("labdaq-core.exe").is_err() {
        return Err("安装资源缺少主程序或采集核心".into());
    }
    fs::create_dir_all(destination).map_err(|e| e.to_string())?;
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let relative = entry
            .enclosed_name()
            .ok_or("安装包含越界路径")?
            .to_path_buf();
        let target = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(target).map_err(|e| e.to_string())?;
        } else {
            if let Some(p) = target.parent() {
                fs::create_dir_all(p).map_err(|e| e.to_string())?;
            }
            let mut out = fs::File::create(target)
                .map_err(|e| format!("无法写入程序，请先关闭 LabDAQ 和后台核心：{e}"))?;
            std::io::copy(&mut entry, &mut out).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}
fn install(destination: &Path, usb: bool, register: bool) -> Result<(), String> {
    if usb && !has_driver() {
        return Err("此构建尚未包含有效签名的 USB 驱动包，无法完成 USB 自动安装。可先安装软件进行模拟验证。".into());
    }
    extract(PAYLOAD, destination)?;
    if usb {
        let output = std::process::Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
            ])
            .arg(destination.join("driver/install-driver.ps1"))
            .arg("-DriverDirectory")
            .arg(destination.join("driver"))
            .output()
            .map_err(|e| e.to_string())?;
        if !output.status.success() {
            return Err(String::from_utf8_lossy(&output.stderr).into_owned());
        }
        let bound = driver::bind_present_devices(&destination.join("driver/labdaq-winusb.inf"))?;
        if bound.reboot_required || bound.verification_pending {
            return Err("驱动包已安装，需要重启或设备重新枚举后完成绑定核对。".into());
        }
    }
    if register {
        create_shortcuts(destination)?;
    }
    Ok(())
}
fn create_shortcuts(destination: &Path) -> Result<(), String> {
    let menu = PathBuf::from(std::env::var_os("APPDATA").ok_or("APPDATA unavailable")?)
        .join("Microsoft/Windows/Start Menu/Programs/LabDAQ");
    fs::create_dir_all(&menu).map_err(|e| e.to_string())?;
    // InternetShortcut launches a local executable without scripts or a Python runtime.
    let exe = destination.join("LabDAQ.exe");
    fs::write(
        menu.join("LabDAQ.url"),
        format!(
            "[InternetShortcut]\r\nURL=file:///{}\r\nIconFile={}\r\nIconIndex=0\r\n",
            exe.to_string_lossy().replace('\\', "/"),
            exe.display()
        ),
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}
#[cfg(windows)]
fn elevated_install(destination: &str) -> Result<(), String> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{GetExitCodeProcess, INFINITE, WaitForSingleObject},
        UI::Shell::{SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW, ShellExecuteExW},
    };
    let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let file = wide(&exe.to_string_lossy());
    let verb = wide("runas");
    let args = wide(&format!("--install \"{}\" --usb", destination));
    unsafe {
        let mut info: SHELLEXECUTEINFOW = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = args.as_ptr();
        info.nShow = 0;
        if ShellExecuteExW(&mut info) == 0 {
            return Err(format!(
                "管理员授权或安装启动失败：{}",
                std::io::Error::last_os_error()
            ));
        }
        WaitForSingleObject(info.hProcess, INFINITE);
        let mut code = 1;
        GetExitCodeProcess(info.hProcess, &mut code);
        CloseHandle(info.hProcess);
        if code != 0 {
            return Err(
                fs::read_to_string(Path::new(destination).join("installation-error.txt"))
                    .unwrap_or_else(|_| format!("安装未完成（退出码 {code}）")),
            );
        }
    }
    Ok(())
}
#[cfg(not(windows))]
fn elevated_install(_: &str) -> Result<(), String> {
    Err("Windows only".into())
}
struct Setup {
    directory: String,
    usb: bool,
    available: bool,
    status: String,
    job: Option<mpsc::Receiver<Result<(), String>>>,
    done: bool,
}
impl Setup {
    fn new() -> Self {
        let available = has_driver();
        Self {
            directory: PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_default())
                .join("Programs/LabDAQ")
                .to_string_lossy()
                .into(),
            usb: available,
            available,
            status: String::new(),
            job: None,
            done: false,
        }
    }
}
impl eframe::App for Setup {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        if let Some(result) = self.job.as_ref().and_then(|r| r.try_recv().ok()) {
            self.job = None;
            match result {
                Ok(()) => {
                    self.done = true;
                    self.status = "软件安装完成。".into();
                }
                Err(e) => self.status = e,
            }
        }
        egui::CentralPanel::default().show(ctx,|ui|{ui.add_space(18.0);ui.heading("安装 LabDAQ");ui.label("Rust 数据采集与信号分析软件");ui.separator();ui.label("安装目录");ui.text_edit_singleline(&mut self.directory);ui.add_enabled(self.available,egui::Checkbox::new(&mut self.usb,"同时安装 DT9837 系列 WinUSB 驱动（需要管理员授权）"));
 if !self.available{ui.colored_label(egui::Color32::from_rgb(175,90,20),"当前构建缺少正式签名的驱动包。可安装软件进行模拟或使用已配置好的设备。");}
 ui.small("软件数据保存在用户目录；安装不会删除已有记录。驱动绑定覆盖冷启动与运行态两个身份。");ui.add_space(15.0);
 if ui.add_enabled(self.job.is_none()&&!self.done,egui::Button::new("安装").min_size(egui::vec2(130.0,40.0))).clicked(){let dir=self.directory.clone();let usb=self.usb;let(tx,rx)=mpsc::channel();std::thread::spawn(move||{let result=if usb{elevated_install(&dir)}else{install(Path::new(&dir),false,true)};let _=tx.send(result);});self.job=Some(rx);}
 if self.job.is_some(){ui.spinner();ui.label("正在安装…");}ui.label(&self.status);if self.done&&ui.button("打开 LabDAQ（模拟输入）").clicked(){let _=std::process::Command::new(Path::new(&self.directory).join("LabDAQ.exe")).arg("--demo").spawn();ctx.send_viewport_cmd(egui::ViewportCommand::Close);}
 });
        ctx.request_repaint_after(std::time::Duration::from_millis(100));
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_missing_payload() {
        assert!(extract(&[], Path::new("unused")).is_err());
    }
    #[test]
    fn unsigned_driver_not_reported_installed() {
        if !has_driver() {
            assert!(
                install(Path::new("unused"), true, false)
                    .unwrap_err()
                    .contains("签名")
            );
        }
    }
}
