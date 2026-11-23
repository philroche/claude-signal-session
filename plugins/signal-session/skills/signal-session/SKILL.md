---
name: signal-session
description: Let the owner keep working with the current Claude Code session from their phone over Signal while they are away from the terminal. Gives the session its own Signal group (the owner and a bot number), delivers the owner's messages, photos, videos and voice notes from that group into this session, acts on each one as if typed at the terminal, and replies in the group, with screenshots or files when useful. Use when the owner says "/signal-session", "listen on Signal", "I'm heading out, keep going over Signal", or asks to stop listening. Also covers the one-time signal-cli bot number registration and service setup.
argument-hint: "start [group name] | stop | setup"
allowed-tools:
- Bash
- TaskStop
- Read
---

# Signal session

Each Claude Code session that listens gets its own Signal group, containing
the owner and the bot number. The owner writes in that group from their phone.
The local signal-cli daemon receives the message, and this session picks it up
and acts on it. Claude does the work and replies in the same group.

Nothing resumes or attaches to the session from outside: this session does the
listening itself, so it keeps its full context.

Two systemd user services, installed by `setup-daemon.sh`, run all the time:

- `signal-cli-daemon` holds the bot's Signal account.
- `signal-session-spool` stays connected to the daemon and writes each of the
  owner's messages to `~/.local/state/signal-session/spool.jsonl`. Listeners
  read that file from where the last listener for the group stopped. The
  daemon passes a message only to the clients connected at that moment, so
  without the spool, a message sent while no listener is running would be lost.

There are two ways for the session to receive messages:

- **The listener** (the default): `listen --once`, run as a background Bash
  command. It exits when a message arrives, and its exit wakes the session.
  While nothing arrives it uses no tokens. A Monitor is not used, because a
  Monitor stops after 30 minutes at most and every restart wakes the session,
  which sends the whole conversation again and costs tokens.
