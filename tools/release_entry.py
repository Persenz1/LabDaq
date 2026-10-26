from pathlib import Path
import sys

if Path(sys.executable).stem.lower() == "labdaq-api":
    from labdaq.api import main
else:
    from labdaq.gui import main

raise SystemExit(main())
