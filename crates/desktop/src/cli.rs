use serde_json::{Value, json};
use std::{
    io::{self, Read},
    path::PathBuf,
};
fn main() {
    match run() {
        Ok(value) => println!("{}", serde_json::to_string_pretty(&value).unwrap()),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
fn run() -> Result<Value, String> {
    let args: Vec<_> = std::env::args().collect();
    let command = args.get(1).map(String::as_str).unwrap_or("help");
    if command == "help" {
        return Ok(
            json!({"usage":["labdaq-cli data request.json (or - for stdin)","labdaq-cli analyze request.json","labdaq-cli filter request.json","labdaq-cli core endpoint.json request.json","labdaq-cli demo-profile"],"runtime":"Rust","usb":"Only explicit core requests can connect hardware; data/analyze/filter never access USB."}),
        );
    }
    if command == "demo-profile" {
        let mut s = labdaq_desktop::model::Settings::default();
        s.source = "simulation".into();
        return Ok(s.profile());
    }
    let file = args
        .get(if command == "core" { 3 } else { 2 })
        .ok_or("缺少 JSON 请求文件（- 表示标准输入）")?;
    let bytes = if file == "-" {
        let mut b = Vec::new();
        io::stdin().read_to_end(&mut b).map_err(|e| e.to_string())?;
        b
    } else {
        std::fs::read(file).map_err(|e| e.to_string())?
    };
    let request: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    match command {
        "data" => labdaq_data::execute_json(&request),
        "analyze" => labdaq_analysis::analyze_json(&request),
        "filter" => labdaq_analysis::filter_json(&request),
        "core" => {
            let path = PathBuf::from(args.get(2).ok_or("缺少核心连接文件")?);
            labdaq_desktop::client::CoreClient::from_endpoint(&path)?.request(request)
        }
        _ => Err(format!("未知命令：{command}")),
    }
}
