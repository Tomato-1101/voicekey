"""voicekey のユニットテスト（ロジック単位・ネットワーク/実機非依存）。"""

import os

# テスト実行中であることをアプリ側に知らせる（API 使用量などの実ファイルをリポジトリ直下に書かないため）
os.environ.setdefault("VOICEKEY_TESTING", "1")
