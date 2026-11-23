#!/usr/bin/env bash
# Optional: set up local transcription of voice notes and the sound of videos.
# Installs faster-whisper in its own virtual environment, copies transcribe.py
# next to signal-session.py, downloads the model, and records the command in
# ~/.config/signal-session/config.json. Run setup-daemon.sh first.
#
#   setup-transcriber.sh [--cuda] [--model <name>]
#
# --cuda also installs NVIDIA's cuBLAS and cuDNN libraries from pip, for a
# machine with an NVIDIA GPU. Without a GPU, transcription runs on the CPU.
# --model picks the Whisper model to download now, default large-v3, about
# 3 GB. On a slow CPU, small or medium is quicker and less accurate.
set -o errexit -o nounset -o pipefail

cuda=0
model="large-v3"
while [ $# -gt 0 ]; do
  case "$1" in
    --cuda) cuda=1; shift ;;
    --model) model="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

config="$HOME/.config/signal-session/config.json"
[ -f "$config" ] || { echo "no $config: run setup-daemon.sh first" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is required: install it, for example with: sudo apt install ffmpeg" >&2; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "Python's venv module is missing: install it, for example with: sudo apt install python3-venv" >&2
  exit 1
}

venv="$HOME/.local/share/signal-session/whisper-venv"
lib_dir="$HOME/.local/lib/signal-session"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet faster-whisper numpy
if [ "$cuda" = 1 ]; then
  "$venv/bin/pip" install --quiet nvidia-cublas-cu12 nvidia-cudnn-cu12
fi
echo "installed faster-whisper in $venv"

mkdir --parents "$lib_dir"
install --mode=644 "$script_dir/transcribe.py" "$lib_dir/transcribe.py"
echo "installed $lib_dir/transcribe.py"

echo "downloading the $model model, if it is not already downloaded"
SIGNAL_SESSION_WHISPER_MODEL="$model" "$venv/bin/python" "$lib_dir/transcribe.py" --download-only

command_line="env SIGNAL_SESSION_WHISPER_MODEL=$model $venv/bin/python $lib_dir/transcribe.py"
umask 077
python3 -I - "$config" "$command_line" <<'EOF'
import json, os, sys
path, command = sys.argv[1:3]
with open(path) as f:
    config = json.load(f)
config["transcribe_command"] = command
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
EOF
echo "recorded transcribe_command in $config:"
echo "  $command_line"
echo "To expect particular words or one language, add SIGNAL_SESSION_WHISPER_PROMPT=... or"
echo "SIGNAL_SESSION_WHISPER_LANGUAGE=... after 'env' in that command in $config."
