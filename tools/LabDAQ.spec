from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "tools" / "release_entry.py")],
    pathex=[str(root)],
    binaries=[(str(root / "target" / "release" / "labdaq-core.exe"), ".")],
    datas=[(str(root / "docs" / "用户手册"), "docs/用户手册"),
           (str(root / "docs" / "开发文档" / "technical-notes.md"), "docs/开发文档")],
    excludes=["torch", "tensorflow", "jax", "cupy", "numba", "matplotlib",
              "pandas", "IPython", "notebook", "pytest", "tkinter"],
)
# Qt 6.12 uses the Windows system ICU API. A Poppler/Conda copy found on PATH
# can have incompatible exports and makes QtCore fail before the GUI starts.
a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() != "icuuc.dll"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LabDAQ", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LabDAQ")
