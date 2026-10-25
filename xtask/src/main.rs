use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
fn main() {
    if let Err(e) = run() {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .into()
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    match args.get(1).map(String::as_str).unwrap_or("help") {
        "firmware" => prepare_firmware(&root())?,
        "package" => package(&root())?,
        "help" => println!(
            "cargo run -p xtask -- firmware | package\nfirmware: convert local archived libuldaq C arrays.\npackage: assemble release ZIP and self-contained Rust setup EXE."
        ),
        other => return Err(format!("Unknown task: {other}").into()),
    }
    Ok(())
}
fn parse_records(source: &str) -> Result<Vec<Value>, Box<dyn std::error::Error>> {
    let re = regex::Regex::new(
        r"(?s)(\d+)\s*,\s*(0[xX][0-9a-fA-F]+|\d+)\s*,\s*(\d+)\s*,\s*\{([^}]*)\}",
    )?;
    let parse = |s: &str| -> Result<u64, std::num::ParseIntError> {
        if s.starts_with("0x") || s.starts_with("0X") {
            u64::from_str_radix(&s[2..], 16)
        } else {
            s.parse()
        }
    };
    let mut records = Vec::new();
    for c in re.captures_iter(source) {
        let len = parse(&c[1])?;
        let address = parse(&c[2])?;
        let typ = parse(&c[3])?;
        if typ == 1 {
            continue;
        }
        if typ != 0 {
            return Err("Unsupported firmware record type".into());
        }
        let data: Vec<u64> = c[4]
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(parse)
            .collect::<Result<_, _>>()?;
        if len != data.len() as u64
            || len == 0
            || len > 16
            || address + len > 65536
            || data.iter().any(|b| *b > 255)
        {
            return Err("Invalid firmware record".into());
        }
        records.push(json!({"address":address,"data":data}));
    }
    if records.is_empty() {
        return Err("No firmware records found".into());
    }
    Ok(records)
}
fn prepare_firmware(root: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let source = root.join("resources/opensource/libuldaq-1.2.2/src/usb/fw");
    let loader = fs::read_to_string(source.join("DTFX2LDR_FW.c"))?;
    let runtime = fs::read_to_string(source.join("DT9837A_FW.c"))?;
    let notice = loader.split("#include").next().unwrap_or("");
    let result = json!({"format":"dt9837-ram-v1","source":"libuldaq 1.2.2: DTFX2LDR_FW.c, DT9837A_FW.c","loader_notice":notice,"loader":parse_records(&loader)?,"runtime":parse_records(&runtime)?});
    let target = root.join("resources/firmware");
    fs::create_dir_all(&target)?;
    fs::write(
        target.join("dt9837-ram.json"),
        serde_json::to_vec_pretty(&result)?,
    )?;
    fs::write(
        target.join("NOTICE.txt"),
        format!(
            "Prepared from https://github.com/mccdaq/uldaq/tree/v1.2.2/src/usb/fw\n\n{notice}\nFirmware resource is locally prepared; source repository excludes firmware images.\n"
        ),
    )?;
    println!(
        "Prepared {} loader + {} runtime records",
        result["loader"].as_array().unwrap().len(),
        result["runtime"].as_array().unwrap().len()
    );
    Ok(())
}
fn copy(source: &Path, dest: &Path) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(p) = dest.parent() {
        fs::create_dir_all(p)?;
    }
    fs::copy(source, dest)?;
    Ok(())
}
fn zip_tree(
    folder: &Path,
    prefix: &Path,
    w: &mut zip::ZipWriter<fs::File>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut entries = fs::read_dir(folder)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let path = e.path();
        if path.is_dir() {
            zip_tree(&path, prefix, w)?;
        } else {
            let name = path
                .strip_prefix(prefix)?
                .to_string_lossy()
                .replace('\\', "/");
            w.start_file(
                name,
                zip::write::SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated),
            )?;
            let mut file = fs::File::open(path)?;
            std::io::copy(&mut file, w)?;
        }
    }
    Ok(())
}
fn package(root: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let release = root.join("target/release");
    let dist = root.join("dist");
    let stage = dist.join("LabDAQ-0.2.0-windows-x64");
    if stage.exists() {
        return Err("Package directory already exists; move the obsolete package to the user's Delete directory before rebuilding.".into());
    }
    fs::create_dir_all(&stage)?;
    for name in ["LabDAQ.exe", "labdaq-core.exe", "labdaq-cli.exe"] {
        copy(&release.join(name), &stage.join(name))?;
    }
    // The GNU Windows toolchain uses these runtimes only if they occur in the binary import table.
    let mingw = PathBuf::from("C:/DevTools/MinGW/mingw64/bin");
    let mut dlls = std::collections::BTreeSet::new();
    let mut todo = vec![
        "LabDAQ.exe".to_owned(),
        "labdaq-core.exe".to_owned(),
        "labdaq-cli.exe".to_owned(),
    ];
    while let Some(name) = todo.pop() {
        let output = Command::new(mingw.join("objdump.exe"))
            .args(["-p", stage.join(&name).to_str().ok_or("Invalid path")?])
            .output()?;
        if !output.status.success() {
            return Err("Unable to inspect executable dependencies".into());
        }
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            if let Some(dll) = line.trim().strip_prefix("DLL Name: ") {
                if mingw.join(dll).is_file() && dlls.insert(dll.to_owned()) {
                    copy(&mingw.join(dll), &stage.join(dll))?;
                    todo.push(dll.to_owned());
                }
            }
        }
    }
    for name in ["README.md", "THIRD_PARTY_NOTICES.md"] {
        copy(&root.join(name), &stage.join(name))?;
    }
    for name in ["dt9837-ram.json", "NOTICE.txt"] {
        copy(
            &root.join("resources/firmware").join(name),
            &stage.join("resources/firmware").join(name),
        )?;
    }
    copy(
        &root.join("configs/电信号模拟.json"),
        &stage.join("configs/电信号模拟.json"),
    )?;
    for entry in fs::read_dir(root.join("installer/driver"))? {
        let p = entry?.path();
        if p.is_file() {
            copy(&p, &stage.join("driver").join(p.file_name().unwrap()))?;
        }
    }
    copy(
        &root.join("docs/用户手册/rust-version.md"),
        &stage.join("使用说明.md"),
    )?;
    let zip_path = dist.join("LabDAQ-0.2.0-windows-x64.zip");
    let mut zip = zip::ZipWriter::new(fs::File::create(&zip_path)?);
    zip_tree(&stage, &stage, &mut zip)?;
    zip.finish()?;
    let status = Command::new("cargo")
        .current_dir(root)
        .env("LABDAQ_PAYLOAD", &zip_path)
        .args(["build", "--release", "-p", "labdaq-setup"])
        .status()?;
    if !status.success() {
        return Err("Setup build failed".into());
    }
    copy(
        &release.join("LabDAQ-Setup.exe"),
        &dist.join("LabDAQ-Setup.exe"),
    )?;
    println!(
        "{}\n{}",
        zip_path.display(),
        dist.join("LabDAQ-Setup.exe").display()
    );
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn firmware_records_validate_lengths_and_values() {
        assert_eq!(
            parse_records("2, 0x100, 0, {0x01,0xff}, 0,0,1,{}")
                .unwrap()
                .len(),
            1
        );
        assert!(parse_records("2,0,0,{1}").is_err());
        assert!(parse_records("1,0,0,{256}").is_err());
        assert!(parse_records("1,65536,0,{1}").is_err());
    }
}
