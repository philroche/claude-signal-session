#!/usr/bin/env python3
"""Talk to a running Claude Code session over Signal, through the signal-cli daemon.

Each Claude Code session gets its own Signal group, containing the owner and the
bot. The owner talks to a session by writing in that session's group.

Subcommands:
  check                          Confirm the daemon answers and print the accounts.
  open --session-id S --name N   Reuse the session's group, or a group with the same
                                 name, while the owner and the bot are both still
                                 in it; otherwise create one. Prints the group id.
  spool                          Stay connected to the daemon and append every
                                 message from the owner to the spool file. Runs as
                                 the signal-session-spool systemd user service.
  listen --group-id G [--once]   Print one JSON line per message from the owner in
                                 that group. With --once, exit after the first
                                 batch, for a background command that wakes the
                                 session only when something arrives; without it,
                                 run until killed. Reads the spool file from where
                                 the last listener for the group stopped, so a
                                 restarted listener also gets messages that came
                                 in while none was running. Falls back to reading
                                 the daemon directly when the spool service is not
                                 running.
  send --group-id G              Send stdin (or --message) to the group, with any
                                 --attachment files on the first message.
  close --group-id G             Send --message (if given), leave the group and
                                 forget it.

Configuration is read from ~/.config/signal-session/config.json (or
$SIGNAL_SESSION_CONFIG):
  {"bot_account": "+<country code><number>", "owner_number": "+<country code><number>",
   "socket": "<optional path>", "data_dir": "<optional, default ~/.local/share/signal-cli>",
   "transcribe_command": "<optional command that prints a transcript of the audio file given as its argument>"}
The session-to-group record is kept next to it in groups.json.

The spool file, its heartbeat and each group's read position are kept in
~/.local/state/signal-session/ (or $SIGNAL_SESSION_STATE), readable by this
user only. Only the owner's messages are written to the spool.

The daemon listens on a UNIX socket under $XDG_RUNTIME_DIR, which only this user
can open, so no other local user can read messages or send as the bot.

Standard library only, so it can run with `python3 -I`.
"""

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timezone

CONFIG_PATH = os.path.expanduser(
    os.environ.get("SIGNAL_SESSION_CONFIG", "~/.config/signal-session/config.json")
)
GROUPS_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "groups.json")
# Signal limits group names to 32 characters.
MAX_GROUP_NAME = 32
# Signal sends long text as an attachment; keep each message well under that size.
MAX_CHUNK = 1800
RECONNECT_SECONDS = 5
STATE_DIR = os.path.expanduser(
    os.environ.get("SIGNAL_SESSION_STATE", "~/.local/state/signal-session")
)
SPOOL_PATH = os.path.join(STATE_DIR, "spool.jsonl")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "spool.heartbeat")
OFFSETS_DIR = os.path.join(STATE_DIR, "offsets")
# The spool service touches its heartbeat this often; a listener treats the
# service as stopped when the heartbeat is older than HEARTBEAT_STALE_SECONDS.
HEARTBEAT_SECONDS = 15
HEARTBEAT_STALE_SECONDS = 60
# A group's saved read position older than this is ignored, so a group reused
# days later does not replay old messages; reading starts at the end instead.
OFFSET_MAX_AGE_SECONDS = 2 * 60 * 60
# The spool file is moved aside when it passes this size, at service start.
SPOOL_MAX_BYTES = 10 * 1024 * 1024
POLL_SECONDS = 0.5


def fail(message):
    print(f"signal-session: {message}", file=sys.stderr)
    sys.exit(1)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        fail(f"no config at {CONFIG_PATH}; see the signal-session skill setup steps")
    except json.JSONDecodeError as e:
        fail(f"config at {CONFIG_PATH} is not valid JSON: {e}")
    for key in ("bot_account", "owner_number"):
        if not config.get(key, "").startswith("+"):
            fail(f"config key {key!r} must be a number in +<country code> form")
    return config


def load_groups():
    try:
        with open(GROUPS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_groups(groups):
    tmp = GROUPS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(groups, f, indent=2, sort_keys=True)
    os.replace(tmp, GROUPS_PATH)


def socket_path(config):
    if config.get("socket"):
        return os.path.expanduser(config["socket"])
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, "signal-cli", "socket")


def digits(number):
    return re.sub(r"\D", "", number or "")


def connect(config, timeout=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path(config))
    return sock


