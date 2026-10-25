//! Bind a validated, signed WinUSB package to present DT9837-family devices.
//!
//! The installer must check elevation, package signature, and user consent before
//! calling this module. Driver-store staging is done by the outer installer.
//! These A/B/C models share hardware IDs; C subtype is checked by the core later.
//! No certificate stores, driver-signing policy, or Secure Boot settings are changed.

use std::path::{Path, PathBuf};

pub const HARDWARE_IDS: [&str; 2] = [r"USB\VID_0867&PID_9839", r"USB\VID_0867&PID_3998"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriverPlan {
    pub inf: PathBuf,
    pub hardware_ids: [&'static str; 2],
    pub force_existing_binding: bool,
}

/// Pure planning: does not inspect devices, read files, or change the computer.
pub fn binding_plan(inf: &Path) -> Result<DriverPlan, String> {
    if !inf.is_absolute()
        || !inf
            .extension()
            .is_some_and(|x| x.eq_ignore_ascii_case("inf"))
    {
        return Err("驱动安装需要绝对路径的 INF 文件".into());
    }
    if inf.as_os_str().to_string_lossy().contains('\0') {
        return Err("INF 路径包含 NUL 字符".into());
    }
    Ok(DriverPlan {
        inf: inf.to_path_buf(),
        hardware_ids: HARDWARE_IDS,
        force_existing_binding: true,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BoundDevice {
    pub instance_id: String,
    pub hardware_id: String,
    pub service: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DriverResult {
    pub updated_hardware_ids: Vec<String>,
    pub absent_hardware_ids: Vec<String>,
    pub devices: Vec<BoundDevice>,
    pub reboot_required: bool,
    pub verification_pending: bool,
}

fn matching_id(ids: &[String]) -> Option<&'static str> {
    HARDWARE_IDS
        .into_iter()
        .find(|target| ids.iter().any(|id| id.eq_ignore_ascii_case(target)))
}

fn verify_services(devices: &[BoundDevice], reboot_required: bool) -> Result<bool, String> {
    let wrong: Vec<_> = devices
        .iter()
        .filter(|d| !d.service.eq_ignore_ascii_case("WinUSB"))
        .collect();
    if wrong.is_empty() {
        return Ok(false);
    }
    if reboot_required {
        return Ok(true);
    }
    Err(format!(
        "驱动安装后仍有设备未绑定 WinUSB：{}",
        wrong
            .iter()
            .map(|d| format!(
                "{}（服务：{}）",
                d.instance_id,
                if d.service.is_empty() {
                    "未设置"
                } else {
                    &d.service
                }
            ))
            .collect::<Vec<_>>()
            .join("；")
    ))
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::mem::size_of;
    use std::os::windows::ffi::OsStrExt;
    use std::{mem, ptr};
    use windows_sys::Win32::Devices::DeviceAndDriverInstallation::*;
    use windows_sys::Win32::Foundation::{
        ERROR_INSUFFICIENT_BUFFER, ERROR_INVALID_DATA, ERROR_NO_MORE_ITEMS, ERROR_NO_SUCH_DEVINST,
        GetLastError,
    };

    struct DeviceSet(HDEVINFO);
    impl Drop for DeviceSet {
        fn drop(&mut self) {
            unsafe {
                SetupDiDestroyDeviceInfoList(self.0);
            }
        }
    }

    fn win_error(action: &str, error: u32) -> String {
        format!(
            "{action}：Windows 错误 0x{error:08X}（{}）",
            std::io::Error::from_raw_os_error(error as i32)
        )
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn properties(
        set: HDEVINFO,
        data: &SP_DEVINFO_DATA,
        property: u32,
    ) -> Result<Vec<String>, String> {
        let mut required = 0;
        unsafe {
            SetupDiGetDeviceRegistryPropertyW(
                set,
                data,
                property,
                ptr::null_mut(),
                ptr::null_mut(),
                0,
                &mut required,
            );
            let error = GetLastError();
            if error == ERROR_INVALID_DATA {
                return Ok(Vec::new());
            }
            if error != ERROR_INSUFFICIENT_BUFFER {
                return Err(win_error("读取设备属性长度", error));
            }
            let mut buffer = vec![0u16; (required as usize).div_ceil(2)];
            if SetupDiGetDeviceRegistryPropertyW(
                set,
                data,
                property,
                ptr::null_mut(),
                buffer.as_mut_ptr().cast(),
                required,
                &mut required,
            ) == 0
            {
                return Err(win_error("读取设备属性", GetLastError()));
            }
            if required % 2 != 0 {
                return Err("Windows 返回了无效 UTF-16 设备属性".into());
            }
            buffer.truncate(required as usize / 2);
            Ok(buffer
                .split(|c| *c == 0)
                .filter(|s| !s.is_empty())
                .map(String::from_utf16_lossy)
                .collect())
        }
    }

    fn instance_id(set: HDEVINFO, data: &SP_DEVINFO_DATA) -> Result<String, String> {
        let mut required = 0;
        unsafe {
            SetupDiGetDeviceInstanceIdW(set, data, ptr::null_mut(), 0, &mut required);
            let error = GetLastError();
            if error != ERROR_INSUFFICIENT_BUFFER {
                return Err(win_error("读取设备实例 ID 长度", error));
            }
            let mut buffer = vec![0u16; required as usize];
            if SetupDiGetDeviceInstanceIdW(set, data, buffer.as_mut_ptr(), required, &mut required)
                == 0
            {
                return Err(win_error("读取设备实例 ID", GetLastError()));
            }
            let end = buffer.iter().position(|c| *c == 0).unwrap_or(buffer.len());
            Ok(String::from_utf16_lossy(&buffer[..end]))
        }
    }

    fn present_targets() -> Result<Vec<BoundDevice>, String> {
        let usb = wide("USB");
        let raw = unsafe {
            SetupDiGetClassDevsW(
                ptr::null(),
                usb.as_ptr(),
                ptr::null_mut(),
                DIGCF_PRESENT | DIGCF_ALLCLASSES,
            )
        };
        if raw == -1 {
            return Err(win_error("枚举已连接 USB 设备", unsafe {
                GetLastError()
            }));
        }
        let set = DeviceSet(raw);
        let mut results = Vec::new();
        let mut index = 0;
        loop {
            let mut data: SP_DEVINFO_DATA = unsafe { mem::zeroed() };
            data.cbSize = size_of::<SP_DEVINFO_DATA>() as u32;
            if unsafe { SetupDiEnumDeviceInfo(set.0, index, &mut data) } == 0 {
                let error = unsafe { GetLastError() };
                if error == ERROR_NO_MORE_ITEMS {
                    break;
                }
                return Err(win_error("枚举设备实例", error));
            }
            index += 1;
            let ids = properties(set.0, &data, SPDRP_HARDWAREID)?;
            if let Some(hardware_id) = matching_id(&ids) {
                results.push(BoundDevice {
                    instance_id: instance_id(set.0, &data)?,
                    hardware_id: hardware_id.into(),
                    service: properties(set.0, &data, SPDRP_SERVICE)?
                        .into_iter()
                        .next()
                        .unwrap_or_default(),
                });
            }
        }
        Ok(results)
    }

    pub fn bind(inf: &Path) -> Result<DriverResult, String> {
        let plan = binding_plan(inf)?;
        if !plan.inf.is_file() {
            return Err(format!("驱动 INF 不存在：{}", plan.inf.display()));
        }
        let inf_wide: Vec<u16> = plan
            .inf
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let before = present_targets()?;
        let mut result = DriverResult::default();
        for id in plan.hardware_ids {
            if !before
                .iter()
                .any(|d| d.hardware_id.eq_ignore_ascii_case(id))
            {
                result.absent_hardware_ids.push(id.into());
                continue;
            }
            let id_wide = wide(id);
            let mut reboot = 0;
            let ok = unsafe {
                UpdateDriverForPlugAndPlayDevicesW(
                    ptr::null_mut(),
                    id_wide.as_ptr(),
                    inf_wide.as_ptr(),
                    INSTALLFLAG_FORCE,
                    &mut reboot,
                )
            };
            if ok == 0 {
                let error = unsafe { GetLastError() };
                if error == ERROR_NO_SUCH_DEVINST {
                    result.absent_hardware_ids.push(id.into());
                    continue;
                }
                return Err(win_error(
                    &format!("为 {id} 安装已签名的 WinUSB 驱动包"),
                    error,
                ));
            }
            result.reboot_required |= reboot != 0;
            result.updated_hardware_ids.push(id.into());
        }
        result.devices = present_targets()?;
        result.verification_pending = verify_services(&result.devices, result.reboot_required)?;
        if !before.is_empty() && result.devices.is_empty() {
            // A reset/re-enumeration can temporarily remove the device. The caller
            // must not report this as verified until a later device check passes.
            result.verification_pending = true;
        }
        Ok(result)
    }
}

/// Requires an elevated installer and an already validated signed package.
/// Calling this function really changes present-device driver bindings.
pub fn bind_present_devices(inf: &Path) -> Result<DriverResult, String> {
    #[cfg(windows)]
    {
        windows::bind(inf)
    }
    #[cfg(not(windows))]
    {
        let _ = inf;
        Err("USB 驱动安装仅支持 Windows".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn plan_is_exactly_two_device_ids_and_requires_absolute_inf_path() {
        #[cfg(windows)]
        let inf = Path::new(r"C:\Program Files\LabDAQ\driver\labdaq-winusb.inf");
        #[cfg(not(windows))]
        let inf = Path::new("/opt/labdaq/driver/labdaq-winusb.inf");
        let plan = binding_plan(inf).unwrap();
        assert_eq!(plan.hardware_ids, HARDWARE_IDS);
        assert!(plan.force_existing_binding);
        assert!(binding_plan(Path::new("relative.inf")).is_err());
        assert!(binding_plan(&inf.with_extension("exe")).is_err());
    }
    #[test]
    fn hardware_id_match_is_case_insensitive_but_not_a_prefix_match() {
        assert_eq!(
            matching_id(&[r"usb\vid_0867&pid_9839".into()]),
            Some(HARDWARE_IDS[0])
        );
        assert!(
            matching_id(&[
                r"USB\VID_0867&PID_98390".into(),
                r"USB\VID_0867&PID_3998&MI_00".into()
            ])
            .is_none()
        );
        assert!(matching_id(&[r"USB\VID_1234&PID_3998".into()]).is_none());
    }
    #[test]
    fn verification_does_not_claim_success_for_an_old_driver_or_pending_restart() {
        let mut device = BoundDevice {
            instance_id: "test-device".into(),
            hardware_id: HARDWARE_IDS[1].into(),
            service: "old-vendor-driver".into(),
        };
        assert!(verify_services(&[device.clone()], false).is_err());
        assert_eq!(verify_services(&[device.clone()], true), Ok(true));
        device.service = "winusb".into();
        assert_eq!(verify_services(&[device], false), Ok(false));
    }
}
