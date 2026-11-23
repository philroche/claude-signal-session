#!/usr/bin/env python3
"""Tests for the signal-session plugin's scripts, against a fake signal-cli
daemon on a UNIX socket in a temporary folder. No Signal account is needed.

Run: python3 -I tests/test_plugin.py
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "plugins", "signal-session", "scripts")
SESSION = os.path.join(SCRIPTS, "signal-session.py")
CHANNEL = os.path.join(SCRIPTS, "signal-channel.py")
BOT = "+15550000001"
OWNER = "+15550000002"
STRANGER = "+15550000003"


class FakeDaemon:
    """Answers the JSON-RPC methods the scripts use, and can push "receive"
    notifications to every connected client, as signal-cli's daemon does."""

    def __init__(self, path):
        self.path = path
        self.groups = {}
        self.sent = []
        self.clients = []
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(conn)
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        try:
            for line in conn.makefile("rb"):
                request = json.loads(line)
                result = self.answer(request["method"], request.get("params") or {})
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            conn.close()

    def answer(self, method, params):
        if method == "version":
            return {"version": "fake"}
        if method == "updateGroup":
            group_id = base64.b64encode(os.urandom(32)).decode()
            self.groups[group_id] = {"id": group_id, "name": params["name"], "isMember": True,
                                     "isTerminated": False,
                                     "members": [{"number": BOT}] + [{"number": n} for n in params["member"]]}
            return {"groupId": group_id}
        if method == "listGroups":
            wanted = params.get("groupId")
            return [g for g in self.groups.values() if not wanted or g["id"] in wanted]
        if method == "send":
            self.sent.append(params)
            return {"timestamp": 1}
        if method == "quitGroup":
            self.groups[params["groupId"]]["isMember"] = False
            self.quit_admin = params.get("admin")
            return {}
        raise AssertionError(f"unexpected method {method}")

    def push(self, sender, group_id, text):
        envelope = {"sourceNumber": sender, "timestamp": int(time.time() * 1000),
                    "dataMessage": {"message": text, "groupInfo": {"groupId": group_id}}}
        line = json.dumps({"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope}}) + "\n"
        with self.lock:
            for conn in self.clients:
                conn.sendall(line.encode())

    def close(self):
        self.server.close()
        with self.lock:
            for conn in self.clients:
                conn.close()


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.daemon = FakeDaemon(os.path.join(self.tmp, "socket"))
        os.makedirs(os.path.join(self.tmp, "config"))
        self.config = os.path.join(self.tmp, "config", "config.json")
        with open(self.config, "w") as f:
            json.dump({"bot_account": BOT, "owner_number": OWNER,
                       "socket": os.path.join(self.tmp, "socket")}, f)
        self.state = os.path.join(self.tmp, "state")
        self.env = dict(os.environ, SIGNAL_SESSION_CONFIG=self.config, SIGNAL_SESSION_STATE=self.state)
        self.processes = []

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.wait()
            for stream in (p.stdin, p.stdout):
                if stream:
                    stream.close()
        self.daemon.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args):
        out = subprocess.run([sys.executable, "-I", SESSION, *args], env=self.env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def groups_file(self):
        with open(os.path.join(self.tmp, "config", "groups.json")) as f:
            return json.load(f)

    def start_spool(self):
        p = subprocess.Popen([sys.executable, "-I", SESSION, "spool"], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(p)
        heartbeat = os.path.join(self.state, "spool.heartbeat")
        for _ in range(100):
            if os.path.exists(heartbeat) and self.daemon.clients:
                return
            time.sleep(0.1)
        self.fail("the spool did not start")

    def wait_for(self, condition, what):
        for _ in range(100):
            if condition():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def test_check(self):
        self.assertEqual(self.run_script("check")["daemon"], "ok")

    def test_open_reuses_by_session_then_by_name(self):
        first = self.run_script("open", "--session-id", "a", "--name", "Claude:  my   project")
        self.assertEqual(first["reused"], "created")
        self.assertEqual(first["name"], "Claude: my project")
        again = self.run_script("open", "--session-id", "a", "--name", "Claude: my project")
        self.assertEqual((again["group_id"], again["reused"]), (first["group_id"], "session"))
        other = self.run_script("open", "--session-id", "b", "--name", "Claude: my project")
        self.assertEqual((other["group_id"], other["reused"]), (first["group_id"], "name"))
        self.assertEqual(list(self.groups_file()), ["b"])

    def test_open_creates_when_owner_left(self):
        first = self.run_script("open", "--session-id", "a", "--name", "Claude: x")
        self.daemon.groups[first["group_id"]]["members"] = [{"number": BOT}]
        second = self.run_script("open", "--session-id", "a", "--name", "Claude: x")
        self.assertEqual(second["reused"], "created")
        self.assertNotEqual(second["group_id"], first["group_id"])

    def test_send_splits_long_text(self):
        group = self.run_script("open", "--session-id", "a", "--name", "Claude: x")["group_id"]
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(40))
        self.assertEqual(self.run_script("send", "--group-id", group, "--message", text)["sent_parts"], 2)
        self.assertTrue(self.daemon.sent[0]["message"].startswith("(1/2) "))

    def test_close_leaves_and_forgets(self):
        group = self.run_script("open", "--session-id", "a", "--name", "Claude: x")["group_id"]
        self.run_script("close", "--group-id", group, "--message", "bye")
        self.assertFalse(self.daemon.groups[group]["isMember"])
        self.assertEqual(self.daemon.quit_admin, [OWNER])
        self.assertEqual(self.groups_file(), {})

    def test_spool_and_listen_once(self):
        group = self.run_script("open", "--session-id", "a", "--name", "Claude: x")["group_id"]
        self.start_spool()
        listener = subprocess.Popen([sys.executable, "-I", SESSION, "listen", "--once", "--group-id", group],
                                    env=self.env, stdout=subprocess.PIPE, text=True)
        self.processes.append(listener)
        time.sleep(1)
        self.daemon.push(STRANGER, group, "from someone else")
        self.daemon.push(OWNER, "another-group", "for another session")
        self.daemon.push(OWNER, group, "hello")
        out, _ = listener.communicate(timeout=20)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([e["text"] for e in events], ["hello"])
        self.assertEqual(events[0]["from"], "owner")
        with open(os.path.join(self.state, "spool.jsonl")) as f:
            spooled = [json.loads(line).get("text") for line in f]
        self.assertEqual(spooled, ["for another session", "hello"])

    def start_channel(self, as_channel):
        command = [sys.executable, "-I", CHANNEL]
        if as_channel:
            # The server checks its parent's command line for a channel option,
            # so run it under a parent that has one.
            command = [sys.executable, "-I", "-c",
                       "import subprocess, sys; sys.exit(subprocess.call(sys.argv[2:]))",
                       "--dangerously-load-development-channels", *command]
        p = subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        self.processes.append(p)
        return p

    def rpc(self, p, msg_id, method, params=None):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                                  "params": params or {}}) + "\n")
        p.stdin.flush()
        return self.read_message(p)

    def read_message(self, p):
        line = p.stdout.readline()
        self.assertTrue(line, "the channel server closed its output")
        return json.loads(line)

    def call(self, p, msg_id, tool, arguments):
        return self.rpc(p, msg_id, "tools/call", {"name": tool, "arguments": arguments})["result"]

    def test_channel_refuses_without_channel_option(self):
        p = self.start_channel(as_channel=False)
        init = self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertIn("claude/channel", init["result"]["capabilities"]["experimental"])
        result = self.call(p, 2, "signal_start", {"session_id": "a", "name": "Claude: x"})
        self.assertTrue(result.get("isError"))
        self.assertIn("--dangerously-load-development-channels", result["content"][0]["text"])

    def test_channel_pushes_and_replies(self):
        self.start_spool()
        p = self.start_channel(as_channel=True)
        self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18"})
        tools = [t["name"] for t in self.rpc(p, 2, "tools/list")["result"]["tools"]]
        self.assertEqual(tools, ["signal_start", "reply", "signal_stop"])
        started = json.loads(self.call(p, 3, "signal_start", {
            "session_id": "a", "name": "Claude: x", "opening_message": "Listening"})["content"][0]["text"])
        group = started["group_id"]
        self.assertEqual(started["reused"], "created")
        self.assertEqual(self.daemon.sent[-1]["message"], "Listening")
        time.sleep(1)
        self.daemon.push(STRANGER, group, "from someone else")
        self.daemon.push(OWNER, group, "hello channel")
        pushed = self.read_message(p)
        self.assertEqual(pushed["method"], "notifications/claude/channel")
        self.assertEqual(pushed["params"]["content"], "hello channel")
        self.assertEqual(pushed["params"]["meta"]["kind"], "message")
        self.call(p, 4, "reply", {"text": "got it"})
        self.assertEqual(self.daemon.sent[-1], {"groupId": group, "message": "got it"})
        stopped = json.loads(self.call(p, 5, "signal_stop", {"message": "Stopped"})["content"][0]["text"])
        self.assertEqual(stopped, {"listening": False, "left_group": False})
        self.assertTrue(self.daemon.groups[group]["isMember"])


