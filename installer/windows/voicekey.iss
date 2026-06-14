; voicekey Windows インストーラ（Inno Setup 6）
;
; ビルド方法（Windows 実機・Inno Setup 6 インストール済み前提）:
;   ISCC.exe installer\windows\voicekey.iss /DAppVersion=1.0.0
; 通常は scripts\build\build_windows_dist.ps1 から呼ばれる。
;
; 設計:
; - PrivilegesRequired=lowest + {localappdata} 配下インストールで UAC 昇格なし
;   （自動アップデートのサイレント実行が管理者権限なしで完結する）
; - AppId は固定 GUID。変更すると更新時に別アプリ扱いになるので絶対に変えない
; - CloseApplications=yes で更新時に実行中の旧プロセスを閉じる

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{1EA86DED-3DDC-45D4-90DD-DB5B915A38B7}
AppName=voicekey
AppVersion={#AppVersion}
AppPublisher=voicekey
DefaultDirName={localappdata}\Programs\voicekey
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist\installer
OutputBaseFilename=voicekey-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
; PyInstaller の onedir 出力をそのまま取り込む
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
; デスクトップショートカットは任意（既定でチェック済み）。ユーザーが外せる。
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加アイコン:"

[Files]
Source: "..\..\dist\voicekey\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\voicekey"; Filename: "{app}\voicekey.exe"
Name: "{autostartup}\voicekey"; Filename: "{app}\voicekey.exe"; Comment: "ログイン時に voicekey を起動"
; デスクトップショートカット（desktopicon タスクが選択された場合のみ作成）
Name: "{autodesktop}\voicekey"; Filename: "{app}\voicekey.exe"; Tasks: desktopicon

[Run]
; インストール/サイレント更新の完了後に新版を自動起動する（skipifsilent は付けない）
Filename: "{app}\voicekey.exe"; Flags: nowait postinstall
