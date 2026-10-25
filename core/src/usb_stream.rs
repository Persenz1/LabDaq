//! Queued libusb ADC reads. Transfer storage outlives every cancellation callback.
use crate::{
    dt9837::Decoder,
    usb_input::{Fault, set_fault},
};
use rusb::ffi::constants::*;
use rusb::{Context, DeviceHandle, UsbContext, ffi};
use std::collections::BTreeMap;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{self, SyncSender},
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

struct Completed {
    sequence: u64,
    bytes: Vec<u8>,
    status: i32,
}
struct Shared {
    completed: SyncSender<Completed>,
    stopping: Arc<AtomicBool>,
    fault: Arc<Mutex<Option<Fault>>>,
    sequence: AtomicU64,
}
struct Slot {
    transfer: *mut ffi::libusb_transfer,
    buffer: Box<[u8]>,
    active: AtomicBool,
    sequence: AtomicU64,
    shared: Arc<Shared>,
}

extern "system" fn completed(transfer: *mut ffi::libusb_transfer) {
    // SAFETY: only libusb invokes this callback, while its Box<Slot>, buffer,
    // handle and context remain alive. The event lock serializes callbacks.
    // Rust-side mutable status uses atomics/Mutex because another event thread
    // (including the synchronous status/command reader) may invoke us.
    unsafe {
        let t = &*transfer;
        let slot = &*(t.user_data as *const Slot);
        let shared = &slot.shared;
        let status = t.status;
        let bytes = if status == LIBUSB_TRANSFER_COMPLETED {
            std::slice::from_raw_parts(t.buffer, t.actual_length as usize).to_vec()
        } else {
            Vec::new()
        };
        if shared
            .completed
            .try_send(Completed {
                sequence: slot.sequence.load(Ordering::Relaxed),
                bytes,
                status,
            })
            .is_err()
        {
            set_fault(
                &shared.fault,
                Fault::new("host_overrun", "USB 完成队列已满，采集已停止"),
            );
        }
        if status != LIBUSB_TRANSFER_COMPLETED
            && !(status == LIBUSB_TRANSFER_CANCELLED && shared.stopping.load(Ordering::Acquire))
        {
            set_fault(
                &shared.fault,
                Fault::new(
                    if status == LIBUSB_TRANSFER_NO_DEVICE {
                        "device_disconnected"
                    } else {
                        "usb_error"
                    },
                    format!("异步 ADC 传输失败（状态 {status}），未重试或拼接后续数据"),
                ),
            );
        }
        if status == LIBUSB_TRANSFER_COMPLETED
            && !shared.stopping.load(Ordering::Acquire)
            && shared.fault.lock().unwrap().is_none()
        {
            slot.sequence.store(
                shared.sequence.fetch_add(1, Ordering::Relaxed),
                Ordering::Relaxed,
            );
            let code = ffi::libusb_submit_transfer(transfer);
            if code == 0 {
                return;
            }
            set_fault(
                &shared.fault,
                Fault::new(
                    "usb_error",
                    format!("重新提交 ADC 读取失败（libusb {code}）"),
                ),
            );
        }
        // Last access to Slot. It is freed only after this release is observed.
        slot.active.store(false, Ordering::Release);
    }
}

