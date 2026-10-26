# Third-party notices

The DT9837C command definitions, clock planning and initialization in labdaq/protocol.py, core/src/dt9837.rs, core/src/usb_input.rs, core/src/output.rs and core/src/usb_boot.rs are adapted from Measurement Computing libuldaq 1.2.2 (https://github.com/mccdaq/uldaq/tree/v1.2.2). The following upstream license applies to those portions. No installed vendor SDK or vendor driver is used by the Rust USB backend. Cold boot requires a separately prepared local RAM firmware resource; tools/prepare_usb_firmware.py preserves the original loader notice. Firmware images and vendor reference binaries are not distributed in this source repository.

MIT License

Copyright (c) 2018 Measurement Computing

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


Python runtime dependencies are installed separately: PySide6 (LGPL/GPL/commercial options), pyqtgraph (MIT), NumPy and SciPy (BSD), PyUSB (BSD), libusb (LGPL). The Rust core uses rusb (MIT) and its vendored libusb build (LGPL-2.1-or-later). Their source distributions retain their respective licenses; a binary distribution must also meet those dependency license requirements.
