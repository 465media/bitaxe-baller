; Inno Setup script for Bitaxe Baller.
;
; Builds a single-file Windows installer that drops the PyInstaller
; one-folder output into %LOCALAPPDATA%\Programs\BitaxeBaller, creates
; Start Menu + optional Desktop shortcuts, and registers an Uninstall
; entry under Add/Remove Programs.
;
; Compile from the repo root in the CI workflow:
;   iscc build\installer.iss
; Output: dist\Bitaxe-Baller-Windows.exe
;
; AppId is permanent — never change it. Inno Setup uses it to find
; previous installs so version upgrades replace cleanly instead of
; piling up side-by-side. If we ever fork or rename, generate a new one.

#define MyAppName       "Bitaxe Baller"
#define MyAppVersion    "1.9.1"
#define MyAppPublisher  "465 Media"
#define MyAppURL        "https://bitaxeballer.com"
#define MyAppExeName    "Bitaxe Baller.exe"

[Setup]
AppId={{6dc5cd19-b99a-4180-bc53-17fa64888c16}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/support.html
AppUpdatesURL={#MyAppURL}
; Per-user install — no admin prompt, works on locked-down machines.
DefaultDirName={localappdata}\Programs\BitaxeBaller
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Bitaxe-Baller-Windows
SetupIconFile=icons\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; The dashboard runs as a normal user-mode app; no special manifest needed.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Recursively bundle the entire PyInstaller one-folder output.
Source: "..\dist\Bitaxe Baller\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; Note: user data (CSV logs, config.json) lives in %APPDATA%\Bitaxe Baller\,
; not under {app}, so an uninstall doesn't touch the user's tuning history.
; The uninstaller intentionally leaves that directory alone.
