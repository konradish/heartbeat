#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""heartbeat.py - a rotating heartbeat for a named Claude Code agent.

One YAML file per agent sets the beats, their order and the rules. The agent owns
that file and may rewrite it. Two delivery paths call the same `next`, so they can
never disagree about whose turn it is:

  /loop    Inside the agent's Claude session. Each wake-up runs `next`, does the
           beat it prints, and schedules the next wake-up from its WAKE_IN line.
  watch    The backup (run as a systemd user service). Keeps the tmux session and
           the Claude process alive, answers known dialogs, sends the boot prompt,
           clears context after COMPLETE, and re-arms /loop when beats stop. It
           never types into a screen it does not recognise.

Commands:
  heartbeat.py next   <agent.yaml>   pick and print the next beat (the agent runs this)
  heartbeat.py watch  <agent.yaml>   run the watchdog loop
  heartbeat.py status <agent.yaml>   show state, the YAML's health and the pane
  heartbeat.py check  <agent.yaml>   validate the YAML (run after editing it)

Beat choice, first match wins: close (NULL streak hit its `close` threshold) ->
override (temporary, always ends) -> pick script (the agent's own logic) ->
forced beats (NULL streak) -> the rotation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

EFFECTS = ("clear", "reset", "keep", "count")  # precedence when several markers show up
SHELLS = {"zsh", "bash", "sh", "fish", "dash"}
BUSY = "esc to interrupt"
STALE_FACTOR = 1.5  # no beat for this many intervals while idle = loop is dead
EARLY = 0.75  # a wake-up sooner than this many intervals after the last beat is a duplicate
MIN_WAKE = 60  # ScheduleWakeup clamps to [60, 3600]


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads NULL / null / ~ as plain words. NULL is a marker name here,
    and plain YAML would turn `NULL: count` into a key of None."""


_Loader.yaml_implicit_resolvers = {
    ch: [r for r in rs if r[0] != "tag:yaml.org,2002:null"]
    for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
NULLISH = {"", "~", "null", "Null", "NULL", "none", "None"}


def load_yaml(text: str):
    cfg = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    if isinstance(cfg, dict):  # top-level `pick: null` etc. still mean "not set"
        cfg = {k: (None if isinstance(v, str) and v in NULLISH else v) for k, v in cfg.items()}
    return cfg

LOOP_PROMPT = (
    "/loop Heartbeat tick for {name}. Run `{cmd} next {yaml}` and do what it prints. "
    "When the beat is done, schedule your next wake-up in the number of seconds on its "
    "WAKE_IN line. If WAKE_IN says STOP, end your turn without scheduling one."
)


# ── config ──────────────────────────────────────────────────────────────────

def parse_duration(text) -> int:
    if isinstance(text, (int, float)):
        return int(text)
    parts = re.findall(r"(\d+)\s*([smhd])", str(text))
    if not parts or "".join(n + u for n, u in parts) != re.sub(r"\s", "", str(text)):
        raise ValueError(f"bad duration {text!r} (use 90s, 30m, 1h, 1h30m)")
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in parts)


def validate(cfg) -> list[str]:
    errs = []
    if not isinstance(cfg, dict):
        return ["the file is not a YAML mapping"]
    for key in ("name", "every", "beats"):
        if key not in cfg:
            errs.append(f"missing `{key}`")
    try:
        parse_duration(cfg.get("every", "30m"))
    except ValueError as e:
        errs.append(f"every: {e}")
    beats = cfg.get("beats") or []
    names = []
    if not isinstance(beats, list) or not beats:
        errs.append("`beats` must be a non-empty list of `- name: prompt` items")
    else:
        for i, b in enumerate(beats):
            if not (isinstance(b, dict) and len(b) == 1 and isinstance(next(iter(b.values())), str)):
                errs.append(f"beats[{i}] must be one `name: prompt` pair")
            else:
                names.append(next(iter(b)))
    markers = (cfg.get("log") or {}).get("markers") or {}
    for m, eff in markers.items():
        if eff not in EFFECTS:
            errs.append(f"log.markers.{m}: effect must be one of {', '.join(EFFECTS)}")
    for n, act in (cfg.get("when_nulls") or {}).items():
        if not isinstance(n, int):
            errs.append(f"when_nulls key {n!r} must be a number")
        if act == "close":
            if not cfg.get("close"):
                errs.append("when_nulls uses `close` but there is no `close:` prompt")
        elif not (isinstance(act, list) and act and all(a in names for a in act)):
            errs.append(f"when_nulls.{n} must be `close` or a list of beat names")
    ov = cfg.get("override")
    if ov:
        if not isinstance(ov, dict):
            errs.append("override must be a mapping")
        else:
            if not ov.get("run") and not ov.get("until"):
                errs.append("override needs an end: a `run` list, an `until` time, or both")
            if ov.get("every") and not ov.get("until"):
                errs.append("override.every needs an `until` time")
            for a in ov.get("run") or []:
                if a not in names:
                    errs.append(f"override.run: unknown beat {a!r}")
            if ov.get("until"):
                try:
                    datetime.fromisoformat(str(ov["until"]))
                except ValueError:
                    errs.append(f"override.until {ov['until']!r} is not a time like 2026-10-13 06:00")
            if ov.get("every"):
                try:
                    parse_duration(ov["every"])
                except ValueError as e:
                    errs.append(f"override.every: {e}")
    return errs


class Agent:
    """The YAML plus where its state lives. Falls back to the last good YAML."""

    def __init__(self, yaml_path: str):
        self.yaml_path = Path(yaml_path).expanduser().resolve()
        self.warnings: list[str] = []
        raw_err = None
        try:
            cfg = load_yaml(self.yaml_path.read_text())
            errs = validate(cfg)
        except Exception as e:  # noqa: BLE001 - any parse failure falls back
            cfg, errs, raw_err = None, [], e
        self.home = Path((cfg or {}).get("home") or self.yaml_path.parent).expanduser()
        self.dir = self.home / ".heartbeat"
        self.dir.mkdir(parents=True, exist_ok=True)
        last_good = self.dir / "last-good.yaml"
        problems = [str(raw_err)] if raw_err else errs
        if problems:
            if not last_good.exists():
                sys.exit(f"{self.yaml_path} is broken and there is no last good copy:\n  " + "\n  ".join(problems))
            self.warnings.append(
                f"Your {self.yaml_path.name} edit is broken, so this beat runs on the last good "
                f"version. Fix it: " + "; ".join(problems)
            )
            cfg = load_yaml(last_good.read_text())
        else:
            text = self.yaml_path.read_text()
            if not last_good.exists() or last_good.read_text() != text:
                last_good.write_text(text)
        self.cfg = cfg
        self.name = cfg["name"]
        self.beats = [(next(iter(b)), next(iter(b.values()))) for b in cfg["beats"]]
        self.beat_names = [n for n, _ in self.beats]
        log = cfg.get("log") or {}
        self.log_dir = self.home / log.get("dir", "memory/heartbeat")
        self.markers = log.get("markers") or {}
        target = cfg.get("send_to") or f"{self.name.lower()}:claude"
        self.session, _, self.window = target.partition(":")
        self.window = self.window or "claude"
        self.socket = cfg.get("tmux_socket") or self.session

    # interval in force right now (an active override may change it)
    def interval(self, now: float | None = None) -> int:
        ov = self.active_override(now)
        if ov and ov.get("every"):
            return parse_duration(ov["every"])
        return parse_duration(self.cfg["every"])

    def active_override(self, now: float | None = None):
        ov = self.cfg.get("override")
        if not ov:
            return None
        if ov.get("until"):
            if datetime.fromisoformat(str(ov["until"])).timestamp() <= (now or time.time()):
                return None
        return ov

    def cmd(self) -> str:
        return str(Path(__file__).resolve())


# ── state ───────────────────────────────────────────────────────────────────

@contextmanager
def state_of(agent: Agent):
    lock = open(agent.dir / "lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = agent.dir / "state.json"
    st = json.loads(path.read_text()) if path.exists() else {}
    st.setdefault("index", 0)
    st.setdefault("nulls", 0)
    st.setdefault("last_check_at", time.time())
    st.setdefault("history", [])
    try:
        yield st
    finally:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(path)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def read_markers(agent: Agent, since: float) -> str | None:
    """Strongest marker effect in the last 3 lines of beat logs written since `since`."""
    found = set()
    if not agent.log_dir.is_dir():
        return None
    for f in sorted(agent.log_dir.glob("*.md")):
        if f.stat().st_mtime <= since:
            continue
        tail = "\n".join([ln for ln in f.read_text(errors="replace").splitlines() if ln.strip()][-3:])
        for word, effect in agent.markers.items():
            if re.search(rf"\b{re.escape(word)}\b", tail):
                found.add(effect)
    return next((e for e in EFFECTS if e in found), None)


# ── next ────────────────────────────────────────────────────────────────────

def cmd_next(agent: Agent) -> None:
    now = time.time()
    out = []
    with state_of(agent) as st:
        interval = agent.interval(now)
        since_last = now - st.get("last_beat_at", 0)
        if since_last < EARLY * interval:
            # A legit wake-up comes a full interval after the last beat. An early one is a
            # second loop (e.g. re-armed while the old one lived on; /loop survives /clear).
            # Stop it. If this was the only loop, the watchdog re-arms after STALE_FACTOR.
            print(f"SKIP: the last beat ({st.get('last_beat')}) went out {int(since_last // 60)} min ago, "
                  f"so this wake-up is a duplicate loop. Do nothing.\nWAKE_IN: STOP")
            return

        effect = read_markers(agent, st["last_check_at"])
        st["last_check_at"] = now
        if effect == "reset":
            st["nulls"] = 0
        elif effect == "count":
            st["nulls"] += 1
        elif effect == "clear":
            st["nulls"] = 0
            st["pending_clear"] = True
        if st.get("pending_clear"):
            print("COMPLETE is recorded. End your turn now and do not schedule a wake-up. "
                  "The watchdog will clear your context and boot you fresh.\nWAKE_IN: STOP")
            return

        beat, prompt, why = choose(agent, st, now)
        st["last_beat_at"] = now
        st["last_beat"] = beat
        st["history"] = (st["history"] + [[int(now), beat, why]])[-30:]

    stamp = datetime.now().astimezone()
    log_name = f"{stamp:%Y-%m-%d-%H%M}-{beat}.md"
    out.append(f"Heartbeat for {agent.name} · {stamp:%A %Y-%m-%d %H:%M %Z} · {beat.upper()} beat ({why})")
    out.append("")
    out.append(prompt.strip())
    if agent.cfg.get("footer"):
        out.append("")
        out.append(agent.cfg["footer"].strip())
    out.append("")
    out.append(f"Write this beat's log to: {agent.log_dir.relative_to(agent.home)}/{log_name}")
    for w in agent.warnings:
        out.append(f"NOTE: {w}")
    out.append(f"WAKE_IN: {max(MIN_WAKE, min(3600, agent.interval(now)))}")
    print("\n".join(out))


def choose(agent: Agent, st: dict, now: float) -> tuple[str, str, str]:
    prompts = dict(agent.beats)
    nulls = st["nulls"]
    rules = sorted((agent.cfg.get("when_nulls") or {}).items())

    for n, act in rules:
        if act == "close" and nulls >= n:
            return "close", agent.cfg["close"], f"{nulls} NULL beats in a row"

    ov = agent.active_override(now)
    if ov and ov.get("run"):
        key = hashlib.sha1(json.dumps(ov, sort_keys=True, default=str).encode()).hexdigest()[:12]
        if st.get("override_key") != key:
            st["override_key"], st["override_used"] = key, 0
        used = st["override_used"]
        if used < len(ov["run"]):
            st["override_used"] = used + 1
            name = ov["run"][used]
            return name, prompts[name], f"override {used + 1}/{len(ov['run'])}"

    pick = agent.cfg.get("pick")
    if pick:
        name = run_pick(agent, st, now, pick)
        if name:
            return name, prompts[name], "picked by " + Path(pick).name

    forced = [(n, act) for n, act in rules if act != "close" and nulls >= n]
    if forced:
        n, choices = forced[-1]
        name = choices[(nulls - n) % len(choices)]
        return name, prompts[name], f"forced: {nulls} NULL beats in a row"

    i = st["index"] % len(agent.beats)
    st["index"] = i + 1
    name, prompt = agent.beats[i]
    return name, prompt, f"rotation {i + 1}/{len(agent.beats)}"


def run_pick(agent: Agent, st: dict, now: float, pick: str) -> str | None:
    """The agent's own picker: JSON state on stdin, a beat name (or nothing) on stdout."""
    path = (agent.home / pick).expanduser()
    argv = [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
    payload = {"name": agent.name, "now": int(now), "nulls": st["nulls"], "index": st["index"],
               "last_beat": st.get("last_beat"), "history": st["history"], "beats": agent.beat_names}
    try:
        r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                           timeout=30, cwd=agent.home)
    except Exception as e:  # noqa: BLE001
        agent.warnings.append(f"pick script {pick} failed ({e}); used the normal order.")
        return None
    name = (r.stdout.strip().splitlines() or [""])[0].strip()
    if r.returncode != 0:
        agent.warnings.append(f"pick script {pick} exited {r.returncode}: {r.stderr.strip()[:300]}; used the normal order.")
        return None
    if name and name not in agent.beat_names:
        agent.warnings.append(f"pick script {pick} returned unknown beat {name!r}; used the normal order.")
        return None
    return name or None


# ── tmux ────────────────────────────────────────────────────────────────────

class Pane:
    def __init__(self, agent: Agent):
        self.a = agent
        self.target = f"{agent.session}:{agent.window}"

    def tmux(self, *args, check=False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.a.socket, *args], capture_output=True, text=True, check=check)

    def exists(self) -> bool:
        return self.tmux("has-session", "-t", self.a.session).returncode == 0

    def create(self) -> None:
        self.tmux("new-session", "-d", "-s", self.a.session, "-n", self.a.window,
                  "-c", str(self.a.home), "-x", "220", "-y", "50", check=True)

    def command(self) -> str:
        return self.tmux("display", "-p", "-t", self.target, "#{pane_current_command}").stdout.strip()

    def screen(self) -> list[str]:
        return self.tmux("capture-pane", "-p", "-t", self.target).stdout.rstrip("\n").splitlines()

    def paste(self, text: str) -> None:
        buf = self.a.dir / "paste.txt"
        buf.write_text(text)
        self.tmux("load-buffer", "-b", "heartbeat", str(buf), check=True)
        self.tmux("paste-buffer", "-d", "-b", "heartbeat", "-t", self.target, check=True)
        time.sleep(2)
        self.tmux("send-keys", "-t", self.target, "Enter", check=True)

    def keys(self, *keys: str) -> None:
        for k in keys:
            self.tmux("send-keys", "-t", self.target, k, check=True)
            time.sleep(0.5)


