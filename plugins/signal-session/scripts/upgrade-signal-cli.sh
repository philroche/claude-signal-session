#!/usr/bin/env bash
# Upgrade the signal-cli native build that the signal-cli-daemon service runs.
#
#   upgrade-signal-cli.sh [--version <x.y.z>] [--dry-run] [--skip-signature-check]
#   upgrade-signal-cli.sh --check-signature <archive>   (checks <archive>.asc only)
#
# The signal-cli project says releases older than three months may stop
# working with Signal's servers, and advises backing up the account data before
# upgrading, because a new version may migrate it and an older version may then
# refuse it. So this script:
#   1. finds the signal-cli the daemon service runs, which must be a link (as
#      the setup steps install it), and its version
#   2. downloads the new release (default: the latest) into its own folder
#      under ~/.local/opt, checks that the maintainer's key signed it, and
#      checks that it runs. The key is in this plugin's keys/ folder, and only
#      that key is accepted. Without a good signature from it, the script
#      stops, unless --skip-signature-check is given.
#   3. stops both services, copies ~/.local/share/signal-cli to a backup folder
#   4. moves the link to the new version, starts both services, and checks the
#      daemon answers
#   5. if the check fails: restores the backup, moves the link back, and starts
#      the old version again
# The old version's folder and the backup are kept; the script prints both.
set -o errexit -o nounset -o pipefail

version=""
dry_run=0
skip_signature=0
check_only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="${2#v}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --skip-signature-check) skip_signature=1; shift ;;
    --check-signature) check_only="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The signal-cli maintainer's signing key, AsamK <asamk@gmx.de>. The same key
# signs the release tags on GitHub. Exported from keys.openpgp.org on
# 23 November 2026.
maintainer_fingerprint="FA10826A74907F9EC6BBB7FC2BA2CD21B5B09570"
maintainer_key_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/keys/signal-cli-maintainer.asc"
unit="$HOME/.config/systemd/user/signal-cli-daemon.service"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/signal-cli"
socket="${XDG_RUNTIME_DIR:-/run/user/$(id --user)}/signal-cli/socket"
check_script="$HOME/.local/lib/signal-session/signal-session.py"

say() { echo "upgrade-signal-cli: $*"; }
die() { echo "upgrade-signal-cli: $*" >&2; exit 1; }

# check_signature <archive> <signature file>: succeeds only when the
# maintainer's key made a good signature of the archive.
check_signature() {
  local home signer
  # A keyring holding only the maintainer's key, so no other key the user has
  # can make the check pass.
  home="$(mktemp --directory)"
  gpg --homedir "$home" --quiet --import "$maintainer_key_file" 2>/dev/null
  # VALIDSIG's last field is the fingerprint of the primary key that signed.
  signer="$(gpg --homedir "$home" --status-fd 1 --verify "$2" "$1" 2>/dev/null \
    | awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" {print $NF}')" || true
  rm -rf "$home"
  if [ "$signer" != "$maintainer_fingerprint" ]; then
    echo "upgrade-signal-cli: $(basename "$1") has no good signature from the signal-cli maintainer's key $maintainer_fingerprint: either the file was changed, or another key signed it. If the maintainer has changed keys, update keys/signal-cli-maintainer.asc in the plugin" >&2
    return 1
  fi
  say "signature checked: $(basename "$1") is signed by the signal-cli maintainer's key $maintainer_fingerprint"
}

if [ -n "$check_only" ]; then
  [ -f "$check_only" ] && [ -f "$check_only.asc" ] || die "--check-signature needs the archive and its .asc file next to it"
  check_signature "$check_only" "$check_only.asc"
  exit
fi

[ "$(uname --machine)" = "x86_64" ] || die "the native build is for x86_64 Linux only; on $(uname --machine), install the Java build from https://github.com/AsamK/signal-cli/releases by hand"
[ -f "$unit" ] || die "no $unit: run setup-daemon.sh first"
command -v curl >/dev/null || die "curl is required"
if [ "$skip_signature" = 0 ]; then
  command -v gpg >/dev/null || die "gpg is required to check the download's signature (or pass --skip-signature-check)"
  [ -f "$maintainer_key_file" ] || die "the maintainer's key is missing from $maintainer_key_file"
fi

