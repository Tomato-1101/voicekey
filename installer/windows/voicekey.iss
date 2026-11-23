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
; - 自動更新ではアプリがインストーラを起動してから自分で終了する。インストーラは PrepareToInstall で
;   旧プロセス（voicekey.exe）が終わるのを最大 30 秒待ってからファイルを置き換え、[Run] で新版を起動する。
;   待たずに進むと、終了途中の旧プロセスがファイルを掴んだまま「使用中」で中止され（/SUPPRESSMSGBOXES は
;   中止を選ぶ）、新版も起動しなかった。待っても残っていれば CloseApplications=force で閉じる
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
; 資産名の規則は src\utils\updater.py の WINDOWS_ASSET_NAME と揃える（Mac の zip と同じリリースに並ぶため windows を入れる）
OutputBaseFilename=voicekey-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
; 旧プロセスが自分で終わるのを PrepareToInstall で待ち、それでも残っていれば強制で閉じる
; （yes だと閉じられなかった時にサイレント更新が中止され、新版も起動しなかった）
CloseApplications=force
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

[Code]
{ 実行中の voicekey.exe の数を数える（WMI。自分のユーザーのプロセスが見えれば足りる）。
  数えられなかった時は 0 を返し、待たずに CloseApplications に任せる }
function CountRunningVoicekey(): Integer;
var
  Locator, Service, Procs: Variant;
begin
  Result := 0;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''voicekey.exe''');
    Result := Procs.Count;
  except
    Result := 0;
  end;
end;

{ ファイルを置き換える前に、旧プロセスが自分で終わるのを待つ（最大 30 秒）。
  アプリ側は更新のための終了を 12 秒で打ち切るので、通常はその前に終わる }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to 120 do
  begin
    if CountRunningVoicekey() = 0 then
      Exit;
    Sleep(250);
  end;
  Log('voicekey.exe が 30 秒で終わらなかったため、CloseApplications で閉じます');
end;