def read_screen(lines: list[str]) -> str:
    """'busy', 'idle', or 'other' for a Claude Code pane."""
    text = "\n".join(lines)
    if BUSY in text:
        return "busy"
    # The input box is a `❯` line right under a rule line. A `❯ 1.` line is a menu cursor.
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].startswith("❯"):
            if lines[i - 1].lstrip().startswith("─") and not re.match(r"❯\s*\d+\.", lines[i]):
                return "idle"
            break
    return "other"


# ── watch ───────────────────────────────────────────────────────────────────

def log(agent: Agent, msg: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] [{agent.name}] {msg}", flush=True)


def launch_line(agent: Agent) -> str:
    launch = agent.cfg.get("launch") or "claude --dangerously-skip-permissions"
    cap = agent.cfg.get("memory_max")
    if cap:
        launch = f"systemd-run --user --scope -q -p MemoryMax={cap} -- {launch}"
    return launch


def watch_once(agent: Agent, pane: Pane, st: dict) -> None:
    now = time.time()
    if not pane.exists():
        pane.create()
        log(agent, f"created tmux session {agent.session} (socket {agent.socket})")

    cmd = pane.command()
    if cmd in SHELLS:
        recent = [t for t in st.get("launches", []) if now - t < 1800]
        if len(recent) >= 3:
            if not st.get("gave_up"):
                log(agent, "Claude exited 3 times in 30 min. Not relaunching; fix it and restart the service.")
                st["gave_up"] = True
            return
        pane.tmux("send-keys", "-t", pane.target, launch_line(agent), "Enter", check=True)
        st["launches"] = recent + [now]
        st["needs_boot"] = True
        st.pop("armed_at", None)
        log(agent, f"launched: {launch_line(agent)}")
        return

    lines = pane.screen()
    state = read_screen(lines)
    if state == "busy":
        st.pop("stuck_since", None)
        return

    if state == "other":
        text = "\n".join(lines)
        for pattern, keys in (agent.cfg.get("dialogs") or {}).items():
            if pattern.lower() in text.lower():
                pane.keys(*keys)
                log(agent, f"answered dialog {pattern!r} with {keys}")
                return
        if not st.get("stuck_since"):
            st["stuck_since"] = now
            tail = " | ".join(ln.strip() for ln in lines if ln.strip())[-300:]
            log(agent, f"unknown screen (pane runs {cmd!r}); not typing. Screen ends: {tail}")
        return

    # idle
    if st.pop("stuck_since", None):
        log(agent, "screen is back to a normal prompt")
    if st.get("needs_boot"):
        if agent.cfg.get("boot"):
            pane.paste(agent.cfg["boot"].strip())
            log(agent, "sent boot prompt")
        st["needs_boot"] = False
        st["needs_arm"] = True
        return
    if st.get("pending_clear"):
        pane.paste("/clear")
        st.update(pending_clear=False, nulls=0, index=0, needs_boot=True)
        st.pop("armed_at", None)
        log(agent, "COMPLETE: sent /clear; boot follows")
        return
    stale = now - max(st.get("last_beat_at", 0), st.get("armed_at", 0)) > STALE_FACTOR * agent.interval(now)
    if st.get("needs_arm") or stale:
        pane.paste(LOOP_PROMPT.format(name=agent.name, cmd=agent.cmd(), yaml=agent.yaml_path))
        st["armed_at"] = now
        st["needs_arm"] = False
        log(agent, "armed /loop" + (" (beats had stopped)" if stale and st.get("last_beat_at") else ""))


