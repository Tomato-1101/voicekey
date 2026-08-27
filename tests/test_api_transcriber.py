"""ApiTranscriber 群（OpenAI/Groq/ElevenLabs/Deepgram）のロジックテスト。

認証ヘッダー・言語解決・空音声・ステータス検査を、ネットワークなしで検証する。
"""

import unittest
from unittest.mock import Mock, patch

import httpx
import numpy as np

from src.core import api_transcriber
from src.core.api_transcriber import (
    DeepgramTranscriber,
    ElevenLabsTranscriber,
    GeminiTranscriber,
    GroqTranscriber,
    OpenAITranscriber,
    TranscriptionError,
)
from src.core.backend_client import BackendError


class TestAuthHeaders(unittest.TestCase):
    def test_openai_bearer(self):
        self.assertEqual(OpenAITranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_groq_bearer(self):
        self.assertEqual(GroqTranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_deepgram_token(self):
        self.assertEqual(DeepgramTranscriber("nova-3")._auth_headers("K"), {"Authorization": "Token K"})

    def test_elevenlabs_xi_key(self):
        self.assertEqual(ElevenLabsTranscriber("scribe_v1")._auth_headers("K"), {"xi-api-key": "K"})


class TestDeepgramLanguage(unittest.TestCase):
    def test_nova3_keeps_ja(self):
        # multi は日本語を韓国語等に誤判定するため廃止（nova-3 は ja サポート済み）
        self.assertEqual(DeepgramTranscriber("nova-3", "ja")._dg_language, "ja")

    def test_nova2_keeps(self):
        self.assertEqual(DeepgramTranscriber("nova-2", "ja")._dg_language, "ja")

    def test_empty_defaults_ja(self):
        self.assertEqual(DeepgramTranscriber("nova-2", "")._dg_language, "ja")


class TestEmptyAudio(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        empty = np.array([], dtype=np.float32)
        # ネットワークに到達せず即 "" を返す（キー不要）
        self.assertEqual(DeepgramTranscriber("nova-3").transcribe(empty), "")
        self.assertEqual(ElevenLabsTranscriber("scribe_v1").transcribe(empty), "")
        self.assertEqual(OpenAITranscriber("gpt-4o-mini-transcribe").transcribe(empty), "")


class TestRaiseForStatus(unittest.TestCase):
    def test_errors_raise(self):
        t = OpenAITranscriber("m")
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(401))
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(429))
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(500))

    def test_ok_passes(self):
        t = OpenAITranscriber("m")
        t._raise_for_status(httpx.Response(200))  # 例外を投げない


class TestAvailableModels(unittest.TestCase):
    def test_model_sets(self):
        self.assertIn("nova-3", DeepgramTranscriber.available_models)
        self.assertIn("scribe_v1", ElevenLabsTranscriber.available_models)
        self.assertIn("gpt-4o-transcribe", OpenAITranscriber.available_models)
        self.assertIn("whisper-large-v3-turbo", GroqTranscriber.available_models)


