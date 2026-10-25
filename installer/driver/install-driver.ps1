param([Parameter(Mandatory=$true)][string]$DriverDirectory)
$ErrorActionPreference = 'Stop'
$daqInf = Join-Path $DriverDirectory 'labdaq-winusb.inf'
$daqCat = Join-Path $DriverDirectory 'labdaq-winusb.cat'
if (-not (Test-Path -LiteralPath $daqCat)) { throw '驱动包缺少正式签名目录文件 labdaq-winusb.cat，USB 安装未完成。' }
if ((Get-AuthenticodeSignature -LiteralPath $daqCat).Status -ne 'Valid') { throw 'USB 驱动包签名不受当前 Windows 信任，安装已停止。' }
& "$env:SystemRoot\System32\pnputil.exe" /add-driver $daqInf /install
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) { throw "Windows 驱动安装失败，退出码 $LASTEXITCODE。" }
if ($LASTEXITCODE -eq 3010) { exit 3010 }
# The Rust installer then selects the signed package for the two hardware IDs
# and checks the resulting services. Staging alone is not reported as binding.
Write-Output '双身份 WinUSB 驱动包已预装，接下来核对并切换设备绑定。'
