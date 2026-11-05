; The Windows setup: the PyInstaller folder build, a Start menu entry, an uninstaller. Data stays
; in %LOCALAPPDATA%\haretic\installer. ISCC /DVersion=x.y.z /DSource=dist\haretic-installer
#ifndef Version
  #define Version "0.0.0"
#endif
[Setup]
AppId={{6C3E7A52-3B1F-4E0B-9A0C-6B2E1A7F4D21}
AppName=haretic installer
AppVersion={#Version}
AppPublisher=haretic
AppPublisherURL=https://github.com/haretic/mtkclient
DefaultDirName={autopf}\haretic installer
DefaultGroupName=haretic installer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=haretic-installer-{#Version}-windows-x64-setup
SetupIconFile=haretic-installer.ico
UninstallDisplayIcon={app}\haretic-installer.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE

[Files]
Source: "..\{#Source}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\haretic installer"; Filename: "{app}\haretic-installer.exe"
Name: "{autodesktop}\haretic installer"; Filename: "{app}\haretic-installer.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\haretic-installer.exe"; Description: "Start haretic installer"; Flags: nowait postinstall skipifsilent
