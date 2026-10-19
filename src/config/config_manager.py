"""
設定管理モジュール

YAMLファイルからの設定読み込み、保存、ホットリロードを提供する。
設定ファイルが変更されると自動的に再読み込みされる。
"""

import copy
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from . import llm_models
from ..utils import corrupt_file
from ..utils.logger import get_logger
from . import backends
from .constants import DEFAULT_CONFIG, SETTINGS_FILE_NAME, default_format_enabled

logger = get_logger(__name__)
# 動かせるバックエンド（Mac と同じ選択肢 6 つ＋選択肢から外した Deepgram）。
# Deepgram は Mac で選択肢から外したが、Windows は保存済みの設定ならそのまま動かす
# （本人の今の設定のエンジンを勝手に変えない）。これ以外（未知の値）は groq へ移す（Mac と同じ）
API_BACKENDS = set(backends.SUPPORTED)
RELEASE_TRANSCRIBE_BACKENDS = backends.SUPPORTED
# 範囲外（未知の値）の保存値を移行する先（Mac と同じく Groq）
RELEASE_DEFAULT_BACKEND = backends.GROQ


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    2つの辞書を深くマージする。

    updatesの値がbaseの値を上書きする。
    ネストされた辞書も再帰的にマージされる。

    Args:
        base: ベースとなる辞書
        updates: 更新値を含む辞書

    Returns:
        マージされた辞書
    """
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # 両方が辞書の場合は再帰的にマージ
            result[key] = _deep_merge(result[key], value)
        else:
            # それ以外は上書き
            result[key] = value
    return result


class ConfigManager:
    """
    アプリケーション設定の管理クラス。
    
    YAMLファイルからの設定読み込み、保存を行い、
    ファイル変更時には自動的に再読み込みを行う。
    
    Attributes:
        config_path: 設定ファイルのパス
        config: 現在の設定辞書
    """
    
    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        ConfigManagerを初期化する。
        
        Args:
            config_path: 設定ファイルのパス。Noneの場合はプロジェクトルートから自動検索
        """
        self.config_path = self._resolve_config_path(config_path)
        self.last_mtime: Optional[float] = None  # ファイル更新時刻
        # 起動時（＝この初回ロード時）に settings.yaml が既に存在していたか。
        # 「完全な初回起動（新規インストール）」か「既存ユーザー」かの判定に使う
        # （初回オンボーディング表示要否・Phase 5）。以後の save/reload では変えない。
        self.config_file_existed: bool = os.path.exists(self.config_path)
        # config は listener / 設定監視 / UI の各スレッドから read/write されるため保護する
        self._lock = threading.RLock()
        # 起動時に読めなかった settings.yaml を退避できなかったら、元ファイルを守るため保存しない
        self._save_blocked = False
        self.config: Dict[str, Any] = self._load_config(initial=True)

    def _resolve_config_path(self, config_path: Optional[str]) -> str:
        """設定ファイルのパスを解決する。"""
        if config_path:
            return config_path

        if getattr(sys, 'frozen', False):
            # PyInstallerでビルドされた実行ファイルの場合
            base_dir = Path(sys.executable).parent
        else:
            # スクリプトとして実行：プロジェクトルート（srcの親）を使用
            base_dir = Path(__file__).parent.parent.parent

        return str(base_dir / SETTINGS_FILE_NAME)

    def _migrate_legacy_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        旧形式の設定を新形式にマイグレーションする。

        旧形式（単一hotkey）を検出した場合、hotkey1に変換し、
        hotkey2はデフォルト値を設定する。

        Args:
            config: 読み込まれた設定辞書

        Returns:
            マイグレーション済みの設定辞書
        """
        if "hotkey" in config and "hotkey1" not in config:
            # 旧形式を検出
            logger.info("旧設定フォーマットを検出。新形式にマイグレーション中...")

            backend = self._normalize_backend(config.pop("transcription_backend", "openai"))
            model_key = "groq_model" if backend == "groq" else "openai_model"
            prompt_key = "groq_prompt" if backend == "groq" else "openai_prompt"

            # 旧ホットキー設定をhotkey1に移行
            config["hotkey1"] = {
                "hotkey": config.pop("hotkey", "<f2>"),
                "hotkey_mode": config.pop("hotkey_mode", "toggle"),
                "backend": backend,
                "api_model": config.get(model_key, ""),
                "api_prompt": config.get(prompt_key, ""),
            }

            # hotkey2にデフォルト値
            config["hotkey2"] = DEFAULT_CONFIG["hotkey2"].copy()

            # 旧キーを削除
            for key in [
                "groq_model",
                "openai_model",
                "groq_prompt",
                "openai_prompt",
                "transcription_backend",
                "model_size",
                "compute_type",
                "release_memory_delay",
                "condition_on_previous_text",
                "no_speech_threshold",
                "log_prob_threshold",
                "no_speech_prob_cutoff",
                "beam_size",
                "model_cache_dir",
                "local_backend",
            ]:
                config.pop(key, None)

            logger.info("設定マイグレーション完了")

        # スロット設定が存在する場合は、動かせるバックエンドに正規化する。
        # api_model の空はそのまま残す（実行時に config/backends.py の既定モデルが使われる）
        for slot_id in [1, 2]:
            slot_key = f"hotkey{slot_id}"
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue

            raw_backend = str(slot.get("backend", "groq")).lower()
            backend = self._normalize_backend(raw_backend)
            if backend != raw_backend:
                # 別のエンジンへ移したら、旧エンジンのモデル名は意味が無いので既定へ戻す（Mac と同じ）
                slot["api_model"] = ""
            slot["backend"] = backend

        # 廃止したローカル推論設定キーを削除
        for key in [
            "local_backend",
            "model_size",
            "compute_type",
            "release_memory_delay",
            "condition_on_previous_text",
            "no_speech_threshold",
            "log_prob_threshold",
            "no_speech_prob_cutoff",
            "beam_size",
            "model_cache_dir",
        ]:
            config.pop(key, None)

        return config

    @staticmethod
    def _normalize_backend(backend: Any) -> str:
        """
        バックエンド値を動かせる値に正規化する。

        `local` や未知の値（新しい版で保存した値など）は groq にフォールバックする（Mac と同じ）。
        """
        backend_str = str(backend).lower()
        if backend_str in API_BACKENDS:
            return backend_str
        return RELEASE_DEFAULT_BACKEND

    def _load_config(self, initial: bool = False) -> Dict[str, Any]:
        """
        ファイルから設定を読み込み、デフォルト値とマージする。

        Args:
            initial: 起動時の初回読み込みか。読めなかったとき、起動時は壊れたファイルを退避して
                既定値で始める。ホットリロード中は手で編集している途中の可能性が高いので、
                退避も既定値への差し替えもしない（None を返し、呼び出し側が今の設定を保つ）

        Returns:
            すべてのキーが保証された設定辞書（ホットリロードで読めなかったときは None）

        Note:
            土台は必ず DEFAULT_CONFIG の deepcopy にする。浅い copy / _deep_merge だと
            「保存側に無いネスト辞書」（audio_preprocess・hotkey1 等）をモジュール既定値と
            共有参照したまま返し、後続の矯正処理（_force_always_on 等）が既定値そのものを
            書き換えて以降のロード結果を汚染する。
        """
        if not os.path.exists(self.config_path):
            logger.warning(f"設定ファイルが見つかりません: {self.config_path}。デフォルト値を使用します。")
            return copy.deepcopy(DEFAULT_CONFIG)

        try:
            self.last_mtime = os.path.getmtime(self.config_path)

            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded_config = yaml.safe_load(f) or {}

            if not isinstance(loaded_config, dict):
                raise ValueError("settings.yaml の中身が設定の形（キーと値の組）になっていません")

            # 旧形式の設定をマイグレーション
            loaded_config = self._migrate_legacy_config(loaded_config)

            # デフォルト設定と深くマージ（ネストされた辞書も保証）
            config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), loaded_config)
            # 常時 ON 固定の項目を矯正（UI から撤去したため保存済みの古い false を上書き）
            self._force_always_on(config)
            # 製品版は文字起こしバックエンドを 3 択に制限（範囲外の openai 等は groq へ移行・deepgram は維持）
            self._constrain_release_backends(config)
            # モード別整形既定の一回限りマイグレーション（deepgram スロットの整形を初回だけ既定 OFF へ）
            self._migrate_format_defaults(config)
            # 選択肢から外したモデル → そのエンジンの既定（一回限り・Mac の V19）
            self._migrate_retired_models(config)
            # 廃止された LLM モデル（Groq の llama 系）→ おすすめ（一回限り・Mac の V20）
            self._migrate_llm_models(config)
            return config

        except Exception as e:
            logger.error(f"設定読み込みエラー: {e}")
            if not initial:
                # 手で編集している途中の書きかけを読んだ可能性が高い。今の設定を保ち、次の変更を待つ
                return None
            # 起動時に読めない＝壊れている。既定値で始めると次の保存で元の内容を上書きして消すので、
            # 退避して本人に 1 回知らせる。退避できなければ保存しない（元ファイルを守る）
            if corrupt_file.quarantine(self.config_path, label="設定ファイル（settings.yaml）") is None:
                self._save_blocked = True
            return copy.deepcopy(DEFAULT_CONFIG)

    @staticmethod
    def _force_always_on(config: Dict[str, Any]) -> None:
        """
        UI から撤去し常時 ON に固定する項目を矯正する（破壊的更新）。

        VAD・長文分割・ストリーミング・録音 HUD・音量正規化は設定 UI から撤去したため、
        保存済み settings.yaml に古い false が残っていても無視して True に固定する。
        ユーザーが OFF にする手段を持たないことを保証する。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        config["vad_filter"] = True
        config["split_parallel_enabled"] = True
        config["streaming_enabled"] = True
        config["hud_enabled"] = True
        # 音量正規化は Windows 固有の前処理（audio_preprocess セクション）
        if not isinstance(config.get("audio_preprocess"), dict):
            config["audio_preprocess"] = {}
        config["audio_preprocess"]["volume_normalize"] = True

    @staticmethod
    def _constrain_release_backends(config: Dict[str, Any]) -> None:
        """
        文字起こしバックエンドを動かせる集合（Mac と同じ 6 つ＋旧 Deepgram）に制限する。

        未知の値（新しい版で保存した値など）だけ groq へ移し、api_model を空にして
        既定モデルへフォールバックさせる。Deepgram は選択肢から外したが、保存済みなら維持する
        （本人の設定のエンジンを勝手に変えない）。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            backend = str(slot.get("backend", "")).lower()
            if backend not in RELEASE_TRANSCRIBE_BACKENDS:
                slot["backend"] = RELEASE_DEFAULT_BACKEND
                # 移行先バックエンドのモデルへフォールバックさせる
                slot["api_model"] = ""

    @staticmethod
    def _migrate_format_defaults(config: Dict[str, Any]) -> None:
        """モード別整形既定を既存ユーザーへ一回限りで適用する（破壊的更新・Mac 版と対）。

        既存ユーザーは format_enabled=true が明示保存されているため、即時入力(deepgram)
        スロットの整形を初回だけ既定 OFF（速度全振り）へ矯正する。矯正済みマーカー
        （migrated_format_defaults）を立て、以後は二度と触らない＝その後ユーザーが deepgram で
        整形 ON にしたらそれを尊重する。マーカーは ConfigManager.save の deep merge で保持される
        （save は self.config を土台にマージするので、_load_config で立てたマーカーが次回保存で
        settings.yaml へ書き戻る）。deepgram 以外（スタンダード=groq 等）は既定 ON のままなので触らない。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        if config.get("migrated_format_defaults"):
            return  # 一度矯正したら二度と触らない（ユーザーの選択を尊重）
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            backend = str(slot.get("backend", "")).lower()
            # deepgram のみ既定 OFF へ矯正（他バックエンドの既定 ON は明示保存済みなので触らない）
            if not default_format_enabled(backend):
                slot["format_enabled"] = False
        config["migrated_format_defaults"] = True

    @staticmethod
    def _migrate_retired_models(config: Dict[str, Any]) -> None:
        """2026-10-01 に選択肢から外したモデルを、そのエンジンの既定へ戻す（一回限り・Mac の V19）。

        毎回置換すると、移行後に利用者が自由入力で選び直した旧モデル名まで消えるので一度きりにする。
        エンジンそのものは変えない（本人の設定のエンジンを勝手に変えない）。
        """
        if config.get("migrated_retired_models_v19"):
            return
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            if str(slot.get("api_model", "") or "").strip() in backends.RETIRED_MODELS:
                slot["api_model"] = ""  # 空＝実行時に既定モデル
        config["migrated_retired_models_v19"] = True

    @staticmethod
    def _migrate_llm_models(config: Dict[str, Any]) -> None:
        """廃止と分かっている Groq の LLM モデル（llama 系）をおすすめへ戻す（一回限り・Mac の V20）。

        Windows の旧既定 llama-3.1-8b-instant は 2026-10 に Groq の API から廃止され、
        整形が毎回失敗する。以後の提供終了は実行時の差し替え（text_formatter）で拾う。
        """
        if config.get("migrated_llm_models_v20"):
            return
        for key in ("format_model", "translate_input_groq_model"):
            replacement = llm_models.migrated_model(str(config.get(key, "") or ""), llm_models.GROQ)
            if replacement:
                logger.info(f"保存モデルを移行: {key} {config.get(key)} → {replacement}")
                config[key] = replacement
        config["migrated_llm_models_v20"] = True

    def reload_if_changed(self) -> bool:
        """
        ファイルが変更されていれば設定を再読み込みする。
        
        Returns:
            再読み込みした場合True、しなかった場合False
        """
        if not os.path.exists(self.config_path):
            return False
            
        try:
            current_mtime = os.path.getmtime(self.config_path)
            if self.last_mtime is None or current_mtime > self.last_mtime:
                logger.info("設定ファイルが変更されました。再読み込み中...")
                new_config = self._load_config()
                if new_config is None:
                    # 読めなかった（手で編集中など）。同じ書きかけを毎秒読み直さないよう時刻だけ進める
                    self.last_mtime = current_mtime
                    logger.warning("settings.yaml を読めなかったため、今の設定のまま続けます")
                    return False
                with self._lock:
                    self.config = new_config
                return True
        except Exception as e:
            logger.error(f"設定確認エラー: {e}")
            
        return False

    def get(self, key: str, default: Any = None) -> Any:
        """
        設定値を取得する。
        
        Args:
            key: 設定キー
            default: キーが存在しない場合のデフォルト値
            
        Returns:
            設定値
        """
        with self._lock:
            return self.config.get(key, default)

    def save(self, new_config: Dict[str, Any]) -> bool:
        """
        設定をファイルに保存する。
        
        Args:
            new_config: 新しい設定値を含む辞書
            
        Returns:
            成功した場合True、失敗した場合False
        """
        if self._save_blocked:
            # 起動時に壊れていた settings.yaml を退避できなかった。上書きすると元の内容が消えるので保存しない
            logger.error("設定ファイルが壊れていて退避できなかったため、保存を止めています")
            return False
        try:
            with self._lock:
                # 内部設定を更新（ネストした辞書のカスタムキーを失わないよう deep merge する。
                # 浅い update だと default_api_models 等の手書き追加キーが丸ごと消える）
                self.config = _deep_merge(self.config, new_config)
                # 常時 ON 固定の項目はファイルにも True で書き戻す（UI から撤去済み）
                self._force_always_on(self.config)

                # アトミック書き込み: 一時ファイルへ全量書いてから os.replace で置換する。
                # open("w") の直接書きは truncate 後にクラッシュ/ディスク不足が起きると
                # settings.yaml が空・破損で残り、ユーザーの全設定が消える。同一ディレクトリの
                # 一時ファイル経由なら、置換は同一ファイルシステム上の原子的 rename になる
                # （os.replace は Windows でも既存ファイルを上書きする）。
                tmp_path = f"{self.config_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
                    f.flush()
                    os.fsync(f.fileno())  # 置換前に中身をディスクへ確実に落とす
                os.replace(tmp_path, self.config_path)

                # 再読み込みループを防ぐために更新時刻を記録
                self.last_mtime = os.path.getmtime(self.config_path)
            logger.info("設定を保存しました。")
            return True
            
        except Exception as e:
            logger.error(f"設定保存エラー: {e}")
            return False

    # 後方互換性のためのエイリアス
    def save_config(self, new_config: Dict[str, Any]) -> bool:
        """save()のエイリアス。"""
        return self.save(new_config)
