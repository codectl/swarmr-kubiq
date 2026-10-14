"""Shaping what a tool returns: byte-capping and error containment.

One responsibility: everything that happens to a tool result on its way back to
a subagent, and nothing about how the result was obtained. An unhandled
exception inside a tool kills the whole run, and a malformed drill, a session
the server refused or a kubiq that is still restarting are all feedback the
agent can act on, so every tool wears the same wrappers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx

from swarmr_kubiq.kubiq_api import KubiqError
from swarmr_kubiq.target import TargetError

__all__ = ["MAX_BYTES", "emit", "guard"]

MAX_BYTES = 12_000


def emit(payload: Any) -> str:
    """Serialise a tool result, byte-capped."""
    text = json.dumps(payload, indent=1, default=str, sort_keys=False)
    if len(text) <= MAX_BYTES:
        return text
    return (
        text[:MAX_BYTES] + f"\n... TRUNCATED at {MAX_BYTES} bytes. Narrow the query "
        "(one drill, one question, a smaller limit) instead of paging."
    )


def guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn any tool failure into a message the model can act on.

    An unhandled exception inside a tool aborts the whole LangGraph run and
    takes every concurrent member down with it. A drill kubiq would refuse, a
    session that failed to provision or an unreachable server is feedback, not
    a fatal condition. Return it as text and let the agent retry.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:  # DrillError is a ValueError
            return f"tool error: {exc}"
        except TargetError as exc:
            return f"tool error: {exc}"
        except KubiqError as exc:
            return f"tool error: {exc}"
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.strip()
            return (
                f"tool error: kubiq returned {exc.response.status_code} for "
                f"{exc.request.method} {exc.request.url.path}: {body or 'no body'}"
            )
        except httpx.HTTPError as exc:
            return (
                f"tool error: kubiq unreachable ({type(exc).__name__}: {exc}). "
                "Is the server at the target URL running?"
            )
        except Exception as exc:
            return f"tool error: {type(exc).__name__}: {exc}"

    return wrapper
