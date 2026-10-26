$ErrorActionPreference = 'Stop'
$env:CARGO_HOME = 'C:\DevTools\Rust\cargo'
$env:RUSTUP_HOME = 'C:\DevTools\Rust\rustup'
$env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
$daqMingw = 'C:\DevTools\MinGW\mingw64\bin'
if (Test-Path -LiteralPath (Join-Path $daqMingw 'gcc.exe')) {
    $env:PATH = "$daqMingw;$env:PATH"
    $env:RUSTUP_TOOLCHAIN = 'stable-x86_64-pc-windows-gnu'
}
$daqVsCandidates = @('C:\DevTools\BuildTools\Common7\Tools\VsDevCmd.bat')
$daqVsWhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $daqVsWhere) {
    $daqFound = & $daqVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($daqFound) { $daqVsCandidates += Join-Path $daqFound 'Common7\Tools\VsDevCmd.bat' }
}
$daqDevCmd = $daqVsCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($daqDevCmd -and -not (Test-Path -LiteralPath (Join-Path $daqMingw 'gcc.exe'))) {
    $daqEnvironmentLines = & cmd.exe /d /s /c "`"$daqDevCmd`" -arch=x64 -host_arch=x64 >nul && set"
    foreach ($daqLine in $daqEnvironmentLines) {
        if ($daqLine -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
}
if (-not (Get-Command cargo.exe -ErrorAction SilentlyContinue)) { throw '请先将 Rust 安装至 C:\DevTools\Rust。' }
