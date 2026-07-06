"""
録音中 HUD モジュール（画面下部中央の小型ピル）

方針は Mac 版 Hud.swift と同じく「小型・情報最小限・状態は色とサイズで示す」:
- 待機中: 極小の待機ピル（hud_always_visible が ON のときだけ・中身なし）
- 録音中: 音声レベル連動の波形バー（ストリーミング字幕があればそれを優先表示、
  自動 Enter 時は ⏎ バッジ、ハンズフリー時はドット/波形をティール着色）
- 変換中: 「変換中…」テキストをゆっくり明滅（回転スピナーは廃止）
- 通知: エラー・無音検出などを 2 秒だけ表示
ピルは Mac 版カプセルと同様に内容の幅に合わせて縮み、待機 < 変換中 < 録音 の
三段階サイズを OutCubic 補間で滑らかにモーフさせる（Mac の spring 近似）。

最重要の制約: HUD は**絶対にキーフォーカスを奪ってはならない**。
フォーカスを奪うと貼り付け先ウィンドウが変わり、文字起こし結果が別の場所に
入力されてしまう。そのため非アクティブ表示・入力透過・タスクバー非登録の
フレームレスウィンドウとして実装する。
"""

from collections import deque
from typing import Deque

from PySide6.QtCore import (
    Qt,
    QTimer,
    QRectF,
    QVariantAnimation,
    QEasingCurve,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QGuiApplication,
)
from PySide6.QtWidgets import QWidget


