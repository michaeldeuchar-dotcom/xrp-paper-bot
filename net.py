"""HTTP GET with a HARD wall-clock deadline.

requests' timeout= is per socket operation (connect, each read) and does not cover
DNS lookup, so a single call can block far longer than the timeout. Every network
read in the bot goes through hard_get_json(), which runs the request in a worker
thread and gives up after `deadline` seconds no matter what the socket is doing.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

import requests

import config

_local = threading.local()
_POOL = ThreadPoolExecutor(max_workers=24, thread_name_prefix="http")
_PAR_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="par")
_inflight = 0
_lock = threading.Lock()


class HardTimeout(Exception):
    pass


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        _local.s = s
    return s


def _do(url: str, sock_timeout: tuple):
    global _inflight
    try:
        r = _session().get(url, timeout=sock_timeout)
        r.raise_for_status()
        return r.json()
    finally:
        with _lock:
            _inflight -= 1


def hard_get_json(url: str, deadline: float | None = None):
    global _inflight
    deadline = float(deadline or config.HTTP_HARD_DEADLINE)
    with _lock:
        if _inflight >= 20:
            raise HardTimeout(f"too many stuck requests in flight ({_inflight}); skipping {url[:60]}")
        _inflight += 1
    fut = _POOL.submit(_do, url, (config.HTTP_CONNECT_TIMEOUT, config.HTTP_READ_TIMEOUT))
    try:
        return fut.result(timeout=deadline)
    except FutTimeout:
        raise HardTimeout(f"hard deadline {deadline:.0f}s exceeded for {url[:80]}")


def parallel(calls: dict, deadline: float):
    """Run {name: zero-arg fn} in parallel; return ({name: result}, {name: error}) within deadline."""
    futs = {name: _PAR_POOL.submit(fn) for name, fn in calls.items()}
    end = time.time() + deadline
    ok, err = {}, {}
    for name, f in futs.items():
        try:
            ok[name] = f.result(timeout=max(0.05, end - time.time()))
        except FutTimeout:
            err[name] = "timeout"
        except Exception as exc:  # noqa: BLE001
            err[name] = str(exc)[:200]
    return ok, err
