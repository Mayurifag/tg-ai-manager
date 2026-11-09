import asyncio
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, List, Optional

from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.adapters.telegram.event_handlers import EventHandlers
from src.adapters.telegram.forum_ops import ForumOps
from src.adapters.telegram.write_ops import WriteOps
from src.adapters.telegram.media import MediaManager
from src.adapters.telegram.message_parser import MessageParser
from src.adapters.telegram.query_errors import unavailable
from src.adapters.telegram.recovering_client import RecoveringTelegramClient
from src.adapters.telegram.types import ITelethonClient
from src.domain.errors import AccountChanged
from src.domain.models import ForumTopicCursor, MediaContent, SystemEvent
from src.domain.ports import ChatRepository
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_cancelled, raise_if_task_stopped
from src.infrastructure.telegram_queue import TelegramWriteQueue

logger = get_logger(__name__)
# shortcut: after cancellation, give each remaining cooperative adapter close
# half a second; revisit if post-cancellation cleanup routinely requires longer.
ADAPTER_CANCEL_CLEANUP_TIMEOUT = 0.5


class TelethonAdapter(ChatRepository):
    def __init__(
        self,
        session_string: Optional[str],
        api_id: Optional[int],
        api_hash: Optional[str],
    ):
        self.session = StringSession(session_string or "")
        self._source_session_string = session_string
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[ITelethonClient] = None

        self._self_id: Optional[int] = None

        if self.api_id and self.api_hash:
            self.client = RecoveringTelegramClient(
                self.session, self.api_id, self.api_hash
            )  # type: ignore
        else:
            logger.info("adapter_initialized_no_credentials")

        self._event_handler_registered = False
        self._retired = False
        self._is_connected_flag = False
        self._connect_lock = asyncio.Lock()

        # QR Login State
        self._qr_login: Any = None
        self._login_finalized = False
        self._login_attempt_id: Optional[str] = None
        self._expected_login_account_id: Optional[int] = None
        self._qr_task: Optional[asyncio.Task] = None
        self._qr_status = (
            "none"  # none, waiting, authorized, needs_password, expired, error
        )

        # Write queue
        self._write_queue = TelegramWriteQueue()

        # Build collaborators
        self._media = MediaManager(self.client)
        self._parser = MessageParser(self.client, self._media)
        self._chat_query_ops = ChatQueryOps(
            client=self.client, parser=self._parser, media=self._media
        )
        self._forum_ops = ForumOps(client=self.client)
        self._write_ops = WriteOps(
            client=self.client,
            parser=self._parser,
            write_queue=self._write_queue,
            dispatch_fn=None,
            get_topic_name_fn=self._forum_ops.get_topic_name,
            get_topic_name_before_fn=self._forum_ops.get_topic_name,
        )
        self._event_handlers = EventHandlers(
            client=self.client,
            parser=self._parser,
            media=self._media,
            get_topic_name_fn=self._forum_ops.get_topic_name,
            invalidate_topic_name_fn=self._forum_ops.invalidate_topic_name,
            # Do not cache an event title after its shared lookup deadline.
            get_topic_name_before_fn=self._forum_ops.get_topic_name,
        )
        self._write_ops.set_dispatch_fn(self._event_handlers._dispatch)

    def is_connected(self) -> bool:
        return (
            not self._retired
            and self._is_connected_flag
            and self.client is not None
            and self.client.is_connected()
            and self._event_handler_registered
            and self._write_queue.is_running()
        )

    async def connect(self, *, is_current: Optional[Callable[[], bool]] = None):
        async with self._connect_lock:
            raise_if_task_stopped()
            if self._retired or (is_current is not None and not is_current()):
                return
            try:
                await self._connect(is_current=is_current)
            finally:
                # Disconnect may have been cancelled while waiting for this lock.
                await self._disconnect_retired_client()

    async def _disconnect_retired_client(self) -> None:
        if self._retired and self.client and self.client.is_connected():
            await self.client.disconnect()

    async def _connect(self, *, is_current: Optional[Callable[[], bool]] = None):
        def still_current() -> bool:
            raise_if_task_stopped()
            if self._retired or (is_current is not None and not is_current()):
                self._is_connected_flag = False
                return False
            return True

        if not still_current():
            return
        if not self.client:
            logger.warning("connect_skipped_no_client_initialized")
            return

        try:
            if not self.client.is_connected():
                await self.client.connect()

            if not still_current():
                return
            authorized = await self.client.is_user_authorized()
            if not still_current():
                return
            if authorized:
                await self._fetch_self_id(is_current=is_current)
                if not still_current():
                    return
                self._is_connected_flag = True
                await self._write_queue.start()
                if not still_current():
                    return
                self._register_handlers()
            else:
                self._is_connected_flag = False
                logger.warning("client_connected_but_unauthorized")
        except Exception as e:
            raise_if_task_stopped()
            logger.error("connect_failed", category=type(e).__name__)
            self._is_connected_flag = False

    async def _fetch_self_id(self, *, is_current: Optional[Callable[[], bool]] = None):
        """Cache the authenticated user ID for reaction parsing."""
        try:
            raise_if_task_stopped()
            if is_current is not None:
                self._ensure_login_current(is_current)
            if self.client:
                me = await self.client.get_me()
                raise_if_task_stopped()
                if self._retired:
                    return
                if is_current is not None:
                    self._ensure_login_current(is_current)
                if not me or not isinstance(me.id, int):
                    raise RuntimeError("Could not identify the Telegram account")
                self._self_id = me.id
                self._parser.self_id = me.id
                logger.info("self_id_cached", user_id=self._self_id)
        except Exception as e:
            raise_if_task_stopped()
            logger.error("fetch_self_id_failed", category=type(e).__name__)
            raise

    def _register_handlers(self):
        if self.client and not self._event_handler_registered:
            self._event_handlers.register_handlers(self.client)
            self._event_handler_registered = True

    def retire(self) -> None:
        self._retired = True
        # Quiesce writes and QR waits before any asynchronous teardown starts.
        self._write_queue.retire()
        if self._qr_task and not self._qr_task.cancelling():
            self._qr_task.cancel()

    async def disconnect(self):
        self.retire()
        # A reconnect may already be awaiting Telegram. Finish closing only
        # after that attempt exits so it cannot reopen the retired client.
        first_error = None
        cancellation_seen = False

        async def close_component(component, close):
            nonlocal first_error, cancellation_seen
            try:
                task = asyncio.current_task()
                if cancellation_seen or (task is not None and task.cancelling()):
                    async with asyncio.timeout(ADAPTER_CANCEL_CLEANUP_TIMEOUT):
                        await close()
                else:
                    await close()
            except asyncio.CancelledError:
                cancellation_seen = True
                raise
            except Exception as error:
                logger.error(
                    "adapter_cleanup_failed",
                    component=component,
                    category=type(error).__name__,
                )
                if first_error is None:
                    first_error = error

        try:
            async with self._connect_lock:
                async with AsyncExitStack() as cleanup:
                    for component, close in (
                        ("telegram", self._disconnect_retired_client),
                        ("media", self._media.close),
                        ("write_queue", self._write_queue.stop),
                        ("write_operations", self._write_ops.close),
                    ):
                        cleanup.push_async_callback(close_component, component, close)
                    if self._qr_task:
                        # Retirement already cancelled the QR task. Shield the
                        # join so caller cancellation can continue cleanup even
                        # if the waiter takes time to acknowledge cancellation.
                        await asyncio.shield(
                            asyncio.gather(self._qr_task, return_exceptions=True)
                        )
                        self._qr_task = None
        finally:
            self._is_connected_flag = False
        # A component can swallow the caller's cancellation. Finish attempting
        # the remaining closes, but never report a cancelled disconnect as a
        # success to an account-reset or replacement caller.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if first_error is not None:
            raise first_error

    # --- Auth Methods ---

    async def get_password_hint(self) -> str:
        if not self.client:
            return ""
        try:
            pwd_info = await self.client(GetPasswordRequest())
            return pwd_info.hint if pwd_info and pwd_info.hint else "No hint provided"
        except Exception as e:
            logger.error("get_password_hint_failed", category=type(e).__name__)
            return ""

    def _ensure_login_current(
        self, is_current: Optional[Callable[[], bool]] = None
    ) -> None:
        raise_if_task_stopped()
        if self._retired:
            raise RuntimeError("Telegram adapter is retired")
        if is_current is not None and not is_current():
            raise RuntimeError("Telegram login is no longer active")

    async def sign_in_with_password(
        self, password: str, *, is_current: Optional[Callable[[], bool]] = None
    ):
        async with self._connect_lock:
            self._ensure_login_current(is_current)
            if not self.client:
                raise RuntimeError("Client not initialized")

            try:
                logger.info("2fa_attempt")
                await self.client.sign_in(password=password)
                self._ensure_login_current(is_current)
                await self._fetch_self_id(is_current=is_current)
                self._ensure_login_current(is_current)
                self._qr_status = "authorized"
                logger.info("2fa_success")
            except Exception as e:
                logger.error("2fa_failed", category=type(e).__name__)
                raise
            finally:
                try:
                    await self._disconnect_retired_client()
                finally:
                    raise_if_task_stopped()

    # --- QR Code Logic ---

    async def start_qr_login(
        self, *, is_current: Optional[Callable[[], bool]] = None
    ) -> str:
        async with self._connect_lock:
            self._ensure_login_current(is_current)
            if not self.client:
                raise RuntimeError("Client not initialized")

            try:
                if not self.client.is_connected():
                    await self.client.connect()
                self._ensure_login_current(is_current)

                authorized = await self.client.is_user_authorized()
                self._ensure_login_current(is_current)
                if authorized:
                    await self._fetch_self_id(is_current=is_current)
                    self._ensure_login_current(is_current)
                    self._qr_status = "authorized"
                    return "authorized"

                if self._qr_task:
                    self._qr_task.cancel()

                qr_login = await self.client.qr_login()
                self._ensure_login_current(is_current)
                self._qr_login = qr_login
                self._qr_status = "waiting"
                self._qr_task = asyncio.create_task(self._qr_wait_loop())
                return self._qr_login.url
            finally:
                # Disconnect may have timed out while waiting for this lock.
                try:
                    if self._retired:
                        self._qr_login = None
                        await self._disconnect_retired_client()
                finally:
                    raise_if_task_stopped()

    async def _qr_wait_loop(self):
        raise_if_cancelled()
        if not self._qr_login:
            logger.error("qr_login_object_missing")
            self._qr_status = "error"
            return

        try:
            logger.info("qr_wait_loop_started")
            await self._qr_login.wait()
            raise_if_cancelled()
            if self._retired:
                return
            await self._fetch_self_id()
            raise_if_cancelled()
            if self._retired:
                return
            self._qr_status = "authorized"
            logger.info("qr_login_success")
        except SessionPasswordNeededError:
            raise_if_cancelled()
            if self._retired:
                return
            self._qr_status = "needs_password"
            logger.info("qr_login_needs_password")
        except asyncio.TimeoutError:
            raise_if_cancelled()
            if self._retired:
                return
            self._qr_status = "expired"
            logger.info("qr_login_expired")
        except Exception as e:
            raise_if_cancelled()
            if self._retired:
                return
            self._qr_status = "error"
            logger.error("qr_login_error", category=type(e).__name__)

    def get_qr_status(self) -> str:
        return self._qr_status

    async def activate_login(
        self,
        session_string: str,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Enable account operations only after the login identity is persisted."""
        self._ensure_login_current(is_current)
        self._source_session_string = session_string
        await self._write_queue.start()
        self._ensure_login_current(is_current)
        self._register_handlers()
        self._is_connected_flag = True

    def get_session_string(self) -> str:
        return self.session.save()

    # --- ChatRepository delegation ---

    async def get_chats(self):
        return await self._chat_query_ops.get_chats()

    async def get_all_unread_chats(self, chat_ids: Optional[List[int]] = None):
        return await self._chat_query_ops.get_all_unread_chats(
            chat_ids, is_current=lambda: not self._retired
        )

    async def get_chat(self, chat_id: int):
        return await self._chat_query_ops.get_chat(
            chat_id, is_current=lambda: not self._retired
        )

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
    ):
        return await self._chat_query_ops.get_messages(
            chat_id,
            limit=limit,
            topic_id=topic_id,
            offset_id=offset_id,
            ids=ids,
            is_current=lambda: not self._retired,
        )

    async def get_message_context(self, chat_id: int, message_id: int):
        return await self._chat_query_ops.get_message_context(
            chat_id, message_id, is_current=lambda: not self._retired
        )

    async def can_selectively_read(
        self, chat_id: int, topic_id: Optional[int], message_id: int
    ) -> bool:
        try:
            return await self._write_ops.can_selectively_read(
                chat_id, topic_id, message_id, is_current=lambda: not self._retired
            )
        except AccountChanged:
            raise
        except Exception as error:
            raise_if_task_stopped()
            if self._retired:
                raise AccountChanged() from error
            raise unavailable(error) from error

    async def get_recent_authors(self, chat_id: int, limit: int = 100):
        return await self._chat_query_ops.get_recent_authors(
            chat_id, limit, is_current=lambda: not self._retired
        )

    async def get_forum_topics(self, chat_id: int, limit: int = 20):
        return await self._forum_ops.get_forum_topics(
            chat_id, limit, is_current=lambda: not self._retired
        )

    async def get_forum_topics_page(
        self, chat_id: int, cursor: Optional[ForumTopicCursor] = None
    ):
        return await self._forum_ops.get_forum_topics_page(
            chat_id, cursor, is_current=lambda: not self._retired
        )

    async def get_forum_topic(self, chat_id: int, topic_id: int):
        return await self._forum_ops.get_forum_topic(
            chat_id, topic_id, is_current=lambda: not self._retired
        )

    async def get_unread_topics(
        self, chat_id: int, topic_ids: Optional[List[int]] = None
    ):
        return await self._forum_ops.get_unread_topics(
            chat_id, topic_ids, is_current=lambda: not self._retired
        )

    async def get_topic_name(self, chat_id: int, topic_id: int):
        return await self._forum_ops.get_topic_name(chat_id, topic_id)

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        return await self._write_ops.mark_as_read(chat_id, topic_id, max_id)

    async def mark_automatic_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
        *,
        selective: bool,
    ) -> None:
        await self._write_ops.mark_automatic_as_read(
            chat_id, topic_id, max_id, validate, on_success, selective=selective
        )

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        return await self._write_ops.send_reaction(chat_id, msg_id, emoji)

    async def send_automatic_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        validate: Callable[[], Awaitable[bool]],
        on_complete: Callable[[Optional[bool]], Awaitable[None]],
    ) -> bool:
        return await self._write_ops.send_automatic_reaction(
            chat_id, msg_id, emoji, validate, on_complete
        )

    async def get_self_premium_status(self) -> bool:
        return await self._chat_query_ops.get_self_premium_status()

    async def download_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[MediaContent]:
        return await self._media.download_media(chat_id, message_id, size_type)

    async def get_chat_avatar(self, chat_id: int) -> Optional[bytes]:
        return await self._media.get_chat_avatar(chat_id)

    def add_event_listener(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ) -> None:
        self._event_handlers.add_event_listener(callback)

    # --- Additional methods used by routes ---

    async def get_custom_emoji_media(self, doc_id: int) -> Optional[MediaContent]:
        return await self._media.get_custom_emoji_media(doc_id)

    async def get_stream_document(self, chat_id: int, message_id: int) -> Any:
        return await self._media.get_document(chat_id, message_id)

    def stream_document(
        self, chat_id: int, message_id: int, document: Any, start: int, length: int
    ):
        return self._media.open_document_stream(
            chat_id, message_id, document, start, length
        )
