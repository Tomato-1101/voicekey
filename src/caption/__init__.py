"""
ライブ字幕・議事録・Google Meet 議事録ボット（Windows 版）

Mac 版 `macos/Sources/Voicekey/Caption/` の移植。Windows では**ローカルの認識・翻訳を使わない**
（2026-10-19 本人の決定「Windows はローカルの計算は無し。全部 API の文字起こしにして。翻訳は安い API」）。

- 音: WASAPI ループバック（Process Loopback でアプリ単位。使えなければシステム全体）
- 認識: Soniox / OpenAI ライブ（WebSocket）
- 翻訳: Soniox（認識と一緒）/ Groq / Gemini（確定文だけ送る）

**ディクテーションのクリティカルパスに 1ms も足さない**ため、このパッケージは字幕を開始するまで
import されない（app.py は遅延生成する）。
"""