class Hud(QWidget):
    """
    録音状態を示すフローティング HUD（フレームレス・入力透過・非アクティブ）。

    すべての公開メソッドはメインスレッドから呼ばれる前提（アプリは
    Qt シグナルのキュー接続でワーカースレッドからメインへホップさせる）。

    Attributes:
        enabled: HUD を表示するか（設定で無効化可能。False なら常に非表示）
        always_visible: 待機中も小型ピルを常時表示するか（config.hud_always_visible）
    """

    # ウィンドウ寸法・バー本数は Mac 版 HudView と一致させる
    WIDTH = 460
    HEIGHT = 56
    # 三段階サイズ（待機=最小 < 変換中=小 < 録音=大）。Mac 版に合わせて全体を一回り小さくした。
    PILL_HEIGHT = 36          # 録音・通知（大）のピル高さ
    TRANSCRIBING_HEIGHT = 26  # 変換中（小）のピル高さ
    IDLE_PILL_WIDTH = 64      # 待機ピル（最小・横長）の幅
    IDLE_PILL_HEIGHT = 14     # 待機ピル（最小・横長）の高さ
    PADDING_X = 13            # ピル内の左右パディング（録音・通知。Mac 13 に一致）
    TRANSCRIBING_PADDING_X = 12  # 変換中は録音より詰める（三段階で一回り小さく）
    CONTENT_GAP = 10          # ドット・バー・バッジ間の間隔
    BAR_COUNT = 24
    BAR_WIDTH = 3
    BAR_GAP = 2.5
    BAR_MIN_H = 3.0           # 波形バー最小高さ
    BAR_MAX_H = 18.0          # 波形バー最大高さ（22→18 に縮小）
    CAPTION_MAX_WIDTH = 360   # ライブ字幕の最大幅（超えたら頭を省略）
    NOTICE_MSEC = 2000        # 通知の表示時間（ミリ秒）
    FONT_POINT_SIZE = 11      # HUD テキストのポイントサイズ（12→11 に縮小）
    MORPH_MSEC = 200          # サイズモーフの所要時間（Mac の spring 近似・180〜220ms）
    # ハンズフリー録音のアクセント色（Mac Hud.swift の handsFreeAccent = rgb(0,0.78,0.72) の hex）
    HANDS_FREE_COLOR = "#00C7B8"

    def __init__(self, enabled: bool = True) -> None:
        """HUD を初期化する（生成時は非表示）。"""
        super().__init__()
        self.enabled = enabled
        self.always_visible = False  # 待機ピル常時表示（app が config から設定する）

        self._mode = "hidden"            # hidden/idle/recording/recording_auto_enter/transcribing/notice
        self._caption = ""               # ストリーミングのライブ字幕
        self._notice_text = ""           # 一時通知の本文
        self._hands_free = False         # 現在の録音がハンズフリー（toggle 実効）か
        self._levels: Deque[float] = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)

        # 表示中のピルサイズ（描画に使う唯一の真実）。待機ピルサイズを初期値にしておくと、
        # 非表示 → 録音の初回表示で極小ピルから育つ見た目になる。
        self._disp_w = float(self.IDLE_PILL_WIDTH)
        self._disp_h = float(self.IDLE_PILL_HEIGHT)

        self._setup_window()

        # 通知の自動消去タイマー（単発）
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._on_notice_timeout)

        # サイズモーフ用アニメ（0.0→1.0 の進捗を OutCubic で補間し、_disp_w/h を lerp する）。
        # Mac の spring による三段階モーフの近似。表示中のモード遷移でだけ走らせる。
        self._start_w = self._disp_w
        self._start_h = self._disp_h
        self._end_w = self._disp_w
        self._end_h = self._disp_h
        self._size_anim = QVariantAnimation(self)
        self._size_anim.setDuration(self.MORPH_MSEC)
        self._size_anim.setStartValue(0.0)
        self._size_anim.setEndValue(1.0)
        self._size_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._size_anim.valueChanged.connect(self._on_size_tick)

        # 変換中の明滅（「変換中…」の不透明度を 1.0⇄0.35 で 0.8 秒ずつ往復）。
        # Mac 版がスピナーを廃して文字明滅にしたのに合わせ、回転スピナーを置き換える。
        self._transcribe_opacity = 1.0
        self._blink = QVariantAnimation(self)
        self._blink.setDuration(1600)  # 0.8s ずつの往復
        self._blink.setStartValue(1.0)
        self._blink.setKeyValueAt(0.5, 0.35)
        self._blink.setEndValue(1.0)
        self._blink.setLoopCount(-1)
        self._blink.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._blink.valueChanged.connect(self._on_blink_tick)

        # タスクバー自動非表示の切替・解像度変更・プライマリ画面変更に追従して再配置する。
        self._primary_screen = None
        self._connect_screen_signals()

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

        Dock（タスクバー相当）の自動非表示 ON/OFF や解像度変更で availableGeometry が
        変わると下部中央の位置がずれる。表示中なら再配置して縦位置を追従させる（Mac 版の
        didChangeScreenParameters 追従と対）。プライマリ画面が入れ替わったら接続を張り替える。

        なお Windows ではフルスクリーン Space を確実・軽量に判定する公開 API が乏しいため、
        「フルスクリーン時は待機ピルを隠す」挙動（Mac のみ実装）は今回見送る。
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
                # 待機中も極小の待機ピルを残す（録音停止後は録音サイズから待機サイズへ縮む）
                self._blink.stop()
                self._caption = ""
                self._mode = "idle"
                self._show_pill()
            else:
                self._hide()
            return

        if state in ("recording", "recording_auto_enter"):
            self._notice_timer.stop()
            self._blink.stop()
            self._caption = ""  # 新しい録音のたびに字幕をリセット
            self._levels = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
            self._mode = state
            self._show_pill()
        elif state == "transcribing":
            self._notice_timer.stop()
            self._mode = "transcribing"
            self._transcribe_opacity = 1.0
            self._blink.stop()
            self._blink.start()
            self._show_pill()

    def set_hands_free(self, hands_free: bool) -> None:
        """現在の録音がハンズフリー（toggle 実効）かを反映する（ドット/波形の着色に使う）。

        status_changed より先に届く前提。ラベルは出さず色だけで通常録音と区別する（Mac 版と対）。
        """
        self._hands_free = bool(hands_free)
        if self._mode in ("recording", "recording_auto_enter"):
            self.update()

    def push_level(self, level: float) -> None:
        """音声レベル（0.0-1.0）を波形バーに反映する（録音中のみ）。"""
        if self._mode not in ("recording", "recording_auto_enter"):
            return
        self._levels.append(max(0.0, min(1.0, float(level))))
        self.update()

    def set_caption(self, text: str) -> None:
        """ストリーミングのライブ字幕を更新する（録音中のみ反映）。"""
        if self._mode not in ("recording", "recording_auto_enter"):
            return
        self._caption = text or ""
        # 字幕は幅がこまめに変わるので、モーフ用アニメを挟まず即座に幅を合わせる（追従が遅れない）
        self._retarget(animate=False)

    def show_notice(self, text: str) -> None:
        """一時通知を NOTICE_MSEC だけ表示する。"""
        if not self.enabled:
            return
        self._blink.stop()
        self._notice_text = text or ""
        self._mode = "notice"
        self._show_pill()
        self._notice_timer.start(self.NOTICE_MSEC)

    # ------------------------------------------------------------------
    # 内部
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

    def _on_size_tick(self, value) -> None:
        """サイズモーフの進捗（0.0-1.0）に応じて表示ピルサイズを補間する。"""
        t = float(value)
        self._disp_w = self._start_w + (self._end_w - self._start_w) * t
        self._disp_h = self._start_h + (self._end_h - self._start_h) * t
        self.update()

    def _on_blink_tick(self, value) -> None:
        """変換中の「変換中…」の明滅を反映する。"""
        self._transcribe_opacity = float(value)
        self.update()

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

    def _target_size(self) -> tuple:
        """現在のモードでのピル目標サイズ（幅, 高さ）を返す。三段階サイズの根拠。"""
        metrics = QFontMetrics(self._hud_font())
        if self._mode == "idle":
            return (float(self.IDLE_PILL_WIDTH), float(self.IDLE_PILL_HEIGHT))
        if self._mode in ("recording", "recording_auto_enter"):
            return (self._content_width(metrics) + 2 * self.PADDING_X, float(self.PILL_HEIGHT))
        if self._mode == "transcribing":
            w = metrics.horizontalAdvance("変換中…") + 2 * self.TRANSCRIBING_PADDING_X
            return (float(w), float(self.TRANSCRIBING_HEIGHT))
        if self._mode == "notice":
            return (self._content_width(metrics) + 2 * self.PADDING_X, float(self.PILL_HEIGHT))
        return (self._disp_w, self._disp_h)

    def _retarget(self, animate: bool) -> None:
        """現在モードの目標サイズへ、必要なら OutCubic 補間でモーフさせる。

        Args:
            animate: True なら現在表示サイズから目標へアニメ、False なら即座に合わせる
        """
        w, h = self._target_size()
        if animate and self.isVisible():
            self._size_anim.stop()
            self._start_w, self._start_h = self._disp_w, self._disp_h
            self._end_w, self._end_h = w, h
            self._size_anim.start()
        else:
            # 初回表示（非表示→表示）や字幕追従は即座に合わせる（イベントループ非依存で静止画も正しく描ける）
            self._size_anim.stop()
            self._disp_w, self._disp_h = w, h
            self._end_w, self._end_h = w, h
            self.update()

    def _show_pill(self) -> None:
        """HUD を画面下部中央へ配置して表示し、現在モードのサイズへモーフする（アクティブ化しない）。"""
        if not self.enabled:
            return
        was_visible = self.isVisible()
        self._reposition()
        if not was_visible:
            self.show()
        self.raise_()
        # 既に表示中のモード切替だけモーフさせる。初回表示は目標サイズへ即座に置く（ポップイン）
        self._retarget(animate=was_visible)

    def _hide(self) -> None:
        """HUD を隠す。"""
        self._mode = "hidden"
        self._size_anim.stop()
        self._blink.stop()
        # 次の初回表示が待機ピルサイズから育つよう、表示サイズを最小へ戻しておく
        self._disp_w = float(self.IDLE_PILL_WIDTH)
        self._disp_h = float(self.IDLE_PILL_HEIGHT)
        self.hide()

    def _reposition(self) -> None:
        """プライマリスクリーンの下部中央に配置する。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.center().x() - self.WIDTH // 2
        y = geo.bottom() - self.HEIGHT - 24  # 画面下から少し浮かせる（Mac 版と同じ）
        self.move(x, y)

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------

    @staticmethod
    def _hud_font() -> QFont:
        """HUD のテキスト描画に使う共通フォントを返す。"""
        font = QFont()
        font.setPointSize(Hud.FONT_POINT_SIZE)
        font.setWeight(QFont.Weight.Medium)
        return font

    def _bars_width(self) -> float:
        """波形バー群の合計幅を返す。"""
        return self.BAR_COUNT * self.BAR_WIDTH + (self.BAR_COUNT - 1) * self.BAR_GAP

    def _content_width(self, metrics: QFontMetrics) -> float:
        """現在のモードで描く内容の幅を計算する（ピルを内容に合わせて縮めるため）。"""
        if self._mode in ("recording", "recording_auto_enter"):
            if self._caption:
                middle = min(
                    float(metrics.horizontalAdvance(self._caption)),
                    float(self.CAPTION_MAX_WIDTH),
                )
            else:
                middle = self._bars_width()
            width = 7 + self.CONTENT_GAP + middle  # 状態ドット(7) + 間隔 + 中身
            if self._mode == "recording_auto_enter":
                width += self.CONTENT_GAP + metrics.horizontalAdvance("⏎")
            return width
        if self._mode == "notice":
            return min(
                float(metrics.horizontalAdvance(self._notice_text)),
                float(self.WIDTH - 2 * self.PADDING_X - 2),
            )
        return 0.0

    def paintEvent(self, event) -> None:
        """ピル背景と状態別コンテンツを描画する。"""
        if self._mode == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._hud_font())
        metrics = painter.fontMetrics()

        # ピル背景。サイズはモーフ中の表示サイズ（_disp_w/_disp_h）を使う
        pill_w = min(self.width() - 2.0, self._disp_w)
        pill_h = self._disp_h
        pill_rect = QRectF(
            (self.width() - pill_w) / 2,
            (self.height() - pill_h) / 2,
            pill_w,
            pill_h,
        )
        radius = pill_rect.height() / 2

        # 影（QGraphicsDropShadowEffect は透過ウィンドウで効かないため自前で描く。
        # 数枚の半透明レイヤーを下方向にずらして柔らかい落ち影にする）
        for i in range(1, 4):
            shadow_rect = pill_rect.adjusted(-i, -i + 2, i, i + 2)
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(shadow_rect, radius + i, radius + i)
            painter.fillPath(shadow_path, QColor(0, 0, 0, 26 - i * 7))

        # ピル本体: 縦グラデーションの半透明ダーク（Mac 版 .ultraThinMaterial 風）
        path = QPainterPath()
        path.addRoundedRect(pill_rect, radius, radius)
        gradient = QLinearGradient(pill_rect.topLeft(), pill_rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(58, 58, 60, 228))
        gradient.setColorAt(1.0, QColor(24, 24, 26, 240))
        painter.fillPath(path, gradient)
        # 白 10% のボーダー（暗い背景上でも輪郭が出る、Mac 版と同じ値）
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawPath(path)

        # 中身はピル形にクリップして描く。モーフ中にピルより広い中身がはみ出さず、
        # ピルが育つのに合わせて現れる（Mac のカプセルクリップと同じ狙い）。
        painter.save()
        painter.setClipPath(path)
        if self._mode in ("recording", "recording_auto_enter"):
            self._paint_recording(painter, metrics, pill_rect)
        elif self._mode == "transcribing":
            self._paint_transcribing(painter, pill_rect)
        elif self._mode == "notice":
            self._paint_notice(painter, metrics, pill_rect)
        # "idle"（待機ピル）は中身なしの極小ピル（モーフの起点）
        painter.restore()

        painter.end()

    def _paint_recording(self, painter: QPainter, metrics: QFontMetrics, pill_rect: QRectF) -> None:
        """録音中: 状態ドット + （字幕 or 波形バー） + 自動 Enter 時は ⏎ バッジ。

        ハンズフリー中はラベルを出さず、ドットと波形をティール（HANDS_FREE_COLOR）に着色して
        通常録音と区別する（Mac 版 handsFree と同じ表現）。
        """
        auto_enter = self._mode == "recording_auto_enter"
        # 状態ドット色: ハンズフリー=ティール / 自動送信=パープル / 通常=レッド
        if self._hands_free:
            dot_color = QColor(self.HANDS_FREE_COLOR)
        elif auto_enter:
            dot_color = QColor("#BF5AF2")
        else:
            dot_color = QColor("#FF453A")

        x = pill_rect.left() + self.PADDING_X
        cy = self.height() / 2
        # 左の状態ドット（8→7 に縮小）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        painter.drawEllipse(QRectF(x, cy - 3.5, 7, 7))
        x += 7 + self.CONTENT_GAP

        if self._caption:
            # ライブ字幕（末尾を残して頭を省略 = 常に最新の語尾が見える）
            avail = min(
                metrics.horizontalAdvance(self._caption), self.CAPTION_MAX_WIDTH
            )
            elided = metrics.elidedText(self._caption, Qt.TextElideMode.ElideLeft, avail)
            painter.setPen(QColor(245, 245, 247))
            painter.drawText(
                QRectF(x, 0, avail, self.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )
            x += avail
        else:
            self._paint_bars(painter, x)
            x += self._bars_width()

        # 自動 Enter のバッジ（Mac 版の return アイコン相当）
        if auto_enter:
            x += self.CONTENT_GAP
            painter.setPen(QColor("#BF5AF2"))
            painter.drawText(
                QRectF(x, 0, metrics.horizontalAdvance("⏎") + 2, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                "⏎",
            )

    def _paint_bars(self, painter: QPainter, start_x: float) -> None:
        """音声レベル連動の波形バーを描画する（ハンズフリー中はアクセント色）。"""
        painter.setPen(Qt.PenStyle.NoPen)
        if self._hands_free:
            painter.setBrush(QColor(self.HANDS_FREE_COLOR))
        else:
            painter.setBrush(QColor(200, 200, 205, 200))
        cy = self.height() / 2
        for i, level in enumerate(self._levels):
            h = self.BAR_MIN_H + (self.BAR_MAX_H - self.BAR_MIN_H) * level
            bx = start_x + i * (self.BAR_WIDTH + self.BAR_GAP)
            painter.drawRoundedRect(QRectF(bx, cy - h / 2, self.BAR_WIDTH, h), 1.5, 1.5)

    def _paint_transcribing(self, painter: QPainter, pill_rect: QRectF) -> None:
        """変換中: 「変換中…」をピル中央にゆっくり明滅させる（回転スピナーは廃止）。"""
        alpha = int(255 * max(0.0, min(1.0, self._transcribe_opacity)))
        painter.setPen(QColor(200, 200, 205, alpha))
        painter.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), "変換中…")

    def _paint_notice(self, painter: QPainter, metrics: QFontMetrics, pill_rect: QRectF) -> None:
        """一時通知をピル中央に描画する。"""
        avail = int(pill_rect.width() - 2 * self.PADDING_X)
        elided = metrics.elidedText(self._notice_text, Qt.TextElideMode.ElideRight, avail)
        painter.setPen(QColor(220, 220, 225))
        painter.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), elided)
