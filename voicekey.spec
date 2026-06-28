# -*- mode: python ; coding: utf-8 -*-
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

# src/ は同梱しない（Python ソース平文同梱の防止）。コードは PYZ アーカイブに入る。
# src/ 内に実行時読み込みのデータファイルは無いことを確認済み（.py のみ）
datas = []
binaries = []
hiddenimports = ['tzdata']

if sys.platform == "darwin":
    hiddenimports += [
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
    ]
elif sys.platform.startswith("win"):
    hiddenimports += [
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
    ]

# websockets は streaming_transcriber 内で遅延 import するため静的解析されない。
# Deepgram ストリーミングに必須なので明示的に全サブモジュールを収集する
hiddenimports += collect_submodules('websockets')

# src を datas で同梱しなくなった代わりに、動的 import の取りこぼしが
# 起きないよう全サブモジュールを PYZ へ確実に収集する
hiddenimports += collect_submodules('src')

# cryptography（自動アップデートの Ed25519 署名検証）。Rust 拡張バイナリと
# サブモジュールを確実に同梱する（取りこぼすと配布版で更新検証が import エラーになる）
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Collect silero_vad with all its data files
tmp_ret = collect_all('silero_vad')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Also collect silero_vad submodules explicitly
hiddenimports += collect_submodules('silero_vad')

# onnxruntime（Silero VAD の ONNX を CPU 実行する本体）。これを同梱しないと
# vad.py の遅延 import onnxruntime が配布版で失敗し、analyze() が安全側の
# (True, None) を返して VAD が常に無効化される（無音圧縮・長文分割が効かない）。
# requirements.txt への宣言（onnxruntime>=1.16.1）と対で必須。
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Collect PySide6 (only core modules needed)
tmp_ret = collect_all('PySide6')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.scripts.deploy_lib'],
    # noarchive=True だと各モジュールが .pyc ファイルとして dist に並び
    # 逆コンパイルが容易になるため、配布前提で PYZ アーカイブに固める
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='voicekey',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='voicekey',
)

# macOS 専用: メニューバー常駐アプリとして配布する .app バンドル。
# LSUIElement=True で起動時から Dock / Cmd+Tab に出さない（バックグラウンド常駐）。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name='voicekey.app',
        icon=None,
        bundle_identifier='com.voicekey.app',
        info_plist={
            'LSUIElement': True,
            'NSPrincipalClass': 'NSApplication',
            'NSHighResolutionCapable': True,
            'NSMicrophoneUsageDescription': '音声入力を文字起こしするためにマイクを使用します。',
        },
    )
