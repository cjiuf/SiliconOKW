"""Read-back verification for write actions.

A command accepted by the queue is not a command the game executed.  On the PE
build `cmdwin.enqueue` hands the object straight to `CCommandQueue`, which runs
`CanExecute` later, on the game thread, when it pops the command.  A refusal
leaves no trace in the object we built: the tool used to return `ok: true` for
orders the game had silently dropped.

So every write answers with a state, not a bool:

    verified    the observable the command should change, changed
    rejected    the clock moved and it did not change before the deadline
    unverified  nothing to read back, or the clock never moved while waiting

`unverified` is deliberately not `ok`.  A caller that cannot check must not claim
the action happened; the gateway turns `unverified` into "I do not know yet" and
re-reads on the next turn.
"""
from __future__ import annotations

import time

VERIFIED = "verified"
REJECTED = "rejected"
UNVERIFIED = "unverified"

DEFAULT_TIMEOUT = 8.0
DEFAULT_INTERVAL = 0.2


def poll(probe, timeout_s=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL, clock=None):
    """Call `probe` until it returns something truthy, or the deadline passes.

    `clock` is the game clock (hours).  It is what separates "the engine refused"
    from "the game never got a chance to run": with a frozen clock nothing can
    change, and reporting that as a rejection would be a lie in the other
    direction.

    Returns ``(state, detail, observed)``.
    """
    t0 = time.time()
    c0 = clock() if clock is not None else None
    observed = None
    while True:
        try:
            observed = probe()
        except Exception as e:                       # a read fault is not a refusal
            observed = None
            detail = "probe raised %s: %s" % (type(e).__name__, e)
        else:
            if observed:
                return VERIFIED, "observed", observed
            detail = "no change within %.1fs" % timeout_s
        if time.time() - t0 >= timeout_s:
            break
        time.sleep(interval)
    if clock is not None:
        c1 = clock()
        if c0 is not None and c1 == c0:
            return UNVERIFIED, "the game clock did not advance while waiting (paused?)", observed
    return REJECTED, detail, observed


def attach(result, state, detail, observed=None):
    """Fold a verdict into a tool result without losing what the tool said."""
    out = dict(result) if isinstance(result, dict) else {"ok": bool(result),
                                                         "result": result}
    out.setdefault("queued", True)
    out["verification"] = state
    out["verification_detail"] = detail
    if observed is not None:
        out["observed"] = observed
    # `ok` may only stay true when something was actually observed to change.
    if state != VERIFIED:
        out["ok"] = False
    return out


# --------------------------------------------------------------- probes
# Each takes the live Game and returns a truthy value when the effect is visible.

def idea_active(game, key, tag=None):
    pol = game.politics(tag)
    return key in (pol.get("active_ideas") or [])


def conveyor_count(game, tag=None):
    return len(game.conveyors(tag))


def line_factories(game, line, tag=None):
    for row in game.production(tag):
        if row.get("line") == line or row.get("index") == line:
            return row.get("factories")
    return None
