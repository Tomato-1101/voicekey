"""
文字起こしエンジンの一覧（Mac 版 AppConfig.swift の Backend と同じ並び・同じモデル名）

設定画面の選択肢・既定モデル・表示名・整形の既定・使う API キーを 1 か所に集める。
Mac の仕様が正本で、Windows はこの表を写して持つ（並びとモデル名を Mac に合わせる）。

- 選べる並びは「日本語で使うときのおすすめ順」（Mac 2026-10-02 本人指示）:
  Soniox → ElevenLabs → OpenAI（gpt-transcribe）→ Microsoft MAI → OpenAI ライブ → Groq。
  Mac にある「ローカル（Apple）」は Windows に無いので並びから外す。
- Deepgram は Mac で 2026-10-01 に選択肢から外した。Windows も選択肢には出さないが、
  設定ファイルに deepgram が入っている人はそのまま動かす（本人の設定のエンジンを勝手に変えない）。
"""

from typing import Dict, List, Optional, Tuple

from .types import TranscriptionBackend

# 識別子（settings.yaml の hotkeyN.backend に保存する値）
SONIOX = TranscriptionBackend.SONIOX.value
ELEVENLABS = TranscriptionBackend.ELEVENLABS.value
OPENAI = TranscriptionBackend.OPENAI.value
AZURE_MAI = TranscriptionBackend.AZURE_MAI.value
OPENAI_LIVE = TranscriptionBackend.OPENAI_LIVE.value
GROQ = TranscriptionBackend.GROQ.value
DEEPGRAM = TranscriptionBackend.DEEPGRAM.value

# 設定画面で選べるエンジン（表示順＝おすすめ順。Mac の Backend.selectableCases から Apple を除いたもの）
SELECTABLE: Tuple[str, ...] = (SONIOX, ELEVENLABS, OPENAI, AZURE_MAI, OPENAI_LIVE, GROQ)

# 選択肢には出さないが、保存済みの設定ならそのまま動かすエンジン（旧構成の互換）
LEGACY: Tuple[str, ...] = (DEEPGRAM,)

# 動くエンジン全部（保存値として受け付ける集合）
SUPPORTED: Tuple[str, ...] = SELECTABLE + LEGACY

# 録音中から WebSocket で並行認識する「ライブ型」。離鍵時に確定を受け取る
LIVE_BACKENDS: Tuple[str, ...] = (SONIOX, OPENAI_LIVE, DEEPGRAM)

# 画面・エラー文に出すエンジン名（Mac の Backend.label。Windows は製品版の特徴名を使わず実名にする）
_LABELS: Dict[str, str] = {
    SONIOX: "Soniox",
    ELEVENLABS: "ElevenLabs",
    OPENAI: "OpenAI",
    AZURE_MAI: "Microsoft MAI",
    OPENAI_LIVE: "OpenAI ライブ",
    GROQ: "Groq",
    DEEPGRAM: "Deepgram",
}

# 既知のモデル（先頭が既定＝推奨）。Mac の Backend.knownModels と一致させる
_KNOWN_MODELS: Dict[str, Tuple[str, ...]] = {
    SONIOX: ("stt-rt-v5",),
    # scribe_v2 が既定（公開情報で日本語精度が上位）。scribe_v1 はハンズフリーの内部利用と比較用に残す
    ELEVENLABS: ("scribe_v2", "scribe_v1"),
    # 旧 gpt-4o 系を外して日本語精度の高い gpt-transcribe に一本化（2026-10-01）
    OPENAI: ("gpt-transcribe",),
    AZURE_MAI: ("MAI-Transcribe-2",),
    OPENAI_LIVE: ("gpt-live-transcribe",),
    GROQ: ("whisper-large-v3-turbo",),
    DEEPGRAM: ("nova-3",),
}

# 2026-10-01 に選択肢から外したモデル。保存値がこれなら一回限りの移行で既定モデルへ戻す
# （一覧外の自由入力は保持する＝ここに無い名前は触らない）。Mac の Backend.retiredModels と同じ
RETIRED_MODELS = frozenset({
    "nova-2", "gpt-realtime-whisper", "whisper-large-v3",
    "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "scribe_v1_experimental",
})

