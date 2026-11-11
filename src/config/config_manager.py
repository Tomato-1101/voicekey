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

from ..utils.logger import get_logger
from .constants import DEFAULT_CONFIG, SETTINGS_FILE_NAME, default_format_enabled

logger = get_logger(__name__)

# 旧版で既定だったモデル名。選択肢には残っているが、既定のまま使っていた人を新しい既定へ
# 一度だけ移す（移した後に利用者が選び直した値は触らない）
_OLD_DEFAULT_MODELS = {"elevenlabs": "scribe_v1"}


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
        self.config: Dict[str, Any] = self._load_config()

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

            backend = self._normalize_backend(config.pop("transcription_backend", "groq"))
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
        エンジンの値を、いま選べるエンジン id に読み替える。

        Deepgram は Soniox へ、それ以外の知らない値は Groq へ寄せる（core/engines.py）。
        """
        from ..core import engines
        return engines.normalize_id(backend)

    def _load_config(self) -> Dict[str, Any]:
        """
        ファイルから設定を読み込み、デフォルト値とマージする。

        Returns:
            すべてのキーが保証された設定辞書

        Note:
            土台は必ず DEFAULT_CONFIG の deepcopy にする。浅い copy / _deep_merge だと
            「保存側に無いネスト辞書」（audio_preprocess・hotkey1 等）をモジュール既定値と
            共有参照したまま返し、後続の矯正処理（_force_always_on 等）が既定値そのものを
            書き換えて以降のロード結果を汚染する。
        """
        if not os.path.exists(self.config_path):
            logger.warning(f"設定ファイルが見つかりません: {self.config_path}。デフォルト値を使用します。")
            config = copy.deepcopy(DEFAULT_CONFIG)
            # 新規の設定には移行するものが無い。済みの印を付けておかないと、初回保存後の
            # 読み込みで「旧版からの移行」が走り、利用者が選んだ値（Soniox で整形 ON 等）を戻してしまう
            config["migrated_format_defaults"] = True
            config["migrated_engine_catalog"] = True
            return config

        try:
            self.last_mtime = os.path.getmtime(self.config_path)

            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded_config = yaml.safe_load(f) or {}

            # 旧形式の設定をマイグレーション
            loaded_config = self._migrate_legacy_config(loaded_config)

            # デフォルト設定と深くマージ（ネストされた辞書も保証）
            config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), loaded_config)
            # 常時 ON 固定の項目を矯正（UI から撤去したため保存済みの古い false を上書き）
            self._force_always_on(config)
            # エンジン・モデル・整形モデルを、いま選べる値へ読み替える（Deepgram → Soniox 等）
            self._normalize_engines(config)
            # エンジン別の整形既定を一度だけ適用する（読み替え後のエンジンで判定するのでこの順）
            self._migrate_format_defaults(config)
            return config

        except Exception as e:
            logger.error(f"設定読み込みエラー: {e}")
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
    def _normalize_engines(config: Dict[str, Any]) -> None:
        """
        エンジン・モデル・整え方・整形モデルを、いま選べる値へ読み替える（破壊的更新）。

        毎回の読み込みで通す。読み替え後の値は読み替えの対象にならないので、何度通しても
        結果は変わらない:
        - 選択肢から外したエンジン（Deepgram → Soniox）・知らないエンジン（→ Groq）
        - エンジンを読み替えたスロットのモデル、空・提供の終わったモデル名 → そのエンジンの既定
        - 提供の終わった整形モデル → おすすめ（利用者が選んだ別のモデルは残す）
        一度だけ行うもの（migrated_engine_catalog の印で管理）:
        - 旧版の既定モデル名（ElevenLabs の scribe_v1）→ 新しい既定。scribe_v1 は選択肢に
          残っているので、移した後に利用者が選び直した値まで毎回戻さないため

        Args:
            config: 対象の設定辞書（その場で書き換える）
        """
        from ..core import engines, llm_catalog, text_formatter

        first_time = not config.get("migrated_engine_catalog")
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            saved = str(slot.get("backend", "")).lower()
            engine_id = engines.normalize_id(saved)
            slot["backend"] = engine_id
            if engine_id != saved:
                # 別のエンジンのモデル名は使えないので既定へ
                slot["api_model"] = engines.engine(engine_id).default_model
            else:
                slot["api_model"] = engines.normalize_model(engine_id, slot.get("api_model"))
            if first_time and slot["api_model"] == _OLD_DEFAULT_MODELS.get(engine_id):
                slot["api_model"] = engines.engine(engine_id).default_model
            slot["format_preset"] = text_formatter.normalize_preset(slot.get("format_preset"))

        config["format_model"] = llm_catalog.normalize_model(config.get("format_model"))
        # 旧版が書いていたエンジン別の既定モデル表は使わなくなった（既定はカタログが持つ）
        config.pop("default_api_models", None)
        config["migrated_engine_catalog"] = True

    @staticmethod
    def _migrate_format_defaults(config: Dict[str, Any]) -> None:
        """エンジン別の整形既定を既存ユーザーへ一回限りで適用する（破壊的更新・Mac 版と対）。

        既存ユーザーは format_enabled=true が明示保存されているため、速さ優先のエンジン
        （Soniox・OpenAI ライブ）のスロットの整形を初回だけ既定 OFF へ矯正する。矯正済みの印
        （migrated_format_defaults）を立て、以後は二度と触らない＝その後ユーザーが整形 ON に
        したらそれを尊重する。印は ConfigManager.save の deep merge で保持される。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        if config.get("migrated_format_defaults"):
            return  # 一度矯正したら二度と触らない（ユーザーの選択を尊重）
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            if not default_format_enabled(slot.get("backend", "")):
                slot["format_enabled"] = False
        config["migrated_format_defaults"] = True

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
        try:
            with self._lock:
                # 内部設定を更新（ネストした辞書のカスタムキーを失わないよう deep merge する。
                # 浅い update だとスロット内の手書き追加キーが丸ごと消える）
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
