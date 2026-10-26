#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppFiles
  #error AppFiles is required
#endif
#ifndef OemInstaller
  #error OemInstaller is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{D6C4A74F-9FCB-46DE-9DCB-B645CF74698B}
AppName=LabDAQ
AppVersion={#AppVersion}
DefaultDirName={autopf}\LabDAQ
DefaultGroupName=LabDAQ
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=LabDAQ-{#AppVersion}-windows-x64-setup
UninstallDisplayIcon={app}\LabDAQ.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#OemInstaller}"; DestName: "SetupOem.exe"; Flags: dontcopy
Source: "{#AppFiles}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LabDAQ"; Filename: "{app}\LabDAQ.exe"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"

[Run]
Filename: "{app}\LabDAQ.exe"; Description: "启动 LabDAQ"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: CanLaunchApp

[Code]
var
  VendorInstalled: Boolean;
  VendorRestartRequired: Boolean;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if VendorInstalled then
    Exit;
  WizardForm.PreparingLabel.Caption := '正在安装原厂设备驱动与 Open Layers 运行库...';
  ExtractTemporaryFile('SetupOem.exe');
  { Official OEM silent invocation, with the standard MSI property to defer reboot. }
  if not Exec(ExpandConstant('{tmp}\SetupOem.exe'),
      '/s /v"/qn REBOOT=ReallySuppress"', ExpandConstant('{tmp}'),
      SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    Result := '无法启动原厂安装程序：' + SysErrorMessage(ExitCode);
    Exit;
  end;
  Log(Format('原厂 OEM 安装程序退出码：%d', [ExitCode]));
  if (ExitCode <> 0) and (ExitCode <> 3010) and (ExitCode <> 1641) then
  begin
    Result := Format('原厂驱动与运行库安装失败（退出码 %d）。请处理该错误后重新运行安装程序。', [ExitCode]);
    Exit;
  end;
  VendorRestartRequired := (ExitCode = 3010) or (ExitCode = 1641);
  VendorInstalled := True;
end;

function NeedRestart: Boolean;
begin
  Result := VendorRestartRequired;
end;

function CanLaunchApp: Boolean;
begin
  Result := not VendorRestartRequired;
end;
