; Riffle — Windows kurulum betiği (Inno Setup 6)
; Derleme:  iscc /DAppVersion=1.0.0 packaging\installer.iss   (önce python packaging\build_exe.py)
; Yönetici yetkisi istemez: %LOCALAPPDATA%\Programs\Riffle altına kurar, menü HKCU'ya yazılır.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B1E2C3A-7D4F-4E8B-9A51-50F2C7E1A0B3}
AppName=Riffle
AppVersion={#AppVersion}
AppPublisher=Halil Tuna
AppPublisherURL=https://github.com/halil-tuna/riffle
AppComments=Riffle — right-click PDF & document tools
DefaultDirName={localappdata}\Programs\Riffle
DefaultGroupName=Riffle
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Riffle-{#AppVersion}-setup
SetupIconFile=..\riffle\assets\riffle.ico
UninstallDisplayIcon={app}\Riffle.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
ArchitecturesInstallIn64BitMode=x64compatible

ShowLanguageDialog=no

; Kurulum dili Windows dilinden seçilir (Türkçe değilse İngilizce). Uygulamanın kendi dili de
; sistemden gelir; Ayarlar'dan değiştirilebilir.
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
english.Organizer=Riffle — Page organizer
turkish.Organizer=Riffle — Sayfa düzenleyici
english.Uninstall=Uninstall Riffle
turkish.Uninstall=Riffle'ı kaldır
english.AddingMenu=Adding the right-click menu…
turkish.AddingMenu=Sağ tık menüsü ekleniyor…
english.GetLibreOffice=Download LibreOffice (for Word/Excel/PowerPoint → PDF)
turkish.GetLibreOffice=LibreOffice'i indir (Word/Excel/PowerPoint → PDF için)
english.GetTesseract=Download Tesseract OCR (scanned PDFs; tick your languages while installing)
turkish.GetTesseract=Tesseract OCR'ı indir (taranmış PDF; kurarken Turkish'i işaretle)
english.GetGhostscript=Download Ghostscript (for PDF/A)
turkish.GetGhostscript=Ghostscript'i indir (PDF/A için)

[Files]
Source: "..\dist\Riffle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{cm:Organizer}"; Filename: "{app}\Riffle.exe"; Parameters: "organize"
Name: "{group}\{cm:Uninstall}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Riffle.exe"; Parameters: "--install-menu"; StatusMsg: "{cm:AddingMenu}"; Flags: runhidden waituntilterminated
Filename: "https://www.libreoffice.org/download/"; Description: "{cm:GetLibreOffice}"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "https://github.com/UB-Mannheim/tesseract/wiki"; Description: "{cm:GetTesseract}"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "https://ghostscript.com/releases/gsdnld.html"; Description: "{cm:GetGhostscript}"; Flags: shellexec postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{app}\Riffle.exe"; Parameters: "--uninstall-menu"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveMenu"

[Messages]
english.FinishedLabel=Setup is complete. Right-click a file and choose "Show more options" → "Riffle".
turkish.FinishedLabel=Kurulum tamamlandı. Bir dosyaya sağ tıklayıp "Daha fazla seçenek göster" → "Riffle" menüsünü kullanabilirsin.
