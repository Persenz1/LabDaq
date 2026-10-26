param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [string]$IsccPath,
    [string]$VendorInstallerPath
)
$ErrorActionPreference = 'Stop'
$daqRoot = Split-Path -Parent $PSScriptRoot
$daqPython = (Resolve-Path -LiteralPath $PythonPath).Path
Set-Location -LiteralPath $daqRoot

if (-not $IsccPath) {
    $daqCompiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($daqCompiler) { $IsccPath = $daqCompiler.Source }
    else {
        $IsccPath = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
    }
}
if (-not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw '未找到 Inno Setup 编译器。请用 -IsccPath 指定现有 ISCC.exe。'
}
$daqIscc = (Resolve-Path -LiteralPath $IsccPath).Path
$daqWork = Join-Path $daqRoot 'build\installer'
$daqAppFiles = Join-Path $daqWork 'app\LabDAQ'
$daqOutput = Join-Path $daqRoot 'dist'

if (-not $VendorInstallerPath) {
    $daqOmni = Join-Path $daqRoot 'resources\vendor\installers\DataAcqOMNI_V7.8.9.zip'
    $daqVendor = Join-Path $daqWork 'vendor'
    New-Item -ItemType Directory -Path $daqVendor -Force | Out-Null
    $VendorInstallerPath = Join-Path $daqVendor 'SetupOem.exe'
    $daqArchive = [System.IO.Compression.ZipFile]::OpenRead($daqOmni)
    try {
        $daqEntry = $daqArchive.GetEntry('OEM Setup/SetupOem.exe')
        if (-not $daqEntry) { throw 'OMNI ZIP 中缺少完整 OEM Setup/SetupOem.exe。' }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($daqEntry, $VendorInstallerPath, $true)
    }
    finally { $daqArchive.Dispose() }
}
$daqOem = (Resolve-Path -LiteralPath $VendorInstallerPath).Path
$daqVersion = & $daqPython -c 'from labdaq import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0) { throw '无法读取 LabDAQ 版本号' }

. .\tools\dev-env.ps1
& cargo build --release -p labdaq-core
if ($LASTEXITCODE -ne 0) { throw 'Rust 核心构建失败' }
& $daqPython -m PyInstaller --noconfirm --distpath (Join-Path $daqWork 'app') --workpath (Join-Path $daqWork 'pyinstaller') tools\LabDAQ.spec
if ($LASTEXITCODE -ne 0) { throw 'Qt 打包失败' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'release-readme.txt') -Destination (Join-Path $daqAppFiles '使用说明.txt')
Copy-Item -LiteralPath (Join-Path $daqRoot 'THIRD_PARTY_NOTICES.md') -Destination $daqAppFiles
New-Item -ItemType Directory -Path $daqOutput -Force | Out-Null
& $daqIscc "/DAppVersion=$daqVersion" "/DAppFiles=$daqAppFiles" "/DOemInstaller=$daqOem" "/DOutputDir=$daqOutput" (Join-Path $PSScriptRoot 'LabDAQ.iss')
if ($LASTEXITCODE -ne 0) { throw 'Windows 安装包构建失败' }
Write-Output (Join-Path $daqOutput "LabDAQ-$daqVersion-windows-x64-setup.exe")