# The program the service runs: the first word after ExecStart=, with %h expanded.
link="$(sed --quiet 's/^ExecStart=\([^ ]*\).*/\1/p' "$unit" | head --lines=1)"
link="${link//%h/$HOME}"
[ -n "$link" ] || die "could not read the signal-cli path from $unit"
[ -L "$link" ] || die "$link is not a link, so this script can't switch versions; if a package manager installed signal-cli, upgrade it with that package manager"
old_target="$(readlink "$link")"
old_version="$("$link" --version 2>/dev/null | awk '{print $2}')"
[ -n "$old_version" ] || die "could not read the version of $link"

if [ -z "$version" ]; then
  version="$(curl --silent --fail --location --max-time 30 https://api.github.com/repos/AsamK/signal-cli/releases/latest \
    | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')" \
    || die "could not find the latest release on GitHub"
fi
say "installed: $old_version ($old_target)"
say "upgrading to: $version"
if [ "$version" = "$old_version" ]; then
  say "already at $version; nothing to do"
  exit 0
fi

new_dir="$HOME/.local/opt/signal-cli-$version-native"
backup="$data_dir.backup-$old_version-$(date +%Y%m%d-%H%M%S)"
if [ "$dry_run" = 1 ]; then
  say "dry run, nothing changed. The upgrade would:"
  say "  download https://github.com/AsamK/signal-cli/releases/download/v$version/signal-cli-$version-Linux-native.tar.gz"
  say "  unpack it into $new_dir"
  say "  stop signal-session-spool and signal-cli-daemon"
  say "  copy $data_dir to $backup"
  say "  point $link at $new_dir/signal-cli, start both services and check the daemon"
  exit 0
fi

work="$(mktemp --directory)"
trap 'rm -rf "$work"' EXIT
archive="signal-cli-$version-Linux-native.tar.gz"
base="https://github.com/AsamK/signal-cli/releases/download/v$version"
say "downloading $archive"
curl --silent --show-error --fail --location --output "$work/$archive" "$base/$archive"

if [ "$skip_signature" = 1 ]; then
  say "signature not checked: --skip-signature-check was given"
else
  curl --silent --show-error --fail --location --output "$work/$archive.asc" "$base/$archive.asc" \
    || die "could not download $archive.asc, so the signature can't be checked; not installing it"
  check_signature "$work/$archive" "$work/$archive.asc" || die "not installing $archive"
fi

mkdir --parents "$work/unpack"
tar --extract --gzip --file "$work/$archive" --directory "$work/unpack"
binary="$(find "$work/unpack" -type f -name signal-cli -perm -u+x | head --lines=1)"
[ -n "$binary" ] || die "no signal-cli program found in $archive"
"$binary" --version | grep --quiet --fixed-strings "$version" || die "the downloaded program does not report version $version"
mkdir --parents "$new_dir"
install --mode=755 "$binary" "$new_dir/signal-cli"
say "installed $new_dir/signal-cli"

daemon_answers() {
  for _ in $(seq 1 60); do
    [ -S "$socket" ] && break
    sleep 1
  done
  [ -f "$check_script" ] || { [ -S "$socket" ]; return; }
  for _ in $(seq 1 10); do
    python3 -I "$check_script" check >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

say "stopping signal-session-spool and signal-cli-daemon"
systemctl --user stop signal-session-spool signal-cli-daemon
say "backing up $data_dir to $backup"
rsync --archive "$data_dir/" "$backup/"
ln --symbolic --force --no-dereference "$new_dir/signal-cli" "$link"
systemctl --user start signal-cli-daemon signal-session-spool

if daemon_answers; then
  say "signal-cli $("$link" --version | awk '{print $2}') is running and the daemon answers"
  say "kept for going back: the old program at $old_target and the account data backup at $backup"
  say "delete both once the new version has worked for a few days"
  exit 0
fi

say "the daemon did not answer after the upgrade; going back to $old_version"
systemctl --user stop signal-session-spool signal-cli-daemon
rsync --archive --delete "$backup/" "$data_dir/"
ln --symbolic --force --no-dereference "$old_target" "$link"
systemctl --user start signal-cli-daemon signal-session-spool
if daemon_answers; then
  die "the upgrade failed and $old_version is running again. See: journalctl --user --unit signal-cli-daemon --since '-10min'"
fi
die "the upgrade failed and $old_version did not start either. See: journalctl --user --unit signal-cli-daemon --since '-10min'"
