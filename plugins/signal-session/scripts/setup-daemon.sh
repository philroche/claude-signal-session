#!/usr/bin/env bash
# Write the signal-session config and install two systemd user services: the
# signal-cli daemon, and the spool that records the owner's messages. Run after
# the bot number is registered and verified with signal-cli. Run it again after
# updating the plugin, so the spool service uses the new script.
#
#   setup-daemon.sh --bot-account +<country code><number> --owner-number +<country code><number>
#                   [--signal-cli <path>] [--transcribe-command '<command>']
#
# The config and the services are machine-local on purpose: the Signal account
# keys live in ~/.local/share/signal-cli on one machine only.
set -o errexit -o nounset -o pipefail

bot_account=""
owner_number=""
signal_cli=""
transcribe_command=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bot-account) bot_account="$2"; shift 2 ;;
    --owner-number) owner_number="$2"; shift 2 ;;
    --signal-cli) signal_cli="$2"; shift 2 ;;
    --transcribe-command) transcribe_command="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for value in "$bot_account" "$owner_number"; do
  if [[ ! "$value" =~ ^\+[0-9]{6,15}$ ]]; then
    echo "both --bot-account and --owner-number are required, in +<country code><number> form" >&2
    exit 2
  fi
done

command -v systemctl >/dev/null || { echo "systemd is required: this setup installs systemd user services" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

if [ -z "$signal_cli" ]; then
  signal_cli="$(command -v signal-cli || true)"
fi
[ -n "$signal_cli" ] && [ -x "$signal_cli" ] || {
  echo "signal-cli not found; install it (https://github.com/AsamK/signal-cli) or pass --signal-cli <path>" >&2
  exit 1
}
# Keep the path as given, not where a link points, so an upgrade only has to
# move the link.
signal_cli="$(cd "$(dirname "$signal_cli")" && pwd)/$(basename "$signal_cli")"
if ! "$signal_cli" --account "$bot_account" listIdentities >/dev/null 2>&1; then
  echo "signal-cli has no usable registration for $bot_account; register and verify it first." >&2
  echo "If the daemon is already running, stop it first: systemctl --user stop signal-cli-daemon" >&2
  exit 1
fi

config_dir="$HOME/.config/signal-session"
mkdir --parents --mode=700 "$config_dir"
umask 077
python3 -I - "$config_dir/config.json" "$bot_account" "$owner_number" "$transcribe_command" <<'EOF'
import json, sys
path, bot, owner, transcribe = sys.argv[1:5]
config = {"bot_account": bot, "owner_number": owner}
if transcribe:
    config["transcribe_command"] = transcribe
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
EOF
echo "wrote $config_dir/config.json"

# The plugin's own folder changes with every plugin update, so the spool
# service runs a copy of the script from a fixed place.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lib_dir="$HOME/.local/lib/signal-session"
mkdir --parents "$lib_dir"
install --mode=644 "$script_dir/signal-session.py" "$lib_dir/signal-session.py"
echo "installed $lib_dir/signal-session.py"

unit_dir="$HOME/.config/systemd/user"
mkdir --parents "$unit_dir"
umask 022
# --no-receive-stdout keeps message contents out of the journal.
# --send-read-receipts shows read ticks on the phone, so a message is visibly received.
cat > "$unit_dir/signal-cli-daemon.service" <<EOF
[Unit]
Description=signal-cli daemon for the signal-session Claude Code plugin
After=network-online.target

[Service]
ExecStart=$signal_cli --account $bot_account daemon --socket %t/signal-cli/socket --receive-mode on-start --no-receive-stdout --send-read-receipts
RuntimeDirectory=signal-cli
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
echo "wrote $unit_dir/signal-cli-daemon.service"

# The spool service stays connected to the daemon and writes the owner's
# messages to ~/.local/state/signal-session/spool.jsonl. Listeners read that
# file, so a message sent while no listener is running is not lost: the daemon
# only passes a message to the clients connected at that moment.
cat > "$unit_dir/signal-session-spool.service" <<EOF
[Unit]
Description=Spool of Signal messages for the signal-session Claude Code plugin
Wants=signal-cli-daemon.service
After=signal-cli-daemon.service

[Service]
ExecStart=$(command -v python3) -I %h/.local/lib/signal-session/signal-session.py spool
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
echo "wrote $unit_dir/signal-session-spool.service"

systemctl --user daemon-reload
systemctl --user enable --now signal-cli-daemon.service
systemctl --user enable signal-session-spool.service
systemctl --user restart signal-session-spool.service
for _ in $(seq 1 30); do
  [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id --user)}/signal-cli/socket" ] && break
  sleep 1
done
systemctl --user --no-pager status signal-cli-daemon.service | head --lines=5
systemctl --user --no-pager status signal-session-spool.service | head --lines=5