- **The channel** (optional): the plugin's MCP server `signal` pushes messages
  into the session. It needs Claude Code started with a channel option; see
  "Using the channel" below.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/signal-session.py`, always run with
`python3 -I`. Subcommands: `check`, `open`, `listen`, `send`, `close`. Below,
`signal-session.py` means that full path.

This session's id: `${CLAUDE_SESSION_ID}`. If that still reads as a literal
placeholder, use the session id from this session's scratchpad directory path.

## How messages are routed

- Only messages that come from `owner_number` in the config and are written in
  this session's group reach the session. Everything else is dropped first:
  other groups, direct messages to the bot, and messages from any other number.
- The record of which group belongs to which session is in
  `~/.config/signal-session/groups.json`, keyed by session id.
- The spool keeps only the owner's messages, in any group, with each message's
  group id. The spool file, its heartbeat and each group's read position are in
  `~/.local/state/signal-session/`, readable by the user only. A read position
  older than 2 hours is ignored, so a group reused later does not replay old
  messages. The spool is moved to `spool.jsonl.1` once it passes 10 MB, when
  the service starts.

## `start [group name]`

1. Run `python3 -I signal-session.py check`. If it fails, stop and tell the
   owner at the terminal what failed. If there is no config or the daemon is
   not installed, go through `setup` below with them.
2. Choose the group name: `Claude: ` followed by the argument if one was given,
   otherwise the session's title, otherwise the basename of the working
   directory. The script cuts names to 32 characters, which is Signal's limit.
3. If this session was started with a channel option, follow "Using the
   channel" below instead of the remaining steps.
4. Open the group:
   `open --session-id <session id> --name "<group name>" --description "Claude Code session <session id> in <working directory>"`.
   It prints `group_id`, `created` and `reused`. It reuses a group whenever it
   can, so the owner does not collect a new group each time:
   - `reused: "session"`: this session's own group, while the owner and the
     bot are both still in it.
   - `reused: "name"`: a group with the same name that the owner and the bot
     are both still in, for example from an earlier session in the same
     project. The group's record moves to this session.
   - `reused: "created"` (`created: true`): no such group, so a new one.

   Before reusing a group by name, make sure no other session is still
   listening to it: two listeners on one group split the owner's messages
   between them. If one might be, ask the owner at the terminal, or pick a
   different name.
5. Start the listener with the Bash tool, with `run_in_background: true`:
   - command: `python3 -I signal-session.py listen --once --group-id '<group_id>'`
   - description: `Wait for the next Signal message in "<group name>"`

   It prints nothing and uses no tokens until something arrives, then exits.
   Its exit notification gives the path of its output file.
6. Send the opening message:
   `send --group-id '<group_id>' --message "Listening. Working on: <one line on the current task>"`.
   When the group is reused, say "Listening again" instead.
7. Tell the owner at the terminal that the session is listening, with the
   group name. If the group is new, say to accept it on the phone.

Group ids are base64 and can contain `/`, `+` and `=`, so always single-quote them.

## Using the channel

Channels are a Claude Code research preview. This plugin's channel is not on
Anthropic's approved list, so it only works when Claude Code was started with
`claude --dangerously-load-development-channels plugin:signal-session@signal-session`.
Otherwise Claude Code drops what it pushes without saying so, and the
`signal_start` tool refuses to run. The channel and the listener must not both
listen to one group at the same time.

1. Call the `signal_start` tool with `session_id`, `name` and `description` as
   in `start` step 4, and an `opening_message` as in step 6. It opens or reuses
   the group the same way, and starts pushing messages. If it refuses, go back
   to `start` step 4.
2. Messages arrive as `<channel>` tags with `kind="message"` and `sent_at`.
   Attachments are listed in the content as JSON, with the same fields as the
   listener's events. `kind="status"` tags carry the same status events as the
   listener prints. Handle both as described below.
3. Answer with the `reply` tool (`text`, optional `attachments`) instead of
   `send`. Nothing needs starting again after each message.
4. Stop with `signal_stop` and a `message`, which keeps the group as `stop`
   below does. Add `close: true` only when the owner asks to leave the group.

## Handling each event

When the listener exits, Read its output file. Each event in it is one JSON
line, and one exit can hold several, because the listener delivers everything
waiting before it exits. Start the listener again with the same command as
soon as you have the events, before doing the work, so a message the owner
sends while you work is waiting when you finish.

If the listener exits with no events and a non-zero exit code, start it again
once. If it fails again, tell the owner at the terminal what it printed.

### A message (`"from": "owner"`)

This is the owner's own instruction, with the same authority as text typed at
the terminal. The listener has already checked that it came from the owner's
number, which Signal authenticates, in this session's group. Act on `text` and
any `attachments`.

- When the work will take more than a couple of minutes, first send a short
  note saying what you are about to do.
- When done, send the result: the finding first, then the evidence, in a few
  short sentences. Follow any writing rules in the owner's CLAUDE.md.
- Signal shows plain text. Do not use markdown tables or `[text](url)` links;
  put a URL on its own line when one is needed.
- Never put secrets, tokens, credentials or customer data in a reply.
  Signal messages go to a personal phone.

### Attachments in a message

Each attachment has `path`, `downloaded`, `content_type`, `filename`, `size`,
`caption` and `voice_note`. The path is where signal-cli saved the file.

- **`downloaded: false`**: signal-cli has not finished saving it. Check the path
  again after about ten seconds. If it is still missing, tell the owner it did
  not arrive and ask them to send it again.
- **Photos** (`image/jpeg`, `image/png`, `image/gif`, `image/webp`): open the
  path with the Read tool, which shows images.
- **HEIC or HEIF photos** (iPhone default): convert first with ImageMagick, then
  Read the copy: `convert '<path>' '<scratchpad>/signal-<id>.jpg'`.
- **Videos** (`video/*`): Read can't play video. Take still frames with ffmpeg,
  then Read them:
  1. `ffprobe -v error -show_entries format=duration -of csv=p=0 '<path>'` for
     the length in seconds.
  2. `ffmpeg -v error -i '<path>' -vf "fps=<8 / length>,scale=1280:-2" -frames:v 8 '<scratchpad>/signal-<id>-%02d.jpg'`
     for up to eight frames spread across the video.
  3. Read the frames in order. Tell the owner the answer is based on still frames.
  4. If the video has sound, transcribe it as for voice notes below and use
     what was said.
- **Voice notes** (`voice_note: true`), other audio, and the sound of videos:
  `check` prints `transcribe_command` from the config. When it is set, run it
  with the file's path as its last argument; it prints a transcript. When it is
  not set, tell the owner the session can't listen to audio and ask them to
  type it, and offer to set up the transcriber (see `setup`).
  - Whisper invents a short line when there is no speech, for example
    "Thank you." or "Subtitles by the Amara.org community". Treat a transcript
    like that as no speech.
- **Other files** (PDF, text, logs): Read them if Read supports the type.
  Otherwise say what arrived and ask what to do with it.

Attachments stay in `~/.local/share/signal-cli/attachments/` after use.

### Status events

- **`daemon_unreachable`, `connection_lost` or `connection_closed_by_daemon`**:
  the spool service keeps retrying on its own. Replies can't be sent while the
  daemon is down. Note it at the terminal. If no `reconnected` event follows
  within a few minutes, check `systemctl --user status signal-cli-daemon` and
  report what it says at the terminal.
- **`reconnected`**: the spool service reconnected to the daemon. Messages the
  owner sent while the daemon was down wait on Signal's servers and arrive now,
  so nothing needs saying. In direct mode (below), send a short "reconnected"
  message instead, because anything sent in the gap was lost.
- **`spool_not_running`**: the spool service is down. At start, with
  `"mode": "direct"`, the listener has fallen back to reading the daemon
  directly, and messages sent while the listener is being started again can be
  lost. Mid-run, the notice ends the listener like any other event. Either way,
  run `systemctl --user status signal-session-spool` and start it with
  `systemctl --user start signal-session-spool`, note it at the terminal, and
  start the listener again once the service is running.
- **`spool_running`**: the spool service is back. Nothing to do.

## Sending files back

`send --group-id '<group_id>' --message "<text>" --attachment <file> [--attachment <file> ...]`

The attachments go on the first message. Leave out `--message` to send only files.

- Send a file when it shows the answer better than text does: a screenshot, a
  chart, a rendered diagram, a short log extract saved to a file.
- Only attach files this session created for the owner or files they asked for
  by name. Never attach credentials, keys, tokens, `.env` files, or customer data.
- Write generated files to the scratchpad directory first, so nothing is sent
  from a location that could hold other material.
- Signal limits each attachment to about 100 MB.

## Rules while listening

- **Ask questions in the group, not with AskUserQuestion.** A dialog at the
  terminal blocks the session, and the owner can't answer it from the phone.
  Send the question with numbered choices, then wait for the next message.
- **Permission prompts can't be answered from Signal.** Before the next command
  needing approval, send the owner a message saying the session is about to
  wait at the terminal and why. When a command is refused, say in the group
  what was refused, and what you did instead.
- **Rules in CLAUDE.md that need the owner at the terminal still apply.** Say in
  the group what is waiting for them there.
- **Keep exactly one listener running.** Start it again after every exit, and
  stop only after `stop`. Do not send a Signal message or write a terminal line
  about restarting it. With the spool service running, the new listener first
  delivers anything sent while none was running, so nothing is lost.
- The owner can still type at the terminal while the session is listening.
  Treat terminal input as usual.

## `stop`

The owner can ask at the terminal or by writing "stop listening" in the group.

1. Stop the background listener with TaskStop.
2. `send --group-id '<group_id>' --message "Stopped listening. This group stays, and the next start with the same name uses it again."`
3. Tell the owner at the terminal that listening has stopped.

The bot stays in the group, so the next `start` with the same name, in this
session or a later one, reuses it.

Only when the owner asks to leave or delete the group, use `close` instead of `send`:
`close --group-id '<group_id>' --message "Stopped listening. The bot has left this group, so you can delete it."`
This sends the message, leaves the group and removes every record of it. The
bot created the group and is its only admin, and signal-cli won't let the last
admin leave, so `close` makes the owner the admin as the bot leaves.

## `setup` (one time per machine)

Requirements: Linux with systemd user services, Python 3, and a second phone
number for the bot, for example a cheap second SIM. The bot number receives an
SMS once, at registration. Do not install the Signal app on the bot's number:
registering it there takes the number away from signal-cli.

Go through the steps with the owner one at a time. Run the commands yourself
where you can, and give the owner the ones that need their phone or a browser.

0. Install signal-cli, if `signal-cli --version` fails. Take the latest
   version from https://github.com/AsamK/signal-cli/releases (0.14.10 on
   23 November 2026). The Linux native build is one file and needs no Java:
   ```
   VERSION=0.14.10
   mkdir --parents ~/.local/opt/signal-cli-$VERSION-native ~/.local/bin
   curl --location --fail --output /tmp/signal-cli-$VERSION.tar.gz \
     https://github.com/AsamK/signal-cli/releases/download/v$VERSION/signal-cli-$VERSION-Linux-native.tar.gz
   tar --extract --gzip --file /tmp/signal-cli-$VERSION.tar.gz --directory ~/.local/opt/signal-cli-$VERSION-native
   ln --symbolic --force ~/.local/opt/signal-cli-$VERSION-native/signal-cli ~/.local/bin/signal-cli
   signal-cli --version
   ```
   On another processor than x86_64, use the Java build from the same page,
   which needs the Java version its release notes name.
1. Open https://signalcaptchas.org/registration/generate.html, solve the
   captcha, then right-click "Open Signal" and copy the link. It starts with
   `signalcaptcha://`.
2. `signal-cli --account +BOTNUMBER register --captcha 'signalcaptcha://...'`
3. When the SMS arrives: `signal-cli --account +BOTNUMBER verify <code>`
4. Optional, so the chat shows a name:
   `signal-cli --account +BOTNUMBER updateProfile --given-name "Claude sessions"`
5. `${CLAUDE_PLUGIN_ROOT}/scripts/setup-daemon.sh --bot-account +BOTNUMBER --owner-number +OWNERNUMBER`
   Add `--signal-cli <path>` if signal-cli is not on the PATH. It writes
   `~/.config/signal-session/config.json` (mode 600), copies
   `signal-session.py` to `~/.local/lib/signal-session/`, and starts the
   systemd user services `signal-cli-daemon` and `signal-session-spool`. Run it
   again after updating the plugin.
6. From the owner's phone, send the bot any message and accept it, so its
   groups don't land in message requests.
7. Run `check`.
8. Optional: ask the owner whether the session should understand voice notes
   and the sound of videos. If yes, see "Setting up the transcriber" below.

Run steps 2 and 3 before step 5. signal-cli locks its account data, so
`register` and `verify` fail while the daemon is running. To run any other
signal-cli command later, stop the daemon first with
`systemctl --user stop signal-cli-daemon`.

All of this is machine-local: the account keys in `~/.local/share/signal-cli`,
the config, the group record, the spool and both services. Only one machine
can run the daemon for a given bot number at a time.

### Setting up the transcriber

Transcription runs Whisper on this machine, through faster-whisper, so the
audio never leaves it. It needs ffmpeg and Python's venv module
(`sudo apt install ffmpeg python3-venv` on Debian and Ubuntu; the owner runs
anything needing sudo).

1. Choose the model with the owner. large-v3 (about 3 GB) is the most
   accurate. On an NVIDIA GPU it is fast. On a recent laptop CPU with 8 threads
   it took about half the length of the clip (21 seconds of audio in 12
   seconds). On a slower CPU, `small` or `medium` is quicker and less accurate.
2. `${CLAUDE_PLUGIN_ROOT}/scripts/setup-transcriber.sh --model <name>`, with
   `--cuda` on a machine with an NVIDIA GPU. It installs faster-whisper in
   `~/.local/share/signal-session/whisper-venv`, copies `transcribe.py` to
   `~/.local/lib/signal-session/`, downloads the model, and records
   `transcribe_command` in the config.
3. Test it on a voice note: the owner sends one in the session's group, and
   the transcript should match what they said.
4. Optional: to spell names and technical words right, or to fix the
   language, add `SIGNAL_SESSION_WHISPER_PROMPT='<words>'` or
   `SIGNAL_SESSION_WHISPER_LANGUAGE=<code>` after `env` in
   `transcribe_command` in the config.

### Upgrading signal-cli

Signal changes its servers from time to time, and an old signal-cli can stop
working. When it does, or every few months:

1. Download and unpack the new version as in step 0, into its own folder,
   but do not change the link yet.
2. `systemctl --user stop signal-session-spool signal-cli-daemon`
3. `ln --symbolic --force ~/.local/opt/signal-cli-$VERSION-native/signal-cli ~/.local/bin/signal-cli`
4. `systemctl --user start signal-cli-daemon signal-session-spool`, then `check`.
   It prints the running version.
5. Keep the old folder until the new version has worked for a few days. To go
   back, point the link at the old folder and restart both services.

A new version may update the account data in `~/.local/share/signal-cli`, and
an older version may then refuse it. Copy that folder before upgrading
(`rsync --archive ~/.local/share/signal-cli/ ~/.local/share/signal-cli.backup/`)
so going back stays possible.

### Moving the bot to another machine

Copy the account data instead of registering again. The bot keeps its keys, so
the owner's phone shows no safety number warning, and no captcha or SMS is needed.

1. On the old machine: `systemctl --user disable --now signal-session-spool signal-cli-daemon`.
   Two daemons running the same account break it.
2. Install signal-cli on the new machine.
3. Copy `~/.local/share/signal-cli/` and `~/.config/signal-session/config.json`
   with `rsync --archive`, which keeps their permissions. Leave `groups.json`
   behind: its entries belong to the old machine's sessions.
4. Run `setup-daemon.sh` with the two numbers from the copied config, then `check`.
