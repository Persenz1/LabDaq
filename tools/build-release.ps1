param([Parameter(Mandatory = $true)][string]$PythonPath)
$ErrorActionPreference = 'Stop'
$daqRoot = Split-Path -Parent $PSScriptRoot
$daqPython = (Resolve-Path -LiteralPath $PythonPath).Path
Set-Location -LiteralPath $daqRoot
. .\tools\dev-env.ps1
& cargo build --release -p labdaq-core
if ($LASTEXITCODE -ne 0) { throw 'Rust 核心构建失败' }
& $daqPython -m PyInstaller --noconfirm --distpath build\qt-dist --workpath build\qt tools\LabDAQ.spec
if ($LASTEXITCODE -ne 0) { throw 'Qt 打包失败' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'release-readme.txt') -Destination 'build\qt-dist\LabDAQ\使用说明.txt'
# Package the clean build, then update program files without deleting local recordings.
New-Item -ItemType Directory -Path 'dist\LabDAQ' -Force | Out-Null
Copy-Item -Path 'build\qt-dist\LabDAQ\*' -Destination 'dist\LabDAQ' -Recurse -Force
& $daqPython -c 'from pathlib import Path; from zipfile import ZipFile, ZIP_DEFLATED; root=Path("build/qt-dist/LabDAQ"); archive=Path("dist/LabDAQ-windows-x64.zip"); z=ZipFile(archive,"w",ZIP_DEFLATED,compresslevel=6); [z.write(p,p.relative_to(root.parent)) for p in root.rglob("*") if p.is_file()]; z.close(); print(f"{archive}: {archive.stat().st_size/1_000_000:.2f} MB")'
if ($LASTEXITCODE -ne 0) { throw 'ZIP 打包失败' }
