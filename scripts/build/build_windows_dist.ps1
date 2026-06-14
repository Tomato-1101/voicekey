# voicekey Windows 配布ビルドスクリプト
#
# 使い方（Windows 実機・リポジトリ直下で実行）:
#   powershell -ExecutionPolicy Bypass -File scripts\build\build_windows_dist.ps1 -Version 1.0.0
#
# 前提:
#   - venv\Scripts\python.exe に依存導入済み（pip install -r requirements.txt + pyinstaller）
#   - Inno Setup 6 インストール済み（ISCC.exe）
#   - リポジトリ直下に .env.dist がある
#     （Mac 側で macos/scripts/generate_embedded_keys.sh --export-env を実行して手動コピー）
#
# 処理の流れ:
#   キー埋め込み生成 → PyInstaller → ソース平文混入チェック → Inno Setup →
#   SHA256 → version.json 出力 → 埋め込みキーの後始末
#
# 出力:
#   dist\installer\voicekey-<Version>-setup.exe
#   dist\installer\version.json   （voicekey-releases/windows/ へコミットする）

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

$EmbeddedKeys = Join-Path $Root "src\config\embedded_keys.py"

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

try {
    # --- 1. バージョンを constants.py に反映（アップデータの新旧比較に使う恒久変更。ビルド後にコミットする） ---
    $ConstantsPath = Join-Path $Root "src\config\constants.py"
    $Constants = Get-Content $ConstantsPath -Raw -Encoding UTF8
    $NewConstants = $Constants -replace 'APP_VERSION: str = "[^"]+"', "APP_VERSION: str = `"$Version`""
    if ($NewConstants -eq $Constants -and $Constants -notmatch [regex]::Escape("`"$Version`"")) {
        Write-Error "constants.py の APP_VERSION を更新できませんでした（書式が変わっていないか確認してください）"
    }
    Set-Content $ConstantsPath $NewConstants -Encoding UTF8 -NoNewline
    Write-Host "==> APP_VERSION = $Version（src\config\constants.py）"

    # --- 2. 埋め込みキー生成（.env.dist → src\config\embedded_keys.py） ---
    & $Python (Join-Path $Root "scripts\build\generate_embedded_keys.py")
    if ($LASTEXITCODE -ne 0) { Write-Error "埋め込みキー生成に失敗しました" }

    # --- 3. PyInstaller（onedir） ---
    & $Python -m PyInstaller voicekey.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller に失敗しました" }

    # --- 4. 自分のソース平文混入チェック ---
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

    # --- 5. Inno Setup でインストーラ作成 ---
    & $Iscc (Join-Path $Root "installer\windows\voicekey.iss") "/DAppVersion=$Version"
    if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup（ISCC）に失敗しました" }

    $Setup = Join-Path $Root "dist\installer\voicekey-$Version-setup.exe"
    if (-not (Test-Path $Setup)) { Write-Error "インストーラが見つかりません: $Setup" }

    # --- 6. SHA256 と version.json（自動アップデータが参照する） ---
    $Sha256 = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
    $VersionJson = @{
        version = $Version
        url     = "https://github.com/Tomato-1101/voicekey-releases/releases/download/v$Version/voicekey-$Version-setup.exe"
        sha256  = $Sha256
        notes   = "voicekey $Version"
    } | ConvertTo-Json
    $VersionJsonPath = Join-Path $Root "dist\installer\version.json"
    # BOM 無し UTF-8 で書く。Set-Content -Encoding UTF8（PS 5.1）は BOM を付けてしまい、
    # 素朴な json.loads(...decode("utf-8")) 系パーサが先頭 BOM で落ちる。
    # 自動アップデータのフィードなので確実に BOM 無しにする。
    [System.IO.File]::WriteAllText($VersionJsonPath, $VersionJson, (New-Object System.Text.UTF8Encoding $false))
    Write-Host ""
    Write-Host "==> 完了"
    Write-Host "    インストーラ : $Setup"
    Write-Host "    SHA256       : $Sha256"
    Write-Host "    version.json : $VersionJsonPath"
    Write-Host ""
    Write-Host "次の手順:"
    Write-Host "  1. setup.exe を voicekey-site/downloads/ へ、version.json を voicekey-site/windows/ へ配置"
    Write-Host "  2. voicekey-site で vercel deploy --prod（exe と version.json が同時に公開される）"
    Write-Host "  3. constants.py の APP_VERSION 変更を voicekey 本体リポジトリへコミット"
}
finally {
    # 埋め込みキーは開発環境に残さない（gitignore 済みだが多層防御）
    if (Test-Path $EmbeddedKeys) {
        Remove-Item $EmbeddedKeys -Force
        Write-Host "==> 後始末: embedded_keys.py を削除しました"
    }
}
