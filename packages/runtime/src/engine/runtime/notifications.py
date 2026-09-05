"""Reporting a run's progress back to the conversation that asked for it.

A run started from a chat message has somewhere to answer: the thread the
request arrived in. This is the one place that knows how to get back there, so
the executor and the run-bound tool server each say what happened rather than
each working out where to say it.

Delivery is best effort throughout. A chat provider that is unreachable, not
connected, or refusing the channel must not fail the work it was reporting on
-- the run is the thing that matters, and its record lives in the store either
way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from engine.domain import RunOrigin, RunState
from engine.ports import Communications, Message, MessageLink

logger = logging.getLogger(__name__)


class RunNotifier:
    """Post a run's progress into the conversation it came from."""

    def __init__(self, communications: Communications, public_url: str = "") -> None:
        self._communications = communications
        self._public_url = public_url.rstrip("/")

    def work_order_link(self, state: RunState) -> MessageLink | None:
        """Where a person goes to watch this run, if this deployment has a URL."""
        if not self._public_url:
            return None
        return MessageLink("View work order", f"{self._public_url}/runs/{state.run_id}")

    async def announce(
        self,
        state: RunState,
        text: str,
        *,
        links: Iterable[MessageLink] = (),
        mention: bool = False,
    ) -> None:
        """Say something in this run's thread, if it has one.

        A run created from the web has no origin and nothing to say to, so this
        is a no-op rather than a fallback to some configured channel: an update
        addressed to nobody in particular is noise in somebody else's room.
        """
        origin = state.origin
        if origin is None or not origin.channel:
            return
        await self.post(
            origin,
            Message(
                text,
                tuple(links),
                origin.author if mention and origin.author else "",
            ),
            state,
        )

    async def post(
        self, origin: RunOrigin, message: Message, state: RunState | None = None
    ) -> None:
        try:
            await self._communications.post(
                origin.channel,
                message,
                state.run_id if state is not None else None,
                thread_id=origin.thread_id,
            )
        except Exception:
            logger.exception(
                "could not report progress for run %s",
                state.run_id if state is not None else origin.channel,
            )


__all__ = ["RunNotifier"]
