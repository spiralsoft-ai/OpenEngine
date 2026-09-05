"""Communications capability, backed by Slack."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import keyring
from engine.ports import Message

_SERVICE = "openengine"
_CLIENT_ID = "slack-client-id"
_CLIENT_SECRET = "slack-client-secret"
_ACCESS_TOKEN = "slack-access-token"
_SIGNING_SECRET = "slack-signing-secret"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_REVOKE_URL = "https://slack.com/api/auth.revoke"
_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_LIST_CONVERSATIONS_URL = "https://slack.com/api/conversations.list"


class SlackAuthError(RuntimeError):
    """Slack authorization or secure storage failed."""


@dataclass(frozen=True, slots=True)
class SlackCredentials:
    client_id: str
    client_secret: str


class SlackCredentialStore:
    def _check_backend(self) -> None:
        try:
            if keyring.get_keyring().priority < 1:
                raise SlackAuthError("no secure keyring backend available on this system")
        except (keyring.errors.NoKeyringError, NotImplementedError):
            raise SlackAuthError("no secure keyring backend available on this system")

    def credentials(self) -> SlackCredentials | None:
        try:
            client_id = keyring.get_password(_SERVICE, _CLIENT_ID)
            client_secret = keyring.get_password(_SERVICE, _CLIENT_SECRET)
        except keyring.errors.NoKeyringError:
            return None
        if not client_id or not client_secret:
            return None
        return SlackCredentials(client_id, client_secret)

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        self._check_backend()
        previous = {
            _CLIENT_ID: keyring.get_password(_SERVICE, _CLIENT_ID),
            _CLIENT_SECRET: keyring.get_password(_SERVICE, _CLIENT_SECRET),
        }
        try:
            keyring.set_password(_SERVICE, _CLIENT_ID, client_id)
            keyring.set_password(_SERVICE, _CLIENT_SECRET, client_secret)
        except keyring.errors.KeyringError as error:
            for username, value in previous.items():
                try:
                    if value is None:
                        keyring.delete_password(_SERVICE, username)
                    else:
                        keyring.set_password(_SERVICE, username, value)
                except keyring.errors.KeyringError:
                    pass
            raise SlackAuthError("could not securely save Slack credentials") from error
        # A token belongs to the app that issued it. Changing apps requires a
        # fresh authorization rather than retaining a misleading connection.
        # The signing secret is the same app's, so it goes with it -- a stale
        # one would reject every event the new app delivers.
        self.disconnect()
        try:
            keyring.delete_password(_SERVICE, _SIGNING_SECRET)
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass

    def token(self) -> str | None:
        try:
            return keyring.get_password(_SERVICE, _ACCESS_TOKEN)
        except keyring.errors.NoKeyringError:
            return None

    def set_token(self, token: str) -> None:
        self._check_backend()
        keyring.set_password(_SERVICE, _ACCESS_TOKEN, token)

    def signing_secret(self) -> str | None:
        """The secret Slack signs its event deliveries with, if one is saved.

        Kept beside the OAuth pair rather than in configuration: it is a
        credential, and a deployment that has one has it for the same app.
        """
        try:
            return keyring.get_password(_SERVICE, _SIGNING_SECRET)
        except keyring.errors.NoKeyringError:
            return None

    def set_signing_secret(self, secret: str) -> None:
        self._check_backend()
        keyring.set_password(_SERVICE, _SIGNING_SECRET, secret)

    def disconnect(self) -> None:
        for username in (_ACCESS_TOKEN,):
            try:
                keyring.delete_password(_SERVICE, username)
            except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
                pass


class SlackCommunications:
    """Deliver Engine notifications through the connected Slack workspace."""

    def __init__(self, credential_store: SlackCredentialStore) -> None:
        self._credential_store = credential_store

    async def post(
        self,
        channel: str,
        message: str | Message,
        run_id=None,
        thread_id: str = "",
    ) -> str:
        token = self._credential_store.token()
        if not token:
            return ""
        payload: dict[str, str] = {}
        if thread_id:
            payload["thread_ts"] = thread_id
        async with httpx.AsyncClient() as client:
            channel_id = await self._resolve_channel(client, token, channel)
            response = await client.post(
                _POST_MESSAGE_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "channel": channel_id,
                    "text": self._render(message),
                    **payload,
                },
            )
        if response.is_error:
            raise SlackAuthError(
                f"Slack returned {response.status_code} while posting a notification"
            )
        body = response.json()
        if not body.get("ok"):
            raise SlackAuthError(
                f"Slack notification failed: {body.get('error', 'message was not sent')}"
            )
        return str(body.get("ts", ""))

    @staticmethod
    def _render(message: str | Message) -> str:
        if isinstance(message, str):
            return message
        text = f"<@{message.mention}> {message.text}" if message.mention else message.text
        links = "\n".join(f"<{link.url}|{link.label}>" for link in message.links)
        return f"{text}\n{links}" if links else text

    async def _resolve_channel(
        self, client: httpx.AsyncClient, token: str, channel: str
    ) -> str:
        if re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel):
            return channel
        cursor = ""
        while True:
            params = {"types": "public_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(
                _LIST_CONVERSATIONS_URL,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            body = response.json()
            if response.is_error or not body.get("ok"):
                raise SlackAuthError(
                    "Slack channel lookup failed: "
                    f"{body.get('error', response.status_code)}"
                )
            match = next(
                (
                    item
                    for item in body.get("channels", [])
                    if str(item.get("name", "")).casefold() == channel.casefold()
                ),
                None,
            )
            if match is not None:
                return str(match["id"])
            cursor = str(body.get("response_metadata", {}).get("next_cursor", ""))
            if not cursor:
                raise SlackAuthError(f"Slack channel not found: {channel}")

    async def reply(self, message_id: str, message: str) -> str:
        raise NotImplementedError("Slack notification threads are not supported")


#: How old a delivery may be and still be acted on. Slack's own guidance: a
#: request replayed from a capture is refused rather than run again.
_SIGNATURE_MAX_AGE_SECONDS = 60 * 5


@dataclass(frozen=True, slots=True)
class SlackMention:
    """Somebody addressing the bot, reduced to what starting work needs."""

    channel: str
    thread_id: str
    author: str
    text: str


def verify_signature(
    signing_secret: str, timestamp: str, signature: str, body: bytes
) -> bool:
    """Whether Slack signed this delivery, recently, with our secret.

    The whole check, rather than the HMAC alone: an old-but-valid signature is
    a replay, and a route that only compared digests would run it again.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs(time.time() - float(timestamp))
    except ValueError:
        return False
    if age > _SIGNATURE_MAX_AGE_SECONDS:
        return False
    expected = "v0=" + hmac.new(
        signing_secret.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def mention_from_event(payload: Mapping[str, object]) -> SlackMention | None:
    """The request in an `app_mention` delivery, or ``None`` for anything else.

    Anything else is most of what arrives: subscription handshakes, message
    events the app also hears, and the bot's own replies -- which is the one
    worth naming, because a bot that answered its own thread posts would work
    itself into a loop.

    The thread is the mention's own thread when it has one and the mention
    itself otherwise, so a run started from a top-level message reports under
    that message rather than into the channel.
    """
    if payload.get("type") != "event_callback":
        return None
    event = payload.get("event")
    if not isinstance(event, Mapping) or event.get("type") != "app_mention":
        return None
    if event.get("bot_id") or event.get("subtype"):
        return None
    channel = str(event.get("channel", ""))
    author = str(event.get("user", ""))
    timestamp = str(event.get("ts", ""))
    if not channel or not author or not timestamp:
        return None
    text = _without_mentions(str(event.get("text", "")))
    if not text:
        return None
    return SlackMention(
        channel=channel,
        thread_id=str(event.get("thread_ts") or timestamp),
        author=author,
        text=text,
    )


def _without_mentions(text: str) -> str:
    """Drop the `<@U123>` tokens so what is left is what was asked for.

    Slack may write the same mention as `<@U123|display-name>`, so this takes
    everything up to the closing bracket rather than the id alone -- otherwise
    the display name would survive into the prompt as if it were the request.
    """
    return " ".join(re.sub(r"<@[^>]+>", " ", text).split())


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return _AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            # `app_mentions:read` is what makes the bot hearable: without it
            # Slack delivers no `app_mention`, and pinging it does nothing.
            "scope": "app_mentions:read,chat:write,chat:write.public,channels:read",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


async def exchange_code(credentials: SlackCredentials, code: str, redirect_uri: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _TOKEN_URL,
            data={"code": code, "redirect_uri": redirect_uri},
            auth=(credentials.client_id, credentials.client_secret),
        )
    if response.is_error:
        raise SlackAuthError(f"Slack returned {response.status_code} during authorization")
    body = response.json()
    if not body.get("ok") or not body.get("access_token"):
        raise SlackAuthError(f"Slack authorization failed: {body.get('error', 'no access token')}")
    return str(body["access_token"])


async def revoke_token(token: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(_REVOKE_URL, headers={"Authorization": f"Bearer {token}"})
    if response.is_error:
        raise SlackAuthError(f"Slack returned {response.status_code} while disconnecting")
    body = response.json()
    if not body.get("ok"):
        raise SlackAuthError(f"Slack disconnect failed: {body.get('error', 'token was not revoked')}")


__all__ = [
    "SlackAuthError",
    "SlackCommunications",
    "SlackCredentials",
    "SlackCredentialStore",
    "SlackMention",
    "authorization_url",
    "exchange_code",
    "mention_from_event",
    "revoke_token",
    "verify_signature",
]
