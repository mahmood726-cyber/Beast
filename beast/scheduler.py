"""Simple self-running scheduler: call a run function on a fixed interval.

Kept deliberately minimal and dependency-free so it works as a long-running
foreground process or under nohup / a service wrapper. For OS-native scheduling
prefer cron or Windows Task Scheduler driving ``beast run`` (see the README); use
this loop when you want a single persistent process.

The loop is fail-closed at the *run* boundary: an exception inside one run is
logged and the loop continues to the next tick rather than dying.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("beast.scheduler")


def run_loop(
    run_fn: Callable[[], object],
    interval_seconds: float,
    max_runs: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Invoke ``run_fn`` every ``interval_seconds`` until stopped.

    Parameters
    ----------
    max_runs:
        Stop after this many runs (``None`` = run forever). Used by tests and for
        one-shot scheduled invocations.
    stop_event:
        If set, the loop exits promptly when the event is set.
    sleep_fn:
        Injectable sleep (tests pass a no-op to avoid real waiting).

    Returns the number of completed runs.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    runs = 0
    log.info("scheduler started: interval=%ss max_runs=%s", interval_seconds, max_runs)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                run_fn()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("scheduled run raised: %s", exc)
            runs += 1
            if max_runs is not None and runs >= max_runs:
                break
            # Sleep in short slices so a stop_event is honoured quickly.
            waited = 0.0
            while waited < interval_seconds:
                if stop_event is not None and stop_event.is_set():
                    break
                step = min(1.0, interval_seconds - waited)
                sleep_fn(step)
                waited += step
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
        log.info("scheduler interrupted; shutting down cleanly")
    log.info("scheduler stopped after %d run(s)", runs)
    return runs
