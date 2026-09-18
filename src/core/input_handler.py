"""
テキスト入力ハンドラーモジュール

クリップボードとキーボードシミュレーションを使用して、
文字起こし結果をアクティブなウィンドウに入力する機能を提供する。
日本語などのマルチバイト文字にも対応。
"""

import threading
import time
from enum import Enum
from typing import Optional

import pyperclip
from pynput.keyboard import Controller, Key

from ..utils.logger import get_logger
from ..platform import PlatformAdapter, get_platform_adapter

logger = get_logger(__name__)

# クリップボード貼り付け前の待機時間（秒）。Mac 版（Paster.swift）と揃えて短く保つ
PASTE_DELAY: float = 0.05
# 貼り付け後、クリップボードを元に戻すまでの待機時間（秒）。
# 貼り付け先アプリが Ctrl+V を処理してクリップボードを読み終える前に復元すると、
# 復元後の内容＝ユーザーが前にコピーしていたものが貼られる。0.3 秒では
# ブラウザや Electron 製アプリが読み終わらないことがあり、実際に
# 「音声の内容が入らず前のコピーが貼られた」不具合が出たため広げた。
# 復元が遅れる害は「貼り付け直後 1 秒以内の手動 Ctrl+V で文字起こし結果が貼られる」だけで、
# 取り違えより軽い（Mac 版 Paster.swift の restoreDelay と揃える）
RESTORE_DELAY: float = 1.0


class RestoreDecision(Enum):
    """貼り付け後にクリップボードをどう戻すかの判定結果。"""

    #: より新しい貼り付けが復元を担当する → 何もしない（世代分離）
    SKIP = "skip"
    #: 待っている間にユーザー・他アプリが新しくコピーした → 触らない
    LEAVE_USER_CONTENT = "leave_user_content"
    #: 退避した原本を書き戻す
    RESTORE = "restore"
    #: 戻せる原本が無い（空 or 非テキスト）→ 自分が入れたテキストを消す
    CLEAR = "clear"


def decide_restore(
    current_generation: int,
    task_generation: int,
    clipboard_is_still_ours: bool,
    original: Optional[str],
) -> RestoreDecision:
    """クリップボード復元の行動を決める（副作用なし＝テスト対象）。

    Args:
        current_generation: 現在の貼り付け世代
        task_generation: この復元タスクが担当する世代
        clipboard_is_still_ours: クリップボードが自分の挿入テキストのままか
        original: 退避したユーザーの原本（無ければ None / 空文字）

    Returns:
        取るべき行動
    """
    if current_generation != task_generation:
        return RestoreDecision.SKIP
    if not clipboard_is_still_ours:
        return RestoreDecision.LEAVE_USER_CONTENT
    if original:
        return RestoreDecision.RESTORE
    # 原本が無いときに何もしないと文字起こし結果が残り続ける（ユーザー指摘の主因）
    return RestoreDecision.CLEAR


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
        # クリップボード復元の世代管理。listener / Timer / 呼び出し元の複数スレッドから
        # 触れるためロックで保護する。
        # - _paste_gen: 貼り付けごとに増える世代番号。古い Timer の復元を無効化する
        # - _injected_text: 直近に自分がコピーしたテキスト（復元可否の判定に使う）
        # - _saved_original: 復元すべきユーザーの真のクリップボード内容
        self._clip_lock = threading.Lock()
        self._paste_gen = 0
        self._injected_text: Optional[str] = None
        self._saved_original: Optional[str] = None

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
            try:
                current = pyperclip.paste() or ""
            except Exception:
                current = ""  # 退避失敗は復元を諦めるだけで、挿入自体は続行する

            # 世代を採番し、復元すべき「真のオリジナル」を確定する。
            # 連続貼り付け（前回の復元がまだ終わっていない）でクリップボードが自分の
            # 挿入テキストのままなら、それを原本と誤認せず前回保存したオリジナルを
            # 引き継ぐ。こうしないと最後の復元で自分の挿入テキストを書き戻してしまう。
            with self._clip_lock:
                self._paste_gen += 1
                gen = self._paste_gen
                if self._saved_original is not None and current == self._injected_text:
                    original = self._saved_original
                else:
                    original = current
                self._saved_original = original
                self._injected_text = text

            # クリップボードにコピー
            pyperclip.copy(text)

            # ここから先は何が起きても復元タイマーを必ず登録する（try/finally）。
            # キー合成の失敗等で復元が一度も予約されないと、ユーザーの元のクリップボード
            # 内容が挿入テキストで上書きされたまま失われる
            try:
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
            finally:
                # 貼り付け先がクリップボードを読み終えてから元の内容を復元する。
                # 復元待ち（RESTORE_DELAY）でこのスレッドを塞ぐと、呼び出し元の Enter 自動送信や
                # 録音中 UI の非表示がその分（実測 0.3 秒）遅れる。待機と復元はバックグラウンド
                # スレッドに逃がし、insert_text は貼り付け直後に返す
                threading.Timer(
                    RESTORE_DELAY, self._restore_clipboard, args=(gen,)
                ).start()

            logger.debug(f"テキスト挿入: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"テキスト挿入エラー: {e}")
            return False

    def _restore_clipboard(self, gen: int) -> None:
        """退避したクリップボード内容を復元する（貼り付け完了後にバックグラウンドで遅延実行）。

        復元待ちの間にユーザーが新しくコピーした場合や、より新しい貼り付けが
        発生した場合は、その内容を壊さないために復元しない。

        Args:
            gen: この復元に対応する貼り付け世代。現在の世代と一致しなければ無効
        """
        try:
            with self._clip_lock:
                if gen != self._paste_gen:
                    return  # 世代分離（判定関数に渡すまでもない早期 return）
                try:
                    current = pyperclip.paste() or ""
                except Exception:
                    return
                decision = decide_restore(
                    current_generation=self._paste_gen,
                    task_generation=gen,
                    clipboard_is_still_ours=(current == self._injected_text),
                    original=self._saved_original,
                )
                original = self._saved_original
                self._injected_text = None
                self._saved_original = None

            if decision is RestoreDecision.RESTORE:
                pyperclip.copy(original)
            elif decision is RestoreDecision.CLEAR:
                # 戻せる原本が無い（空 or 画像などの非テキスト）。ここで何もしないと
                # 文字起こし結果がクリップボードに残ってしまうため明示的に空にする
                pyperclip.copy("")
                logger.debug("クリップボード復元: 原本なしのため消去")
            elif decision is RestoreDecision.LEAVE_USER_CONTENT:
                logger.debug("クリップボード復元: スキップ（ユーザーが新しくコピー）")
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
