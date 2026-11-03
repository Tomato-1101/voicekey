"""
Soniox リアルタイム応答 → 字幕の行 への組み立てと、接続の方針（純ロジック）

Mac 版 SonioxCaptionAssembler.swift / SonioxCaptionStream.swift（SonioxReconnectPolicy /
SonioxIdleTracker / SonioxCaptionLinkState）の移植。通信と切り離して単体テストする。

Soniox は 1 つの WebSocket で「認識（原文）」と「翻訳（訳）」のトークンを混ぜて返す。
- is_final=false のトークンは毎回の応答で丸ごと置き換わる（＝ライブ行）
- is_final=true のトークンは 1 度だけ届き、以後は変わらない（＝確定分として積む）
- 訳トークン（translation_status="translation"）は元のトークンより後に、塊ごとに届く
- 発話の切れ目は {"text":"<end>","is_final":true} で届く（エンドポイント検出）
"""

import json
import math
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# 文末とみなす記号（原文・訳の両方）
SENTENCE_ENDINGS = frozenset(".!?。！？")
END_TOKEN = "<end>"
FIN_TOKEN = "<fin>"

# 文末が来ないまま喋り続けたときに、確定分だけで行を切る長さ（原文の文字数・Mac と同じ 60）
MAX_CONFIRMED_SOURCE_CHARACTERS = 60

LIVE = "live"
CONFIRMED = "confirmed"


@dataclass
class Token:
    """応答のトークン 1 つ（字幕に要る項目だけ）。"""

    text: str
    is_final: bool = False
    is_translation: bool = False
    # 後で訳が届く原文トークンか（translation_status="original"）。"none" は訳が来ない
    awaits_translation: bool = False


@dataclass
class Response:
    """解析した応答 1 件。"""

    tokens: List[Token] = field(default_factory=list)
    finished: bool = False
    error_code: Optional[int] = None
    error_message: Optional[str] = None


def parse(message: Any) -> Optional[Response]:
    """応答の JSON を読む（読めなければ None）。"""
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", errors="replace")
    try:
        root = json.loads(message) if isinstance(message, str) else message
    except Exception:
        return None
    if not isinstance(root, dict):
        return None
    tokens: List[Token] = []
    for raw in root.get("tokens") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            continue
        status = raw.get("translation_status")
        tokens.append(Token(
            text=raw["text"],
            is_final=raw.get("is_final") is True,
            is_translation=status == "translation",
            awaits_translation=status == "original",
        ))
    code = root.get("error_code")
    error_code: Optional[int] = None
    if code is not None:
        try:
            error_code = int(code)
        except (TypeError, ValueError):
            error_code = -1
    message_text = root.get("error_message")
    return Response(
        tokens=tokens,
        finished=root.get("finished") is True,
        error_code=error_code,
        error_message=message_text if isinstance(message_text, str) else None,
    )


def split_sentences(text: str) -> Tuple[List[str], str]:
    """文末で区切る（区切った文の並びと、文末まで来ていない残り）。

    英語の "." は直後が空白か末尾のときだけ文末とみなす（"3.5" や "U.S" の途中で切らない）。
    全角の「。！？」は直後に空白が来ないので常に文末とみなす。
    """
    sentences: List[str] = []
    current = ""
    for i, char in enumerate(text):
        current += char
        nxt = text[i + 1] if i + 1 < len(text) else None
        if char in SENTENCE_ENDINGS and (nxt is None or nxt.isspace() or not char.isascii()):
            sentences.append(current)
            current = ""
    return sentences, current


