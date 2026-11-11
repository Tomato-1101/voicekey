"""voicekey のユニットテスト（ロジック単位・ネットワーク/実機非依存）。"""

import os

# テストが実機の資格情報ストア（Keychain / 資格情報マネージャー）へ絶対に触れないようにする。
# keyring のサービス名は実アプリが使う項目と同じなので、実ストアを読み書きすると利用者の実キーを壊す。
# keyring は最初の利用時にこの環境変数を見てバックエンドを決めるため、どのテストよりも先に設定する。
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
