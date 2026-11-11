# voicekey Windows 配布ビルドスクリプト
#
# 使い方（Windows 実機・リポジトリ直下で実行）:
#   powershell -ExecutionPolicy Bypass -File scripts\build\build_windows_dist.ps1 -Version 1.0.0
#
# 前提:
#   - venv\Scripts\python.exe に依存導入済み（pip install -r requirements.txt + pyinstaller）
#   - Inno Setup 6 インストール済み（ISCC.exe）
#
# 注意:
#   配布するのは personal 版（ログイン・課金・サーバー経由の層は無い）。利用者は設定画面で
#   自分の API キーを入れる。作者のキーを配布物に入れないため、ビルドに API キーは一切渡さず、
#   PyInstaller 後に verify_no_embedded_keys.py で exe にキーが混ざっていないことを検査する。
#
# 処理の流れ:
#   APP_VERSION 反映 → PyInstaller → ソース平文混入チェック → 鍵混入チェック →
#   Inno Setup → SHA256 表示
#
# 出力:
#   dist\installer\voicekey-<Version>-windows-setup.exe
#   （資産名の規則は src\utils\updater.py の WINDOWS_ASSET_NAME と揃える。
#     GitHub Release v<Version> への添付は .github/workflows/windows-build.yml が行う）

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"

# リポジトリ直下（このファイルは scripts\build\ にある）
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    # venv が無ければシステムの python に落とす（CI 等）
    $Python = "python"
}

# ISCC.exe の探索（PATH → 既定インストール先）
$Iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($Iscc) {
    $Iscc = $Iscc.Source
} else {
    $Candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Iscc) {
        Write-Error "ISCC.exe が見つかりません。Inno Setup 6 をインストールしてください（https://jrsoftware.org/isdl.php）"
    }
}

# --- 1. バージョンを constants.py に反映（アップデータの新旧比較に使う恒久変更。ビルド後にコミットする） ---
$ConstantsPath = Join-Path $Root "src\config\constants.py"
$Constants = Get-Content $ConstantsPath -Raw -Encoding UTF8
$NewConstants = $Constants -replace 'APP_VERSION: str = "[^"]+"', "APP_VERSION: str = `"$Version`""
if ($NewConstants -eq $Constants -and $Constants -notmatch [regex]::Escape("`"$Version`"")) {
    Write-Error "constants.py の APP_VERSION を更新できませんでした（書式が変わっていないか確認してください）"
}
Set-Content $ConstantsPath $NewConstants -Encoding UTF8 -NoNewline
Write-Host "==> APP_VERSION = $Version（src\config\constants.py）"

# --- 2. PyInstaller（onedir） ---
& $Python -m PyInstaller voicekey.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller に失敗しました" }

# --- 2.5 onnxruntime 同梱チェック（VAD は onnxruntime が無いと黙って無効化される） ---
# vad.py は遅延 import onnxruntime で Silero VAD を CPU 実行する。これが同梱されないと
# analyze() が安全側の (True, None) を返し、無音圧縮・長文分割が一切効かない（文字起こし
# 自体は動くため気付きにくい潜在バグ）。onnxruntime のコア拡張が dist にあるか実機確認する。
$OrtCore = Get-ChildItem (Join-Path $Root "dist\voicekey") -Recurse -Filter "onnxruntime_pybind11_state*" -ErrorAction SilentlyContinue
if (-not $OrtCore) {
    Write-Error "onnxruntime が dist に同梱されていません（VAD が無効になります）。requirements.txt の onnxruntime と voicekey.spec の collect_all('onnxruntime') を確認してください"
}
Write-Host "==> onnxruntime 同梱を確認（VAD 有効）: $($OrtCore[0].Name)"

# --- 3. 自分のソース平文混入チェック ---
# 目的は「自分の proprietary な src/ が平文 .py で同梱されていないこと」の確認。
# torch / torchaudio 等の OSS ライブラリは PyInstaller onedir では .py を必ず同梱する
# （しかも公開コードなので IP 漏洩には当たらない）。ここでは src 配下の .py のみを対象にする。
# src は voicekey.spec で datas=[] とし PYZ にバイトコード格納するため、本来 0 件のはず。
$LeakedPy = Get-ChildItem (Join-Path $Root "dist\voicekey") -Recurse -Filter "*.py" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\src\\' }
if ($LeakedPy) {
    $LeakedPy | ForEach-Object { Write-Host "  混入: $($_.FullName)" }
    Write-Error "dist に自分のソース（src の .py）が含まれています。voicekey.spec の datas を確認してください"
}
Write-Host "==> 自分のソース平文混入なし（dist\voicekey に src の .py なし）"

# --- 3.5 プロバイダーキー混入チェック（自分の exe にキーが無いこと） ---
# 作者のキーを配布物に入れないための安全装置。万一の混入をここで止める（外さないこと）。
# torch 等 OSS の DLL 群は誤検出ノイズになるため走査対象に含めず、自分の exe だけ渡す。
$ExeForScan = Join-Path $Root "dist\voicekey\voicekey.exe"
& $Python (Join-Path $Root "scripts\build\verify_no_embedded_keys.py") $ExeForScan
if ($LASTEXITCODE -ne 0) { Write-Error "鍵混入チェックに失敗しました（exe にプロバイダーキーの痕跡）" }

# --- 4. Inno Setup でインストーラ作成 ---
& $Iscc (Join-Path $Root "installer\windows\voicekey.iss") "/DAppVersion=$Version"
if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup（ISCC）に失敗しました" }

$Setup = Join-Path $Root "dist\installer\voicekey-$Version-windows-setup.exe"
if (-not (Test-Path $Setup)) { Write-Error "インストーラが見つかりません: $Setup" }

# --- 5. SHA256（確認用の表示。アプリ側は GitHub が資産ごとに出す digest と照合する） ---
$Sha256 = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "==> 完了"
Write-Host "    インストーラ : $Setup"
Write-Host "    SHA256       : $Sha256"
Write-Host ""
Write-Host "次の手順: GitHub Release v$Version へ setup.exe を添付し、Mac で署名（.sig）を足す"
Write-Host "  （CI の windows-build.yml が添付まで行う。署名の手順は scripts\build\sign_update.py の冒頭）"
