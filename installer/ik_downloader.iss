; IK DOWNLOADER installer (Inno Setup 6).  Build:  ISCC.exe installer\ik_downloader.iss
; Needs dist\IK-DOWNLOADER.exe (run build_exe.bat first).  Output: dist\IK-DOWNLOADER-Setup.exe
; Wizard pages: Welcome > License agreement > Folder > Start Menu folder > Extra tasks > Ready > Installing > Finish

#define AppName "IK DOWNLOADER"
#ifndef AppVersion
#define AppVersion "2.6.0"
#endif
#define AppExe "IK-DOWNLOADER.exe"

[Setup]
AppId={{6F2B8C14-3A5D-4E7B-9C21-8D4E5F1A7B30}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=IK DOWNLOADER
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=license.txt
SetupIconFile=..\assets\icon.ico
WizardImageFile=..\assets\wizard.bmp
WizardSmallImageFile=..\assets\wizard_small.bmp
WizardStyle=classic
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=IK-DOWNLOADER-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nIt is recommended that you close your browser and other programs before continuing.%n%nClick Next to continue.
FinishedLabel=Setup has finished installing [name] on your computer.%n%nTo catch downloads and videos in your browser, load the browser extension once (the last checkbox below opens its folder).

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startup"; Description: "Start {#AppName} when Windows starts"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\browser-extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Browser extension folder"; Filename: "{app}\browser-extension"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "explorer.exe"; Parameters: """{app}\browser-extension"""; Description: "Open the browser-extension folder (then load it at chrome://extensions)"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "KillIKD"
