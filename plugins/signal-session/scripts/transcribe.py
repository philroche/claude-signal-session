#!/usr/bin/env python3
"""Print a transcript of an audio or video file, using faster-whisper on this
machine, so the audio never leaves it.

Usage: transcribe.py [--download-only] <file>

Runs on an NVIDIA GPU when CUDA finds one, otherwise on the CPU with 8-bit
weights. Settings, from the environment:
  SIGNAL_SESSION_WHISPER_MODEL     model name, default large-v3. On a slow CPU,
                                   small or medium is quicker and less accurate.
  SIGNAL_SESSION_WHISPER_LANGUAGE  language code such as en; default: detect it.
  SIGNAL_SESSION_WHISPER_PROMPT    words Whisper should expect, such as names
                                   and technical terms, to spell them right.

setup-transcriber.sh installs this with faster-whisper in its own virtual
environment. Decoding uses ffmpeg, which must be installed.
"""
import glob
import os
import site
import subprocess
import sys


def cuda_libs():
    # ctranslate2 needs the cuBLAS and cuDNN libraries installed from pip.
    paths = []
    for sp in site.getsitepackages():
        paths += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
    return paths


def decode(path):
    # Decode with ffmpeg rather than faster-whisper's own decoder, which fails
    # with some PyAV versions.
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def load_model():
    import ctranslate2
    from faster_whisper import WhisperModel
    name = os.environ.get("SIGNAL_SESSION_WHISPER_MODEL") or "large-v3"
    if ctranslate2.get_cuda_device_count() > 0:
        return WhisperModel(name, device="cuda", compute_type="float16")
    return WhisperModel(name, device="cpu", compute_type="int8")


def main():
    args = sys.argv[1:]
    download_only = "--download-only" in args
    args = [a for a in args if a != "--download-only"]
    if download_only:
        load_model()
        print("model ready")
        return
    if len(args) != 1:
        sys.exit("usage: transcribe.py [--download-only] <file>")
    model = load_model()
    segments, info = model.transcribe(
        decode(args[0]),
        language=os.environ.get("SIGNAL_SESSION_WHISPER_LANGUAGE") or None,
        initial_prompt=os.environ.get("SIGNAL_SESSION_WHISPER_PROMPT") or None,
        vad_filter=True, beam_size=5)
    lines = []
    for s in segments:
        if s.no_speech_prob > 0.6:  # background noise, not speech
            continue
        minutes, seconds = divmod(int(s.start), 60)
        lines.append(f"[{minutes}:{seconds:02d}] {s.text.strip()}")
    print(f"=== {os.path.basename(args[0])} ({info.duration:.0f} s, language {info.language})")
    print("\n".join(lines) or "(no speech found)")


if __name__ == "__main__":
    libs = cuda_libs()
    if libs and os.environ.get("_SIGNAL_SESSION_CUDA_LIBS") != "1":
        env = dict(os.environ, _SIGNAL_SESSION_CUDA_LIBS="1",
                   LD_LIBRARY_PATH=":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")]))
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    main()
