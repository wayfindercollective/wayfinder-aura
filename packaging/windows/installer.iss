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
  #define MyAppVersion "1.1.8"
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller onedir output.
Source: "..\..\dist\Wayfinder Aura\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the whole install tree (incl. PyInstaller's _internal folder) so an
; uninstall leaves nothing behind. User data (models, config, license) lives in
; %APPDATA%/%LOCALAPPDATA%\wayfinder-aura and is intentionally left untouched.
Type: filesandordirs; Name: "{app}"
