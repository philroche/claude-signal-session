# signal-session

A Claude Code plugin that lets you keep working with a running Claude Code
session from your phone over Signal. Each session that listens gets its own
Signal group, with you and a bot number you own. You write in the group, the
session does the work, and it replies in the group, with screenshots or files
when they help. You can send photos, videos and voice notes too.

Everything runs on your own machine. Messages go through Signal and a
signal-cli daemon you run, not through any other service.

## How it works

Two systemd user services run all the time:

- `signal-cli-daemon` runs [signal-cli](https://github.com/AsamK/signal-cli)
  for the bot number.
- `signal-session-spool` writes each of your messages to a file, so nothing is
  lost while a session is busy or between listens.

A session listens in one of two ways:

- **A background command** (the default). It waits without using any tokens
  and exits when you write, which wakes the session. The session acts on your
  message, replies, and starts the command again.
- **A Claude Code channel** (optional). Channels are a research preview in
  Claude Code that let an MCP server push messages into a session. This plugin
  includes one. It needs Claude Code started with an option Anthropic marks as
  dangerous, described below.

Only messages from your own number, written in the session's own group, reach
the session. Messages from anyone else, and direct messages to the bot, are
dropped before Claude sees them.

## Requirements

- Linux with systemd user services. macOS is not supported yet.
- Python 3.
- signal-cli. The skill's setup steps show how to install it. It is not
  included in this repository.
- A second phone number for the bot, for example a cheap second SIM. It gets
  one SMS, at registration. Don't install the Signal app on that number.
- Optional, to understand voice notes and the sound of videos: ffmpeg, and
  disk space for a Whisper model (about 3 GB for the most accurate one).

## Install

```
claude plugin marketplace add <this repository's URL or local path>
claude plugin install signal-session@signal-session
```

Then, in a Claude Code session, run `/signal-session:signal-session setup`.
Claude takes you through it one step at a time:

1. Installing signal-cli.
2. Registering the bot number. You solve a captcha in a browser and type in
   the code from the SMS.
3. Running `setup-daemon.sh`, which writes the config and starts the two
   services.
4. Optional: running `setup-transcriber.sh`, which installs faster-whisper so
   the session can understand voice notes. Nothing leaves your machine.

## Use

- `/signal-session:signal-session start [group name]` starts listening. The
  first time, accept the new group on your phone.
- Write "stop listening" in the group, or `/signal-session:signal-session stop`
  at the terminal. The group stays, and the next start with the same name uses
  it again, from any session.

While a session listens from your phone, it asks its questions in the group,
not at the terminal. It can't answer permission prompts for you: it tells you
in the group when one is waiting at the terminal.

### Using the channel instead

Start Claude Code with:

```
claude --dangerously-load-development-channels plugin:signal-session@signal-session
```

Then start listening as above. The session uses the channel and needs no
background command. Without that option, the plugin's channel tools refuse to
start and the session uses the background command. Anthropic may change how
channels work while they are a research preview.

## Security

- Anyone who can send messages from your phone number in the session's group
  can make the session do anything you could type at the terminal. Signal
  authenticates the sender, and the plugin passes on only your number, but
  keep your phone locked.
- The bot's Signal keys are in `~/.local/share/signal-cli` on your machine.
  Anyone who can read that folder can send and receive as the bot.
- Replies go to your phone. The skill tells Claude never to send secrets,
  credentials or customer data in a reply, but check what you ask for.
- The daemon listens on a UNIX socket that only your user can open.

## Upgrading

- The plugin: `claude plugin update signal-session@signal-session`, then run
  `setup-daemon.sh` again, so the services use the new scripts.
- signal-cli: the skill's setup section has the steps. Signal changes its
  servers from time to time, and an old signal-cli can stop working.

## Tests

```
python3 -I tests/test_plugin.py
```

The tests run the scripts against a fake signal-cli daemon, so they need no
Signal account.

## Licence

MIT. See `LICENSE`. signal-cli and faster-whisper are installed from their own
releases under their own licences.
