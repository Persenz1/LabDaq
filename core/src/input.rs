use crate::{
    Config, Source, output,
    usb_input::{self, DeviceSelector, Fault},
    vendor_input,
};
use serde_json::Value;

pub fn enumerate(source: Source) -> Result<Vec<Value>, Fault> {
    match source {
        Source::Usb => usb_input::enumerate(),
        Source::Vendor => vendor_input::enumerate(),
        Source::Simulation => Err(Fault::new(
            "configuration_error",
            "模拟输入没有可枚举的物理设备",
        )),
    }
}

pub enum Input {
    Usb(usb_input::UsbInput),
    Vendor(vendor_input::VendorInput),
}

impl Input {
    pub fn connect(source: Source, selector: Option<&DeviceSelector>) -> Result<Self, Fault> {
        match source {
            Source::Usb => usb_input::UsbInput::connect_selected(selector).map(Self::Usb),
            Source::Vendor => vendor_input::VendorInput::connect(selector).map(Self::Vendor),
            Source::Simulation => Err(Fault::new(
                "configuration_error",
                "模拟输入不需要连接物理设备",
            )),
        }
    }

    pub fn info(&self) -> Value {
        match self {
            Self::Usb(v) => serde_json::to_value(&v.info).unwrap(),
            Self::Vendor(v) => v.info.clone(),
        }
    }

    pub fn source(&self) -> Source {
        match self {
            Self::Usb(_) => Source::Usb,
            Self::Vendor(_) => Source::Vendor,
        }
    }

    pub fn matches_selector(&self, selector: &DeviceSelector) -> bool {
        match self {
            Self::Usb(v) => selector.bus == v.info.bus && selector.ports == v.info.ports,
            Self::Vendor(v) => selector
                .vendor_name
                .as_deref()
                .is_none_or(|name| v.name == name),
        }
    }

    pub fn actual_rate(&self) -> f64 {
        match self {
            Self::Usb(v) => v.actual_rate,
            Self::Vendor(v) => v.actual_rate,
        }
    }

    pub fn start(
        &mut self,
        config: &Config,
        external: bool,
        sync: &str,
        defer: bool,
    ) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.start(config, external, sync, defer),
            Self::Vendor(v) => v.start(config, external, sync, defer),
        }
    }

    pub fn arm(&self, external: bool) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.arm(external),
            Self::Vendor(v) => v.arm(external),
        }
    }

    pub fn take_blocks(&self, max: usize) -> Vec<Vec<f64>> {
        match self {
            Self::Usb(v) => v.blocks.try_iter().take(max).collect(),
            Self::Vendor(v) => v.blocks.try_iter().take(max).collect(),
        }
    }

    pub fn error(&self) -> Option<Fault> {
        match self {
            Self::Usb(v) => v.error(),
            Self::Vendor(v) => v.error(),
        }
    }

    pub fn check_connection(&self) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.check_connection(),
            Self::Vendor(v) => v.check_connection(),
        }
    }

    pub fn stop(&mut self) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.stop(),
            Self::Vendor(v) => v.stop(),
        }
    }

    pub fn disconnect(&mut self) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.disconnect(),
            Self::Vendor(v) => v.disconnect(),
        }
    }

    pub fn output_status(&self) -> &output::Status {
        match self {
            Self::Usb(v) => &v.output.status,
            Self::Vendor(v) => &v.output_status,
        }
    }

    pub fn output_active(&self) -> bool {
        match self {
            Self::Usb(v) => v.output.active.load(std::sync::atomic::Ordering::Acquire),
            Self::Vendor(v) => v.output_active(),
        }
    }

    pub fn refresh_output(&mut self) {
        match self {
            Self::Usb(v) => v.output.refresh(),
            Self::Vendor(v) => v.refresh_output(),
        }
    }

    pub fn start_output(&mut self, settings: output::Settings) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.start_output(settings),
            Self::Vendor(v) => v.start_output(settings),
        }
    }

    pub fn stop_output(&mut self) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.stop_output(),
            Self::Vendor(v) => v.stop_output(),
        }
    }

    pub fn update_output(&mut self, settings: output::Settings) -> Result<(), Fault> {
        match self {
            Self::Usb(v) => v.output.update(settings),
            Self::Vendor(v) => v.update_output(settings),
        }
    }
}
