"""
翻訳して入力モジュール

文字起こし結果を、貼り付ける直前に Groq の LLM で 1 回だけ翻訳する。
「常に翻訳して入力」（全体の設定）と「1 回だけ翻訳するキー」（録音中に押した回だけ）の
2 つのスイッチがあり、どちらかが立っている回だけ翻訳する。

判定・文言・リクエストの中身は Mac 版 `DictationTranslator.swift` と揃えてある。
Mac は Apple の翻訳も選べるが、Windows には OS の翻訳が無いので Groq だけを使う。

翻訳は失敗しうるので、失敗は TranslationError で呼び出し側へ知らせる
（呼び出し側が原文を貼り、「翻訳できなかった」と通知する。黙って原文にすると、
訳されたと思い込んだまま送信してしまうため）。
"""

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import engines, llm_catalog
from . import text_formatter
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 翻訳先の候補（コード, 表示名）。Mac 版の設定画面と同じ並び・同じ文言
TARGET_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("en", "英語"),
    ("zh-Hans", "中国語（簡体）"),
    ("ko", "韓国語"),
    ("es", "スペイン語"),
    ("ja", "日本語"),
)

# 翻訳先の既定
DEFAULT_TARGET: str = "en"
# 1 回だけ翻訳するキーの既定（Mac の右⇧と同じ位置のキー）
DEFAULT_TRANSLATE_ONCE_KEY: str = "<shift_r>"

# 翻訳に失敗して原文を貼ったときの通知（Mac 版と同じ文言）
FAILED_NOTICE: str = "翻訳できなかったため原文を入力しました"
# 設定画面に出す注記（Mac 版と同じ文言）
DOUBLE_LLM_NOTE: str = "整形と同時に使うと、整形と翻訳で 2 回 LLM を呼びます"

_LANGUAGE_NAMES: Dict[str, str] = dict(TARGET_LANGUAGES)

# 修飾キーの汎用名（左右を区別しない書き方）。app._GENERIC_MODIFIERS と同じ
_GENERIC_MODIFIERS = ("ctrl", "alt", "shift", "cmd")


