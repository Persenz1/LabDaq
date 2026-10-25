fn main() {
    println!("cargo:rerun-if-env-changed=LABDAQ_PAYLOAD");
    let dest = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("payload.zip");
    if let Some(path) = std::env::var_os("LABDAQ_PAYLOAD") {
        println!(
            "cargo:rerun-if-changed={}",
            std::path::Path::new(&path).display()
        );
        std::fs::copy(path, dest).expect("copy installer payload");
    } else {
        std::fs::write(dest, []).unwrap();
    }
}