class TestServerRouting(unittest.TestCase):
    """段階3: ログイン済みはサーバー経路で文字起こしする（並存ガード）。"""

    @staticmethod
    def _audio():
        # 非空の音声（is_logged_in 判定まで到達させる）
        return np.full(1600, 0.1, dtype=np.float32)

    def test_elevenlabs_uses_proxy_when_logged_in(self):
        """正確性（ElevenLabs）はログイン時サーバープロキシを使い直叩きしない。"""
        t = ElevenLabsTranscriber("scribe_v1", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_elevenlabs",
                             return_value="プロキシ結果") as proxy, \
                patch.object(t, "_get_client") as direct:
            self.assertEqual(t.transcribe(self._audio()), "プロキシ結果")
            proxy.assert_called_once()
            self.assertEqual(proxy.call_args.args[1], "ja")  # language を渡す
            direct.assert_not_called()

    def test_elevenlabs_backend_error_becomes_transcription_error(self):
        """プロキシ失敗は TranscriptionError へ写す（呼び出し側の通知に乗る）。"""
        t = ElevenLabsTranscriber("scribe_v1", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_elevenlabs",
                             side_effect=BackendError("だめ", status=403)):
            with self.assertRaises(TranscriptionError):
                t.transcribe(self._audio())

    def test_groq_uses_proxy_and_propagates_server_format(self):
        """高速（Groq）はログイン時プロキシを使い、server_format をそのまま伝搬する。"""
        t = GroqTranscriber("whisper-large-v3-turbo", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_groq",
                             return_value="整形済み") as proxy, \
                patch.object(t, "_get_client") as direct:
            self.assertEqual(t.transcribe(self._audio(), server_format=True), "整形済み")
            proxy.assert_called_once()
            self.assertEqual(proxy.call_args.args[1], "ja")           # language を渡す
            self.assertTrue(proxy.call_args.kwargs["server_format"])  # 統合整形フラグを伝搬
            direct.assert_not_called()

    def test_groq_default_no_server_format(self):
        """server_format 未指定なら False を伝搬（統合整形しない）。"""
        t = GroqTranscriber("whisper-large-v3-turbo", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_groq",
                             return_value="生") as proxy:
            t.transcribe(self._audio())
            self.assertFalse(proxy.call_args.kwargs["server_format"])

    def test_deepgram_uses_jwt_when_logged_in(self):
        """高速リアルタイム（Deepgram）はログイン時 短命 JWT で直叩き（低レイテンシ維持）。"""
        t = DeepgramTranscriber("nova-3", "ja")
        captured = {}

        def fake_post(path, **kwargs):
            captured["url"] = path
            captured["auth"] = kwargs["headers"]["Authorization"]
            return httpx.Response(
                200,
                json={"results": {"channels": [{"alternatives": [{"transcript": "本日は晴天"}]}]}},
            )

        # JWT 経路は共有クライアント（prewarm で温める接続プール）を使う
        jwt_client = Mock()
        jwt_client.post.side_effect = fake_post

        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "fetch_ephemeral_token",
                             return_value={"token": "dg-jwt"}), \
                patch.object(t, "_get_jwt_client", return_value=jwt_client), \
                patch.object(t, "_get_client") as direct:
            self.assertEqual(t.transcribe(self._audio()), "本日は晴天")
            self.assertEqual(captured["auth"], "Bearer dg-jwt")  # Token ではなく Bearer
            self.assertTrue(captured["url"].endswith("/listen"))
            jwt_client.post.assert_called_once()  # 毎回 httpx.post ではなく共有クライアント
            direct.assert_not_called()  # キャッシュ済み Token クライアントはバイパス

    def test_deepgram_token_error_becomes_transcription_error(self):
        """短命トークン取得失敗は TranscriptionError へ写す。"""
        t = DeepgramTranscriber("nova-3", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "fetch_ephemeral_token",
                             side_effect=BackendError("無効", status=403)):
            with self.assertRaises(TranscriptionError):
                t.transcribe(self._audio())


