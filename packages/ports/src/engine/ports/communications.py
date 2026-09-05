"""Communications capability.

Getting messages to humans -- status updates, review requests, failure notices.
Buzz is the intended first implementation; Slack, email, or a log line satisfy
the same shape.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from engine.domain.ids import RunId


@dataclass(frozen=True, slots=True)
class MessageLink:
    """A provider-neutral link included in a human-facing message."""

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Message:
    """Structured message content rendered by a communications adapter."""

    text: str
    links: tuple[MessageLink, ...] = ()
    mention: str = ""
    """Who to address, in the provider's own vocabulary for naming a person.

    A provider-neutral *intent* rather than a rendered mention: the adapter
    knows that Slack spells one ``<@U123>`` and another spells it something
    else, and a caller that wrote the markup itself would be wrong everywhere
    but one place.
    """


@runtime_checkable
class Communications(Protocol):
    """Delivers messages to humans on behalf of a run."""

    async def post(
        self,
        channel: str,
        message: str | Message,
        run_id: RunId | None = None,
        thread_id: str = "",
    ) -> str:
        """Send a message. Returns a provider-specific message id.

        `thread_id` addresses an existing conversation -- the id an earlier
        `post` returned, or the one the request arrived under -- so a run's
        progress stays under the message that asked for it. Empty posts to the
        channel itself.
        """
        ...

    async def reply(self, message_id: str, message: str) -> str:
        """Thread a follow-up under an earlier message."""
        ...


__all__ = ["Communications", "Message", "MessageLink"]
