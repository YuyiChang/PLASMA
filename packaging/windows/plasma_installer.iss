; Inno Setup script for PLASMA's Windows installer.
;
; Built by .github/workflows/build.yml's build-windows job, against the
; PyInstaller onedir output at dist/PLASMA_Windows_x64/ (see app_windows.spec)
; — run:
;   ISCC.exe /DMyAppVersion=<version> packaging\windows\plasma_installer.iss
; from the repo root. Writes dist/PLASMA_Windows_x64_Setup.exe.
;
; Unsigned (no SignTool= directive) — first run shows a Windows SmartScreen
; "unknown publisher" warning, same as today's raw unsigned .exe. Add
; SignTool= here later if a code-signing cert is set up.

#define MyAppName "PLASMA"
#define MyAppPublisher "YuyiChang"
#define MyAppURL "https://github.com/YuyiChang/PLASMA"
#define MyAppExeName "PLASMA_Windows_x64.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; Generated once for this app — DO NOT regenerate. Inno uses this GUID
; (not the app name/version) to recognize "this is an upgrade of the same
; app" across installs.
AppId={{A9F3E6B2-4C1D-4E7A-9B6F-3D2C8E1F0A7B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\PLASMA
DefaultGroupName=PLASMA
DisableProgramGroupPage=yes
; relative to this .iss file's own directory (packaging\windows\)
OutputDir=..\..\dist
OutputBaseFilename=PLASMA_Windows_x64_Setup
SetupIconFile=..\..\plasma\resources\icons\plasma.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; the whole onedir folder (exe + its support-files directory) — everything
; PLASMA needs to run, copied as-is
Source: "..\..\dist\PLASMA_Windows_x64\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PLASMA"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall PLASMA"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PLASMA"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; PLASMA has no window of its own (console app, opens a browser tab) — just
; launch it directly, no Terminal-wrapper trick needed the way macOS's .app
; bundle requires.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch PLASMA now"; Flags: nowait postinstall skipifsilent