class TestDistGate(unittest.TestCase):
    """配布版の無料ゲート: ログイン＋アクティベーション必須（_dist_guard）。"""

    @staticmethod
    def _audio():
        return np.full(1600, 0.1, dtype=np.float32)

    def test_dist_not_logged_in_blocks_deepgram(self):
        """配布版で未ログインなら、直叩きせず「ログイン必須」を出す。"""
        t = DeepgramTranscriber("nova-3", "ja")
        with patch.object(api_transcriber.secrets, "is_dist_build", return_value=True), \
                patch.object(api_transcriber.backend_client, "is_logged_in", return_value=False), \
                patch.object(t, "_get_client") as direct:
            with self.assertRaises(TranscriptionError) as cm:
                t.transcribe(self._audio())
            direct.assert_not_called()  # 埋め込みキー直叩きへ落ちない
        self.assertIn("ログイン", str(cm.exception))

    def test_dist_not_logged_in_blocks_elevenlabs(self):
        t = ElevenLabsTranscriber("scribe_v1", "ja")
        with patch.object(api_transcriber.secrets, "is_dist_build", return_value=True), \
                patch.object(api_transcriber.backend_client, "is_logged_in", return_value=False), \
                patch.object(t, "_get_client") as direct:
            with self.assertRaises(TranscriptionError):
                t.transcribe(self._audio())
            direct.assert_not_called()

    def test_dist_openai_groq_unsupported(self):
        """配布版では OpenAI/Groq の文字起こし（サーバー非対応）は使えない。"""
        t = OpenAITranscriber("gpt-4o-mini-transcribe", "ja")
        with patch.object(api_transcriber.secrets, "is_dist_build", return_value=True), \
                patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(t, "_get_client") as direct:
            with self.assertRaises(TranscriptionError) as cm:
                t.transcribe(self._audio())
            direct.assert_not_called()
        self.assertIn("配布版", str(cm.exception))

    def test_dev_build_no_gate(self):
        """開発ビルドはゲートしない（埋め込み/設定キー直叩きの並存を維持）。"""
        t = DeepgramTranscriber("nova-3", "ja")
        captured = {}

        def fake_post(client, path, **kwargs):
            captured["path"] = path
            return httpx.Response(
                200, json={"results": {"channels": [{"alternatives": [{"transcript": "開発"}]}]}}
            )

        with patch.object(api_transcriber.secrets, "is_dist_build", return_value=False), \
                patch.object(api_transcriber.backend_client, "is_logged_in", return_value=False), \
                patch.object(t, "_get_client", return_value=object()), \
                patch.object(t, "_post", side_effect=fake_post):
            self.assertEqual(t.transcribe(self._audio()), "開発")
            self.assertEqual(captured["path"], "/listen")


class TestWhisperPrompt(unittest.TestCase):
    """Whisper（Groq/OpenAI）へ数字を半角で出させる style プロンプトを付与する。"""

    @staticmethod
    def _audio():
        return np.full(1600, 0.1, dtype=np.float32)

    def _capture_direct_data(self, transcriber):
        """開発直叩き（Whisper 系）の multipart data を捕捉して返す。"""
        captured = {}

        def fake_post(client, path, **kwargs):
            captured["data"] = kwargs.get("data")
            return httpx.Response(200, text="結果")

        with patch.object(api_transcriber.secrets, "is_dist_build", return_value=False), \
                patch.object(api_transcriber.backend_client, "is_logged_in", return_value=False), \
                patch.object(transcriber, "_get_client", return_value=object()), \
                patch.object(transcriber, "_post", side_effect=fake_post):
            transcriber.transcribe(self._audio())
        return captured["data"]

    def test_groq_direct_includes_numeral_prompt(self):
        data = self._capture_direct_data(GroqTranscriber("whisper-large-v3-turbo", "ja"))
        self.assertEqual(data["prompt"], api_transcriber._NUMERAL_STYLE_PROMPT)

    def test_openai_direct_includes_numeral_prompt(self):
        data = self._capture_direct_data(OpenAITranscriber("gpt-4o-transcribe", "ja"))
        self.assertEqual(data["prompt"], api_transcriber._NUMERAL_STYLE_PROMPT)

    def test_user_prompt_is_appended_after_hint(self):
        t = GroqTranscriber("whisper-large-v3-turbo", "ja", prompt="固有名詞: VoiceKey")
        data = self._capture_direct_data(t)
        self.assertTrue(data["prompt"].startswith(api_transcriber._NUMERAL_STYLE_PROMPT))
        self.assertIn("固有名詞: VoiceKey", data["prompt"])

    def test_groq_proxy_forwards_prompt(self):
        """release の Groq プロキシ経路は prompt を transcribe_groq へ渡す（サーバーが Groq へ転送）。"""
        t = GroqTranscriber("whisper-large-v3-turbo", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_groq",
                             return_value="結果") as proxy:
            t.transcribe(self._audio())
            self.assertEqual(proxy.call_args.kwargs["prompt"], api_transcriber._NUMERAL_STYLE_PROMPT)


if __name__ == "__main__":
    unittest.main()


