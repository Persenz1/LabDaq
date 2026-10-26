//! Native DT Open Layers C backend (no Python or .NET).
//!
//! ABI: DataAcq SDK 7.8.9 OLDAAPI.H, OLDADEFS.H, OLMEM.h, OLTYPES.H;
//! ANSI Win32 examples ContAdc, IepContAdc, ContDac. On Windows x64 the
//! SDK handles/LPARAM are 64 bit, ECODE/UINT/DWORD/enums are 32 bit, TCHAR
//! is ANSI in these exports. A compiled SDK-header probe verified these widths.
use crate::{
    Config, output,
    usb_input::{DeviceSelector, Fault, set_fault},
};
use libloading::Library;
use serde_json::{Value, json};
use std::{
    ffi::{CStr, CString, c_char, c_void},
    path::PathBuf,
    ptr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

type Handle = *mut c_void;
type Ecode = u32;
type EnumCallback = unsafe extern "system" fn(*mut c_char, *mut c_char, isize) -> i32;
type EnumBoards = unsafe extern "system" fn(Option<EnumCallback>, isize) -> Ecode;
type Initialize = unsafe extern "system" fn(*mut c_char, *mut Handle) -> Ecode;
type One = unsafe extern "system" fn(Handle) -> Ecode;
type SetUint = unsafe extern "system" fn(Handle, u32) -> Ecode;
type GetUint = unsafe extern "system" fn(Handle, *mut u32) -> Ecode;
type SetDouble = unsafe extern "system" fn(Handle, f64) -> Ecode;
type GetDouble = unsafe extern "system" fn(Handle, *mut f64) -> Ecode;
type GetDass = unsafe extern "system" fn(Handle, i32, u32, *mut Handle) -> Ecode;
type GetCaps = unsafe extern "system" fn(Handle, i32, *mut u32) -> Ecode;
type GetName = unsafe extern "system" fn(Handle, *mut c_char, u32) -> Ecode;
type BoardInfo =
    unsafe extern "system" fn(*mut c_char, *mut c_char, *mut c_char, u32, *mut u32) -> Ecode;
type SetEntry = unsafe extern "system" fn(Handle, u32, u32) -> Ecode;
type SetGain = unsafe extern "system" fn(Handle, u32, f64) -> Ecode;
type SetChannelEnum = unsafe extern "system" fn(Handle, u32, i32) -> Ecode;
type GetRange = unsafe extern "system" fn(Handle, *mut f64, *mut f64) -> Ecode;
type SetRange = unsafe extern "system" fn(Handle, f64, f64) -> Ecode;
type PutBuffer = unsafe extern "system" fn(Handle, Handle) -> Ecode;
type GetBuffer = unsafe extern "system" fn(Handle, *mut Handle) -> Ecode;
type Calloc = unsafe extern "system" fn(u32, u32, u32, u32, *mut Handle) -> Ecode;
type BufferPtr = unsafe extern "system" fn(Handle, *mut *mut c_void) -> Ecode;
type Single = unsafe extern "system" fn(Handle, i32, u32, f64) -> Ecode;

macro_rules! api {
    ($($name:ident:$kind:ty),*$(,)?)=>{
        #[allow(non_snake_case)]
        struct Api {_da:Library,_mem:Library,$($name:$kind),*}
        #[allow(non_snake_case)]
        impl Api {
            fn load()->Result<Arc<Self>,Fault> {
                if !cfg!(windows) {return Err(Fault::new("vendor_runtime_required","原厂 Open Layers 后端仅支持 Windows"));}
                let folder=std::env::var_os("LABDAQ_VENDOR_RUNTIME").map(PathBuf::from).unwrap_or_else(||
                    PathBuf::from(std::env::var_os("WINDIR").unwrap_or_else(||"C:\\Windows".into())).join("System32"));
                // Explicit full paths avoid loading unrelated DLLs from a recording folder.
                let da=unsafe{Library::new(folder.join("oldaapi64.dll"))}.map_err(|e|Fault::new("vendor_runtime_required",format!("无法加载原厂 x64 Open Layers：{e}。请运行 LabDAQ 安装器安装原厂驱动与运行库。")))?;
                let mem=unsafe{Library::new(folder.join("olmem64.dll"))}.map_err(|e|Fault::new("vendor_runtime_required",format!("无法加载原厂 olmem64.dll：{e}；需要原厂 OEM 运行时及其 Visual C++ 依赖。")))?;
                $(let $name=unsafe{*if stringify!($name).starts_with("olDm") {&mem}else{&da}
                    .get::<$kind>(concat!(stringify!($name),"\0").as_bytes())
                    .map_err(|e|Fault::new("vendor_runtime_error",format!("原厂运行库缺少 {}：{e}",stringify!($name))))?};)*
                Ok(Arc::new(Self{_da:da,_mem:mem,$($name),*}))
            }
        }
    }
}
api! {
    olDaEnumBoards:EnumBoards,olDaInitialize:Initialize,olDaTerminate:One,
    olDaGetBoardInfo:BoardInfo,olDaGetDeviceName:GetName,olDaGetDASS:GetDass,olDaReleaseDASS:One,
    olDaGetSSCaps:GetCaps,olDaSetDataFlow:SetUint,olDaSetWrapMode:SetUint,olDaSetChannelType:SetUint,
    olDaSetChannelListSize:SetUint,olDaSetChannelListEntry:SetEntry,olDaSetGainListEntry:SetGain,
    olDaSetEncoding:SetUint,olDaGetEncoding:GetUint,olDaGetResolution:GetUint,
    olDaSetRange:SetRange,olDaGetRange:GetRange,olDaSetClockSource:SetUint,
    olDaSetClockFrequency:SetDouble,olDaGetClockFrequency:GetDouble,olDaSetDmaUsage:SetUint,
    olDaSetTrigger:SetUint,olDaSetEdgeType:SetUint,olDaSetSyncMode:SetUint,
    olDaSetCouplingType:SetChannelEnum,olDaSetExcitationCurrentSource:SetChannelEnum,
    olDaConfig:One,olDaStart:One,olDaAbort:One,
    olDaFlushBuffers:One,olDaPutBuffer:PutBuffer,olDaGetBuffer:GetBuffer,olDaPutSingleValue:Single,
    olDmCallocBuffer:Calloc,olDmFreeBuffer:One,olDmGetBufferPtr:BufferPtr,
    olDmGetValidSamples:GetUint,olDmSetValidSamples:SetUint,olDmGetBufferECode:GetUint,
}

fn check(code: Ecode, operation: &str) -> Result<(), Fault> {
    if code == 0 {
        Ok(())
    } else {
        Err(Fault::new(
            "vendor_error",
            format!("原厂 {operation} 失败（ECODE 0x{code:08X}）"),
        ))
    }
}

fn stop_stream_worker(
    active: &AtomicBool,
    worker: &mut Option<JoinHandle<()>>,
    abort: impl FnOnce() -> Ecode,
) -> Result<(), Fault> {
    active.store(false, Ordering::Release);
    if let Some(worker) = worker.take() {
        worker
            .join()
            .map_err(|_| Fault::new("core_error", "原厂数据线程异常退出"))?;
    }
    // OLERRORS.H: OLNOTRUNNING (28) means the subsystem has already stopped.
    // This is only acceptable for Abort; data/buffer errors remain failures.
    let code = abort();
    if code != 28 {
        check(code, "中止缓冲流")?;
    }
    Ok(())
}

fn ready_buffer(api: &Api, subsystem: Handle, operation: &str) -> Result<Handle, Fault> {
    let mut buffer = ptr::null_mut();
    let code = unsafe { (api.olDaGetBuffer)(subsystem, &mut buffer) };
    // OLNOREADYBUFFERS is an empty completion queue, not an acquisition failure.
    if code != 64 {
        check(code, operation)?;
    }
    Ok(buffer)
}

#[cfg(windows)]
fn decode_ansi(bytes: &[u8]) -> String {
    use windows_sys::Win32::Globalization::{CP_ACP, MultiByteToWideChar};
    if bytes.is_empty() {
        return String::new();
    }
    let count = unsafe {
        MultiByteToWideChar(
            CP_ACP,
            0,
            bytes.as_ptr(),
            bytes.len() as i32,
            ptr::null_mut(),
            0,
        )
    };
    if count <= 0 {
        return String::from_utf8_lossy(bytes).into_owned();
    }
    let mut wide = vec![0u16; count as usize];
    unsafe {
        MultiByteToWideChar(
            CP_ACP,
            0,
            bytes.as_ptr(),
            bytes.len() as i32,
            wide.as_mut_ptr(),
            count,
        );
    }
    String::from_utf16_lossy(&wide)
}
#[cfg(not(windows))]
fn decode_ansi(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

#[derive(Clone)]
struct Board {
    name: String,
    ansi: Vec<u8>,
    driver: String,
    model: String,
}
unsafe extern "system" fn enum_board(name: *mut c_char, entry: *mut c_char, context: isize) -> i32 {
    if name.is_null() || context == 0 {
        return 0;
    }
    // SDK's synchronous enumerator owns the C strings during this callback.
    let bytes = unsafe { CStr::from_ptr(name) }.to_bytes().to_vec();
    let driver = if entry.is_null() {
        String::new()
    } else {
        decode_ansi(unsafe { CStr::from_ptr(entry) }.to_bytes())
    };
    unsafe {
        (&mut *(context as *mut Vec<Board>)).push(Board {
            name: decode_ansi(&bytes),
            ansi: bytes,
            driver,
            model: String::new(),
        });
    }
    1
}

fn boards(api: &Api) -> Result<Vec<Board>, Fault> {
    let mut list = Vec::<Board>::new();
    let code = unsafe { (api.olDaEnumBoards)(Some(enum_board), &mut list as *mut _ as isize) };
    // OLNOBOARDSINSTALLED is a valid empty scan, not a missing runtime or driver error.
    if code != 78 {
        check(code, "枚举设备")?;
    }
    for b in &mut list {
        let name = CString::new(b.ansi.clone()).unwrap();
        let mut driver = [0i8; 256];
        let mut model = [0i8; 256];
        let mut instance = 0;
        check(
            unsafe {
                (api.olDaGetBoardInfo)(
                    name.as_ptr().cast_mut(),
                    driver.as_mut_ptr(),
                    model.as_mut_ptr(),
                    256,
                    &mut instance,
                )
            },
            "读取型号信息",
        )?;
        b.driver = decode_ansi(unsafe { CStr::from_ptr(driver.as_ptr()) }.to_bytes());
        b.model = decode_ansi(unsafe { CStr::from_ptr(model.as_ptr()) }.to_bytes());
    }
    list.retain(|b| {
        format!("{} {}", b.driver, b.model)
            .to_uppercase()
            .contains("9837")
    });
    Ok(list)
}

pub fn enumerate() -> Result<Vec<Value>, Fault> {
    let api = Api::load()?;
    Ok(boards(&api)?.into_iter().map(|b|json!({"vendor_name":b.name,"name":b.name,"driver":b.driver,"model":b.model,"backend":"dt-open-layers-c-abi"})).collect())
}

fn check_format(bits: u32, encoding: u32, min: f64, max: f64) -> Result<(), Fault> {
    if bits != 24
        || ![200, 201].contains(&encoding)
        || !min.is_finite()
        || !max.is_finite()
        || max <= min
    {
        return Err(Fault::new(
            "vendor_format_error",
            "DT9837C 必须返回 24 位整数、二进制/补码编码和有效电压量程",
        ));
    }
    Ok(())
}

fn decode_voltage(code: u32, bits: u32, encoding: u32, min: f64, max: f64, gain: f64) -> f64 {
    let mask = (1u32 << bits) - 1;
    let binary = if encoding == 201 {
        (code ^ (1 << (bits - 1))) & mask
    } else {
        code & mask
    };
    (binary as f64 * (max - min) / (1u32 << bits) as f64 + min) / gain
}

fn encode_voltage(volts: f64, bits: u32, encoding: u32, min: f64, max: f64) -> u32 {
    let mask = (1u32 << bits) - 1;
    let binary = ((volts - min) / (max - min) * (1u32 << bits) as f64)
        .round()
        .clamp(0., mask as f64) as u32;
    if encoding == 200 {
        binary
    } else {
        let code = binary ^ (1 << (bits - 1));
        if code & (1 << (bits - 1)) != 0 {
            code | !mask
        } else {
            code
        }
    }
}

struct Stream {
    api: Arc<Api>,
    gate: Arc<Mutex<()>>,
    subsystem: usize,
    buffers: Vec<usize>,
    active: Arc<AtomicBool>,
    completed: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}
impl Stream {
    fn stop(&mut self) -> Result<(), Fault> {
        // GetBuffer is a nonblocking completion-queue poll. Join the reader
        // before Abort moves unfinished buffers to that queue, otherwise a
        // reader already waiting for the gate can report a false stop fault.
        stop_stream_worker(&self.active, &mut self.worker, || {
            let _guard = self.gate.lock().unwrap();
            unsafe { (self.api.olDaAbort)(self.subsystem as Handle) }
        })?; // Do not release driver-owned memory if DMA could still be active.
        let _guard = self.gate.lock().unwrap();
        check(
            unsafe { (self.api.olDaFlushBuffers)(self.subsystem as Handle) },
            "清空缓冲队列",
        )?;
        for _ in 0..=self.buffers.len() {
            let h = ready_buffer(&self.api, self.subsystem as Handle, "收回缓冲区")?;
            if h.is_null() {
                break;
            }
        }
        for handle in self.buffers.drain(..) {
            check(
                unsafe { (self.api.olDmFreeBuffer)(handle as Handle) },
                "释放缓冲区",
            )?;
        }
        Ok(())
    }
}

pub struct VendorInput {
    api: Arc<Api>,
    gate: Arc<Mutex<()>>,
    device: Handle,
    ad: Handle,
    da: Handle,
    pub name: String,
    pub info: Value,
    pub actual_rate: f64,
    pub blocks: Receiver<Vec<f64>>,
    fault: Arc<Mutex<Option<Fault>>>,
    input: Option<Stream>,
    output: Option<Stream>,
    pub output_status: output::Status,
    output_settings: Arc<Mutex<output::Settings>>,
}

impl VendorInput {
    pub fn connect(selector: Option<&DeviceSelector>) -> Result<Self, Fault> {
        let api = Api::load()?;
        let mut devices = boards(&api)?;
        if let Some(name) = selector.and_then(|s| s.vendor_name.as_deref()) {
            devices.retain(|b| b.name == name);
        }
        if devices.is_empty() {
            return Err(Fault::new(
                "device_not_found",
                "未检测到原厂驱动识别的 DT9837C，请插入设备并确认已切换原厂驱动",
            ));
        }
        if devices.len() != 1 {
            return Err(Fault::new(
                "device_selection",
                format!(
                    "原厂驱动检测到 {} 台匹配设备，请选择具体设备名",
                    devices.len()
                ),
            ));
        }
        let board = devices.remove(0);
        let name = CString::new(board.ansi).unwrap();
        let mut device = ptr::null_mut();
        check(
            unsafe { (api.olDaInitialize)(name.as_ptr().cast_mut(), &mut device) },
            "初始化设备",
        )?;
        let (_, blocks) = mpsc::sync_channel(64);
        let mut input = Self {
            api,
            gate: Arc::new(Mutex::new(())),
            device,
            ad: ptr::null_mut(),
            da: ptr::null_mut(),
            name: board.name.clone(),
            info: json!({"model":board.model,"vendor_name":board.name,"driver":board.driver,"backend":"dt-open-layers-c-abi"}),
            actual_rate: 0.,
            blocks,
            fault: Arc::new(Mutex::new(None)),
            input: None,
            output: None,
            output_status: output::Status::default(),
            output_settings: Arc::new(Mutex::new(output::Settings::default())),
        };
        check(
            unsafe { (input.api.olDaGetDASS)(input.device, 0, 0, &mut input.ad) },
            "获取模拟输入子系统",
        )?;
        check(
            unsafe { (input.api.olDaGetDASS)(input.device, 1, 0, &mut input.da) },
            "获取 DT9837C 模拟输出子系统",
        )?;
        let (mut bits, mut channels, mut max, mut min) = (0, 0, 0., 0.);
        check(
            unsafe { (input.api.olDaGetResolution)(input.ad, &mut bits) },
            "读取 ADC 位数",
        )?;
        check(
            unsafe { (input.api.olDaGetSSCaps)(input.ad, 7, &mut channels) },
            "读取输入通道数",
        )?;
        check(
            unsafe { (input.api.olDaGetRange)(input.da, &mut max, &mut min) },
            "读取 DAC 量程",
        )?;
        if bits != 24 || channels != 4 || (min + 3.).abs() > 1e-6 || (max - 3.).abs() > 1e-6 {
            return Err(Fault::new(
                "wrong_model",
                "原厂设备能力与 DT9837C（四路24位AI、±3V AO）不符，未启动采集",
            ));
        }
        input.info["verified_model"] = json!("DT9837C");
        input.info["model_verification"] = json!("4-channel 24-bit AI and ±3 V AO capabilities");
        let mut actual_name = [0i8; 256];
        check(
            unsafe { (input.api.olDaGetDeviceName)(input.device, actual_name.as_mut_ptr(), 256) },
            "读取设备名称",
        )?;
        input.info["device_name"] = json!(decode_ansi(
            unsafe { CStr::from_ptr(actual_name.as_ptr()) }.to_bytes()
        ));
        Ok(input)
    }

    pub fn start(
        &mut self,
        config: &Config,
        external: bool,
        sync: &str,
        defer: bool,
    ) -> Result<(), Fault> {
        if self.input.is_some() {
            return Err(Fault::new("configuration_error", "原厂采集已启动"));
        }
        let api = self.api.clone();
        let h = self.ad;
        let _guard = self.gate.lock().unwrap();
        check(unsafe { (api.olDaSetDataFlow)(h, 800) }, "设置连续采集")?;
        check(
            unsafe { (api.olDaSetEncoding)(h, 200) },
            "设置输入二进制编码",
        )?;
        check(unsafe { (api.olDaSetWrapMode)(h, 1001) }, "设置多缓冲队列")?;
        check(unsafe { (api.olDaSetChannelType)(h, 100) }, "设置单端输入")?;
        check(
            unsafe { (api.olDaSetChannelListSize)(h, config.channels.len() as u32) },
            "设置通道队列",
        )?;
        check(
            unsafe { (api.olDaSetRange)(h, 10., -10.) },
            "设置基础输入量程",
        )?;
        for (entry, &channel) in config.channels.iter().enumerate() {
            check(
                unsafe { (api.olDaSetChannelListEntry)(h, entry as u32, channel as u32) },
                "设置物理通道",
            )?;
            check(
                unsafe {
                    (api.olDaSetGainListEntry)(h, entry as u32, 10. / config.ranges[channel])
                },
                "设置通道增益",
            )?;
        }
        for channel in 0..4 {
            let ac = config.coupling[channel] == "AC";
            let excitation = if config.iepe[channel] { 0 } else { 2 };
            if ac {
                check(
                    unsafe { (api.olDaSetCouplingType)(h, channel as u32, 1) },
                    "设置 AC 耦合",
                )?;
            }
            check(
                unsafe { (api.olDaSetExcitationCurrentSource)(h, channel as u32, excitation) },
                "设置 IEPE 恒流源",
            )?;
            if !ac {
                check(
                    unsafe { (api.olDaSetCouplingType)(h, channel as u32, 0) },
                    "设置 DC 耦合",
                )?;
            }
        }
        check(
            unsafe {
                (api.olDaSetSyncMode)(
                    h,
                    match sync {
                        "master" => 1,
                        "slave" => 2,
                        _ => 0,
                    },
                )
            },
            "设置主从同步",
        )?;
        check(unsafe { (api.olDaSetClockSource)(h, 400) }, "设置采样时钟")?;
        check(
            unsafe { (api.olDaSetClockFrequency)(h, config.sample_rate) },
            "设置采样率",
        )?;
        check(
            unsafe { (api.olDaSetTrigger)(h, if external { 301 } else { 300 }) },
            "设置启动触发",
        )?;
        if external {
            check(unsafe { (api.olDaSetEdgeType)(h, 601) }, "设置 TTL 上升沿")?;
        }
        let mut dma = 0;
        check(
            unsafe { (api.olDaGetSSCaps)(h, 6, &mut dma) },
            "查询 DMA 支持",
        )?;
        check(unsafe { (api.olDaSetDmaUsage)(h, dma.min(1)) }, "设置 DMA")?;
        check(unsafe { (api.olDaConfig)(h) }, "应用输入配置")?;
        check(
            unsafe { (api.olDaGetClockFrequency)(h, &mut self.actual_rate) },
            "读取实际采样率",
        )?;
        let (mut bits, mut encoding, mut max, mut min) = (0, 0, 0., 0.);
        check(
            unsafe { (api.olDaGetResolution)(h, &mut bits) },
            "读取 ADC 位数",
        )?;
        check(
            unsafe { (api.olDaGetEncoding)(h, &mut encoding) },
            "读取 ADC 编码",
        )?;
        check(
            unsafe { (api.olDaGetRange)(h, &mut max, &mut min) },
            "读取 ADC 量程",
        )?;
        check_format(bits, encoding, min, max)?;
        let width = config.channels.len();
        let frames = (self.actual_rate * 0.02).ceil().clamp(16., 4096.) as usize;
        let buffers = allocate(&api, h, frames * width, None)?;
        let (sender, blocks) = mpsc::sync_channel(64);
        self.blocks = blocks;
        let active = Arc::new(AtomicBool::new(true));
        let completed = Arc::new(AtomicBool::new(false));
        let run = active.clone();
        let fault = self.fault.clone();
        let gate = self.gate.clone();
        let sdk = api.clone();
        let handle = h as usize;
        let gains: Vec<_> = config
            .channels
            .iter()
            .map(|&c| 10. / config.ranges[c])
            .collect();
        let sync = sync.to_owned();
        let worker = thread::spawn(move || {
            let mut partial = Vec::<u32>::new();
            let mut received = false;
            let mut last = Instant::now();
            while run.load(Ordering::Acquire) {
                let result = (|| -> Result<bool, Fault> {
                    let _guard = gate.lock().unwrap();
                    let buffer = ready_buffer(&sdk, handle as Handle, "读取输入完成队列")?;
                    if buffer.is_null() {
                        return Ok(false);
                    }
                    let mut count = 0;
                    let mut error = 0;
                    let mut data = ptr::null_mut();
                    check(
                        unsafe { (sdk.olDmGetBufferECode)(buffer, &mut error) },
                        "读取输入缓冲状态",
                    )?;
                    check(error, "输入缓冲数据连续性")?;
                    check(
                        unsafe { (sdk.olDmGetValidSamples)(buffer, &mut count) },
                        "读取有效输入样本数",
                    )?;
                    check(
                        unsafe { (sdk.olDmGetBufferPtr)(buffer, &mut data) },
                        "读取输入缓冲地址",
                    )?;
                    if count as usize > frames * width || (data.is_null() && count > 0) {
                        return Err(Fault::new("vendor_format_error", "原厂返回无效输入缓冲区"));
                    }
                    if count > 0 {
                        partial.extend_from_slice(unsafe {
                            std::slice::from_raw_parts(data.cast::<u32>(), count as usize)
                        });
                    }
                    let complete = partial.len() / width * width;
                    let volts: Vec<_> = partial[..complete]
                        .iter()
                        .enumerate()
                        .map(|(i, &code)| {
                            decode_voltage(code, bits, encoding, min, max, gains[i % width])
                        })
                        .collect();
                    partial.drain(..complete);
                    check(
                        unsafe { (sdk.olDaPutBuffer)(handle as Handle, buffer) },
                        "重新提交输入缓冲",
                    )?;
                    if !volts.is_empty() {
                        sender.try_send(volts).map_err(|_| {
                            Fault::new("host_overrun", "原厂输入主机队列已满，已中止以保留连续前段")
                        })?;
                    }
                    Ok(true)
                })();
                match result {
                    Ok(true) => {
                        received = true;
                        last = Instant::now();
                    }
                    Ok(false) => thread::sleep(Duration::from_millis(1)),
                    Err(e) => {
                        set_fault(&fault, e);
                        break;
                    }
                }
                if (received || (!external && !defer && sync != "slave"))
                    && last.elapsed() > Duration::from_secs(3)
                {
                    set_fault(&fault, Fault::new("data_timeout", "原厂 ADC 数据超时"));
                    break;
                }
            }
            run.store(false, Ordering::Release);
        });
        self.input = Some(Stream {
            api,
            gate: self.gate.clone(),
            subsystem: h as usize,
            buffers,
            active,
            completed,
            worker: Some(worker),
        });
        drop(_guard);
        if !defer {
            self.arm(external)?;
        }
        Ok(())
    }

    pub fn arm(&self, _external: bool) -> Result<(), Fault> {
        let _guard = self.gate.lock().unwrap();
        check(unsafe { (self.api.olDaStart)(self.ad) }, "启动输入")
    }
    pub fn error(&self) -> Option<Fault> {
        self.fault.lock().unwrap().clone()
    }
    pub fn check_connection(&self) -> Result<(), Fault> {
        let _guard = self.gate.lock().unwrap();
        let mut name = [0i8; 256];
        check(
            unsafe { (self.api.olDaGetDeviceName)(self.device, name.as_mut_ptr(), 256) },
            "检查设备连接",
        )
    }
    pub fn stop(&mut self) -> Result<(), Fault> {
        if let Some(mut stream) = self.input.take() {
            stream.stop()?;
        }
        Ok(())
    }
    pub fn output_active(&self) -> bool {
        self.output
            .as_ref()
            .is_some_and(|s| s.active.load(Ordering::Acquire))
    }

    pub fn start_output(&mut self, settings: output::Settings) -> Result<(), Fault> {
        self.refresh_output();
        settings
            .validate()
            .map_err(|e| Fault::new("configuration_error", e))?;
        if self.output.is_some() {
            return Err(Fault::new("configuration_error", "AO 已在运行"));
        }
        let api = self.api.clone();
        let h = self.da;
        let _guard = self.gate.lock().unwrap();
        check(unsafe { (api.olDaSetDataFlow)(h, 800) }, "设置连续输出")?;
        check(
            unsafe { (api.olDaSetEncoding)(h, 200) },
            "设置输出二进制编码",
        )?;
        // Stream fresh output buffers; do not repeat the AI-style buffer wrapping mode.
        check(
            unsafe { (api.olDaSetWrapMode)(h, 1000) },
            "设置输出缓冲队列",
        )?;
        check(
            unsafe { (api.olDaSetChannelListSize)(h, 1) },
            "设置输出通道数",
        )?;
        check(
            unsafe { (api.olDaSetChannelListEntry)(h, 0, 0) },
            "设置 AO0",
        )?;
        check(unsafe { (api.olDaSetClockSource)(h, 400) }, "设置输出时钟")?;
        check(
            unsafe { (api.olDaSetClockFrequency)(h, settings.sample_rate) },
            "设置输出更新率",
        )?;
        check(
            unsafe {
                (api.olDaSetTrigger)(
                    h,
                    if settings.trigger == "external_rising" {
                        301
                    } else {
                        300
                    },
                )
            },
            "设置输出触发",
        )?;
        if settings.trigger == "external_rising" {
            check(
                unsafe { (api.olDaSetEdgeType)(h, 601) },
                "设置输出 TTL 上升沿",
            )?;
        }
        check(unsafe { (api.olDaConfig)(h) }, "应用输出配置")?;
        let (mut rate, mut bits, mut encoding, mut max, mut min) = (0., 0, 0, 0., 0.);
        check(
            unsafe { (api.olDaGetClockFrequency)(h, &mut rate) },
            "读取实际 AO 更新率",
        )?;
        settings
            .validate_frequency(rate)
            .map_err(|e| Fault::new("configuration_error", e))?;
        check(
            unsafe { (api.olDaGetResolution)(h, &mut bits) },
            "读取 DAC 位数",
        )?;
        check(
            unsafe { (api.olDaGetEncoding)(h, &mut encoding) },
            "读取 DAC 编码",
        )?;
        check(
            unsafe { (api.olDaGetRange)(h, &mut max, &mut min) },
            "读取 DAC 量程",
        )?;
        check_format(bits, encoding, min, max)?;
        let frames = (rate * 0.02).ceil().clamp(64., 4096.) as usize;
        let mut generator = output::Generator::new();
        let mut next = 0u64;
        let mut fill = |data: &mut [u32]| {
            let samples = generator.samples(&settings, next, data.len(), rate);
            for (out, v) in data.iter_mut().zip(samples) {
                *out = encode_voltage(v, bits, encoding, min, max);
            }
            next += data.len() as u64;
        };
        let buffers = allocate(&api, h, frames, Some(&mut fill))?;
        *self.output_settings.lock().unwrap() = settings.clone();
        let active = Arc::new(AtomicBool::new(true));
        let completed = Arc::new(AtomicBool::new(false));
        let run = active.clone();
        let done = completed.clone();
        let sdk = api.clone();
        let gate = self.gate.clone();
        let live = self.output_settings.clone();
        let fault = self.fault.clone();
        let handle = h as usize;
        let worker = thread::spawn(move || {
            let mut played = 0u64;
            while run.load(Ordering::Acquire) {
                let result = (|| -> Result<bool, Fault> {
                    let _guard = gate.lock().unwrap();
                    let buffer = ready_buffer(&sdk, handle as Handle, "读取输出完成队列")?;
                    if buffer.is_null() {
                        return Ok(false);
                    }
                    let mut error = 0;
                    check(
                        unsafe { (sdk.olDmGetBufferECode)(buffer, &mut error) },
                        "读取输出缓冲状态",
                    )?;
                    check(error, "输出缓冲连续性")?;
                    played += frames as u64;
                    let settings = live.lock().unwrap().clone();
                    if settings
                        .end_frame(rate)
                        .is_some_and(|end| played >= end.saturating_add(8192))
                    {
                        done.store(true, Ordering::Release);
                        run.store(false, Ordering::Release);
                        return Ok(true);
                    }
                    let mut data = ptr::null_mut();
                    check(
                        unsafe { (sdk.olDmGetBufferPtr)(buffer, &mut data) },
                        "读取输出缓冲地址",
                    )?;
                    if data.is_null() {
                        return Err(Fault::new("vendor_format_error", "原厂返回空输出缓冲地址"));
                    }
                    let samples = generator.samples(&settings, next, frames, rate);
                    next += frames as u64;
                    for (out, v) in
                        unsafe { std::slice::from_raw_parts_mut(data.cast::<u32>(), frames) }
                            .iter_mut()
                            .zip(samples)
                    {
                        *out = encode_voltage(v, bits, encoding, min, max);
                    }
                    check(
                        unsafe { (sdk.olDmSetValidSamples)(buffer, frames as u32) },
                        "设置输出样本数",
                    )?;
                    check(
                        unsafe { (sdk.olDaPutBuffer)(handle as Handle, buffer) },
                        "重新提交输出缓冲",
                    )?;
                    Ok(true)
                })();
                match result {
                    Ok(true) => {}
                    Ok(false) => thread::sleep(Duration::from_millis(1)),
                    Err(e) => {
                        set_fault(&fault, e);
                        break;
                    }
                }
            }
            run.store(false, Ordering::Release);
        });
        self.output = Some(Stream {
            api,
            gate: self.gate.clone(),
            subsystem: h as usize,
            buffers,
            active,
            completed,
            worker: Some(worker),
        });
        self.output_status = output::Status {
            running: true,
            settings: Some(settings),
            actual_rate: rate,
            zeroed: false,
            completed: false,
        };
        drop(_guard);
        let result = check(unsafe { (self.api.olDaStart)(h) }, "启动输出");
        if result.is_err() {
            let cleanup = self.stop_output();
            if let Err(cleanup) = cleanup {
                return Err(Fault::new(
                    "output_error",
                    format!("{}；停止归零也失败：{cleanup}", result.unwrap_err()),
                ));
            }
        }
        result
    }

    fn zero_output(&self) -> Result<(), Fault> {
        let _guard = self.gate.lock().unwrap();
        let (mut bits, mut encoding, mut max, mut min) = (0, 0, 0., 0.);
        check(
            unsafe { (self.api.olDaSetDataFlow)(self.da, 801) },
            "设置单值输出归零",
        )?;
        check(unsafe { (self.api.olDaConfig)(self.da) }, "应用归零配置")?;
        check(
            unsafe { (self.api.olDaGetResolution)(self.da, &mut bits) },
            "读取归零编码位数",
        )?;
        check(
            unsafe { (self.api.olDaGetEncoding)(self.da, &mut encoding) },
            "读取归零编码",
        )?;
        check(
            unsafe { (self.api.olDaGetRange)(self.da, &mut max, &mut min) },
            "读取归零量程",
        )?;
        check_format(bits, encoding, min, max)?;
        check(
            unsafe {
                (self.api.olDaPutSingleValue)(
                    self.da,
                    encode_voltage(0., bits, encoding, min, max) as i32,
                    0,
                    1.,
                )
            },
            "AO 归零",
        )
    }
    pub fn stop_output(&mut self) -> Result<(), Fault> {
        let mut error = self
            .output
            .take()
            .and_then(|mut stream| stream.stop().err());
        if let Err(e) = self.zero_output() {
            error.get_or_insert(e);
        }
        self.output_status.running = false;
        self.output_status.zeroed = error.is_none();
        error.map_or(Ok(()), Err)
    }
    pub fn update_output(&mut self, settings: output::Settings) -> Result<(), Fault> {
        settings
            .validate()
            .map_err(|e| Fault::new("configuration_error", e))?;
        if !self.output_status.running {
            return Err(Fault::new("configuration_error", "AO 尚未运行"));
        }
        settings
            .validate_frequency(self.output_status.actual_rate)
            .map_err(|e| Fault::new("configuration_error", e))?;
        if self
            .output_status
            .settings
            .as_ref()
            .is_some_and(|s| s.sample_rate != settings.sample_rate || s.trigger != settings.trigger)
        {
            return Err(Fault::new(
                "configuration_error",
                "运行中不能更改 AO 更新率或启动触发",
            ));
        }
        *self.output_settings.lock().unwrap() = settings.clone();
        self.output_status.settings = Some(settings);
        Ok(())
    }
    pub fn refresh_output(&mut self) {
        if self
            .output
            .as_ref()
            .is_some_and(|s| !s.active.load(Ordering::Acquire))
        {
            let complete = self
                .output
                .as_ref()
                .unwrap()
                .completed
                .load(Ordering::Acquire);
            match self.stop_output() {
                Ok(()) => self.output_status.completed = complete,
                Err(e) => set_fault(&self.fault, e),
            }
        }
    }
    pub fn disconnect(&mut self) -> Result<(), Fault> {
        let mut error = self.stop().err();
        if !self.da.is_null() {
            if let Err(e) = self.stop_output() {
                error.get_or_insert(e);
            }
        }
        let _guard = self.gate.lock().unwrap();
        for h in [&mut self.ad, &mut self.da] {
            if !h.is_null() {
                if let Err(e) = check(unsafe { (self.api.olDaReleaseDASS)(*h) }, "释放子系统")
                {
                    error.get_or_insert(e);
                }
                *h = ptr::null_mut();
            }
        }
        if !self.device.is_null() {
            if let Err(e) = check(
                unsafe { (self.api.olDaTerminate)(self.device) },
                "释放原厂驱动会话",
            ) {
                error.get_or_insert(e);
            }
            self.device = ptr::null_mut();
        }
        error.map_or(Ok(()), Err)
    }
}
impl Drop for VendorInput {
    fn drop(&mut self) {
        let _ = self.disconnect();
    }
}

fn allocate(
    api: &Api,
    subsystem: Handle,
    count: usize,
    mut fill: Option<&mut dyn FnMut(&mut [u32])>,
) -> Result<Vec<usize>, Fault> {
    let mut buffers = Vec::new();
    let result = (|| -> Result<(), Fault> {
        for _ in 0..8 {
            let mut handle = ptr::null_mut();
            check(
                unsafe { (api.olDmCallocBuffer)(0, 0, count as u32, 4, &mut handle) },
                "分配原厂缓冲区",
            )?;
            buffers.push(handle as usize);
            if let Some(fill) = fill.as_mut() {
                let mut data = ptr::null_mut();
                check(
                    unsafe { (api.olDmGetBufferPtr)(handle, &mut data) },
                    "读取输出缓冲地址",
                )?;
                if data.is_null() {
                    return Err(Fault::new("vendor_format_error", "分配的输出缓冲地址为空"));
                }
                fill(unsafe { std::slice::from_raw_parts_mut(data.cast::<u32>(), count) });
                check(
                    unsafe { (api.olDmSetValidSamples)(handle, count as u32) },
                    "设置有效输出样本",
                )?;
            }
            check(
                unsafe { (api.olDaPutBuffer)(subsystem, handle) },
                "提交原厂缓冲区",
            )?;
        }
        Ok(())
    })();
    if let Err(error) = result {
        let _ = unsafe { (api.olDaAbort)(subsystem) };
        let _ = unsafe { (api.olDaFlushBuffers)(subsystem) };
        for _ in 0..=buffers.len() {
            let mut h = ptr::null_mut();
            if unsafe { (api.olDaGetBuffer)(subsystem, &mut h) } != 0 || h.is_null() {
                break;
            }
        }
        for h in buffers {
            let _ = unsafe { (api.olDmFreeBuffer)(h as Handle) };
        }
        return Err(error);
    }
    Ok(buffers)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stream_stop_joins_reader_before_aborting_and_accepts_already_stopped() {
        let active = Arc::new(AtomicBool::new(true));
        let finished = Arc::new(AtomicBool::new(false));
        let run = active.clone();
        let done = finished.clone();
        let mut worker = Some(thread::spawn(move || {
            while run.load(Ordering::Acquire) {
                thread::yield_now();
            }
            done.store(true, Ordering::Release);
        }));
        stop_stream_worker(&active, &mut worker, || {
            assert!(finished.load(Ordering::Acquire));
            28
        })
        .unwrap();
        assert!(worker.is_none());
        assert!(!active.load(Ordering::Acquire));
    }
    #[test]
    fn stream_stop_preserves_real_abort_and_buffer_errors() {
        let active = AtomicBool::new(true);
        let mut worker = None;
        let fault = stop_stream_worker(&active, &mut worker, || 39).unwrap_err();
        assert_eq!(fault.reason, "vendor_error");
        assert!(fault.message.contains("0x00000027"));
        // OLNOTRUNNING is not globally ignored on input/buffer operations.
        assert!(check(28, "输入缓冲数据连续性").is_err());
    }

    #[test]
    fn official_buffer_encoding_round_trips_with_gain_and_twos_complement() {
        for encoding in [200, 201] {
            for range in [1., 10.] {
                for v in [-range, -range / 2., 0., range / 2., range] {
                    let code = encode_voltage(v, 24, encoding, -range, range);
                    let decoded = decode_voltage(code, 24, encoding, -10., 10., 10. / range);
                    assert!((decoded - v).abs() <= 2. * range / (1u32 << 24) as f64);
                }
            }
        }
        assert_eq!(encode_voltage(0., 24, 200, -3., 3.), 1 << 23);
        assert_eq!(encode_voltage(0., 24, 201, -3., 3.), 0);
        assert_eq!(decode_voltage(0xff800000, 24, 201, -10., 10., 1.), -10.);
    }
    #[test]
    fn abi_types_match_compiled_official_sdk_header_probe() {
        assert_eq!(std::mem::size_of::<Handle>(), 8);
        assert_eq!(std::mem::size_of::<Ecode>(), 4);
        assert_eq!(std::mem::size_of::<isize>(), 8);
        assert_eq!(std::mem::size_of::<c_char>(), 1);
    }
    #[test]
    fn unsupported_driver_data_formats_fail() {
        assert!(check_format(16, 200, -10., 10.).is_err());
        assert!(check_format(24, 999, -10., 10.).is_err());
        assert!(check_format(24, 200, 10., -10.).is_err());
        assert!(check_format(24, 201, -10., 10.).is_ok());
    }
}
