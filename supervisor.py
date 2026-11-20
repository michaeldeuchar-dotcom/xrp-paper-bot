#!/usr/bin/env python3
"""Keeps the paper bot alive for the week.

* starts bot.py (paper only) and restarts it if the process dies,
* kills + restarts it if data/week1/heartbeat.json is older than 3 minutes (a hang),
* stops for good at the run end time, when the bot writes FINISHED, or when ./STOP exists.
Run:  nohup python3 supervisor.py >> data/week1/supervisor.log 2>&1 &
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
STALE_SEC = 180
END = datetime.fromisoformat(config.RUN_UNTIL)
HARD_END = END.timestamp() + config.RESOLVE_GRACE_SEC + 300
SUP_PID = config.DATA_DIR / "supervisor.pid"
_stop = False


def say(msg):
    print(f"{datetime.now(PT):%Y-%m-%d %H:%M:%S} PT supervisor: {msg}", flush=True)


def _sig(signum, _f):
    global _stop
    _stop = True


def hb_age():
    try:
        return time.time() - json.loads(config.HEARTBEAT_PATH.read_text())["ts"]
    except Exception:  # noqa: BLE001
        return None


def kill(p):
    if p.poll() is not None:
        return
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(25)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(10)


def main():
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    SUP_PID.write_text(f"{os.getpid()}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("POLY_")}  # never pass wallet secrets
    restarts = 0
    proc = None
    grace_until = 0.0
    while not _stop:
        if config.KILL_SWITCH_PATH.exists():
            say("STOP file found; stopping")
            break
        if (config.DATA_DIR / "FINISHED").exists():
            say("bot reported FINISHED; done")
            break
        if time.time() > HARD_END:
            say("past hard end time; stopping")
            break
        if proc is None or proc.poll() is not None:
            if proc is not None:
                say(f"bot exited with code {proc.returncode}")
                if (config.DATA_DIR / "FINISHED").exists():
                    continue
                restarts += 1
                time.sleep(min(60, 5 * restarts))  # back off if it keeps crashing
            config.HEARTBEAT_PATH.unlink(missing_ok=True)
            out = open(config.DATA_DIR / "bot.stdout.log", "a")
            proc = subprocess.Popen([sys.executable, str(ROOT / "bot.py"), "--mode", "paper"], cwd=str(ROOT),
                                    stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            started = time.time()
            say(f"started bot pid={proc.pid} (restart #{restarts})")
        t_before = time.time()
        time.sleep(15)
        if time.time() - t_before > 45:
            # Box (VM) was paused; heartbeat is stale only because nothing ran. Give the bot time to catch up.
            say(f"box was asleep ~{time.time() - t_before - 15:.0f}s; giving bot 60s grace instead of restarting")
            grace_until = time.time() + 60
            continue
        age = hb_age()
        if proc.poll() is None and time.time() >= grace_until:
            if (age is None and time.time() - started > STALE_SEC) or (age is not None and age > STALE_SEC):
                say(f"heartbeat stale ({age if age is None else round(age)}s); killing pid={proc.pid} for restart")
                kill(proc)
            elif restarts and time.time() - started > 3600:
                restarts = 0
    if proc is not None:
        kill(proc)
    say("supervisor exit")
    SUP_PID.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
