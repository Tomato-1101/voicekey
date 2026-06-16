"""
テキスト入力ハンドラーモジュール

クリップボードとキーボードシミュレーションを使用して、
文字起こし結果をアクティブなウィンドウに入力する機能を提供する。
日本語などのマルチバイト文字にも対応。
"""

import threading
import time
from typing import Optional

import pyperclip
from pynput.keyboard import Controller, Key

from ..utils.logger import get_logger
from ..platform import PlatformAdapter, get_platform_adapter

logger = get_logger(__name__)

# クリップボード貼り付け前の待機時間（秒）。Mac 版（Paster.swift）と揃えて短く保つ
PASTE_DELAY: float = 0.05
# 貼り付け後、クリップボードを元に戻すまでの待機時間（秒）。
# 貼り付け先アプリがクリップボードを読み終える前に復元すると
# 古い内容が貼られてしまうため、十分なマージンを取る
RESTORE_DELAY: float = 0.3


class InputHandler:
    """
    テキスト入力シミュレーションを管理するクラス。
    
    クリップボード経由でCtrl+Vを使用することで、
    日本語や中国語などのマルチバイト文字を確実に入力できる。
    """
    
    def __init__(self, platform_adapter: Optional[PlatformAdapter] = None) -> None:
        """キーボードコントローラーを初期化する。"""
        self._keyboard = Controller()
        self._platform = platform_adapter or get_platform_adapter()

    def insert_text(self, text: str) -> bool:
        """
        アクティブウィンドウにテキストを挿入する。

        クリップボード経由でCtrl+Vを使用することで、
        マルチバイト文字を確実に入力できる。
        ユーザーが元々コピーしていたテキストは貼り付け後に復元する
        （テキストのみ対象。画像等の非テキスト内容は復元できない）。

        Args:
            text: 挿入するテキスト

        Returns:
            成功した場合True、失敗した場合False
        """
        if not text:
            return False

        try:
            # ユーザーのクリップボード内容を退避
            old_clipboard = ""
            try:
                old_clipboard = pyperclip.paste() or ""
            except Exception:
                pass  # 退避失敗は復元を諦めるだけで、挿入自体は続行する

            # クリップボードにコピー
            pyperclip.copy(text)

            # クリップボードの準備が整うまで少し待機
            time.sleep(PASTE_DELAY)

            # OS別アダプタが定義する貼り付けショートカットを使用
            # 修飾キーが押しっぱなしになる事故を防ぐため try/finally で確実に release
            paste_modifier = self._platform.paste_modifier
            self._keyboard.press(paste_modifier)
            try:
                self._keyboard.press('v')
                self._keyboard.release('v')
            finally:
                try:
                    self._keyboard.release(paste_modifier)
                except Exception as e:
                    # release 失敗は致命ではないが、修飾キーが残ると操作不能になるため警告
                    logger.warning(f"貼り付け修飾キーの解放に失敗: {e}")

            # 貼り付け先がクリップボードを読み終えてから元の内容を復元する。
            # 復元待ち（RESTORE_DELAY）でこのスレッドを塞ぐと、呼び出し元の Enter 自動送信や
            # 録音中 UI の非表示がその分（実測 0.3 秒）遅れる。待機と復元はバックグラウンド
            # スレッドに逃がし、insert_text は貼り付け直後に返す
            if old_clipboard:
                threading.Timer(
                    RESTORE_DELAY, self._restore_clipboard, args=(old_clipboard,)
                ).start()

            logger.debug(f"テキスト挿入: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"テキスト挿入エラー: {e}")
            return False

    @staticmethod
    def _restore_clipboard(content: str) -> None:
        """退避したクリップボード内容を復元する（貼り付け完了後にバックグラウンドで遅延実行）。"""
        try:
            pyperclip.copy(content)
        except Exception as e:
            logger.warning(f"クリップボード復元に失敗: {e}")

    def press_enter(self) -> bool:
        """
        Enterキーを1回押す。

        チャットアプリ等でメッセージ送信に使用。

        Returns:
            成功した場合True、失敗した場合False
        """
        try:
            self._keyboard.press(Key.enter)
            self._keyboard.release(Key.enter)
            logger.debug("Enterキーを送信しました")
            return True
        except Exception as e:
            logger.error(f"Enterキー送信エラー: {e}")
            return False

    def type_text(self, text: str) -> bool:
        """
        テキストを1文字ずつ入力する。
        
        注意: この方法はinsert_text()より遅く、
        非ASCII文字では信頼性が低いため、
        通常はinsert_text()の使用を推奨。
        
        Args:
            text: 入力するテキスト
            
        Returns:
            成功した場合True、失敗した場合False
        """
        if not text:
            return False
            
        try:
            self._keyboard.type(text)
            return True
        except Exception as e:
            logger.error(f"テキスト入力エラー: {e}")
            return False