def rpc(config, method, params=None, timeout=30):
    """Send one JSON-RPC request on a fresh connection and return its result.

    The daemon may push "receive" notifications on any connection, so lines
    are read until the one carrying our request id arrives.
    """
    request_id = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    with connect(config, timeout=timeout) as sock:
        sock.sendall((json.dumps(request) + "\n").encode())
        for line in sock.makefile("rb"):
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise RuntimeError(f"signal-cli {method} failed: {reply['error']}")
            return reply.get("result")
    raise RuntimeError(f"signal-cli closed the connection before answering {method}")


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def cmd_check(config, _args):
    try:
        version = rpc(config, "version", timeout=10)
    except (OSError, RuntimeError) as e:
        fail(f"signal-cli daemon not reachable at {socket_path(config)}: {e}")
    emit({
        "daemon": "ok",
        "socket": socket_path(config),
        "bot_account": config["bot_account"],
        "owner_number": config["owner_number"],
        "signal_cli": version,
        "transcribe_command": config.get("transcribe_command") or None,
    })


def member_numbers(group):
    numbers = []
    for member in group.get("members") or []:
        numbers.append(member.get("number") if isinstance(member, dict) else member)
    return {digits(n) for n in numbers if n}


def owner_still_in_group(config, group_id):
    """True when the bot is still in the group and the owner is a member of it."""
    groups = rpc(config, "listGroups", {"groupId": [group_id], "detailed": True}) or []
    for group in groups:
        if group.get("id") != group_id:
            continue
        if group.get("isMember") is False:
            return False
        return digits(config["owner_number"]) in member_numbers(group)
    return False


def clean_group_name(name):
    return re.sub(r"\s+", " ", name or "").strip()[:MAX_GROUP_NAME]


def live_group_named(config, name, groups):
    """The id of a group with this name that the bot and the owner are both
    still in, or None. Groups recorded in groups.json come first, so of two
    groups with one name, the one a session used last is chosen."""
    owner = digits(config["owner_number"])
    live = []
    for group in rpc(config, "listGroups", {"detailed": True}) or []:
        if (group.get("name") == name and group.get("isMember") is not False
                and not group.get("isTerminated") and owner in member_numbers(group)):
            live.append(group["id"])
    recorded = [v["group_id"] for v in sorted(groups.values(), key=lambda v: v.get("created_at", ""),
                                              reverse=True)]
    for group_id in recorded + live:
        if group_id in live:
            return group_id
    return None


