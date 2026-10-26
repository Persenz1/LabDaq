"""Source resources and writable locations for the portable Qt release."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
RUNTIME_DIR = APP_DIR / ".runtime"
RECORDING_ROOT = APP_DIR / "recordings"
