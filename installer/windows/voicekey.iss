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
; - ログイン時起動は **アプリの設定 UI（HKCU\...\Run）に一元化** する（item 14）。
;   インストーラーは Startup ショートカットを作らない。常設すると UI の OFF を無視して
;   必ず起動し、UI で ON にすると Run キーと二重起動し得るため。旧版が作った
;   ショートカットは [InstallDelete] でアップグレード時に除去する。
;
; ログイン時起動の検証マトリクス（item 14・Windows 実機で確認）:
;   1. 新規インストール直後: Startup ショートカット無し・Run 値無し → ログイン時に起動しない
;   2. UI で ON: Run 値が登録される → 次回ログインで 1 回だけ起動（ショートカットは無いので二重起動しない）
;   3. UI で OFF: Run 値が削除される → ログイン時に起動しない
;   4. 旧版（ショートカット有り）からアップグレード: [InstallDelete] でショートカットが消え、
;      以後は Run 値のみが起動可否を決める（二重起動しない）
;   5. 再インストール: 既存の Run 値はインストーラーが触らないため ON/OFF 状態が維持される

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
; ログイン時起動の Startup ショートカットは作成しない（設定 UI の HKCU\...\Run に一元化・item 14）
; デスクトップショートカット（desktopicon タスクが選択された場合のみ作成）
Name: "{autodesktop}\voicekey"; Filename: "{app}\voicekey.exe"; Tasks: desktopicon

[InstallDelete]
; 旧版が常設していた Startup ショートカットを除去する（アップグレード時の二重起動防止・item 14）。
; 以後ログイン時起動は設定 UI が管理する Run キーのみで決まる。
Type: files; Name: "{autostartup}\voicekey.lnk"

[Run]
; インストール/サイレント更新の完了後に新版を自動起動する（skipifsilent は付けない）
Filename: "{app}\voicekey.exe"; Flags: nowait postinstall
