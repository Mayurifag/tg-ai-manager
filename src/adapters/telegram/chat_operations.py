import asyncio
import traceback
from typing import Any, Dict, List, Optional

from telethon import errors, functions, types, utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputDialogPeer, MessageActionTopicCreate

from src.adapters.telethon_mappers import (
    format_message_preview,
    map_telethon_dialog_to_chat_type,
)
from src.domain.models import Chat, ChatType, Message, SystemEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class ChatOperationsMixin:
    def __init__(self):
        self.client: Any = None

    # Abstract dependencies
    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        raise NotImplementedError

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        raise NotImplementedError

    async def _parse_message(
        self,
        msg: Any,
        replies_map: Dict[int, Any] | None = None,
        chat_id: Optional[int] = None,
    ) -> Message:
        raise NotImplementedError

    def _extract_text(self, msg: Any) -> str:
        raise NotImplementedError

    async def _fetch_forum_topics_response(
        self, peer: Any, limit: int
    ) -> Optional[Any]:
        try:
            return await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=peer,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=limit,
                    q="",
                )
            )
        except Exception as e:
            logger.error("fetch_topics_failed", error=str(e))
            return None

    async def _get_top_messages_map(
        self, entity: Any, top_message_ids: List[int]
    ) -> Dict[int, Any]:
        messages_map = {}
        if top_message_ids:
            try:
                msgs = await self.client.get_messages(entity, ids=top_message_ids)
                if msgs:
                    for m in msgs:
                        if m:
                            messages_map[m.id] = m
            except Exception:
                pass
        return messages_map

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = map_telethon_dialog_to_chat_type(d)
            unread_count = d.unread_count
            unread_topics_count = None

            image_url = await self._get_chat_image(d.entity, d.id)

            msg = getattr(d, "message", None)
            if msg:
                self._cache_message_chat(msg.id, d.id)

            preview = format_message_preview(msg, chat_type, {})

            results.append(
                Chat(
                    id=d.id,
                    name=d.name,
                    unread_count=unread_count,
                    type=chat_type,
                    unread_topics_count=unread_topics_count,
                    last_message_preview=preview,
                    image_url=image_url,
                    is_pinned=d.pinned,
                )
            )
        return results

    async def get_all_unread_chats(self) -> List[Chat]:
        """
        Iterates over all dialogs (Main + Archive) to find those with unread messages.
        Includes non-blocking yields to keep the event loop responsive.
        """
        results = []
        try:
            # We iterate over folder 0 (Main) and folder 1 (Archive)
            folders = [0, 1]

            for folder_id in folders:
                count = 0
                async for d in self.client.iter_dialogs(
                    limit=None, ignore_migrated=True, folder=folder_id
                ):
                    count += 1
                    # Yield control to event loop every 50 items to prevent blocking
                    if count % 50 == 0:
                        await asyncio.sleep(0)

                    if d.unread_count > 0 or d.unread_mentions_count > 0:
                        chat_type = map_telethon_dialog_to_chat_type(d)

                        # Basic chat object
                        chat = Chat(
                            id=d.id,
                            name=d.name,
                            unread_count=d.unread_count,
                            type=chat_type,
                        )
                        results.append(chat)

        except Exception as e:
            logger.error("get_all_unread_chats_failed", error=str(e))

        return results

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            name = utils.get_display_name(entity)

            c_type = ChatType.GROUP
            if isinstance(entity, types.User):
                c_type = ChatType.USER
            elif isinstance(entity, types.Chat):
                c_type = ChatType.GROUP
            elif isinstance(entity, types.Channel):
                if getattr(entity, "forum", False):
                    c_type = ChatType.FORUM
                elif getattr(entity, "broadcast", False):
                    c_type = ChatType.CHANNEL
                else:
                    c_type = ChatType.GROUP

            image_url = await self._get_chat_image(entity, chat_id)

            unread_count = 0
            unread_topics_count = None
            last_message_preview = None
            is_pinned = False

            try:
                input_peer = utils.get_input_peer(entity)
                # Pylance ignore: Telethon InputPeer unions are complex
                res = await self.client(
                    GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)])  # type: ignore
                )
                if res.dialogs:
                    dialog = res.dialogs[0]
                    unread_count = dialog.unread_count
                    is_pinned = dialog.pinned
            except Exception as e:
                logger.warning(
                    "fetch_dialog_stats_failed", chat_id=chat_id, error=str(e)
                )

            try:
                messages = await self.client.get_messages(entity, limit=1)
                if messages:
                    latest_msg = messages[0]
                    self._cache_message_chat(latest_msg.id, chat_id)  # Update Cache
                    last_message_preview = format_message_preview(
                        latest_msg, c_type, {}
                    )
                else:
                    last_message_preview = "No messages"
            except Exception as e:
                logger.warning(
                    "fetch_latest_message_failed", chat_id=chat_id, error=str(e)
                )

            return Chat(
                id=chat_id,
                name=name,
                unread_count=unread_count,
                type=c_type,
                unread_topics_count=unread_topics_count,
                image_url=image_url,
                last_message_preview=last_message_preview,
                is_pinned=is_pinned,
            )
        except Exception as e:
            logger.error("get_chat_failed", chat_id=chat_id, error=str(e))
            return None

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
    ) -> List[Message]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(
                entity, limit=limit, reply_to=topic_id, offset_id=offset_id
            )
            reply_ids = []
            for msg in messages:
                self._cache_message_chat(msg.id, chat_id)
                reply_header = getattr(msg, "reply_to", None)
                if reply_header:
                    rid = getattr(reply_header, "reply_to_msg_id", None)
                    if rid:
                        reply_ids.append(rid)

            replies_map = {}
            if reply_ids:
                try:
                    reply_ids = list(set(reply_ids))
                    replied_msgs = await self.client.get_messages(entity, ids=reply_ids)
                    for r in replied_msgs:
                        if r:
                            replies_map[r.id] = r
                except Exception:
                    pass

            result_messages = []
            for msg in messages:
                parsed = await self._parse_message(msg, replies_map, chat_id=chat_id)
                result_messages.append(parsed)
            return result_messages
        except Exception as e:
            logger.error("get_messages_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            response = await self._fetch_forum_topics_response(entity, limit=limit)
            topics = []
            if not response:
                return topics

            valid_topics = [
                t for t in response.topics if not isinstance(t, types.ForumTopicDeleted)
            ]

            top_message_ids = [t.top_message for t in valid_topics]
            messages_map = await self._get_top_messages_map(entity, top_message_ids)
            for t in valid_topics:
                last_msg = messages_map.get(t.top_message)
                preview = format_message_preview(last_msg, ChatType.TOPIC)
                topics.append(
                    Chat(
                        id=t.id,
                        name=t.title,
                        unread_count=t.unread_count,
                        type=ChatType.TOPIC,
                        last_message_preview=preview,
                        icon_emoji=getattr(t, "icon_emoji", None),
                    )
                )
            return topics
        except Exception as e:
            logger.error("get_forum_topics_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_unread_topics(self, chat_id: int) -> List[Chat]:
        """
        Fetches all topics that have unread messages.
        We have to iterate fairly deeply to ensure we get them all,
        but typically 'limit' in GetForumTopicsRequest acts on the topic list order.
        Topics with new messages usually bubble up, so a limit of 50 or 100 should cover active ones.
        """
        try:
            entity = await self.client.get_entity(chat_id)
            # Fetch a reasonable amount of topics to find unread ones.
            # Usually recent topics are at the top.
            response = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                    q="",
                )
            )

            topics = []
            if not response:
                return topics

            for t in response.topics:
                if isinstance(t, types.ForumTopicDeleted):
                    continue
                if t.unread_count > 0:
                    topics.append(
                        Chat(
                            id=t.id,
                            name=t.title,
                            unread_count=t.unread_count,
                            type=ChatType.TOPIC,
                        )
                    )
            return topics
        except Exception as e:
            logger.error("get_unread_topics_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, ids=[topic_id])

            if messages:
                message = messages[0]
                if message and getattr(message, "action", None):
                    if isinstance(message.action, MessageActionTopicCreate):
                        return message.action.title
        except Exception as e:
            logger.error(
                "get_topic_name_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                error=str(e),
            )
        return None

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        try:
            entity = await self.client.get_entity(chat_id)
            chat_name = utils.get_display_name(entity)
            topic_name = None

            if topic_id:
                try:
                    read_max_id = max_id
                    if not read_max_id:
                        # We need the max_id to mark as read properly.
                        # This fetches the latest message in that topic to use as the read pointer.
                        msgs = await self.client.get_messages(
                            entity, limit=1, reply_to=topic_id
                        )
                        read_max_id = msgs[0].id if msgs else topic_id

                    await self.client(
                        functions.messages.ReadDiscussionRequest(
                            peer=entity, msg_id=topic_id, read_max_id=read_max_id
                        )
                    )

                    # Try fetching topic name for event
                    topic_name = await self.get_topic_name(chat_id, topic_id)
                except Exception as e:
                    if "TOPIC_ID_INVALID" in str(e):
                        logger.warning(
                            "mark_read_topic_invalid",
                            chat_id=chat_id,
                            topic_id=topic_id,
                        )
                    else:
                        raise e
            else:
                # Standard chat or channel
                if max_id:
                    await self.client.send_read_acknowledge(entity, max_id=max_id)
                else:
                    await self.client.send_read_acknowledge(entity)

            # Broadcast event to frontend to clear badge
            # We call self._dispatch which comes from EventHandlersMixin in the Adapter
            if hasattr(self, "_dispatch"):
                event = SystemEvent(
                    type="read",
                    text="Marked as read",
                    chat_name=chat_name,
                    topic_name=topic_name,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    is_read=True,
                    link=f"/chat/{chat_id}",
                )
                await getattr(self, "_dispatch")(event)

        except Exception as e:
            logger.error(
                "mark_as_read_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        """
        Toggles the reaction.
        Attempts to STACK reactions (add to existing).
        If user is not premium (or limit reached), falls back to REPLACING.
        Explicitly fetches the message state after action to broadcast an update.
        """
        try:
            entity = await self.client.get_entity(chat_id)

            # 1. Prepare the Target Reaction Object
            target_reaction = None
            if emoji.isdigit():
                # It's a Custom Emoji ID
                target_reaction = types.ReactionCustomEmoji(document_id=int(emoji))
            else:
                # Standard Emoji
                target_reaction = types.ReactionEmoji(emoticon=emoji)

            # 2. Fetch current message to see existing reactions by ME
            # We must fetch fresh to know what to keep/remove
            msgs = await self.client.get_messages(entity, ids=[msg_id])
            if not msgs:
                return False
            msg = msgs[0]

            current_my_reactions = []
            if hasattr(msg, "reactions") and msg.reactions:
                for rc in msg.reactions.results:
                    if getattr(rc, "chosen", False):
                        current_my_reactions.append(rc.reaction)

            # 3. Modify List (Toggle Logic)
            new_reactions_list = []
            found = False

            for r in current_my_reactions:
                # Check equality:
                is_same = False
                if isinstance(r, types.ReactionEmoji) and isinstance(
                    target_reaction, types.ReactionEmoji
                ):
                    if r.emoticon == target_reaction.emoticon:
                        is_same = True
                elif isinstance(r, types.ReactionCustomEmoji) and isinstance(
                    target_reaction, types.ReactionCustomEmoji
                ):
                    if r.document_id == target_reaction.document_id:
                        is_same = True

                if is_same:
                    found = True
                    # Remove it (don't add to new list)
                else:
                    new_reactions_list.append(r)

            if not found:
                new_reactions_list.append(target_reaction)

            # 4. Try Stacking (Send Full List)
            success = False
            try:
                # Pylance ignore: Reaction list typing mismatch
                await self.client(
                    functions.messages.SendReactionRequest(
                        peer=entity,
                        msg_id=msg_id,
                        reaction=new_reactions_list,  # type: ignore
                        add_to_recent=True,
                    )
                )
                success = True
            except errors.ReactionInvalidError:
                # Fallback: Just send the single reaction (Replace all)
                logger.info(
                    "reaction_stack_failed_fallback_replace",
                    chat_id=chat_id,
                    msg_id=msg_id,
                )
                fallback_list = []
                if not found:
                    fallback_list = [target_reaction]

                await self.client(
                    functions.messages.SendReactionRequest(
                        peer=entity,
                        msg_id=msg_id,
                        reaction=fallback_list,  # type: ignore
                        add_to_recent=True,
                    )
                )
                success = True

            # 5. Force Fetch & Broadcast Update
            if success:
                try:
                    updated_msgs = await self.client.get_messages(entity, ids=[msg_id])
                    if updated_msgs:
                        updated_msg = updated_msgs[0]
                        # self._parse_message comes from MessageParserMixin which Adapter inherits
                        if hasattr(self, "_parse_message") and hasattr(
                            self, "_dispatch"
                        ):
                            parsed_msg = await getattr(self, "_parse_message")(
                                updated_msg, chat_id=chat_id
                            )

                            event = SystemEvent(
                                type="reaction_update",
                                text="",
                                chat_name="",
                                chat_id=chat_id,
                                message_model=parsed_msg,
                            )
                            await getattr(self, "_dispatch")(event)
                except Exception as ex:
                    # Log with full details
                    logger.error(
                        "post_reaction_fetch_failed",
                        error=repr(ex),
                        traceback=traceback.format_exc(),
                    )

            return success

        except Exception as e:
            logger.error(
                "send_reaction_failed", error=str(e), chat_id=chat_id, msg_id=msg_id
            )
            return False

    async def get_self_premium_status(self) -> bool:
        """
        Fetches the current user from the API to bypass local cache and check Premium status.
        """
        try:
            if not self.client:
                return False
            # force fetch
            users = await self.client(
                functions.users.GetUsersRequest(id=[types.InputUserSelf()])
            )
            if users:
                return getattr(users[0], "premium", False)
            return False
        except Exception as e:
            logger.error("get_premium_status_failed", error=str(e))
            return False
