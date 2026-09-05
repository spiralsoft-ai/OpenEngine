"""Communications capability, backed by Buzz.

Placeholder for Ticket 1. Satisfies `engine.ports.Communications` structurally;
no transport and no credentials yet.
"""

from engine.domain.ids import RunId
from engine.ports import Message


class BuzzCommunications:
    """Posts run updates to Buzz.

    Implements `engine.ports.Communications`.
    """

    def __init__(self, base_url: str, api_token: str) -> None:
        self._base_url = base_url
        self._api_token = api_token

    async def post(
        self,
        channel: str,
        message: str | Message,
        run_id: RunId | None = None,
        thread_id: str = "",
    ) -> str:
        raise NotImplementedError("Buzz delivery lands with the communications ticket")

    async def reply(self, message_id: str, message: str) -> str:
        raise NotImplementedError("Buzz threading lands with the communications ticket")


__all__ = ["BuzzCommunications"]