@unittest.skipUnless(shutil.which("gpg"), "gpg is not installed")
class SignatureCheckTest(unittest.TestCase):
    """upgrade-signal-cli.sh must accept only the signal-cli maintainer's key.
    A good signature from the real key needs a real release, so that case is
    checked by hand; these check that everything else is refused."""

    UPGRADE = os.path.join(SCRIPTS, "upgrade-signal-cli.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.archive = os.path.join(self.tmp, "signal-cli-0.0.0-Linux-native.tar.gz")
        with open(self.archive, "wb") as f:
            f.write(b"not really signal-cli")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self):
        return subprocess.run([self.UPGRADE, "--check-signature", self.archive],
                              capture_output=True, text=True, timeout=60)

    def test_refuses_a_signature_from_another_key(self):
        home = os.path.join(self.tmp, "gnupg")
        os.mkdir(home, 0o700)
        gpg = ["gpg", "--homedir", home, "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", ""]
        subprocess.run(gpg + ["--quick-gen-key", "Someone Else <else@example.invalid>", "ed25519", "sign", "1d"],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(gpg + ["--armor", "--detach-sign", "--output", self.archive + ".asc", self.archive],
                       check=True, capture_output=True, timeout=60)
        out = self.check()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no good signature from the signal-cli maintainer's key", out.stderr)

    def test_refuses_without_a_signature_file(self):
        out = self.check()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("needs the archive and its .asc file", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