def find_or_create_group(config, session_id, name, description=""):
    """Return (group_id, how) for the session: how is "session" when the
    session's own group is reused, "name" when a group with the same name is
    reused, and "created" for a new group. The group is recorded under this
    session, and other sessions' records of it are removed, so only one
    session claims a group."""
    name = clean_group_name(name)
    if not name:
        raise RuntimeError("the group name must not be empty")
    groups = load_groups()
    known = groups.get(session_id)
    if known and owner_still_in_group(config, known["group_id"]):
        return known["group_id"], "session"
    group_id = live_group_named(config, name, groups)
    how = "name"
    if not group_id:
        result = rpc(config, "updateGroup", {
            "name": name,
            "description": description or "",
            "member": [config["owner_number"]],
        })
        group_id = (result or {}).get("groupId")
        if not group_id:
            raise RuntimeError(f"signal-cli did not return a group id: {result!r}")
        how = "created"
    for key in [k for k, v in groups.items() if v.get("group_id") == group_id]:
        del groups[key]
    groups[session_id] = {
        "group_id": group_id,
        "name": name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_groups(groups)
    return group_id, how


def cmd_open(config, args):
    name = clean_group_name(args.name)
    try:
        group_id, how = find_or_create_group(config, args.session_id, name, args.description)
    except (OSError, RuntimeError) as e:
        fail(f"could not open the session's group: {e}")
    emit({"group_id": group_id, "name": name, "created": how == "created", "reused": how})


def attachment_dir(config):
    data_dir = os.path.expanduser(config.get("data_dir") or "~/.local/share/signal-cli")
    return os.path.join(data_dir, "attachments")


def describe_attachments(config, data):
    """List the message's attachments with the local path signal-cli saved each one to."""
    described = []
    for attachment in data.get("attachments") or []:
        attachment_id = attachment.get("id") or ""
        # Ids are file names in signal-cli's attachment directory; refuse anything else.
        if not attachment_id or "/" in attachment_id or attachment_id.startswith("."):
            continue
        path = os.path.join(attachment_dir(config), attachment_id)
        described.append({
            "path": path,
            "downloaded": os.path.isfile(path),
            "content_type": attachment.get("contentType"),
            "filename": attachment.get("filename"),
            "size": attachment.get("size"),
            "caption": attachment.get("caption"),
            "voice_note": bool(attachment.get("isVoiceNote")),
        })
    return described


def owner_group_message(config, envelope):
    """Return the event for a message from the owner in any group, or None.

    The event carries the group id; listeners pick out their own group.
    """
    sender = envelope.get("sourceNumber") or envelope.get("source")
    if digits(sender) != digits(config["owner_number"]):
        return None
    data = envelope.get("dataMessage") or {}
    group_id = (data.get("groupInfo") or {}).get("groupId")
    if not group_id:
        return None
    text = (data.get("message") or "").strip()
    attachments = describe_attachments(config, data)
    if not text and not attachments:
        return None
    sent_at = datetime.fromtimestamp(
        envelope.get("timestamp", 0) / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"group_id": group_id, "from": "owner", "sent_at": sent_at, "text": text}
    if attachments:
        event["attachments"] = attachments
    return event


def for_listener(event):
    """The event as a listener prints it: without the group id, and with each
    attachment's downloaded flag checked again, since signal-cli may have
    finished saving the file after the spool recorded the message."""
    out = {k: v for k, v in event.items() if k != "group_id"}
    if out.get("attachments"):
        out["attachments"] = [dict(a, downloaded=os.path.isfile(a.get("path") or ""))
                              for a in out["attachments"]]
    return out


def find_envelope(message):
    params = message.get("params") or {}
    return params.get("envelope") or (params.get("result") or {}).get("envelope")


def daemon_events(config, on_status):
    """Yield the event of every owner message in any group, reconnecting to the
    daemon whenever the connection drops. Status changes go to on_status."""
    connected_before = False
    reported_down = False
    while True:
        try:
            sock = connect(config)
        except OSError as e:
            if not reported_down:
                on_status({"status": "daemon_unreachable", "detail": str(e),
                           "retrying_every_seconds": RECONNECT_SECONDS})
                reported_down = True
            time.sleep(RECONNECT_SECONDS)
            continue
        if reported_down or connected_before:
            on_status({"status": "reconnected"})
        reported_down = False
        connected_before = True
        try:
            for line in sock.makefile("rb"):
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("method") != "receive":
                    continue
                event = owner_group_message(config, find_envelope(message) or {})
                if event is not None:
                    yield event
            on_status({"status": "connection_closed_by_daemon",
                       "retrying_every_seconds": RECONNECT_SECONDS})
        except OSError as e:
            on_status({"status": "connection_lost", "detail": str(e),
                       "retrying_every_seconds": RECONNECT_SECONDS})
        finally:
            sock.close()
        reported_down = True
        time.sleep(RECONNECT_SECONDS)


def ensure_state_dirs():
    os.makedirs(OFFSETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def spool_is_running():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_PATH) < HEARTBEAT_STALE_SECONDS
    except OSError:
        return False


def cmd_spool(config, _args):
    ensure_state_dirs()
    try:
        if os.path.getsize(SPOOL_PATH) > SPOOL_MAX_BYTES:
            os.replace(SPOOL_PATH, SPOOL_PATH + ".1")
    except FileNotFoundError:
        pass
    fd = os.open(SPOOL_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    spool = os.fdopen(fd, "a", encoding="utf-8")
    last_beat = [0.0]

    def beat():
        now = time.time()
        if now - last_beat[0] >= HEARTBEAT_SECONDS:
            with open(HEARTBEAT_PATH, "w") as f:
                f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ\n"))
            os.chmod(HEARTBEAT_PATH, 0o600)
            last_beat[0] = now

    def write(record):
        record["spooled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        spool.write(json.dumps(record, ensure_ascii=False) + "\n")
        spool.flush()
        os.fsync(spool.fileno())

    # The heartbeat must keep going while the daemon connection is quiet, so it
    # runs on its own thread.
    import threading

    def heartbeat_loop():
        while True:
            beat()
            time.sleep(HEARTBEAT_SECONDS)

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    # Daemon status lines carry no group id, so every listener passes them on.
    for event in daemon_events(config, write):
        write(event)


def offset_path(group_id):
    name = hashlib.sha256(group_id.encode()).hexdigest()[:32]
    return os.path.join(OFFSETS_DIR, name)


def load_offset(group_id, spool_size):
    path = offset_path(group_id)
    try:
        if time.time() - os.path.getmtime(path) > OFFSET_MAX_AGE_SECONDS:
            return spool_size
        with open(path) as f:
            offset = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return spool_size
    # A smaller spool than the saved position means it was moved aside.
    return offset if offset <= spool_size else 0


def save_offset(group_id, offset):
    path = offset_path(group_id)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(str(offset))
    os.replace(tmp, path)


def listen_direct(config, group_id, once=False):
    for event in daemon_events(config, emit):
        if event.get("group_id") == group_id:
            emit(for_listener(event))
            if once:
                return


def listen_spool(config, group_id, once=False):
    """Follow the spool for this group. With once, return after the first
    read that delivered anything, so a background command can wake the
    session only when there is something to act on."""
    ensure_state_dirs()
    if not os.path.exists(SPOOL_PATH):
        os.close(os.open(SPOOL_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    offset = load_offset(group_id, os.path.getsize(SPOOL_PATH))
    save_offset(group_id, offset)
    spool_up = True
    while True:
        delivered = False
        try:
            size = os.path.getsize(SPOOL_PATH)
        except FileNotFoundError:
            size = 0
        if size < offset:
            offset = 0
        if size > offset:
            with open(SPOOL_PATH, "rb") as f:
                f.seek(offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # a line still being written; read it next time
                    offset += len(raw)
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "status" in record:
                        emit({k: v for k, v in record.items() if k != "spooled_at"})
                        delivered = True
                    elif record.get("group_id") == group_id:
                        record.pop("spooled_at", None)
                        emit(for_listener(record))
                        delivered = True
                    save_offset(group_id, offset)
            save_offset(group_id, offset)
        running = spool_is_running()
        if running != spool_up:
            emit({"status": "spool_running" if running else "spool_not_running",
                  "detail": "" if running else
                  "systemctl --user status signal-session-spool"})
            spool_up = running
            delivered = True
        if once and delivered:
            return
        time.sleep(POLL_SECONDS)


def cmd_listen(config, args):
    if spool_is_running():
        listen_spool(config, args.group_id, once=args.once)
    else:
        emit({"status": "spool_not_running", "mode": "direct",
              "detail": "reading the daemon directly; messages sent while this "
                        "listener restarts can be lost"})
        listen_direct(config, args.group_id, once=args.once)


def chunks(text, limit=MAX_CHUNK):
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current.strip():
        parts.append(current)
    return [p.rstrip() for p in parts]


def send_to_group(config, group_id, text, attachments=()):
    parts = chunks(text) if text else [""]
    for index, part in enumerate(parts, start=1):
        body = f"({index}/{len(parts)}) {part}" if len(parts) > 1 else part
        params = {"groupId": group_id, "message": body}
        if attachments and index == 1:
            params["attachment"] = list(attachments)
        try:
            # Uploading attachments can take a while on a slow connection.
            # Plain text sends have also taken over 60 seconds to be
            # answered (8 October 2026), so a shorter limit reports a
            # send as failed that then goes through, and a retry doubles it.
            rpc(config, "send", params, timeout=300 if attachments else 120)
        except (OSError, RuntimeError) as e:
            fail(f"send failed on part {index} of {len(parts)}: {e}")
    return len(parts)


def cmd_send(config, args):
    attachments = [os.path.abspath(os.path.expanduser(a)) for a in args.attachment]
    for path in attachments:
        if not os.path.isfile(path):
            fail(f"attachment not found: {path}")
    if args.message is not None:
        text = args.message
    elif attachments and sys.stdin.isatty():
        text = ""
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text and not attachments:
        fail("nothing to send")
    parts = send_to_group(config, args.group_id, text, attachments)
    emit({"sent_parts": parts, "attachments": len(attachments)})


def cmd_close(config, args):
    if args.message:
        send_to_group(config, args.group_id, args.message.strip())
    try:
        # The bot created the group, so it is its only admin, and signal-cli
        # refuses to let the last admin leave. Hand the admin role to the owner.
        rpc(config, "quitGroup", {"groupId": args.group_id, "delete": True,
                                  "admin": [config["owner_number"]]})
    except (OSError, RuntimeError) as e:
        fail(f"could not leave the group: {e}")
    groups = load_groups()
    forgotten = [key for key, value in groups.items() if value.get("group_id") == args.group_id]
    for key in forgotten:
        del groups[key]
    save_groups(groups)
    emit({"left_group": args.group_id, "forgot_sessions": forgotten})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("spool")
    open_ = sub.add_parser("open")
    open_.add_argument("--session-id", required=True)
    open_.add_argument("--name", required=True, help=f"group name, cut to {MAX_GROUP_NAME} characters")
    open_.add_argument("--description", help="group description shown on the phone")
    listen = sub.add_parser("listen")
    listen.add_argument("--group-id", required=True)
    listen.add_argument("--once", action="store_true",
                        help="exit after the first batch of events, for a background "
                             "command that wakes the session only when something arrives")
    send = sub.add_parser("send")
    send.add_argument("--group-id", required=True)
    send.add_argument("--message", help="text to send; read from stdin when omitted")
    send.add_argument("--attachment", action="append", default=[],
                      help="file to attach; repeat for several")
    close = sub.add_parser("close")
    close.add_argument("--group-id", required=True)
    close.add_argument("--message", help="last message to send before leaving")
    args = parser.parse_args()
    config = load_config()
    handlers = {"check": cmd_check, "open": cmd_open, "spool": cmd_spool, "listen": cmd_listen,
                "send": cmd_send, "close": cmd_close}
    handlers[args.command](config, args)


if __name__ == "__main__":
    main()
