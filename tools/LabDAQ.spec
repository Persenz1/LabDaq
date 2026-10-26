from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "tools" / "release_entry.py")],
    pathex=[str(root)],
    binaries=[(str(root / "target" / "release" / "labdaq-core.exe"), ".")],
    datas=[(str(root / "docs" / "用户手册"), "docs/用户手册")],
    excludes=["torch", "tensorflow", "jax", "cupy", "numba", "matplotlib",
              "pandas", "IPython", "notebook", "pytest", "tkinter"],
)
# Qt 6.12 uses the Windows system ICU API. A Poppler/Conda copy found on PATH
# can have incompatible exports and makes QtCore fail before the GUI starts.
a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() != "icuuc.dll"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LabDAQ", console=False)
# The PySide6 runtime hook imports QtCore before release_entry.py runs.
# The CLI shares the bundle but does not need Qt's runtime initialization.
api_scripts = [item for item in a.scripts if Path(item[1]).name != "pyi_rth_pyside6.py"]
api_exe = EXE(pyz, api_scripts, [], exclude_binaries=True, name="LabDAQ-api", console=True)
coll = COLLECT(exe, api_exe, a.binaries, a.datas, name="LabDAQ")