class TranslationError(Exception):
    """
    翻訳に失敗したことを表す例外。

    Attributes:
        reason: ログに残す短い理由（本文やサーバーの応答は入れない）
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ----------------------------------------------------------------------
# 判定（副作用なし）
# ----------------------------------------------------------------------


def normalize_target(value: Any) -> str:
    """
    保存されていた翻訳先を、候補にあるコードへ読み替える（知らない値は既定へ）。

    Args:
        value: 設定ファイルに保存されていた値

    Returns:
        候補にある言語コード
    """
    text = str(value or "")
    return text if text in _LANGUAGE_NAMES else DEFAULT_TARGET


def language_name(target: str) -> str:
    """
    言語コードの表示名を返す。

    Args:
        target: 言語コード

    Returns:
        表示名（例: "英語"）
    """
    return _LANGUAGE_NAMES[normalize_target(target)]


def badge(target: str) -> str:
    """
    録音中の HUD に出す印を返す（例: "→EN"。Mac 版と同じ書き方）。

    Args:
        target: 言語コード

    Returns:
        "→" + 言語コードの先頭部分の大文字（zh-Hans は "→ZH"）
    """
    return "→" + normalize_target(target).split("-")[0].upper()


def should_translate(always_on: bool, translate_once: bool) -> bool:
    """
    この回を翻訳するかを返す。

    Args:
        always_on: 「常に翻訳して入力」が ON か
        translate_once: この録音中に 1 回翻訳キーで ON にしたか

    Returns:
        翻訳するなら True
    """
    return bool(always_on or translate_once)


def toggled_translate(always_on: bool, current: bool) -> bool:
    """
    1 回翻訳キーを押したあとの「この回だけ翻訳」の状態を返す。

    「常に翻訳して入力」が ON の間は、このキーで外さない（2 つは別のスイッチ）。

    Args:
        always_on: 「常に翻訳して入力」が ON か
        current: いまの「この回だけ翻訳」の状態

    Returns:
        押したあとの状態
    """
    return current if always_on else not current


def _tokens(hotkey: str) -> Set[str]:
    """ホットキー文字列（"<ctrl>+<alt>+<v>" 等）をキー名の集合にする（app._parse_hotkey と同じ）。"""
    parts = (hotkey or "").replace("<", "").replace(">", "").split("+")
    return {p.strip() for p in parts if p.strip()}


def _names(token: str) -> Set[str]:
    """キー名 1 つが指しうる実際のキー名の集合を返す（app._acceptable_names と同じ）。"""
    if token in _GENERIC_MODIFIERS:
        return {token, f"{token}_l", f"{token}_r"}
    if token.endswith("_l") and token[:-2] in _GENERIC_MODIFIERS:
        return {token, token[:-2]}
    return {token}


def _covered(inner: Set[str], outer: Set[str]) -> bool:
    """inner のキーが全部 outer に含まれるか（左右の書き分けの違いは同じキーとして扱う）。"""
    if not inner:
        return False
    return all(any(_names(a) & _names(b) for b in outer) for a in inner)


def translate_once_key_warnings(
    key: str,
    slot_hotkeys: Sequence[str],
    handsfree_key: str = "",
    repaste_key: str = "",
) -> List[str]:
    """
    1 回翻訳キーがほかのキーと重なって効かない・別の動きをする場合の警告文を返す。

    設定画面がキーの下に出すための文（Mac 版 translateOnceKeyWarnings と同じ文言）。
    1 回翻訳キーが録音キーを「含む」組み合わせ（録音キーを押したまま別のキーを足す使い方）は
    問題なく効くので警告しない。

    Args:
        key: 1 回翻訳キー（ホットキー文字列。空なら無効）
        slot_hotkeys: 録音キー（スロット 1 から順に）
        handsfree_key: ハンズフリー切替キー（空なら無効）
        repaste_key: 再貼り付けキー（空なら無効）

    Returns:
        警告文のリスト（問題が無ければ空）
    """
    once = _tokens(key)
    if not once:
        return []
    warnings: List[str] = []
    slots = [_tokens(s) for s in slot_hotkeys]
    handsfree = _tokens(handsfree_key)

    slot_hit = False
    for index, slot in enumerate(slots, start=1):
        # 1 回翻訳キーが録音キーの中に収まる＝押しても録音キーの一部としか見えない
        if _covered(once, slot):
            warnings.append(f"録音キー {index} と重なっています。この 1 回翻訳キーは効きません")
            slot_hit = True
    if not slot_hit and handsfree:
        # ハンズフリーは「切替キー + 録音キー」で始めて、同じ組み合わせで止める。
        # その中に収まるキーは、止める操作のたびに翻訳の切り替えにもなってしまう
        if _covered(once, handsfree) or any(_covered(once, slot | handsfree) for slot in slots):
            warnings.append(
                "ハンズフリーキーと重なっています。ハンズフリーが優先され、この 1 回翻訳キーは効きません"
            )
    repaste = _tokens(repaste_key)
    if repaste and _covered(repaste, once):
        warnings.append(
            "再貼り付けキーと重なっています。録音していないときに押すと再貼り付けが動きます"
        )
    return warnings


# ----------------------------------------------------------------------
# 翻訳の実行
# ----------------------------------------------------------------------


def system_prompt(target: str) -> str:
    """
    翻訳用のシステムプロンプトを返す（Mac 版と同じ文言）。

    Args:
        target: 翻訳先の言語コード

    Returns:
        システムプロンプト
    """
    language = language_name(target)
    return (
        f"あなたは音声入力の翻訳エンジンです。入力された文（何語かは決まっていません）を{language}へ"
        "自然に翻訳し、訳文だけを出力してください。説明・注釈・引用符・前置きは一切付けないでください。"
        f"入力が既に{language}ならそのまま出力してください。"
    )


def build_request_body(text: str, target: str, model: str) -> Dict[str, Any]:
    """
    Groq へ送るリクエスト本体を組み立てる。

    Args:
        text: 翻訳する文
        target: 翻訳先の言語コード
        model: Groq のモデル名

    Returns:
        chat/completions に送る JSON（辞書）
    """
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt(target)},
            {"role": "user", "content": text},
        ],
        # 上限を付けないとモデルが暴走したときに待ち時間と料金が膨らむ
        "max_completion_tokens": llm_catalog.max_completion_tokens(len(text)),
    }
    body.update(llm_catalog.extra_parameters(model))
    return body


def translate(
    text: str,
    target: str,
    model: str,
    on_usage: Optional[Callable[[dict], None]] = None,
) -> str:
    """
    テキストを Groq の LLM で翻訳する。

    Args:
        text: 翻訳する文
        target: 翻訳先の言語コード
        model: Groq のモデル名
        on_usage: 課金された応答（HTTP 200）の JSON を受け取るコールバック。
            料金の記録に使う（訳文が途中で切れて捨てる回もトークン代はかかるため、
            切れたかの判定より前に呼ぶ）

    Returns:
        訳文（前後の空白を除いたもの）

    Raises:
        TranslationError: キー未設定・通信失敗・HTTP エラー・応答が途中で切れた・空だったとき
    """
    api_key = engines.api_key("groq")
    if not api_key:
        raise TranslationError("Groq の API キーが未設定")
    try:
        # 整形と同じ接続を使い回す（録音中に温めた接続がそのまま効く）
        resp = text_formatter._get_client().post(
            text_formatter._API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=build_request_body(text, target, model),
            timeout=text_formatter._TIMEOUT_SEC,
        )
    except Exception as e:
        raise TranslationError(type(e).__name__) from e
    if resp.status_code != 200:
        # 応答の本文は通知にも理由にも入れない（読めない JSON が画面に出るため）。ログにだけ残す
        logger.warning(f"翻訳に失敗（HTTP {resp.status_code}）: {resp.text[:200]}")
        raise TranslationError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
        if on_usage is not None:
            on_usage(data)
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason")
        translated = (choice["message"]["content"] or "").strip()
    except Exception as e:
        raise TranslationError(f"応答の形が不正 ({type(e).__name__})") from e
    if finish_reason == "length":
        # 後ろが欠けた訳文を貼るより原文の方がよい
        raise TranslationError("応答が途中で切れた")
    if not translated:
        raise TranslationError("応答が空")
    return translated
