"""
録音中 HUD モジュール（画面下部中央の小型ピル）

見た目は Mac 版 Hud.swift と同じにする（寸法・色・動きの数値はそこが正本）:
- 待機中: 極小の待機ピル（hud_always_visible が ON のときだけ・中身なし）
- 録音中: 貼り付け先アプリのアイコン・灯りの色の点・音声レベル連動の波形
  （ストリーミングの途中結果があれば波形の代わりにそれを出す。自動 Enter は
  リターンの印、翻訳される回は「→EN」等の印、ハンズフリーは波形を灯りの色に）
- 変換中: 「変換中…」を静止で表示
- 通知: エラー・無音検出などを 2 秒だけ表示
ピルは下端を固定したまま内容に合わせて大きさを変え、ばねの動きで滑らかに変形する。

描き方は Windows 向けにしてある。音量が届くたびに描き直すのではなく、16ms の
タイマー 1 本が「大きさ・波形・中身の入れ替わり・出現と消滅」を目標へ近づけて
描く（60fps。届く音量は約 30 回/秒なので、間を補って滑らかに見せる）。
動くものが無くなったらタイマーは止める（待機中に CPU を使わない）。

最重要の制約: HUD は**絶対にキーフォーカスを奪ってはならない**。
フォーカスを奪うと貼り付け先ウィンドウが変わり、文字起こし結果が別の場所に
入力されてしまう。そのため非アクティブ表示・入力透過・タスクバー非登録の
フレームレスウィンドウとして実装する。
"""

from collections import deque
from typing import Deque, List, Optional

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from ..core import fullscreen
from . import theme

# 録音中として扱うモード（自動 Enter の有無だけが違う）
_RECORDING_MODES = ("recording", "recording_auto_enter")


