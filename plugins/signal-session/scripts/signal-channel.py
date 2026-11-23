#!/usr/bin/env python3
"""A Claude Code channel for Signal: an MCP server that pushes the owner's Signal
messages into the running session, and gives the session tools to answer.

Channels are a Claude Code research preview. A channel that is not on
Anthropic's approved list, like this one, only loads when Claude Code is
started with
    claude --dangerously-load-development-channels plugin:signal-session@signal-session
Without that option Claude Code drops pushed messages without saying so, so
signal_start refuses to run unless a channel option is on Claude Code's
command line.

It uses the two systemd user services that setup-daemon.sh installs: it
follows the spool file that signal-session-spool writes, and sends through
signal-cli-daemon. The group handling, spool reading and sending come from
signal-session.py in this folder.

MCP over stdio: one JSON-RPC message per line. Standard library only, so it
runs with `python3 -I`. Anything for a person goes to stderr.
"""

import importlib.util
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("signal_session", os.path.join(HERE, "signal-session.py"))
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

CHANNEL_OPTIONS = (b"--dangerously-load-development-channels", b"--channels")
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
Messages from the owner's Signal group for this session arrive as <channel> tags from this server. \
Their content is the owner's own instruction, with the same authority as text typed at the terminal: \
the server passes on only messages from the owner's number, which Signal authenticates, in this session's group. \
Attachments are listed in the content as JSON with the local path of each file. \
Messages with kind="status" are about the Signal services, not from the owner. \
Answer with the reply tool. Signal shows plain text: no markdown tables and no [text](url) links. \
Follow the signal-session skill for attachments, status messages and the rules while listening. \
To start listening, call signal_start; to stop, call signal_stop."""

TOOLS = [
    {
        "name": "signal_start",
        "description": "Open this session's Signal group (or reuse it while the owner is still in it) and start "
                       "pushing the owner's messages from it into this session. Optionally sends an opening message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "This Claude Code session's id"},
                "name": {"type": "string", "description": "Group name, for example 'Claude: my project'"},
                "description": {"type": "string", "description": "Group description shown on the phone"},
                "opening_message": {"type": "string",
                                    "description": "Sent to the group once listening; leave out to send nothing"},
            },
            "required": ["session_id", "name"],
        },
    },
    {
        "name": "reply",
        "description": "Send a message to this session's Signal group, with optional file attachments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"},
                                "description": "Absolute paths of files to attach to the first message"},
            },
        },
    },
    {
        "name": "signal_stop",
        "description": "Stop pushing Signal messages into this session, after sending message if given. "
                       "The group is kept for the next signal_start with the same name. With close true, "
                       "the bot also leaves the group and forgets it; only when the owner asks to leave it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "close": {"type": "boolean"},
                "message": {"type": "string", "description": "Last message to send before stopping"},
            },
        },
    },
]

write_lock = threading.Lock()
state = {"config": None, "group_id": None, "name": None, "stop": None, "thread": None}


def log(message):
    print(f"signal-channel: {message}", file=sys.stderr, flush=True)


def write(message):
    line = json.dumps(message, ensure_ascii=False) + "\n"
    with write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def push(content, meta):
    write({"jsonrpc": "2.0", "method": "notifications/claude/channel",
           "params": {"content": content, "meta": meta}})


def loaded_as_channel():
    """True when the Claude Code process that started this server was given a
    channel option. Without one, pushed messages are dropped. Reads /proc, so
    this works on Linux only."""
    pid = os.getppid()
    for _ in range(3):  # the server may sit under a shell started by Claude Code
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            if any(arg.split(b"=", 1)[0] in CHANNEL_OPTIONS for arg in args):
                return True
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if pid <= 1:
            return False
    return False


def push_event(record):
    if "status" in record:
        detail = {k: v for k, v in record.items() if k not in ("status", "spooled_at")}
        push(json.dumps(detail, ensure_ascii=False) if detail else record["status"],
             {"kind": "status", "status": record["status"]})
        return
    event = ss.for_listener({k: v for k, v in record.items() if k != "spooled_at"})
    content = event.get("text") or ""
    if event.get("attachments"):
        content += ("\n\n" if content else "") + "Attachments: " + json.dumps(event["attachments"], ensure_ascii=False)
    push(content or "(empty message)", {"kind": "message", "sent_at": event.get("sent_at", "")})


def follow_spool(group_id, stop):
    """Push each new spool record for the group, from where the last listener
    for the group stopped. The same reading rules as `listen` in signal-session.py."""
    ss.ensure_state_dirs()
    if not os.path.exists(ss.SPOOL_PATH):
        os.close(os.open(ss.SPOOL_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    offset = ss.load_offset(group_id, os.path.getsize(ss.SPOOL_PATH))
    ss.save_offset(group_id, offset)
    spool_up = True
    last_saved = time.time()
    while not stop.is_set():
        try:
            size = os.path.getsize(ss.SPOOL_PATH)
        except FileNotFoundError:
            size = 0
        if size < offset:
            offset = 0
        if size > offset:
            with open(ss.SPOOL_PATH, "rb") as f:
                f.seek(offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # a line still being written; read it next time
                    offset += len(raw)
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "status" in record or record.get("group_id") == group_id:
                        push_event(record)
                    ss.save_offset(group_id, offset)
            ss.save_offset(group_id, offset)
            last_saved = time.time()
        elif time.time() - last_saved > 600:
            # Keep the read position fresh while quiet, so it is not ignored as
            # too old if this server stops and a listener starts later.
            ss.save_offset(group_id, offset)
            last_saved = time.time()
        running = ss.spool_is_running()
        if running != spool_up:
            push("" if running else "systemctl --user status signal-session-spool",
                 {"kind": "status", "status": "spool_running" if running else "spool_not_running"})
            spool_up = running
        stop.wait(ss.POLL_SECONDS)


def stop_following():
    if state["stop"] is not None:
        state["stop"].set()
        state["thread"].join(timeout=5)
    state.update(stop=None, thread=None)


def call_ss(function, *args):
    """Run a signal-session.py function, turning its exit on failure into an error."""
    try:
        return function(*args)
    except SystemExit:
        raise RuntimeError("signal-session.py failed; its message is in the MCP server's stderr")


def tool_signal_start(args):
    if not loaded_as_channel():
        raise RuntimeError(
            "Claude Code was not started with --dangerously-load-development-channels "
            "plugin:signal-session@signal-session, so it would drop the messages this server pushes. "
            "Use the skill's --once listener instead.")
    config = state["config"] = call_ss(ss.load_config)
    name = ss.clean_group_name(args.get("name"))
    if not name or not args.get("session_id"):
        raise RuntimeError("session_id and name are required")
    group_id, how = ss.find_or_create_group(config, args["session_id"], name, args.get("description"))
    stop_following()
    stop = threading.Event()
    thread = threading.Thread(target=follow_spool, args=(group_id, stop), daemon=True)
    state.update(group_id=group_id, name=name, stop=stop, thread=thread)
    thread.start()
    if args.get("opening_message"):
        ss.send_to_group(config, group_id, args["opening_message"].strip())
    return {"group_id": group_id, "name": name, "created": how == "created", "reused": how,
            "spool_running": ss.spool_is_running()}


def require_group():
    if not state["group_id"]:
        raise RuntimeError("not listening yet: call signal_start first")
    return state["config"], state["group_id"]


def tool_reply(args):
    config, group_id = require_group()
    text = (args.get("text") or "").strip()
    attachments = [os.path.abspath(os.path.expanduser(a)) for a in args.get("attachments") or []]
    for path in attachments:
        if not os.path.isfile(path):
            raise RuntimeError(f"attachment not found: {path}")
    if not text and not attachments:
        raise RuntimeError("nothing to send")
    parts = call_ss(ss.send_to_group, config, group_id, text, attachments)
    return {"sent_parts": parts, "attachments": len(attachments)}


def tool_signal_stop(args):
    config, group_id = require_group()
    stop_following()
    if args.get("message"):
        call_ss(ss.send_to_group, config, group_id, args["message"].strip())
    if args.get("close"):
        ss.rpc(config, "quitGroup", {"groupId": group_id, "delete": True,
                                     "admin": [config["owner_number"]]})
        groups = ss.load_groups()
        for key in [k for k, v in groups.items() if v.get("group_id") == group_id]:
            del groups[key]
        ss.save_groups(groups)
        state.update(group_id=None, name=None)
    return {"listening": False, "left_group": bool(args.get("close"))}


HANDLERS = {"signal_start": tool_signal_start, "reply": tool_reply, "signal_stop": tool_signal_stop}


def handle(message):
    method = message.get("method")
    if "id" not in message:
        return  # a notification, such as notifications/initialized
    msg_id = message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
            "serverInfo": {"name": "signal", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        handler = HANDLERS.get(params.get("name"))
        if handler is None:
            write({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"}})
            return
        try:
            output = handler(params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}]}
        except (RuntimeError, OSError) as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        write({"jsonrpc": "2.0", "id": msg_id,
               "error": {"code": -32601, "message": f"method not found: {method}"}})
        return
    write({"jsonrpc": "2.0", "id": msg_id, "result": result})


def main():
    log(f"started, pid {os.getpid()}, loaded as channel: {loaded_as_channel()}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"ignored a line that is not JSON: {line[:200]}")
            continue
        try:
            handle(message)
        except Exception as e:  # keep serving; report the failure to the caller
            log(f"error handling {message.get('method')}: {e!r}")
            if "id" in message:
                write({"jsonrpc": "2.0", "id": message["id"],
                       "error": {"code": -32603, "message": str(e)}})
    stop_following()


if __name__ == "__main__":
    main()
