# HANDOFF — voicekey（2026-09-25 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 目的
Mac 版の「メモリが積み上がる・ファンが回る・遅い・固まる」を根本から止め、全体を軽くする。

## 現状（7498144 で反映・常用アプリ入れ替え済み）
- 真因: AVAudioEngine が生成のたびに coreaudiod に集約デバイス（CADefaultDeviceAggregate）を作り、
  dealloc の dispatch_sync でメインが永久ハング。09-24 の「4 分ごと／入力ごとにエンジン入れ替え」が増幅していた。
- 対策: 録音部を入力専用 AUHAL 1 個の使い回しに置き換え、入れ替えは撤回。集約デバイス 0、押下→開始 中央値 約 70ms、
  常駐メモリ 23MB。
- 軽量化: 履歴同期の日付パース（1 入力あたり約 1.1 秒 CPU）ほか、CHANGELOG の Unreleased を参照。
- coreaudiod は 6 日分の肥大（RSS 約 1.1GB、CPU 約 20%）が残っている。voicekey を止めても CPU は変わらない
  ＝ voicekey 由来ではない。解消には本人が `sudo killall coreaudiod` を実行する必要がある（音が一瞬切れる）。
- 遅延の体感は 09-21 に本人が選んだ OpenAI ライブ（離鍵→貼付 約 750ms）が主因。Groq / Apple は約 200ms。
  バックエンドの切替は本人の判断（Claude は変えない）。

## 次にやること
- Bluetooth 入力（AirPods 等）で録音し、行動ログに「サンプルレート」通知と「再構成します」が押下のたびに
  出ていないか確認（IO 開始 1 秒以内の通知を見送る処理が実機で未検証）。
- 数日使って footprint と coreaudiod の推移を確認。MicAutoDetector（設定のマイク自動検出）はまだ
  AVAudioEngine を使う（ユーザー操作時のみ・低優先）。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
