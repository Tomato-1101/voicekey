#!/usr/bin/env python3
"""
難易度の高いベンチ音声を作る（make_audio.sh の合成音声版を厳しくしたもの）。

既存の short/long は「ゆっくり・雑音なし・平易な語彙」なのでモデル差がほとんど出ない
（実測で 6.8 秒の文だと Groq も Gemini も同一テキストを返した）。実運用でモデル差が
出るのは以下の 3 条件なので、それを狙って作る:

  1. 英日混在・固有名詞・言い直し（hard1）: 開発の口述に一番近い
  2. 数字・日付・金額・型番（hard2）: 誤ると致命的で、モデルの得手不得手が割れる
  3. 早口 + 背景雑音（*_fast_noisy）: 実マイク環境の劣化耐性

雑音は SNR を指定して合成する（ピンクノイズ）。ffmpeg の重み付けミックスだと実効 SNR が
わからないため、numpy で RMS を測って目標 SNR ちょうどに合わせる。

使い方:
    python3 make_hard_audio.py            # 既定 SNR 10dB / 速度 250
    python3 make_hard_audio.py --snr 5    # もっと厳しく
"""

import argparse
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

AUDIO = Path(__file__).parent / "audio"
VOICE = "Kyoko"
SAMPLE_RATE = 16000


def say_to_wav(text_path: Path, out_wav: Path, rate: int | None = None) -> None:
    """say で読み上げ → 16kHz モノラル WAV へ変換する。

    Args:
        text_path: 読み上げる原稿（正解テキストを兼ねる）
        out_wav: 出力 WAV
        rate: 読み上げ速度（words per minute 相当。None なら既定速度）
    """
    aiff = out_wav.with_suffix(".aiff")
    cmd = ["say", "-v", VOICE, "-f", str(text_path), "-o", str(aiff)]
    if rate is not None:
        cmd += ["-r", str(rate)]
    subprocess.run(cmd, check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1",
         str(aiff), str(out_wav)],
        check=True,
    )
    aiff.unlink(missing_ok=True)


def read_wav(path: Path) -> np.ndarray:
    """16bit モノラル WAV を float32 [-1, 1] で読む"""
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(path: Path, samples: np.ndarray) -> None:
    """float32 [-1, 1] を 16bit モノラル WAV で書く（クリップは切り詰め）"""
    clipped = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((clipped * 32767).astype(np.int16).tobytes())


def pink_noise(length: int, seed: int = 0) -> np.ndarray:
    """ピンクノイズ（1/f）を作る。

    白色ノイズより実環境（空調・遠くの話し声・PC ファン）に近く、STT を素直に難しくする。
    FFT で 1/sqrt(f) の重みを掛けてから戻す（Voss-McCartney より短時間で素直な特性になる）。
    """
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(length)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(length, d=1.0 / SAMPLE_RATE)
    freqs[0] = freqs[1] if len(freqs) > 1 else 1.0  # DC 成分のゼロ割り回避
    shaped = spectrum / np.sqrt(freqs)
    noise = np.fft.irfft(shaped, n=length)
    peak = np.max(np.abs(noise))
    return noise / peak if peak > 0 else noise


def mix_at_snr(speech: np.ndarray, snr_db: float, seed: int = 0) -> np.ndarray:
    """目標 SNR になるようピンクノイズを足す。

    SNR は「発話区間の RMS」基準で測る。無音込みの全体 RMS で測ると、無音が長い音声ほど
    見かけの SNR が下がって条件が揃わないため。

    Args:
        speech: 原音声
        snr_db: 目標 SNR（dB。小さいほど厳しい）
        seed: ノイズの乱数種（再現性のため固定して使う）

    Returns:
        雑音を重ねた音声
    """
    # 上位 50% の振幅を持つ区間を「発話中」とみなして RMS を測る
    envelope = np.abs(speech)
    threshold = np.percentile(envelope, 60)
    voiced = speech[envelope > threshold]
    speech_rms = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else float(
        np.sqrt(np.mean(speech ** 2))
    )

    noise = pink_noise(len(speech), seed=seed)
    noise_rms = float(np.sqrt(np.mean(noise ** 2)))
    target_noise_rms = speech_rms / (10 ** (snr_db / 20.0))
    return speech + noise * (target_noise_rms / noise_rms)


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snr", type=float, default=10.0, help="雑音版の目標 SNR（dB）")
    parser.add_argument("--rate", type=int, default=250, help="早口版の読み上げ速度")
    args = parser.parse_args()

    for name in ("hard1", "hard2"):
        script = AUDIO / f"{name}_ja.txt"
        if not script.exists():
            print(f"!! {script} がありません", file=sys.stderr)
            return 1

        clean = AUDIO / f"{name}_ja.wav"
        say_to_wav(script, clean)
        print(f"==> {clean.name}: {duration(clean):.1f}s（通常速度・雑音なし）")

        # 早口 + 雑音（実マイク環境の劣化耐性を見る）
        fast = AUDIO / f"{name}_fast_noisy_ja.wav"
        say_to_wav(script, fast, rate=args.rate)
        noisy = mix_at_snr(read_wav(fast), snr_db=args.snr)
        write_wav(fast, noisy)
        print(f"==> {fast.name}: {duration(fast):.1f}s（速度 {args.rate} / SNR {args.snr:.0f}dB）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