def cmd_watch(yaml_path: str) -> None:
    first = True
    last_warnings: list[str] = []
    while True:
        agent = Agent(yaml_path)  # re-read each tick: the agent may have edited its YAML
        pane = Pane(agent)
        if agent.warnings != last_warnings:
            for w in agent.warnings:
                log(agent, w)
            last_warnings = agent.warnings
        tick = parse_duration(agent.cfg.get("watch_every", "60s"))
        with state_of(agent) as st:
            if first:
                # A fresh service start finds Claude either already running (leave it
                # alone; staleness re-arms the loop) or not (watch_once launches + boots).
                st.pop("gave_up", None)
                if not pane.exists():
                    st["needs_boot"] = True
                first = False
            try:
                watch_once(agent, pane, st)
            except subprocess.CalledProcessError as e:
                log(agent, f"tmux failed: {e.stderr or e}")
        time.sleep(tick)


# ── status / check ──────────────────────────────────────────────────────────

def cmd_status(agent: Agent) -> None:
    for w in agent.warnings:
        print("WARNING:", w)
    path = agent.dir / "state.json"
    st = json.loads(path.read_text()) if path.exists() else {}
    now = time.time()
    print(f"agent      {agent.name}  ({agent.yaml_path})")
    print(f"interval   {agent.interval(now)}s" + ("  (override active)" if agent.active_override(now) else ""))
    if st.get("last_beat_at"):
        print(f"last beat  {st.get('last_beat')}  {int((now - st['last_beat_at']) // 60)} min ago")
    print(f"nulls      {st.get('nulls', 0)}    rotation index {st.get('index', 0)}")
    for k in ("pending_clear", "needs_boot", "needs_arm", "gave_up"):
        if st.get(k):
            print(f"flag       {k}")
    if st.get("stuck_since"):
        print(f"STUCK      unknown screen for {int((now - st['stuck_since']) // 60)} min")
    for t, beat, why in st.get("history", [])[-8:]:
        print(f"  {datetime.fromtimestamp(t):%m-%d %H:%M}  {beat:<10} {why}")
    pane = Pane(agent)
    if pane.exists():
        print(f"pane       {pane.command()}  ->  {read_screen(pane.screen())}")
    else:
        print("pane       (no tmux session)")


def cmd_check(yaml_path: str) -> None:
    path = Path(yaml_path).expanduser()
    try:
        errs = validate(load_yaml(path.read_text()))
    except Exception as e:  # noqa: BLE001
        errs = [str(e)]
    if errs:
        print(f"{path}: broken\n  " + "\n  ".join(errs))
        sys.exit(1)
    print(f"{path}: ok")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("next", "watch", "status", "check"):
        sys.exit(__doc__)
    verb, yaml_path = sys.argv[1], sys.argv[2]
    if verb == "check":
        cmd_check(yaml_path)
    elif verb == "watch":
        cmd_watch(yaml_path)
    elif verb == "next":
        cmd_next(Agent(yaml_path))
    else:
        cmd_status(Agent(yaml_path))


if __name__ == "__main__":
    main()
