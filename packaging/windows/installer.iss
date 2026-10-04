; Inno Setup script for Wayfinder Aura — Windows double-click installer.
;
; Build (from repo root, after PyInstaller has produced dist\Wayfinder Aura\):
;     & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\windows\installer.iss
; or via packaging\windows\build.py which runs both steps.
;
; Per-user install (no admin / UAC), so it works for a normal Windows user and
; keeps SmartScreen friction low. Windows-only packaging — it must not touch the
; Linux AppImage/Flatpak or macOS .app build.

#ifndef MyAppVersion
  #define MyAppVersion "1.2.0-beta.5"
#endif
#define MyAppName "Wayfinder Aura"
#define MyAppPublisher "Wayfinder Collective"
#define MyAppURL "https://github.com/wayfindercollective/wayfinder-aura"
#define MyAppExeName "Wayfinder Aura.exe"

[Setup]
AppId={{B05C0360-2D2D-4160-A11A-B2A23B55A652}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\Wayfinder Aura
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist\installer
OutputBaseFilename=WayfinderAura-Setup-{#MyAppVersion}
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#ifdef SignAura
; build.py passes /DSignAura and the "aura" sign tool when a code-signing
; identity is configured: Setup.exe and the uninstaller are then signed too.
SignTool=aura
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the whole PyInstaller payload. Otherwise every file an
; older version shipped and this one dropped (SciPy, Qt pieces, DLLs: 125 MB on
; one PC) stays behind in _internal, where the app can still import it. Runs
; after Aura has been asked to quit; user data never lives under {app}.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; The entire PyInstaller onedir output.
Source: "..\..\dist\Wayfinder Aura\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Settings > System > "Open at login" writes this per-user Run value
; (src/wayfinder/utils/windows_login_item.py). Nothing is created at install;
; uninstall removes it so Windows never tries to start a missing exe.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#MyAppName}"; Flags: uninsdeletevalue dontcreatekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"; ValueType: none; ValueName: "{#MyAppName}"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
; Install Update in the app runs this installer with /VERYSILENT /relaunch=1
; (src/wayfinder/core/app_installer.py): start the new version afterwards.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: RelaunchAfterSilentUpdate

[Code]
{ Aura's window close button hides it to the tray (as on the Mac), so the
  installer's Restart Manager could not close it and an upgrade stopped at
  "Setup was unable to automatically close all applications".
  First ask a running Aura to quit the way Windows does at sign-out: its hidden
  lifecycle window gets WM_ENDSESSION, puts ducked audio back, and quits. Wait
  at most 5 s in all (bounded send, then exit polling). Only a copy that doesn't answer (builds from before
  this handler) is then ended with its helpers; its ducked audio, if any, is
  restored by the app's crash recovery the next time it starts. }
const
  WM_ENDSESSION = $0016;
  ENDSESSION_CLOSEAPP = 1;
  SMTO_ABORTIFHUNG = 2;

{ Bounded send: a hung Aura must never hang the installer before the fallback. }
function SendMessageTimeout(hWnd: HWND; Msg: Cardinal; wParam: Longint; lParam: Longint;
  fuFlags: Cardinal; uTimeout: Cardinal; var lpdwResult: Cardinal): Longint;
  external 'SendMessageTimeoutW@user32.dll stdcall';

procedure StopRunningAura();
var
  ResultCode, Waited: Integer;
  Wnd: HWND;
  Answer: Cardinal;
begin
  Wnd := FindWindowByClassName('WayfinderAuraLifecycle');
  if Wnd <> 0 then
  begin
    { At most 3 s for Aura to take the request, then up to 2 s to exit. }
    SendMessageTimeout(Wnd, WM_ENDSESSION, 1, ENDSESSION_CLOSEAPP,
                       SMTO_ABORTIFHUNG, 3000, Answer);
    Waited := 0;
    while (FindWindowByClassName('WayfinderAuraLifecycle') <> 0) and (Waited < 20) do
    begin
      Sleep(100);
      Waited := Waited + 1;
    end;
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "{#MyAppExeName}"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function RelaunchAfterSilentUpdate(): Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningAura();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningAura();
end;

[UninstallDelete]
; Remove the whole install tree (incl. PyInstaller's _internal folder) so an
; uninstall leaves nothing behind. User data (models, config, license) lives in
; %APPDATA%/%LOCALAPPDATA%\wayfinder-aura and is intentionally left untouched.
Type: filesandordirs; Name: "{app}"