# ハンズフリー（トグル）録音で Groq の代わりに内部で使う ElevenLabs のモデル。
# 既定が scribe_v2 になっても、長時間録音で実績のある v1 に固定する（Mac と同じ）
HANDSFREE_ELEVENLABS_MODEL = "scribe_v1"

# エンジンごとに使う API キーの項目（utils.secrets.API_KEY_ITEMS のキー）
_KEY_ITEMS: Dict[str, str] = {
    SONIOX: "soniox",
    ELEVENLABS: "elevenlabs",
    # OpenAI ライブも同じ OpenAI のキーを使う（1 回入れれば REST もライブも動く）
    OPENAI: "openai",
    OPENAI_LIVE: "openai",
    AZURE_MAI: "azure_key",
    GROQ: "groq",
    DEEPGRAM: "deepgram",
}


def is_supported(backend: str) -> bool:
    """保存値として動かせるエンジンか（選択肢＋旧構成の互換）。"""
    return backend in SUPPORTED


def label(backend: str) -> str:
    """エンジンの表示名（未知の値はそのまま返す）。"""
    return _LABELS.get(backend, backend)


def known_models(backend: str) -> Tuple[str, ...]:
    """既知のモデル一覧（先頭が既定）。未知のエンジンは空。"""
    return _KNOWN_MODELS.get(backend, ())


def default_model(backend: str) -> str:
    """既定モデル（＝設定画面で「（推奨）」を付けるモデル）。未知のエンジンは空文字。"""
    models = known_models(backend)
    return models[0] if models else ""


def default_format_enabled(backend: str) -> bool:
    """エンジン別のテキスト整形の既定 ON/OFF（Mac の Backend.defaultFormatEnabled と一致）。

    ライブ型（Soniox / OpenAI ライブ / Deepgram）は速度全振りのため既定 OFF、
    録音後に送る型（Groq / ElevenLabs / OpenAI / Microsoft MAI）は既定 ON。
    """
    return str(backend).lower() not in LIVE_BACKENDS


def key_item(backend: str) -> Optional[str]:
    """エンジンが使う API キーの項目名（utils.secrets.API_KEY_ITEMS のキー）。"""
    return _KEY_ITEMS.get(backend)


def is_live(backend: str) -> bool:
    """録音中から並行認識するライブ型か。"""
    return backend in LIVE_BACKENDS


def resolve_model(backend: str, model: str) -> str:
    """保存されたモデル名を実際に使うモデル名にする（空なら既定。一覧外の自由入力は保持）。"""
    model = (model or "").strip()
    return model or default_model(backend)


# 提供元名（ポップアップの見出し・閉じた表示に使う。Mac の Backend.providerName と同じ）
_PROVIDERS: Dict[str, str] = {
    SONIOX: "Soniox",
    ELEVENLABS: "ElevenLabs",
    OPENAI: "OpenAI",
    OPENAI_LIVE: "OpenAI",
    AZURE_MAI: "Microsoft",
    GROQ: "Groq",
    DEEPGRAM: "Deepgram",
}


def provider_name(backend: str) -> str:
    """提供元名（OpenAI ライブも OpenAI）。"""
    return _PROVIDERS.get(backend, backend)


def choice_title(backend: str, model: str) -> str:
    """閉じたポップアップに出す名前（「Soniox · stt-rt-v5」のように実プロバイダー名 · モデル名）。"""
    return f"{provider_name(backend)} · {resolve_model(backend, model)}"


def model_label(backend: str, model: str) -> str:
    """見出しの下に並べるモデル名（Mac の EngineModelCatalog.modelLabel と同じ）。

    同じエンジンにモデルが複数あるときだけ既定に「（推奨）」。同じ OpenAI の見出しの下で
    録音後に送る方式と話しながら送る方式を見分けられるよう、ライブには「（ライブ）」を付ける。
    """
    label = model
    if len(known_models(backend)) > 1 and model == default_model(backend):
        label += "（推奨）"
    if backend == OPENAI_LIVE:
        label += "（ライブ）"
    return label


