"""Render the application's own Qt widgets for offline visual QA."""
import argparse
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import time

from PySide6 import QtWidgets as W, QtGui
from PySide6.QtTest import QTest
import pyqtgraph as pg

from labdaq.gui import MainWindow, configure_application


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".work/preview"))
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = W.QApplication([])
    configure_application(app)
    window = MainWindow()
    window.show()
    window.start_preview()
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        app.processEvents()
        QTest.qWait(50)
    window.grab().save(str(args.output / "workbench.png"))
    window.nav.setCurrentRow(1)
    window.analysis_mode.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(args.output / "analysis.png"))
    window.nav.setCurrentRow(3)
    window.simulate_boot()
    app.processEvents()
    window.grab().save(str(args.output / "protocol.png"))
    window.open_vendor_sample()
    app.processEvents()
    window.grab().save(str(args.output / "hpf-replay.png"))
    window.analyze_replay()
    app.processEvents()
    window.grab().save(str(args.output / "hpf-analysis.png"))
    window.close()
    print(f"Rendered to {args.output}")


if __name__ == "__main__":
    main()