class Hud(QWidget):
    """
    録音状態を示すフローティング HUD（フレームレス・入力透過・非アクティブ）。

    すべての公開メソッドはメインスレッドから呼ばれる前提（アプリは
    Qt シグナルのキュー接続でワーカースレッドからメインへホップさせる）。

    Attributes:
        enabled: HUD を表示するか（設定で無効化可能。False なら常に非表示）
        always_visible: 待機中も小型ピルを常時表示するか（config.hud_always_visible）
    """

    # --- 寸法（Mac 版 HudView と同じ値） ---
    WIDTH = 460
    HEIGHT = 56
    # ピル下端はタスクバーの 14px 上（Mac は Dock の 14px 上）。Mac はパネル下 8＋内側 6 だが、
    # 内側が 6 だと影（ぼかし 8＋下へ 2）が窓の下端で切れて線に見えるので、内側を広く取る
    BOTTOM_INSET = 12         # パネル下端からピル下端まで
    SCREEN_GAP = 2            # 作業領域（タスクバーの上端）からパネル下端まで
    PILL_HEIGHT = 30          # 録音（アイコン 18 + 上下 6）
    TRANSCRIBING_HEIGHT = 28  # 変換中（アイコン 18 + 上下 5）
    NOTICE_HEIGHT = 28        # 通知（12px の 1 行 + 上下 6）
    IDLE_PILL_WIDTH = 64      # 待機ピル（中身 40×8 + 左右 12・上下 3）
    IDLE_PILL_HEIGHT = 14
    PADDING_X = 13            # 録音・通知の左右の余白
    TRANSCRIBING_PADDING_X = 12
    CONTENT_GAP = 10          # 中身どうしの間隔
    ICON_SIZE = 18            # 貼り付け先アプリのアイコン
    ICON_RADIUS = 4
    DOT_SIZE = 7              # 状態の点
    BAR_COUNT = 24
    BAR_WIDTH = 3
    BAR_GAP = 2.5
    BAR_MIN_H = 3.0
    BAR_MAX_H = 18.0
    LIVE_TEXT_WIDTH = 190     # 途中結果の欄。幅を固定して横揺れを防ぐ
    RETURN_GLYPH_W = 11       # 自動 Enter の印の幅

    # --- 時間（Mac 版と同じ値） ---
    NOTICE_MSEC = 2000          # 通知の表示時間
    BAR_FOLLOW_SEC = 0.05       # 波形が新しい音量へ届くまで
    CONTENT_OUT_SEC = 0.08      # 中身が消える
    CONTENT_IN_SEC = 0.12       # 中身が現れる
    FULLSCREEN_POLL_MSEC = 700  # 待機ピル表示中にフルスクリーン状態を見る間隔

    def __init__(self, enabled: bool = True) -> None:
        """HUD を初期化する（生成時は非表示）。"""
        super().__init__()
        self.enabled = enabled
        self.always_visible = False  # 待機ピル常時表示（app が config から設定する）

        self._mode = "hidden"   # hidden/idle/recording/recording_auto_enter/transcribing/notice
        self._caption = ""      # ストリーミングの途中結果
        self._notice_text = ""  # 一時通知の本文
        self._hands_free = False
        self._translate_badge = ""               # 翻訳される回の印（空なら出さない）
        self._app_icon: Optional[QPixmap] = None  # 貼り付け先アプリのアイコン
        # 変換中の文字は明滅させない（明滅の谷で「…」が先に消え、文字が横に揺れて見えるため）
        self._transcribe_opacity = 1.0

        # 波形: 届いた音量（目標）と、いま描いている高さ（0.0〜1.0）
        self._levels: Deque[float] = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
        self._bar_from: List[float] = [0.0] * self.BAR_COUNT
        self._bar_shown: List[float] = [0.0] * self.BAR_COUNT
        self._bar_t = 1.0

        # ピルの大きさ（ばね）。待機ピルの大きさから始めると、初回表示で極小ピルから育つ
        self._w = theme.Spring(float(self.IDLE_PILL_WIDTH))
        self._h = theme.Spring(float(self.IDLE_PILL_HEIGHT))
        # 出現・消滅（0.0 = 消えている、1.0 = 出ている）。透明度と拡大率 0.8〜1.0 に使う
        self._presence = theme.Spring(0.0)
        # 中身の入れ替え: いま描いている中身と、その濃さ
        self._content_mode = "hidden"
        self._content_alpha = 1.0

        self._setup_window()

        # 通知の自動消去タイマー（単発）
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._on_notice_timeout)

        # 動きを進めるタイマー。動くものがある間だけ回す
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(theme.Motion.FRAME_MS)
        self._timer.timeout.connect(self._tick)

        # タスクバー自動非表示の切替・解像度変更・プライマリ画面変更に追従して再配置する。
        self._primary_screen = None
        self._connect_screen_signals()

        # フルスクリーン判定（前面ウィンドウがモニタ全面を覆っているか）。テストで差し替え
        # られるよう関数参照で保持する（既定は core.fullscreen の実プラットフォーム判定）。
        self._fullscreen_probe = fullscreen.foreground_is_fullscreen
        # 待機ピル表示中だけ、前面アプリのフルスクリーン化/解除に追従して隠す/出すための監視。
        # Mac は collectionBehavior で OS に委譲するが Windows には相当機構が無いため自前で監視する。
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(self.FULLSCREEN_POLL_MSEC)
        self._fs_timer.timeout.connect(self._tick_fullscreen)

        # 外観（ライト／ダーク）が変わったら描き直す
        theme.Theme.instance().changed.connect(self.update)

    def _setup_window(self) -> None:
        """フォーカスを奪わない・入力を透過するフレームレスウィンドウを構成する。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint        # 枠なし
            | Qt.WindowType.WindowStaysOnTopHint     # 常に最前面
            | Qt.WindowType.Tool                     # タスクバーに出さない
            | Qt.WindowType.WindowDoesNotAcceptFocus  # フォーカスを受け取らない
            | Qt.WindowType.WindowTransparentForInput  # クリック透過
        )
        # 背景を透過してピル形状だけ描画する
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 表示時にアクティブ化しない（貼り付け先のフォーカスを維持する最重要設定）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resize(self.WIDTH, self.HEIGHT)

    def _connect_screen_signals(self) -> None:
        """画面構成変化に追従するためのシグナルを接続する。

        タスクバーの自動非表示 ON/OFF や解像度変更で availableGeometry が変わると
        下部中央の位置がずれる。表示中なら再配置して縦位置を追従させる。
        プライマリ画面が入れ替わったら接続を張り替える。
        """
        app = QGuiApplication.instance()
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_available_geometry_changed)
            self._primary_screen = screen
        if app is not None:
            app.primaryScreenChanged.connect(self._on_primary_screen_changed)

    # ------------------------------------------------------------------
    # 公開 API（メインスレッドから呼ぶ）
    # ------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        """
        アプリ状態に応じて HUD 表示を切り替える。

        Args:
            state: "idle"/"recording"/"recording_auto_enter"/"transcribing"
        """
        if state == "idle":
            # 通知表示中はタイマーに任せて消さない
            if self._mode == "notice":
                return
            if self.always_visible:
                # 待機中も極小の待機ピルを残す（録音停止後は録音サイズから待機サイズへ縮む）。
                # ただし前面アプリがフルスクリーンなら待機ピルは退避する（録音/変換/通知は退避しない）。
                self._caption = ""
                self._mode = "idle"
                if self._foreground_is_fullscreen():
                    self._hide_keep_idle()
                else:
                    self._show_pill()
                self._ensure_fs_timer(True)
            else:
                self._hide()
            return

        if state in _RECORDING_MODES:
            self._notice_timer.stop()
            self._ensure_fs_timer(False)  # 録音中はフルスクリーンでも常に表示する
            if self._mode not in _RECORDING_MODES:
                # 新しい録音のたびに途中結果と波形をリセットする。録音中に自動 Enter へ
                # 切り替わっただけ（ダブルタップの 2 打目）のときは同じ録音なので消さない
                self._caption = ""
                self._levels = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
                self._bar_from = [0.0] * self.BAR_COUNT
                self._bar_shown = [0.0] * self.BAR_COUNT
                self._bar_t = 1.0
            self._mode = state
            self._show_pill()
        elif state == "transcribing":
            self._notice_timer.stop()
            self._ensure_fs_timer(False)  # 変換中はフルスクリーンでも常に表示する
            self._mode = "transcribing"
            self._show_pill()

    def set_hands_free(self, hands_free: bool) -> None:
        """現在の録音がハンズフリー（toggle 実効）かを反映する（波形の着色に使う）。

        status_changed より先に届く前提。ラベルは出さず色だけで通常録音と区別する（Mac 版と対）。
        """
        self._hands_free = bool(hands_free)
        if self._mode in _RECORDING_MODES:
            self.update()

    def push_level(self, level: float) -> None:
        """音声レベル（0.0-1.0）を波形バーに反映する（録音中のみ）。"""
        if self._mode not in _RECORDING_MODES:
            return
        # いま描いている高さを起点に、新しい並びへ BAR_FOLLOW_SEC で近づける
        self._bar_from = list(self._bar_shown)
        self._levels.append(max(0.0, min(1.0, float(level))))
        self._bar_t = 0.0
        self._ensure_ticking()

    def set_caption(self, text: str) -> None:
        """ストリーミングの途中結果を更新する（録音中のみ反映）。"""
        if self._mode not in _RECORDING_MODES:
            return
        had_caption = bool(self._caption)
        self._caption = text or ""
        # 欄の幅は固定なので、大きさが変わるのは「出た／消えた」の瞬間だけ
        if had_caption != bool(self._caption):
            self._retarget(animate=True)
        self.update()

    def set_translate_badge(self, text: str) -> None:
        """翻訳される回の印（「→EN」等）を出す。空文字で消す。"""
        text = text or ""
        if text == self._translate_badge:
            return
        self._translate_badge = text
        if self._mode in _RECORDING_MODES:
            self._retarget(animate=True)
            self.update()

    def set_app_icon(self, icon: Optional[QPixmap]) -> None:
        """貼り付け先アプリのアイコンを設定する（取れなければ None。そのぶん幅が詰まる）。"""
        self._app_icon = icon if icon is not None and not icon.isNull() else None
        if self._mode in _RECORDING_MODES or self._mode == "transcribing":
            self._retarget(animate=True)
            self.update()

    def show_notice(self, text: str) -> None:
        """一時通知を NOTICE_MSEC だけ表示する。"""
        if not self.enabled:
            return
        self._ensure_fs_timer(False)  # 通知はフルスクリーンでも常に表示する
        self._notice_text = text or ""
        self._mode = "notice"
        self._show_pill()
        self._notice_timer.start(self.NOTICE_MSEC)

    def settle(self) -> None:
        """進行中の動きを全部終わらせ、いまの状態の最終形にする（静止画を撮るとき用）。"""
        self._timer.stop()
        w, h = self._target_size()
        self._w.snap(w)
        self._h.snap(h)
        self._content_mode = self._mode
        self._content_alpha = 1.0
        self._bar_shown = list(self._levels)
        self._bar_t = 1.0
        if self._mode == "hidden":
            self._presence.snap(0.0)
            self.hide()
        else:
            self._presence.snap(1.0)
        self.update()

    # ------------------------------------------------------------------
    # フルスクリーン時の待機ピル退避
    # ------------------------------------------------------------------

    def _foreground_is_fullscreen(self) -> bool:
        """前面ウィンドウがフルスクリーンか（判定不能・例外時は False）。"""
        try:
            return bool(self._fullscreen_probe())
        except Exception:
            return False

    def _ensure_fs_timer(self, active: bool) -> None:
        """待機ピルのフルスクリーン監視ポーリングを開始/停止する。

        待機ピル常時表示（always_visible）かつ待機中のときだけ監視する。録音/変換/通知中は
        フルスクリーンでも表示し続けるため監視は不要。
        """
        want = bool(active) and self.always_visible and self.enabled
        if want and not self._fs_timer.isActive():
            self._fs_timer.start()
        elif not want and self._fs_timer.isActive():
            self._fs_timer.stop()

    def _hide_keep_idle(self) -> None:
        """モードは idle のまま、ウィンドウだけ隠す（フルスクリーン退避）。

        _hide() と違い _mode を "hidden" にしない。フルスクリーン解除時に _tick_fullscreen が
        待機ピルを出し直せるよう、待機中であることを保持する。動画やゲームの上に
        消えかけのピルを残さないよう、動きを付けずにすぐ隠す。
        """
        self._timer.stop()
        self._reset_to_smallest()
        self.hide()

    def _tick_fullscreen(self) -> None:
        """待機ピル表示中に前面アプリのフルスクリーン化/解除へ追従する（ポーリング）。"""
        if self._mode != "idle" or not self.always_visible or not self.enabled:
            self._ensure_fs_timer(False)
            return
        fullscreen_now = self._foreground_is_fullscreen()
        if fullscreen_now and self.isVisible():
            self._hide_keep_idle()
        elif not fullscreen_now and not self.isVisible():
            self._show_pill()

    # ------------------------------------------------------------------
    # 内部: 状態の切り替え
    # ------------------------------------------------------------------

    def _on_notice_timeout(self) -> None:
        """通知タイマー満了で待機ピルに戻す（常時表示 OFF なら隠す）。"""
        if self._mode != "notice":
            return
        if self.always_visible:
            self._mode = "idle"
            self._show_pill()
        else:
            self._hide()

    def _on_available_geometry_changed(self, _rect) -> None:
        """画面の作業領域が変わったら（タスクバー出没・解像度変更）表示中は再配置する。"""
        if self.isVisible():
            self._reposition()

    def _on_primary_screen_changed(self, screen) -> None:
        """プライマリ画面が入れ替わったら availableGeometryChanged を張り替えて再配置する。"""
        if self._primary_screen is not None:
            try:
                self._primary_screen.availableGeometryChanged.disconnect(
                    self._on_available_geometry_changed
                )
            except (RuntimeError, TypeError):
                pass
        self._primary_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_available_geometry_changed)
        if self.isVisible():
            self._reposition()

    def _reset_to_smallest(self) -> None:
        """次に出すとき待機ピルの大きさから育つよう、動きの状態を最小へ戻す。"""
        self._w.snap(float(self.IDLE_PILL_WIDTH))
        self._h.snap(float(self.IDLE_PILL_HEIGHT))
        self._presence.snap(0.0)
        self._content_mode = "hidden"
        self._content_alpha = 1.0

    def _retarget(self, animate: bool) -> None:
        """現在モードの目標の大きさへ、ばねで（または即座に）合わせる。

        Args:
            animate: True なら今の大きさから目標へ動かす、False なら即座に合わせる
        """
        w, h = self._target_size()
        if animate and self.isVisible():
            self._w.target = w
            self._h.target = h
            self._ensure_ticking()
        else:
            self._w.snap(w)
            self._h.snap(h)
            self.update()

    def _show_pill(self) -> None:
        """HUD を画面下部中央へ配置して表示し、現在モードへ変形させる（アクティブ化しない）。"""
        if not self.enabled:
            return
        was_visible = self.isVisible()
        self._reposition()
        if not was_visible:
            # 出す瞬間に待たせない: 窓はすぐ出し、育つ動きだけを後から付ける。
            # 待機ピルの大きさから育てる（待機ピル自身はその大きさのまま現れる）
            self._reset_to_smallest()
            self._content_mode = self._mode
            self.show()
        self.raise_()
        self._presence.target = 1.0
        w, h = self._target_size()
        self._w.target = w
        self._h.target = h
        self._ensure_ticking()

    def _hide(self) -> None:
        """HUD を隠す（表示中なら縮みながら消え、消えきってから窓を閉じる）。"""
        self._mode = "hidden"
        self._ensure_fs_timer(False)
        if self.isVisible():
            self._presence.target = 0.0
            self._ensure_ticking()
        else:
            self._timer.stop()
            self._reset_to_smallest()

    def _reposition(self) -> None:
        """プライマリスクリーンの作業領域（タスクバーのすぐ上）の下部中央に配置する。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.WIDTH) // 2
        y = geo.y() + geo.height() - self.HEIGHT - self.SCREEN_GAP
        self.move(x, y)

    # ------------------------------------------------------------------
    # 内部: 動きを進める
    # ------------------------------------------------------------------

    def _ensure_ticking(self) -> None:
        """動きを進めるタイマーを（止まっていれば）回し始める。"""
        if not self._timer.isActive():
            self._clock.restart()
            self._timer.start()
        self.update()

    def _tick(self) -> None:
        """1 コマぶん動きを進めて描き直す。動くものが無くなったら止まる。"""
        dt = max(0.001, self._clock.restart() / 1000.0)
        moving = False

        # 大きさ・出現と消滅
        moving |= self._w.step(dt)
        moving |= self._h.step(dt)
        moving |= self._presence.step(dt)
        if self._mode == "hidden" and self._presence.value <= 0.02:
            # 消えきった: 窓を閉じて止める
            self._timer.stop()
            self._reset_to_smallest()
            self.hide()
            return

        # 中身の入れ替え（前の中身を消してから、新しい中身を出す）
        if self._content_mode != self._mode and self._mode != "hidden":
            self._content_alpha -= dt / self.CONTENT_OUT_SEC
            if self._content_alpha <= 0.0:
                self._content_mode = self._mode
                self._content_alpha = 0.0
            moving = True
        elif self._content_alpha < 1.0:
            self._content_alpha = min(1.0, self._content_alpha + dt / self.CONTENT_IN_SEC)
            moving = True

        # 波形（届いた音量へ線形に近づける）
        if self._bar_t < 1.0:
            self._bar_t = min(1.0, self._bar_t + dt / self.BAR_FOLLOW_SEC)
            t = self._bar_t
            self._bar_shown = [
                a + (b - a) * t for a, b in zip(self._bar_from, self._levels)
            ]
            moving = True

        if not moving:
            self._timer.stop()
        self.update()

    # ------------------------------------------------------------------
    # 大きさの計算
    # ------------------------------------------------------------------

    @staticmethod
    def _font(size: int, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
        """HUD の文字に使うフォントを返す。"""
        return theme.font(size, weight)

    def _bars_width(self) -> float:
        """波形バー群の合計幅を返す。"""
        return self.BAR_COUNT * self.BAR_WIDTH + (self.BAR_COUNT - 1) * self.BAR_GAP

    def _icon_width(self) -> float:
        """アプリアイコンが占める幅（間隔込み。無ければ 0）。"""
        return float(self.ICON_SIZE + self.CONTENT_GAP) if self._app_icon is not None else 0.0

    def _badge_width(self) -> float:
        """翻訳の印の幅（無ければ 0）。"""
        if not self._translate_badge:
            return 0.0
        metrics = QFontMetricsF(self._font(10, QFont.Weight.Bold))
        return metrics.horizontalAdvance(self._translate_badge)

    def _content_width(self, mode: str) -> float:
        """指定モードで描く中身の幅（ピルを中身に合わせるため）。"""
        if mode in _RECORDING_MODES:
            width = self._icon_width() + self.DOT_SIZE
            if not self._caption:
                width += self.CONTENT_GAP + self._bars_width()
            if mode == "recording_auto_enter":
                width += self.CONTENT_GAP + self.RETURN_GLYPH_W
            if self._translate_badge:
                width += self.CONTENT_GAP + self._badge_width()
            if self._caption:
                width += self.CONTENT_GAP + self.LIVE_TEXT_WIDTH
            return width
        if mode == "transcribing":
            metrics = QFontMetricsF(self._font(11))
            return self._icon_width() + metrics.horizontalAdvance("変換中…")
        if mode == "notice":
            metrics = QFontMetricsF(self._font(12))
            return min(
                metrics.horizontalAdvance(self._notice_text),
                float(self.WIDTH - 2 * self.PADDING_X - 2),
            )
        return 0.0

    def _target_size(self) -> tuple:
        """現在のモードでのピル目標サイズ（幅, 高さ）を返す。"""
        mode = self._mode
        if mode == "idle":
            return (float(self.IDLE_PILL_WIDTH), float(self.IDLE_PILL_HEIGHT))
        if mode in _RECORDING_MODES:
            return (self._content_width(mode) + 2 * self.PADDING_X, float(self.PILL_HEIGHT))
        if mode == "transcribing":
            return (
                self._content_width(mode) + 2 * self.TRANSCRIBING_PADDING_X,
                float(self.TRANSCRIBING_HEIGHT),
            )
        if mode == "notice":
            return (self._content_width(mode) + 2 * self.PADDING_X, float(self.NOTICE_HEIGHT))
        return (self._w.value, self._h.value)

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        """ピルの面と、状態ごとの中身を描く。"""
        presence = max(0.0, min(1.0, self._presence.value))
        if self._content_mode == "hidden" and self._mode == "hidden":
            return
        if presence <= 0.0:
            return

        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ピルの外形。下端を固定して上へ育つ（Mac 版と同じ）
        pill_w = max(float(self.IDLE_PILL_HEIGHT), min(self.width() - 2.0, self._w.value))
        pill_h = max(4.0, min(self.height() - self.BOTTOM_INSET - 2.0, self._h.value))
        pill_rect = QRectF(
            (self.width() - pill_w) / 2,
            self.height() - self.BOTTOM_INSET - pill_h,
            pill_w,
            pill_h,
        )

        # 出現・消滅: 透明度と、ピルの中心を軸にした 0.8〜1.0 の拡大
        painter.setOpacity(presence)
        scale = 0.8 + 0.2 * presence
        center = pill_rect.center()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)

        small = self._mode == "idle" and self._content_mode == "idle"
        self._paint_capsule(painter, pill_rect, p, small)

        # 中身はピルの形で切り抜く（変形の途中でピルより広い中身がはみ出さない）
        if self._content_mode not in ("hidden", "idle") and self._content_alpha > 0.0:
            path = QPainterPath()
            radius = pill_rect.height() / 2
            path.addRoundedRect(pill_rect, radius, radius)
            painter.save()
            painter.setClipPath(path)
            painter.setOpacity(presence * self._content_alpha)
            if self._content_mode in _RECORDING_MODES:
                self._paint_recording(painter, pill_rect, p, self._content_mode)
            elif self._content_mode == "transcribing":
                self._paint_transcribing(painter, pill_rect, p)
            elif self._content_mode == "notice":
                self._paint_notice(painter, pill_rect, p)
            painter.restore()

        painter.end()

    def _paint_capsule(
        self, painter: QPainter, rect: QRectF, p: theme.Palette, small: bool
    ) -> None:
        """カプセルの影・面・縁を描く。

        Mac は背景をぼかしたガラスに色を薄く重ねている。Windows では、動いて形の変わる
        カプセルの後ろだけをぼかす手段が無い（窓全体の四角がぼける）ので、同じ色を濃く
        塗って、どんな画面の上でも文字が読めるようにする。
        """
        radius = rect.height() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        # 影（待機ピルは弱く小さく）。外へ広げた層を重ね、中心で指定の濃さになるようにする
        blur, dy, alpha = (4, 1.0, 0.10) if small else (8, 2.0, 0.16)
        total = blur * (blur + 1) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(blur, 0, -1):
            layer = QPainterPath()
            layer.addRoundedRect(rect.adjusted(-i, -i + dy, i, i + dy), radius + i, radius + i)
            painter.fillPath(layer, QColor(0, 0, 0, round(255 * alpha * (blur - i + 1) / total)))

        # 面
        face = theme.rgba(theme.CARBON, 0.92) if p.dark else theme.rgba(theme.BONE, 0.94)
        painter.fillPath(path, face)

        # 縁（文字色の 9%）と、上端のハイライト（白 14% → 0）
        inner = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        edge = QColor(p.text)
        edge.setAlphaF(0.09)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(edge, 1.0))
        painter.drawRoundedRect(inner, inner.height() / 2, inner.height() / 2)
        rim = QLinearGradient(inner.topLeft(), inner.bottomLeft())
        rim.setColorAt(0.0, QColor(255, 255, 255, 36))
        rim.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(QPen(rim, 1.0))
        painter.drawRoundedRect(inner, inner.height() / 2, inner.height() / 2)

    def _content_left(self, pill_rect: QRectF, mode: str) -> float:
        """中身の左端の x 座標（中身をピルの中央に置く）。"""
        return pill_rect.center().x() - self._content_width(mode) / 2

    def _paint_app_icon(self, painter: QPainter, x: float, cy: float) -> float:
        """アプリアイコンを角丸で描き、次の要素の x 座標を返す（無ければ何もしない）。"""
        if self._app_icon is None:
            return x
        rect = QRectF(x, cy - self.ICON_SIZE / 2, self.ICON_SIZE, self.ICON_SIZE)
        clip = QPainterPath()
        clip.addRoundedRect(rect, self.ICON_RADIUS, self.ICON_RADIUS)
        painter.save()
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(rect, self._app_icon, QRectF(self._app_icon.rect()))
        painter.restore()
        return x + self.ICON_SIZE + self.CONTENT_GAP

    def _paint_recording(
        self, painter: QPainter, pill_rect: QRectF, p: theme.Palette, mode: str
    ) -> None:
        """録音中: アイコン・灯りの点・（波形 or 途中結果）・自動 Enter と翻訳の印。"""
        signal = QColor(theme.SIGNAL)
        cy = pill_rect.center().y()
        x = self._content_left(pill_rect, mode)
        x = self._paint_app_icon(painter, x, cy)

        # 状態の点。にじみを足して「点灯」に見せる
        dot_center = QPointF(x + self.DOT_SIZE / 2, cy)
        glow = QRadialGradient(dot_center, self.DOT_SIZE / 2 + 3)
        glow.setColorAt(0.0, theme.rgba(theme.SIGNAL, 0.6))
        glow.setColorAt(1.0, theme.rgba(theme.SIGNAL, 0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(dot_center, self.DOT_SIZE / 2 + 3, self.DOT_SIZE / 2 + 3)
        painter.setBrush(signal)
        painter.drawEllipse(dot_center, self.DOT_SIZE / 2, self.DOT_SIZE / 2)
        x += self.DOT_SIZE

        # 波形（途中結果が出ている間は出さない。二重表示を避ける）
        if not self._caption:
            x += self.CONTENT_GAP
            if self._hands_free:
                painter.setBrush(signal)
            else:
                bar_color = QColor(p.text)
                bar_color.setAlphaF(0.75)
                painter.setBrush(bar_color)
            for i, level in enumerate(self._bar_shown):
                h = self.BAR_MIN_H + (self.BAR_MAX_H - self.BAR_MIN_H) * level
                bx = x + i * (self.BAR_WIDTH + self.BAR_GAP)
                painter.drawRoundedRect(QRectF(bx, cy - h / 2, self.BAR_WIDTH, h), 1.5, 1.5)
            x += self._bars_width()

        # 自動 Enter の印（リターンの矢印）
        if mode == "recording_auto_enter":
            x += self.CONTENT_GAP
            self._paint_return_glyph(painter, x, cy, signal)
            x += self.RETURN_GLYPH_W

        # 翻訳される回の印
        if self._translate_badge:
            x += self.CONTENT_GAP
            painter.setFont(self._font(10, QFont.Weight.Bold))
            painter.setPen(signal)
            width = self._badge_width()
            painter.drawText(
                QRectF(x, pill_rect.top(), width + 1, pill_rect.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._translate_badge,
            )
            x += width

        # 途中結果（頭を省略して、いつも最新の語尾が見えるようにする）
        if self._caption:
            x += self.CONTENT_GAP
            font = self._font(12)
            painter.setFont(font)
            painter.setPen(p.text)
            elided = QFontMetricsF(font).elidedText(
                self._caption, Qt.TextElideMode.ElideLeft, self.LIVE_TEXT_WIDTH
            )
            painter.drawText(
                QRectF(x, pill_rect.top(), self.LIVE_TEXT_WIDTH, pill_rect.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )

    def _paint_return_glyph(self, painter: QPainter, x: float, cy: float, color: QColor) -> None:
        """リターンキーの矢印（右上から下りて左へ向かう）を線で描く。

        記号の字形はフォントで形も太さも変わるので、どの PC でも同じに見えるよう線で描く。
        """
        w = float(self.RETURN_GLYPH_W)
        path = QPainterPath()
        path.moveTo(x + w - 1.0, cy - 4.0)
        path.lineTo(x + w - 1.0, cy + 1.5)
        path.lineTo(x + 1.5, cy + 1.5)
        path.moveTo(x + 4.5, cy - 1.5)
        path.lineTo(x + 1.5, cy + 1.5)
        path.lineTo(x + 4.5, cy + 4.5)
        pen = QPen(color, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawPath(path)

    def _paint_transcribing(self, painter: QPainter, pill_rect: QRectF, p: theme.Palette) -> None:
        """変換中: アイコンと「変換中…」を静止で描く。"""
        cy = pill_rect.center().y()
        x = self._content_left(pill_rect, "transcribing")
        x = self._paint_app_icon(painter, x, cy)
        painter.setFont(self._font(11))
        painter.setPen(p.text_secondary)
        painter.drawText(
            QRectF(x, pill_rect.top(), pill_rect.right() - x, pill_rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "変換中…",
        )

    def _paint_notice(self, painter: QPainter, pill_rect: QRectF, p: theme.Palette) -> None:
        """一時通知をピル中央に描く（長ければ末尾を省略）。"""
        font = self._font(12)
        painter.setFont(font)
        painter.setPen(p.text_secondary)
        avail = self.WIDTH - 2 * self.PADDING_X - 2
        elided = QFontMetricsF(font).elidedText(
            self._notice_text, Qt.TextElideMode.ElideRight, avail
        )
        painter.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), elided)
