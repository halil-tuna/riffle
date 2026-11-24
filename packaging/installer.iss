; Riffle — Windows kurulum betiği (Inno Setup 6)
; Derleme:  iscc /DAppVersion=0.9.2 packaging\installer.iss   (önce python packaging\build_exe.py)
; Yönetici yetkisi istemez: %LOCALAPPDATA%\Programs\Riffle altına kurar, menü HKCU'ya yazılır.

#ifndef AppVersion
  #define AppVersion "0.9.2"
#endif

[Setup]
AppId={{6B1E2C3A-7D4F-4E8B-9A51-50F2C7E1A0B3}
AppName=Riffle
AppVersion={#AppVersion}
AppPublisher=Halil Tuna
AppPublisherURL=https://github.com/halil-tuna/riffle
AppComments=Riffle — sağ tıkta PDF ve belge araçları
DefaultDirName={localappdata}\Programs\Riffle
DefaultGroupName=Riffle
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Riffle-{#AppVersion}-kurulum
SetupIconFile=..\riffle\assets\riffle.ico
UninstallDisplayIcon={app}\Riffle.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Files]
Source: "..\dist\Riffle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Riffle — Sayfa düzenleyici"; Filename: "{app}\Riffle.exe"; Parameters: "organize"
Name: "{group}\Kaldır"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Riffle.exe"; Parameters: "--install-menu"; StatusMsg: "Sağ tık menüsü ekleniyor…"; Flags: runhidden waituntilterminated
Filename: "https://www.libreoffice.org/download/"; Description: "LibreOffice'i indir (Word/Excel/PowerPoint → PDF için)"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "https://github.com/UB-Mannheim/tesseract/wiki"; Description: "Tesseract OCR'ı indir (taranmış PDF; kurarken Turkish'i işaretle)"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "https://ghostscript.com/releases/gsdnld.html"; Description: "Ghostscript'i indir (PDF/A için)"; Flags: shellexec postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{app}\Riffle.exe"; Parameters: "--uninstall-menu"; Flags: runhidden waituntilterminated; RunOnceId: "MenuKaldir"

[Messages]
turkish.FinishedLabel=Kurulum tamamlandı. Bir dosyaya sağ tıklayıp "Daha fazla seçenek göster" → "Riffle" menüsünü kullanabilirsin.