class SonioxCaptionAssembler:
    """Soniox の応答 1 件を字幕の行にまとめる。

    イベントは (種類, 原文, 訳) のタプル。種類は LIVE（ライブ行）か CONFIRMED（確定した 1 行）。
    """

    def __init__(self, translates: bool) -> None:
        self.translates = translates
        self._final_source = ""
        self._final_translation = ""
        self._pending_source = ""
        self._pending_translation = ""
        self._end_reached = False
        self._final_source_awaits_translation = False

    @property
    def is_waiting_for_translation(self) -> bool:
        """切れ目は来たが訳を待っている状態か（呼び出し側が時間切れで flush_confirmed するための目印）。"""
        return self.translates and self._end_reached and bool(self._final_source.strip())

    def apply(self, response: Response) -> List[Tuple[str, str, str]]:
        """応答 1 件を取り込み、画面へ出すイベント（確定行 → ライブ行 の順）を返す。"""
        self._pending_source = ""
        self._pending_translation = ""
        for token in response.tokens:
            # <fin> は「送った音声はすべて確定した」印なので、切れ目と同じく訳が揃いしだい確定させる
            if token.text in (END_TOKEN, FIN_TOKEN):
                if token.is_final:
                    self._end_reached = True
                continue
            if token.is_translation and token.is_final:
                self._final_translation += token.text
            elif token.is_translation:
                self._pending_translation += token.text
            elif token.is_final:
                self._final_source += token.text
                if token.awaits_translation:
                    self._final_source_awaits_translation = True
            else:
                self._pending_source += token.text
        # 訳がまとめて届いたときは 1 回で複数行が確定する（文ごとに原文と組にする）
        events = self._take_confirmed_lines()
        events.append((LIVE, (self._final_source + self._pending_source).strip(),
                       (self._final_translation + self._pending_translation).strip()))
        return events

    def flush_confirmed(self) -> Optional[Tuple[str, str, str]]:
        """確定済みの分だけを 1 行に確定する（訳待ちの時間切れ用）。

        未確定（is_final=false）の分は残す。接続は続いているので、後で確定トークンとして改めて届く
        （ここで行にすると、その確定トークンが届いたときに同じ文がもう一度記録される）。
        """
        source = self._final_source.strip()
        translation = self._final_translation.strip()
        self._final_source = ""
        self._final_translation = ""
        self._end_reached = False
        self._final_source_awaits_translation = False
        if not source and not translation:
            return None
        return (CONFIRMED, source, translation)

    def flush(self) -> Optional[Tuple[str, str, str]]:
        """残っている分を全部 1 行に確定する（停止・切断時。未確定分も含めて落とさない）。"""
        source = (self._final_source + self._pending_source).strip()
        translation = (self._final_translation + self._pending_translation).strip()
        self._final_source = self._pending_source = ""
        self._final_translation = self._pending_translation = ""
        self._end_reached = False
        self._final_source_awaits_translation = False
        if not source and not translation:
            return None
        return (CONFIRMED, source, translation)

    # --- 内部 ---

    def _take_confirmed_lines(self) -> List[Tuple[str, str, str]]:
        source = self._final_source.strip()
        if not source:
            # 原文が無いのに切れ目だけ来た（無音の区切り）ときは何もしない
            if not self._final_translation.strip():
                self._end_reached = False
            return []
        if self.translates:
            return self._take_translated_lines()
        # 文字起こし: 切れ目・文末・長さのどれかで確定する
        ends_sentence = source[-1] in SENTENCE_ENDINGS
        if not (self._end_reached or ends_sentence or len(source) >= MAX_CONFIRMED_SOURCE_CHARACTERS):
            return []
        self._final_source = ""
        self._end_reached = False
        return [(CONFIRMED, source, "")]

    def _take_translated_lines(self) -> List[Tuple[str, str, str]]:
        """翻訳モード: 訳が文末まで確定した分だけ、原文と**文ごとに**組にして確定する。"""
        translation = self._final_translation.strip()
        # 訳されない言語（日本語など）だけの発話は、訳を待たずに原文だけで確定する
        if not self._final_source_awaits_translation and not translation and not self._pending_translation:
            source = self._final_source.strip()
            ends_sentence = bool(source) and source[-1] in SENTENCE_ENDINGS
            if not (self._end_reached or ends_sentence):
                return []
            self._final_source = ""
            self._end_reached = False
            return [(CONFIRMED, source, "")]

        sources, source_tail = split_sentences(self._final_source)
        translations, translation_tail = split_sentences(self._final_translation)
        count = min(len(sources), len(translations))
        lines = [[sources[i], translations[i]] for i in range(count)]
        rest_sources = sources[count:]
        rest_translations = translations[count:]
        rest_source = "".join(rest_sources + [source_tail])
        rest_translation = "".join(rest_translations + [translation_tail])

        # 切れ目まで訳が揃った（残りの原文が無いか、残りの訳がある）: 組にならなかった残りを全部 1 行に
        if (self._end_reached and not self._pending_translation
                and (not rest_source.strip() or rest_translation.strip())):
            if not rest_source.strip() and lines:
                lines[-1][1] += rest_translation
            elif rest_source.strip() or rest_translation.strip():
                lines.append([rest_source, rest_translation])
            self._final_source = ""
            self._final_translation = ""
            self._end_reached = False
            self._final_source_awaits_translation = False
        else:
            # 原文の文が尽きたのに訳の文が余った（1 文が 2 文に訳された）。余りは直前の原文の訳として扱う
            if not rest_sources and not source_tail.strip() and rest_translations and lines:
                lines[-1][1] += "".join(rest_translations)
                rest_translations = []
            if not lines:
                return []
            self._final_source = rest_source
            self._final_translation = "".join(rest_translations + [translation_tail])
            # 持ち越した文は、まだ訳が届いていない前提で扱う
            self._final_source_awaits_translation = bool(self._final_source.strip())
        return [(CONFIRMED, s.strip(), t.strip()) for s, t in lines]