pub fn spawn(
    handle: Arc<DeviceHandle<Context>>,
    size: usize,
    ranges: Vec<f64>,
    stopping: Arc<AtomicBool>,
    fault: Arc<Mutex<Option<Fault>>>,
    blocks: SyncSender<Vec<f64>>,
    timeout: Duration,
    wait_for_first_data: bool,
) -> Result<JoinHandle<()>, Fault> {
    let (ready, wait_ready) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        let (completed_tx, completed_rx) = mpsc::sync_channel(64);
        let shared = Arc::new(Shared {
            completed: completed_tx,
            stopping,
            fault,
            sequence: AtomicU64::new(0),
        });
        let mut slots = Vec::new();
        for _ in 0..8 {
            // SAFETY: allocate, initialize then submit. Box addresses never move.
            // All submitted transfers are cancelled and their callbacks drained below.
            let transfer = unsafe { ffi::libusb_alloc_transfer(0) };
            if transfer.is_null() {
                set_fault(
                    &shared.fault,
                    Fault::new("usb_error", "无法分配 ADC USB 传输"),
                );
                break;
            }
            let mut slot = Box::new(Slot {
                transfer,
                buffer: vec![0; size].into_boxed_slice(),
                active: AtomicBool::new(true),
                sequence: AtomicU64::new(shared.sequence.fetch_add(1, Ordering::Relaxed)),
                shared: shared.clone(),
            });
            let data = (&mut *slot as *mut Slot).cast();
            let code = unsafe {
                ffi::libusb_fill_bulk_transfer(
                    transfer,
                    handle.as_raw(),
                    0x82,
                    slot.buffer.as_mut_ptr(),
                    size as i32,
                    completed,
                    data,
                    0,
                ); // watchdog below; no partial-transfer timeout loss
                ffi::libusb_submit_transfer(transfer)
            };
            if code != 0 {
                slot.active.store(false, Ordering::Release);
                set_fault(
                    &shared.fault,
                    Fault::new("usb_error", format!("提交 ADC 读取失败（libusb {code}）")),
                );
            }
            slots.push(slot);
            if code != 0 {
                break;
            }
        }
        let setup_error = shared.fault.lock().unwrap().clone();
        let _ = ready.send(setup_error.clone().map_or(Ok(()), Err));
        let mut decoder = Decoder::new(ranges);
        let mut ordered = BTreeMap::new();
        let mut next = 0;
        let mut last_data = Instant::now();
        let mut tail_ended = false;
        let mut process = |item: Completed| {
            ordered.insert(item.sequence, item);
            while let Some(item) = ordered.remove(&next) {
                next += 1;
                // A cancelled/failed transfer ends the valid prefix. In-flight
                // bytes after that cutoff are not appended to this recording.
                if item.status != LIBUSB_TRANSFER_COMPLETED {
                    tail_ended = true;
                }
                if tail_ended || item.bytes.is_empty() {
                    continue;
                }
                last_data = Instant::now();
                match decoder.push(&item.bytes) {
                    Ok(values) if !values.is_empty() => {
                        if blocks.try_send(values).is_err() {
                            tail_ended = true;
                            set_fault(
                                &shared.fault,
                                Fault::new(
                                    "host_overrun",
                                    "主机采集队列已满，已停止而未跳过数据继续记录",
                                ),
                            );
                        }
                    }
                    Ok(_) => {}
                    Err(e) => {
                        tail_ended = true;
                        set_fault(&shared.fault, Fault::new("protocol_error", e));
                    }
                }
            }
            last_data
        };
        let mut received_at = Instant::now();
        while !shared.stopping.load(Ordering::Acquire) && shared.fault.lock().unwrap().is_none() {
            if let Err(e) = handle
                .context()
                .handle_events(Some(Duration::from_millis(10)))
            {
                set_fault(&shared.fault, Fault::usb("处理 USB 事件", e));
                break;
            }
            for item in completed_rx.try_iter() {
                received_at = process(item);
            }
            if received_at.elapsed() > timeout
                && (!wait_for_first_data || shared.sequence.load(Ordering::Relaxed) > 8)
            {
                set_fault(
                    &shared.fault,
                    Fault::new("data_timeout", "ADC 数据超时，已停止本次采集"),
                );
            }
        }
        shared.stopping.store(true, Ordering::Release);
        // Serialize cancellation against a callback that had already decided
        // to resubmit before stopping became true. Otherwise cancel could race
        // ahead of the new submission and leave a live transfer behind.
        unsafe {
            ffi::libusb_lock_events(handle.context().as_raw());
        }
        for slot in &slots {
            if slot.active.load(Ordering::Acquire) {
                // NOT_FOUND is possible when a completion races with cancellation.
                // Completion, not the cancel return value, owns the lifetime.
                unsafe {
                    ffi::libusb_cancel_transfer(slot.transfer);
                }
            }
        }
        unsafe {
            ffi::libusb_unlock_events(handle.context().as_raw());
        }
        while slots.iter().any(|s| s.active.load(Ordering::Acquire)) {
            if let Err(e) = handle
                .context()
                .handle_events(Some(Duration::from_millis(20)))
            {
                set_fault(&shared.fault, Fault::usb("等待 USB 取消完成", e));
            }
            for item in completed_rx.try_iter() {
                process(item);
            }
        }
        for item in completed_rx.try_iter() {
            process(item);
        }
        for slot in slots {
            // SAFETY: every submitted transfer has completed/cancelled; no
            // callback or USB DMA can access the buffer or user_data again.
            unsafe {
                ffi::libusb_free_transfer(slot.transfer);
            }
        }
    });
    match wait_ready.recv() {
        Ok(Ok(())) => Ok(worker),
        result => {
            let _ = worker.join();
            Err(result
                .unwrap_or_else(|_| Err(Fault::new("core_error", "USB 读取线程启动失败")))
                .unwrap_err())
        }
    }
}
