use serde_json::{Value, json};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct CoreClient {
    pub endpoint: PathBuf,
    port: u16,
    token: String,
}

pub fn user_directory() -> PathBuf {
    let base = std::env::var_os("LABDAQ_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(std::env::temp_dir)
                .join("LabDAQ")
        });
    if std::env::args().any(|a| a == "--demo" || a == "--ui-qa") {
        base.join("simulation")
    } else {
        base
    }
}

pub fn core_executable() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("LABDAQ_CORE_EXE") {
        return Ok(path.into());
    }
    let dir = std::env::current_exe()
        .map_err(|e| e.to_string())?
        .parent()
        .unwrap()
        .to_path_buf();
    let name = if cfg!(windows) {
        "labdaq-core.exe"
    } else {
        "labdaq-core"
    };
    let paths = [
        dir.join(name),
        dir.join("../").join(name),
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../target/release")
            .join(name),
    ];
    paths
        .into_iter()
        .find(|p| p.is_file())
        .ok_or_else(|| "找不到采集核心，请重新安装 LabDAQ 或先构建 workspace。".into())
}

impl CoreClient {
    pub fn from_endpoint(path: &Path) -> Result<Self, String> {
        let value: Value = serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        Ok(Self {
            endpoint: path.into(),
            port: value["port"]
                .as_u64()
                .filter(|n| *n > 0 && *n <= 65535)
                .ok_or("核心端口无效")? as u16,
            token: value["token"].as_str().ok_or("核心连接凭据缺失")?.into(),
        })
    }
    pub fn request(&self, mut request: Value) -> Result<Value, String> {
        let op = request["op"].as_str().unwrap_or("").to_owned();
        request["token"] = json!(self.token);
        let address: SocketAddr = ([127, 0, 0, 1], self.port).into();
        let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
            .map_err(|e| format!("无法连接采集核心：{e}"))?;
        stream
            .set_read_timeout(Some(Duration::from_secs(
                if matches!(op.as_str(), "start" | "connect") {
                    30
                } else {
                    6
                },
            )))
            .map_err(|e| e.to_string())?;
        stream
            .set_write_timeout(Some(Duration::from_secs(3)))
            .map_err(|e| e.to_string())?;
        let mut bytes = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
        bytes.push(b'\n');
        stream.write_all(&bytes).map_err(|e| e.to_string())?;
        let mut line = String::new();
        BufReader::new(stream.take(32 * 1024 * 1024))
            .read_line(&mut line)
            .map_err(|e| e.to_string())?;
        let response: Value =
            serde_json::from_str(&line).map_err(|e| format!("核心响应不完整：{e}"))?;
        if response["ok"] == true {
            Ok(response["state"].clone())
        } else {
            Err(response["error"].as_str().unwrap_or("核心请求失败").into())
        }
    }
    pub fn connect_or_start(endpoint: &Path) -> Result<(Self, Value), String> {
        fs::create_dir_all(endpoint.parent().ok_or("端点路径无父目录")?)
            .map_err(|e| e.to_string())?;
        let startup_lock = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(endpoint.with_extension("lock"))
            .map_err(|e| e.to_string())?;
        startup_lock.lock().map_err(|e| e.to_string())?;
        if endpoint.exists() {
            let client = Self::from_endpoint(endpoint)?;
            let address: SocketAddr = ([127, 0, 0, 1], client.port).into();
            match TcpStream::connect_timeout(&address, Duration::from_secs(2)) {
                Ok(socket) => {
                    drop(socket);
                    let state = client
                        .request(json!({"op":"poll"}))
                        .map_err(|e| format!("已有核心状态未知，未另启核心：{e}"))?;
                    return Ok((client, state));
                }
                Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {}
                Err(e) => return Err(format!("已有核心连接状态未知，未另启核心：{e}")),
            }
        }
        let token = format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        let exe = core_executable()?;
        let mut command = Command::new(&exe);
        command
            .args([endpoint.to_string_lossy().as_ref(), "0", &token])
            .stdin(Stdio::null())
            .stdout(Stdio::null());
        let log = fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(endpoint.with_extension("log"))
            .map_err(|e| e.to_string())?;
        command.stderr(log).current_dir(exe.parent().unwrap());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000 | 0x00000200);
        }
        let mut child = command
            .spawn()
            .map_err(|e| format!("启动采集核心失败：{e}"))?;
        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline {
            if let Ok(client) = Self::from_endpoint(endpoint) {
                if client.token == token {
                    let state = client.request(json!({"op":"poll"}))?;
                    return Ok((client, state));
                }
            }
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                return Err(format!("采集核心退出：{status}"));
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("采集核心启动超时，请查看程序数据目录中的 core.log".into())
    }
}
