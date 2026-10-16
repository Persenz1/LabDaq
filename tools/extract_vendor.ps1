<#
.SYNOPSIS
Extract the already downloaded Data Translation reference packages without installing them.
.DESCRIPTION
Requires the author's ISx executable (https://github.com/lifenjoiner/ISx/releases/tag/v0.3.11)
and 7-Zip. No vendor EXE/MSI is executed. MSI databases are opened in read-only mode.
Intermediate files remain under .work/windows-analysis for the project's cleanup workflow.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$IsxPath,
    [string]$SevenZipPath = 'C:\Program Files\7-Zip\7z.exe',
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$IsxPath = (Resolve-Path -LiteralPath $IsxPath).Path
$SevenZipPath = (Resolve-Path -LiteralPath $SevenZipPath).Path
$inputDir = Join-Path $ProjectRoot 'resources\vendor\installers'
$outputDir = Join-Path $ProjectRoot 'resources\vendor\extracted'
$workDir = Join-Path $ProjectRoot ('.work\windows-analysis\extract-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

function Expand-ArchiveFile([string]$Archive, [string]$Destination, [string[]]$Members = @()) {
    & $SevenZipPath x $Archive "-o$Destination" '-y' @Members | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "7-Zip failed to extract $Archive" }
}
function Expand-InstallShieldFile([string]$Archive, [string]$Destination) {
    & $IsxPath $Archive $Destination | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ISx failed to extract $Archive" }
}
function Export-MappedFiles([string]$Msi, [string]$Raw, [string]$Destination, [string]$Pattern) {
    $installer = New-Object -ComObject WindowsInstaller.Installer
    $database = $installer.OpenDatabase($Msi, 0)
    $directories = @{}
    $view = $database.OpenView('SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`')
    $view.Execute()
    while ($row = $view.Fetch()) {
        $directories[$row.StringData(1)] = @($row.StringData(2), $row.StringData(3).Split('|')[-1].Split(':')[0])
    }
    $view.Close()
    function Get-MappedDirectory([string]$Id) {
        if (!$directories.ContainsKey($Id)) { return $Id }
        $entry = $directories[$Id]
        if (!$entry[0]) { return '' }
        $parent = Get-MappedDirectory $entry[0]
        if ($entry[1] -eq '.') { return $parent }
        if (!$parent) { return $entry[1] }
        return Join-Path $parent $entry[1]
    }
    $view = $database.OpenView('SELECT `File`.`File`, `File`.`FileName`, `Component`.`Directory_` FROM `File`, `Component` WHERE `File`.`Component_` = `Component`.`Component`')
    $view.Execute()
    $count = 0
    while ($row = $view.Fetch()) {
        $id = $row.StringData(1)
        $name = $row.StringData(2).Split('|')[-1]
        $directory = Get-MappedDirectory $row.StringData(3)
        if ($name -match $Pattern -or $directory -match $Pattern) {
            $source = Join-Path $Raw $id
            $targetDir = Join-Path $Destination $directory
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination (Join-Path $targetDir $name)
            $count++
        }
    }
    $view.Close()
    Write-Host "$count files extracted to $Destination"
}
function Export-MsiFiles([string]$Msi, [string]$StageName, [string]$Destination, [string]$Pattern) {
    $stage = Join-Path $workDir $StageName
    $raw = Join-Path $stage 'files'
    Expand-ArchiveFile $Msi $stage @('Data1.cab')
    Expand-ArchiveFile (Join-Path $stage 'Data1.cab') $raw
    Export-MappedFiles $Msi $raw $Destination $Pattern
}

$omni = Join-Path $workDir 'omni'
Expand-ArchiveFile (Join-Path $inputDir 'DataAcqOMNI_V7.8.9.zip') $omni @('OEM Setup\SetupOem.exe', 'Ol.NET\setup.exe')
$oem = Join-Path $workDir 'oem'
$oem64 = Join-Path $workDir 'oem64'
$olnet = Join-Path $workDir 'olnet'
$quickdaq = Join-Path $workDir 'quickdaq'
Expand-InstallShieldFile (Join-Path $omni 'OEM Setup\SetupOem.exe') $oem
Expand-InstallShieldFile (Join-Path $oem 'SetupOem_x64.SetupOem_x64.exe') $oem64
Expand-InstallShieldFile (Join-Path $omni 'Ol.NET\setup.exe') $olnet
Expand-InstallShieldFile (Join-Path $inputDir 'QuickDAQ-3.7.0.49-Setup.exe') $quickdaq
Export-MsiFiles (Join-Path $oem 'Data Translation Open Layers (OEM).msi') 'oem32-cab' (Join-Path $outputDir 'oem-x86') '9837a|oldaapi|olmem'
Export-MsiFiles (Join-Path $oem64 'Data Translation Open Layers (OEM) (x64).msi') 'oem64-cab' (Join-Path $outputDir 'oem-x64') '9837a|oldaapi|olmem'
Export-MsiFiles (Join-Path $olnet 'Data Translation Open Layers.msi') 'sdk-cab' (Join-Path $outputDir 'sdk-reference') '9837|SDK\\Include|ContADC|ReadBufferedDataAsVolts|ReadBufferedIepeDataAsRaw'
Export-MsiFiles (Join-Path $quickdaq 'Data Translation QuickDAQ.msi') 'quickdaq-cab' (Join-Path $outputDir 'quickdaq') '.*'
Write-Host "No drivers or vendor applications installed. Temporary workspace: $workDir"
