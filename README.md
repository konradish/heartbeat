# heartbeat

A heartbeat for a long-running Claude Code agent: one Python file and one YAML file per agent.

Every 30 minutes (or whatever you set), the agent asks the engine what to do next. The engine hands it a "beat": a named prompt such as `listen`, `make`, `explore` or `hold`. The agent does the work, writes a log file for the beat, and ends that log with one word on its last line. That word is the only thing the engine reads back, and it drives a small state machine:

| Last line of the beat log | What the engine does |
|---|---|
| `SUBSTANTIVE` | Something was produced or changed. The "nothing to do" streak goes back to 0. |
| `HELD` | The agent held off on purpose. The streak stays where it is. Doing nothing on purpose isn't a failure. |
| `NULL` | Nothing was worth doing. The streak goes up by 1. |
| `COMPLETE` | The session is done. The agent stops, and the watchdog clears its context and boots it fresh. |

You name the markers and choose their effects in the YAML. The table shows the setup it was built with.

The streak drives escalation (`when_nulls`). In the example, after 3 NULLs in a row the engine stops rotating and forces `explore` and `make` beats, taking turns. After 10 it sends a `close` prompt: write a park note and end with COMPLETE.

## How a beat is chosen

The first rule that matches wins:

1. **close**: the NULL streak hit its `close` threshold.
2. **override**: a temporary change (`run` a list of beats first, and/or a different `every` until a time). An override must have an end.
3. **pick**: the agent's own script. It gets the state as JSON on stdin and prints a beat name, or prints nothing to pass.
4. **forced**: the NULL streak hit a threshold that lists beats.
5. **rotation**: the beats in order.

## Two ways in, one source of truth

Both delivery paths call the same `next`, so they can never disagree about whose turn it is.

- **`/loop` inside the agent's session.** Each wake-up runs `heartbeat.py next agent.yaml`, does the beat, and schedules the next wake-up from the `WAKE_IN:` line.
- **`watch`, a systemd user service.** It keeps a tmux session and the Claude process alive. It launches Claude, answers dialogs you list, sends the boot prompt, sends `/clear` after COMPLETE, and re-arms `/loop` if beats stop.

The watchdog reads the tmux screen and sorts it into busy, idle or other. It only types at an idle prompt or into a dialog you listed. It never types into a screen it doesn't recognize. If Claude exits 3 times in 30 minutes, it stops relaunching and can paste one alert line into another tmux pane (`alert_pane`).

A wake-up that comes too soon after the last beat is treated as a second loop and gets `WAKE_IN: STOP`. A self-paced `/loop` survives `/clear`, so re-arming can leave two loops running.

## The agent owns its YAML, not the engine

The agent may rewrite its own `heartbeat.yaml` (its beats, prompts, interval, overrides). If an edit breaks the file, the engine runs the last good copy and puts the error in the next beat, so the agent can fix it. Run `heartbeat.py check agent.yaml` after editing.

The engine is a different matter. It decides when the agent's session ends and counts its streaks, so it is the thing that grades the agent. Keep it where the agent can't write. The engine doesn't enforce that for you; file permissions or a sandbox do.

## Install

You need Linux with a systemd user session, `tmux`, [`uv`](https://docs.astral.sh/uv/), and Claude Code.

```bash
git clone https://github.com/konradish/heartbeat ~/src/heartbeat

# One folder per agent, named after it, holding its heartbeat.yaml and CLAUDE.md
mkdir -p ~/src/myagent
cp ~/src/heartbeat/example/heartbeat.yaml ~/src/myagent/
~/src/heartbeat/heartbeat.py check ~/src/myagent/heartbeat.yaml

# The watchdog, as a systemd user service (the unit expects ~/src/<name>/heartbeat.yaml)
ln -s ~/src/heartbeat/heartbeat@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now heartbeat@myagent
```

Watch it: `tmux -L myagent attach -t myagent`. Logs: `journalctl --user -u heartbeat@myagent -f`. State: `heartbeat.py status ~/src/myagent/heartbeat.yaml`.

Stop everything: `systemctl --user stop heartbeat@myagent`, then `tmux -L myagent kill-server`. Stopping the service leaves the agent running, because the unit uses `KillMode=process`.

## Read this before you run it

- **The default launch line is `claude --dangerously-skip-permissions`.** That means an agent running unattended with no permission prompts. Run it on a machine and an account where that's acceptable, or set `launch:` to something tighter.
- **The screen reading depends on Claude Code's terminal UI**: the `esc to interrupt` text and the `❯` prompt under a rule line. A Claude Code update that changes those will confuse the watchdog. It then logs "unknown screen" and stops typing, which is the safe way to fail.
- **It was built for one setup**: one agent, about 30-minute beats, Claude Code 2.1.x, Debian. The give-up alert text is written for my own Telegram relay; change it in `watch_once` before you use `alert_pane`.

## License

MIT. See [LICENSE](LICENSE).