def picker_groups(
    current_backend: str = "", current_model: str = ""
) -> List[Tuple[Optional[str], List[Tuple[str, str, str]]]]:
    """エンジンとモデルを 1 つのポップアップで選ぶための並び（Mac の EngineModelCatalog.groups と同じ・純関数）。

    - 並びはおすすめ順（SELECTABLE）。同じ提供元（OpenAI の通常とライブ）は 1 つの見出しにまとめる
    - 選択肢が 2 つ以上ある提供元は見出しの下にモデルを並べる
    - 選択肢が 1 つの提供元は見出しを付けず「提供元 · モデル」の 1 項目にする
    - 保存中のモデルが一覧に無い（自由入力・旧モデル）ときも、選択中と分かるよう該当の提供元に足す
    - 選択肢から外した Deepgram も、保存中なら末尾に出して選択状態を保つ

    Returns:
        [(見出し or None, [(エンジン, モデル, 表示名), ...]), ...]
    """
    current_model = resolve_model(current_backend, current_model) if current_backend else ""
    order = list(SELECTABLE)
    if current_backend and current_backend not in order and is_supported(current_backend):
        order.append(current_backend)
    providers: List[str] = []
    entries: Dict[str, List[Tuple[str, str]]] = {}
    for backend in order:
        models = list(known_models(backend))
        if backend == current_backend and current_model and current_model not in models:
            models.append(current_model)
        provider = provider_name(backend)
        if backend in LEGACY:
            provider += "（旧・選択肢から外しました）"
        if provider not in entries:
            providers.append(provider)
            entries[provider] = []
        entries[provider].extend((backend, m) for m in models)

    groups: List[Tuple[Optional[str], List[Tuple[str, str, str]]]] = []
    for provider in providers:
        choices = entries[provider]
        if len(choices) == 1:
            backend, model = choices[0]
            groups.append((None, [(backend, model, choice_title(backend, model))]))
        else:
            groups.append((provider, [(b, m, model_label(b, m)) for b, m in choices]))
    return groups


def caption(backend: str) -> str:
    """選択中エンジンの説明文（Mac の SlotSettingsTab.backendCaption と同じ文言。Mac 固有の部分は除く）。"""
    if backend == SONIOX:
        return (
            "しゃべりながら文字起こしし、離した瞬間に入力します（速さ重視のおすすめ）。\n"
            "日本語・英語・中国語の混ざった話にも強いエンジンです（鍵: SONIOX_API_KEY）。"
        )
    if backend == OPENAI:
        return "録音後に OpenAI の gpt-transcribe で文字起こしします（日本語の精度重視）。"
    if backend == AZURE_MAI:
        return (
            "録音後に Microsoft の MAI-Transcribe-2 で文字起こしします（精度が高く単価も安め・プレビュー）。\n"
            "鍵: AZURE_SPEECH_KEY と AZURE_SPEECH_ENDPOINT。リソースは対応リージョンに作ってください（japaneast は非対応）。"
        )
    if backend == ELEVENLABS:
        return (
            "録音後に ElevenLabs の Scribe v2 で文字起こしします（日本語の精度重視）。\n"
            "数字が漢数字で出やすいので、気になるときは「数字入力」の設定と併用してください。"
        )
    if backend == OPENAI_LIVE:
        return (
            "OpenAI の新しいライブ文字起こしで入力します。\n"
            "確定はやや遅めですが（実測 0.7 秒）、固有名詞や数字に強いエンジンです。"
        )
    if backend == GROQ:
        return (
            "録音後にきれいな文章にして入力します（録音後に送る方式で最速）\n"
            "ハンズフリー録音のときは、長い録音に強いエンジンへ自動で切り替わります。"
        )
    if backend == DEEPGRAM:
        return (
            "しゃべり終わった瞬間、全文がまとめて入力されます。\n"
            "Deepgram は選択肢から外しました（今の設定のまま使えます。変えるなら Soniox がおすすめです）。"
        )
    return ""