# ----------------------------------------------------------------------
# 接続の方針
# ----------------------------------------------------------------------

class ReconnectPolicy:
    """再接続の方針（上限つきの指数バックオフ・Mac の SonioxReconnectPolicy と同じ値）。"""

    MAX_ATTEMPTS = 5
    BASE_DELAY = 0.5
    MAX_DELAY = 8.0
    # これだけ安定して受信できたら、失敗の回数を数え直す（秒）
    HEALTHY_DURATION = 30.0

    def __init__(self) -> None:
        self.failures = 0

    def next_delay(self, is_fatal: bool) -> Optional[float]:
        """失敗を 1 回数え、次に張り直すまでの待ち時間を返す（もう諦めるなら None）。"""
        if is_fatal:
            return None
        self.failures += 1
        if self.failures > self.MAX_ATTEMPTS:
            return None
        return min(self.BASE_DELAY * math.pow(2, self.failures - 1), self.MAX_DELAY)

    def note_healthy(self) -> None:
        self.failures = 0

    @staticmethod
    def is_fatal(error_code: Optional[int]) -> bool:
        """張り直しても直らないエラーか（401 キー無効・402 残高不足・403 権限なし・400 不正）。"""
        return error_code in (400, 401, 402, 403)

    @staticmethod
    def give_up_reason(error_code: int, provider: str = "Soniox") -> str:
        """諦めたときにユーザーへ出す理由（何が起きたかが一目で分かる言葉）。"""
        if error_code == 402:
            return f"{provider} の残高不足"
        if error_code in (401, 403):
            return f"{provider} のキーが無効"
        return f"{provider} がエラーを返しました（{error_code}）"


class IdleTracker:
    """無音での切断と、有音での張り直しの判定（Mac の SonioxIdleTracker と同じ）。

    再接続の失敗回数とは独立させている。アイドル切断は失敗ではないので、
    何度切っても張り直しても諦めの上限に近づかない（＝この切断は失敗に数えない）。
    """

    # 無音がこれだけ続いたら切る（秒）。接続時間で課金されるので、無音のあいだの課金を止める
    IDLE_DISCONNECT_INTERVAL = 30.0
    # 有音とみなす RMS（Mac の PauseFinalizeTrigger.silenceRMS と同じ基準）
    VOICE_RMS = 0.004

    def __init__(self) -> None:
        self.last_voice_at = 0.0
        self.is_idle = False

    def note_connected(self, now: float) -> None:
        self.is_idle = False
        self.last_voice_at = max(self.last_voice_at, now)

    def note_frame(self, is_voiced: bool, now: float) -> bool:
        """フレームが届いた。無音で切っていて有音が来たので張り直すべきとき True。"""
        if not is_voiced:
            return False
        self.last_voice_at = now
        if not self.is_idle:
            return False
        self.is_idle = False
        return True

    def should_disconnect(self, now: float) -> bool:
        """接続中に、無音が続いたので切るべきか（True を返したら切った扱いになる）。"""
        if self.is_idle or now - self.last_voice_at < self.IDLE_DISCONNECT_INTERVAL:
            return False
        self.is_idle = True
        return True


LINK_CONNECTED = "connected"
LINK_IDLE = "idle"
LINK_RECONNECTING = "reconnecting"


def link_state_text(state: Optional[str], provider: str = "Soniox") -> str:
    """接続状態のメニュー表示（課金中か・無音で切っているか・張り直し中か）。"""
    if state == LINK_CONNECTED:
        return f"{provider} 接続中"
    if state == LINK_IDLE:
        return f"{provider} 待機中（無音のため切断）"
    if state == LINK_RECONNECTING:
        return f"{provider} 再接続中…"
    return ""