class TestGeminiTranscriber(unittest.TestCase):
    """Gemini 3.5 Transcribe（Interactions API）の純ロジック。

    ネットワークは叩かない（課金 API のため）。実機の 1 往復は Mac 側ハーネス
    `--rest-stt-test <音声> --backend gemini` で手動確認する。
    Mac 版 GeminiTranscribeTests と同じケースを持たせて両 OS の挙動を揃える。
    """

    def test_auth_header_uses_google_key_header(self):
        """Bearer ではなく x-goog-api-key。Api-Revision も必須。"""
        headers = GeminiTranscriber("gemini-3.5-transcribe")._auth_headers("K")
        self.assertEqual(headers["x-goog-api-key"], "K")
        self.assertEqual(headers["Api-Revision"], GeminiTranscriber.API_REVISION)
        self.assertNotIn("Authorization", headers)

    def test_parses_real_response(self):
        """実測（2026-08-27）の応答からテキストを取り出せる。"""
        payload = {
            "status": "completed",
            "steps": [{
                "type": "model_output",
                "content": [{"type": "text", "text": "明日の午後3時に渋谷駅で打ち合わせをしましょう。"}],
            }],
        }
        self.assertEqual(
            GeminiTranscriber.parse_text(payload),
            "明日の午後3時に渋谷駅で打ち合わせをしましょう。",
        )

    def test_joins_multiple_steps(self):
        """step が複数に割れても全文を落とさない。"""
        payload = {"steps": [
            {"content": [{"type": "text", "text": "前半です。"}]},
            {"content": [{"type": "text", "text": "後半です。"}]},
        ]}
        self.assertEqual(GeminiTranscriber.parse_text(payload), "前半です。後半です。")

    def test_ignores_non_text_content(self):
        """word_info 等の注釈は本文に混ぜない。"""
        payload = {"steps": [{"content": [
            {"type": "word_info", "text": "捨てる"},
            {"type": "text", "text": "残す"},
        ]}]}
        self.assertEqual(GeminiTranscriber.parse_text(payload), "残す")

    def test_parse_text_empty_for_error_payload(self):
        """解析できない応答は空文字（呼び出し側が日本語エラーにする）。"""
        self.assertEqual(GeminiTranscriber.parse_text({"error": {"code": 400}}), "")
        self.assertEqual(GeminiTranscriber.parse_text({}), "")

    def test_language_code_gets_region(self):
        """設定の言語コードは BCP-47（地域付き）へ寄せる。"""
        self.assertEqual(GeminiTranscriber.language_code("ja"), "ja-JP")
        self.assertEqual(GeminiTranscriber.language_code("en"), "en-US")
        self.assertEqual(GeminiTranscriber.language_code("ko"), "ko-KR")

    def test_language_code_keeps_explicit_region(self):
        """地域・スクリプト付きと空（自動判定）はそのまま。"""
        self.assertEqual(GeminiTranscriber.language_code("en-GB"), "en-GB")
        self.assertEqual(GeminiTranscriber.language_code("zh-Hans"), "zh-Hans")
        self.assertEqual(GeminiTranscriber.language_code(""), "")

    def test_custom_vocabulary_splits_words(self):
        """ユーザー辞書は語のリストへ分解して渡す。"""
        self.assertEqual(
            GeminiTranscriber.custom_vocabulary("voicekey、Deepgram, 渋谷"),
            ["voicekey", "Deepgram", "渋谷"],
        )

    def test_custom_vocabulary_drops_empty(self):
        """空要素は落とす（空リストなら送らない）。"""
        self.assertEqual(GeminiTranscriber.custom_vocabulary("  "), [])
        self.assertEqual(GeminiTranscriber.custom_vocabulary("語, ,,語2"), ["語", "語2"])

    def test_empty_audio_returns_empty(self):
        """空音声は API を叩かずに空文字。"""
        empty = np.array([], dtype=np.float32)
        self.assertEqual(GeminiTranscriber("gemini-3.5-transcribe").transcribe(empty), "")
