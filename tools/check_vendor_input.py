"""Short Windows SDK input check; sequential single samples, not a DAQ backend.

Close QuickDAQ first. Uses DC, IEPE off, gain 1, +/-10 V on selected inputs
(AI0/1/2 by default; use --channels 3 for the STM32 input test).
--configure-only checks the settings and reads one value per selected input
without recording a file. Does not change the amplifier, EEPROM, or driver binding.
Each CSV row has its own host timestamp; channels are NOT sampled together.
"""
import argparse
from contextlib import nullcontext
import csv
import ctypes as c
from pathlib import Path
import statistics
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', default='DT9837-C(00)')
    parser.add_argument('--channels', type=int, nargs='+', choices=range(4), default=[0, 1, 2])
    parser.add_argument('--configure-only', action='store_true')
    parser.add_argument('--seconds', type=float, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    if len(set(args.channels)) != len(args.channels):
        parser.error('--channels must not contain duplicates')
    if args.configure_only and args.output is not None:
        parser.error('--configure-only does not write an --output file')
    if not args.configure_only and args.output is None:
        parser.error('--output is required unless --configure-only is used')

    api = c.WinDLL('oldaapi64.dll')
    H, U, D, P = c.c_void_p, c.c_uint32, c.c_double, c.POINTER
    signatures = {
        'olDaInitialize': [c.c_char_p, P(H)],
        'olDaGetDASS': [H, c.c_int32, U, P(H)],
        'olDaSetDataFlow': [H, U], 'olDaSetEncoding': [H, U],
        'olDaSetRange': [H, D, D],
        'olDaSetCouplingType': [H, U, c.c_int32],
        'olDaSetExcitationCurrentSource': [H, U, c.c_int32],
        'olDaGetCouplingType': [H, U, P(c.c_int32)],
        'olDaGetExcitationCurrentSource': [H, U, P(c.c_int32)],
        'olDaGetSingleValue': [H, P(c.c_int32), U, D],
        'olDaGetRange': [H, P(D), P(D)],
        'olDaGetResolution': [H, P(U)], 'olDaGetEncoding': [H, P(U)],
        'olDaConfig': [H], 'olDaReleaseDASS': [H], 'olDaTerminate': [H],
    }
    for name, signature in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = signature, U

    def call(name, *values):
        status = getattr(api, name)(*values)
        if status:
            raise RuntimeError(f'{name}: SDK error 0x{status:08X}')

    device, adc = H(), H()
    samples = {channel: [] for channel in args.channels}
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a diagnostic from replacing earlier data.
    with (nullcontext() if args.configure_only else args.output.open('x', newline='', encoding='utf-8')) as handle:
        if handle is not None:
            writer = csv.writer(handle)
            writer.writerow(['elapsed_host_s', 'channel', 'raw_code', 'voltage_V'])
        try:
            call('olDaInitialize', args.board.encode('mbcs'), c.byref(device))
            call('olDaGetDASS', device, 0, 0, c.byref(adc))  # OLSS_AD
            call('olDaSetDataFlow', adc, 801)  # OL_DF_SINGLEVALUE
            for channel in args.channels:
                call('olDaSetExcitationCurrentSource', adc, channel, 2)  # disabled
                call('olDaSetCouplingType', adc, channel, 0)  # DC
            call('olDaSetRange', adc, 10, -10)
            call('olDaSetEncoding', adc, 200)  # offset binary
            call('olDaConfig', adc)
            high, low, bits, encoding = D(), D(), U(), U()
            call('olDaGetRange', adc, c.byref(high), c.byref(low))
            call('olDaGetResolution', adc, c.byref(bits))
            call('olDaGetEncoding', adc, c.byref(encoding))
            if encoding.value != 200 or bits.value != 24:
                raise RuntimeError(f'Unexpected ADC format: {bits.value} bits, encoding {encoding.value}')
            if (low.value, high.value) != (-10.0, 10.0):
                raise RuntimeError(f'Unexpected ADC range: {low.value}..{high.value} V')
            for channel in args.channels:
                coupling, excitation = c.c_int32(), c.c_int32()
                call('olDaGetCouplingType', adc, channel, c.byref(coupling))
                call('olDaGetExcitationCurrentSource', adc, channel, c.byref(excitation))
                if coupling.value != 0 or excitation.value != 2:
                    raise RuntimeError(f'AI{channel}: unexpected SDK settings: coupling={coupling.value}, excitation={excitation.value}')
                print(f'AI{channel}: SDK settings DC, IEPE disabled, +/-10 V, single-value gain 1; output unit V', flush=True)
            if args.configure_only:
                for channel in args.channels:
                    raw = c.c_int32()
                    call('olDaGetSingleValue', adc, c.byref(raw), channel, 1.0)
                    code = raw.value & ((1 << bits.value) - 1)
                    volts = low.value + code * (high.value - low.value) / (1 << bits.value)
                    print(f'AI{channel}: single-value check {volts:+.6f} V (not a calibration)', flush=True)
                return
            print(f'READY: {args.board}, {low.value}..{high.value} V, {bits.value} bits; sequential single samples', flush=True)
            started = time.monotonic()
            next_print = 0
            while time.monotonic() - started < args.seconds:
                readings = []
                for channel in args.channels:
                    raw = c.c_int32()
                    call('olDaGetSingleValue', adc, c.byref(raw), channel, 1.0)
                    elapsed = time.monotonic() - started
                    code = raw.value & ((1 << bits.value) - 1)
                    volts = low.value + code * (high.value - low.value) / (1 << bits.value)
                    writer.writerow([elapsed, channel, code, volts])
                    samples[channel].append(volts)
                    readings.append(volts)
                handle.flush()
                if elapsed >= next_print:
                    print(f'{elapsed:6.2f} s: ' + ', '.join(f'AI{i}={v:+.6f} V' for i, v in zip(args.channels, readings)), flush=True)
                    next_print = elapsed + 1
                time.sleep(0.1)
        finally:
            try:
                if adc:
                    call('olDaReleaseDASS', adc)
            finally:
                if device:
                    call('olDaTerminate', device)
    for channel, values in samples.items():
        print(f'AI{channel}: n={len(values)}, mean={statistics.mean(values):+.6f} V, '
              f'min={min(values):+.6f}, max={max(values):+.6f}, std={statistics.pstdev(values):.6f} V')
    print(f'Saved {args.output}', flush=True)


if __name__ == '__main__':
    main()
