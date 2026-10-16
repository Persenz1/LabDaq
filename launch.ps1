param(
    [string]$CondaEnvironment = 'agents-py312',
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
if (-not $PythonPath) {
    $daqRegistry = Join-Path $env:USERPROFILE '.conda\environments.txt'
    $daqEnvironment = if (Test-Path -LiteralPath $daqRegistry) {
        Get-Content -LiteralPath $daqRegistry | Where-Object {
            $_ -and (Split-Path -Path $_ -Leaf) -eq $CondaEnvironment -and
            (Test-Path -LiteralPath (Join-Path $_ 'python.exe') -PathType Leaf)
        } | Select-Object -First 1
    }
    if (-not $daqEnvironment) {
        throw "未找到已注册的 Conda 环境 '$CondaEnvironment'。请用 -PythonPath 指定现有 python.exe。"
    }
    $PythonPath = Join-Path $daqEnvironment 'python.exe'
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python 不存在：$PythonPath"
}
$daqPython = (Resolve-Path -LiteralPath $PythonPath).Path
Set-Location -LiteralPath $PSScriptRoot
& $daqPython -m labdaq
exit $LASTEXITCODE
