# -*- coding: utf-8 -*-
import random
from typing import Optional
from uuid import UUID, uuid4

import oasst_backend.models.db_payload as db_payload
from loguru import logger
from oasst_backend.exceptions import OasstError, OasstErrorCode
from oasst_backend.journal_writer import JournalWriter
from oasst_backend.models import ApiClient, Message, MessageReaction, Task, TextLabels, User
from oasst_backend.models.payload_column_type import PayloadContainer
from oasst_shared.schemas import protocol as protocol_schema
from sqlmodel import Session, func


class PromptRepository:
    def __init__(self, db: Session, api_client: ApiClient, user: Optional[protocol_schema.User]):
        self.db = db
        self.api_client = api_client
        self.user = self.lookup_user(user)
        self.user_id = self.user.id if self.user else None
        self.journal = JournalWriter(db, api_client, self.user)

    def lookup_user(self, client_user: protocol_schema.User) -> User:
        if not client_user:
            return None
        user: User = (
            self.db.query(User)
            .filter(
                User.api_client_id == self.api_client.id,
                User.username == client_user.id,
                User.auth_method == client_user.auth_method,
            )
            .first()
        )
        if user is None:
            # user is unknown, create new record
            user = User(
                username=client_user.id,
                display_name=client_user.display_name,
                api_client_id=self.api_client.id,
                auth_method=client_user.auth_method,
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)
        elif client_user.display_name and client_user.display_name != user.display_name:
            # we found the user but the display name changed
            user.display_name = client_user.display_name
            self.db.add(user)
            self.db.commit()
        return user

    def validate_frontend_message_id(self, message_id: str) -> None:
        if not isinstance(message_id, str):
            raise OasstError(
                f"message_id must be string, not {type(message_id)}", OasstErrorCode.INVALID_FRONTEND_MESSAGE_ID
            )
        if not message_id:
            raise OasstError("message_id must not be empty", OasstErrorCode.INVALID_FRONTEND_MESSAGE_ID)

    def bind_frontend_message_id(self, task_id: UUID, frontend_message_id: str):
        self.validate_frontend_message_id(frontend_message_id)

        # find task
        task: Task = self.db.query(Task).filter(Task.id == task_id, Task.api_client_id == self.api_client.id).first()
        if task is None:
            raise OasstError(f"Task for {task_id=} not found", OasstErrorCode.TASK_NOT_FOUND)
        if task.expired:
            raise OasstError("Task already expired.", OasstErrorCode.TASK_EXPIRED)
        if task.done or task.ack is not None:
            raise OasstError("Task already updated.", OasstErrorCode.TASK_ALREADY_UPDATED)

        task.frontend_message_id = frontend_message_id
        task.ack = True
        # ToDo: check race-condition, transaction
        self.db.add(task)
        self.db.commit()

    def acknowledge_task_failure(self, task_id):
        # find task
        task: Task = self.db.query(Task).filter(Task.id == task_id, Task.api_client_id == self.api_client.id).first()
        if task is None:
            raise OasstError(f"Task for {task_id=} not found", OasstErrorCode.TASK_NOT_FOUND)
        if task.expired:
            raise OasstError("Task already expired.", OasstErrorCode.TASK_EXPIRED)
        if task.done or task.ack is not None:
            raise OasstError("Task already updated.", OasstErrorCode.TASK_ALREADY_UPDATED)

        task.ack = False
        # ToDo: check race-condition, transaction
        self.db.add(task)
        self.db.commit()

    def fetch_message_by_frontend_message_id(self, frontend_message_id: str, fail_if_missing: bool = True) -> Message:
        self.validate_frontend_message_id(frontend_message_id)
        message: Message = (
            self.db.query(Message)
            .filter(Message.api_client_id == self.api_client.id, Message.frontend_message_id == frontend_message_id)
            .one_or_none()
        )
        if fail_if_missing and message is None:
            raise OasstError(
                f"Message with frontend_message_id {frontend_message_id} not found.", OasstErrorCode.MESSAGE_NOT_FOUND
            )
        return message

    def fetch_task_by_frontend_message_id(self, message_id: str) -> Task:
        self.validate_frontend_message_id(message_id)
        task = (
            self.db.query(Task)
            .filter(Task.api_client_id == self.api_client.id, Task.frontend_message_id == message_id)
            .one_or_none()
        )
        return task

    def store_text_reply(
        self, text: str, frontend_message_id: str, user_frontend_message_id: str, role: str = None
    ) -> Message:
        self.validate_frontend_message_id(frontend_message_id)
        self.validate_frontend_message_id(user_frontend_message_id)

        task = self.fetch_task_by_frontend_message_id(frontend_message_id)

        if task is None:
            raise OasstError(f"Task for {frontend_message_id=} not found", OasstErrorCode.TASK_NOT_FOUND)
        if task.expired:
            raise OasstError("Task already expired.", OasstErrorCode.TASK_EXPIRED)
        if not task.ack:
            raise OasstError("Task is not acknowledged.", OasstErrorCode.TASK_NOT_ACK)
        if task.done:
            raise OasstError("Task already done.", OasstErrorCode.TASK_ALREADY_DONE)

        # If there's no parent message assume user started new conversation
        role = "prompter"
        depth = 0

        if task.parent_message_id:
            parent_message = self.fetch_message(task.parent_message_id)
            parent_message.children_count += 1
            self.db.add(parent_message)

            depth = parent_message.depth + 1
            if parent_message.role == "assistant":
                role = "prompter"
            else:
                role = "assistant"

        # create reply message
        new_message_id = uuid4()
        user_message = self.insert_message(
            message_id=new_message_id,
            frontend_message_id=user_frontend_message_id,
            parent_id=task.parent_message_id,
            message_tree_id=task.message_tree_id or new_message_id,
            task_id=task.id,
            role=role,
            payload=db_payload.MessagePayload(text=text),
            depth=depth,
        )
        if not task.collective:
            task.done = True
            self.db.add(task)
        self.db.commit()
        self.journal.log_text_reply(task=task, message_id=new_message_id, role=role, length=len(text))
        return user_message

    def store_rating(self, rating: protocol_schema.MessageRating) -> MessageReaction:
        message = self.fetch_message_by_frontend_message_id(rating.message_id, fail_if_missing=True)

        task = self.fetch_task_by_frontend_message_id(rating.message_id)
        task_payload: db_payload.RateSummaryPayload = task.payload.payload
        if type(task_payload) != db_payload.RateSummaryPayload:
            raise OasstError(
                f"Task payload type mismatch: {type(task_payload)=} != {db_payload.RateSummaryPayload}",
                OasstErrorCode.TASK_PAYLOAD_TYPE_MISMATCH,
            )

        if rating.rating < task_payload.scale.min or rating.rating > task_payload.scale.max:
            raise OasstError(
                f"Invalid rating value: {rating.rating=} not in {task_payload.scale=}",
                OasstErrorCode.RATING_OUT_OF_RANGE,
            )

        # store reaction to message
        reaction_payload = db_payload.RatingReactionPayload(rating=rating.rating)
        reaction = self.insert_reaction(message.id, reaction_payload)
        if not task.collective:
            task.done = True
            self.db.add(task)

        self.journal.log_rating(task, message_id=message.id, rating=rating.rating)
        logger.info(f"Ranking {rating.rating} stored for task {task.id}.")
        return reaction

    def store_ranking(self, ranking: protocol_schema.MessageRanking) -> MessageReaction:
        # fetch task
        task = self.fetch_task_by_frontend_message_id(ranking.message_id)
        if not task.collective:
            task.done = True
            self.db.add(task)

        task_payload: db_payload.RankConversationRepliesPayload | db_payload.RankInitialPromptsPayload = (
            task.payload.payload
        )

        match type(task_payload):

            case db_payload.RankPrompterRepliesPayload | db_payload.RankAssistantRepliesPayload:
                # validate ranking
                num_replies = len(task_payload.replies)
                if sorted(ranking.ranking) != list(range(num_replies)):
                    raise OasstError(
                        f"Invalid ranking submitted. Each reply index must appear exactly once ({num_replies=}).",
                        OasstErrorCode.INVALID_RANKING_VALUE,
                    )

                # store reaction to message
                reaction_payload = db_payload.RankingReactionPayload(ranking=ranking.ranking)
                reaction = self.insert_reaction(task.id, reaction_payload)
                # TODO: resolve message_id
                self.journal.log_ranking(task, message_id=None, ranking=ranking.ranking)

                logger.info(f"Ranking {ranking.ranking} stored for task {task.id}.")

                return reaction

            case db_payload.RankInitialPromptsPayload:
                # validate ranking
                if sorted(ranking.ranking) != list(range(num_prompts := len(task_payload.prompts))):
                    raise OasstError(
                        f"Invalid ranking submitted. Each reply index must appear exactly once ({num_prompts=}).",
                        OasstErrorCode.INVALID_RANKING_VALUE,
                    )

                # store reaction to message
                reaction_payload = db_payload.RankingReactionPayload(ranking=ranking.ranking)
                reaction = self.insert_reaction(task.id, reaction_payload)
                # TODO: resolve message_id
                self.journal.log_ranking(task, message_id=None, ranking=ranking.ranking)

                logger.info(f"Ranking {ranking.ranking} stored for task {task.id}.")

                return reaction

            case _:
                raise OasstError(
                    f"task payload type mismatch: {type(task_payload)=} != {db_payload.RankConversationRepliesPayload}",
                    OasstErrorCode.TASK_PAYLOAD_TYPE_MISMATCH,
                )

    def store_task(
        self,
        task: protocol_schema.Task,
        message_tree_id: UUID = None,
        parent_message_id: UUID = None,
        collective: bool = False,
    ) -> Task:
        payload: db_payload.TaskPayload
        match type(task):
            case protocol_schema.SummarizeStoryTask:
                payload = db_payload.SummarizationStoryPayload(story=task.story)

            case protocol_schema.RateSummaryTask:
                payload = db_payload.RateSummaryPayload(
                    full_text=task.full_text, summary=task.summary, scale=task.scale
                )

            case protocol_schema.InitialPromptTask:
                payload = db_payload.InitialPromptPayload(hint=task.hint)

            case protocol_schema.PrompterReplyTask:
                payload = db_payload.PrompterReplyPayload(conversation=task.conversation, hint=task.hint)

            case protocol_schema.AssistantReplyTask:
                payload = db_payload.AssistantReplyPayload(type=task.type, conversation=task.conversation)

            case protocol_schema.RankInitialPromptsTask:
                payload = db_payload.RankInitialPromptsPayload(tpye=task.type, prompts=task.prompts)

            case protocol_schema.RankPrompterRepliesTask:
                payload = db_payload.RankPrompterRepliesPayload(
                    tpye=task.type, conversation=task.conversation, replies=task.replies
                )

            case protocol_schema.RankAssistantRepliesTask:
                payload = db_payload.RankAssistantRepliesPayload(
                    tpye=task.type, conversation=task.conversation, replies=task.replies
                )

            case _:
                raise OasstError(f"Invalid task type: {type(task)=}", OasstErrorCode.INVALID_TASK_TYPE)

        task = self.insert_task(
            payload=payload,
            id=task.id,
            message_tree_id=message_tree_id,
            parent_message_id=parent_message_id,
            collective=collective,
        )
        assert task.id == task.id
        return task

    def insert_task(
        self,
        payload: db_payload.TaskPayload,
        id: UUID = None,
        message_tree_id: UUID = None,
        parent_message_id: UUID = None,
        collective: bool = False,
    ) -> Task:
        c = PayloadContainer(payload=payload)
        task = Task(
            id=id,
            user_id=self.user_id,
            payload_type=type(payload).__name__,
            payload=c,
            api_client_id=self.api_client.id,
            message_tree_id=message_tree_id,
            parent_message_id=parent_message_id,
            collective=collective,
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def insert_message(
        self,
        *,
        message_id: UUID,
        frontend_message_id: str,
        parent_id: UUID,
        message_tree_id: UUID,
        task_id: UUID,
        role: str,
        payload: db_payload.MessagePayload,
        payload_type: str = None,
        depth: int = 0,
    ) -> Message:
        if payload_type is None:
            if payload is None:
                payload_type = "null"
            else:
                payload_type = type(payload).__name__

        message = Message(
            id=message_id,
            parent_id=parent_id,
            message_tree_id=message_tree_id,
            task_id=task_id,
            user_id=self.user_id,
            role=role,
            frontend_message_id=frontend_message_id,
            api_client_id=self.api_client.id,
            payload_type=payload_type,
            payload=PayloadContainer(payload=payload),
            depth=depth,
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def insert_reaction(self, task_id: UUID, payload: db_payload.ReactionPayload) -> MessageReaction:
        if self.user_id is None:
            raise OasstError("User required", OasstErrorCode.USER_NOT_SPECIFIED)

        container = PayloadContainer(payload=payload)
        reaction = MessageReaction(
            task_id=task_id,
            user_id=self.user_id,
            payload=container,
            api_client_id=self.api_client.id,
            payload_type=type(payload).__name__,
        )
        self.db.add(reaction)
        self.db.commit()
        self.db.refresh(reaction)
        return reaction

    def store_text_labels(self, text_labels: protocol_schema.TextLabels) -> TextLabels:
        model = TextLabels(
            api_client_id=self.api_client.id,
            text=text_labels.text,
            labels=text_labels.labels,
        )
        if text_labels.has_message_id:
            self.fetch_message_by_frontend_message_id(text_labels.message_id, fail_if_missing=True)
            model.message_id = text_labels.message_id
        self.db.add(model)
        self.db.commit()
        self.db.refresh(model)
        return model

    def fetch_random_message_tree(self, require_role: str = None) -> list[Message]:
        """
        Loads all messages of a random message_tree.

        :param require_role: If set loads only message_tree which has
            at least one message with given role.
        """
        distinct_message_trees = self.db.query(Message.message_tree_id).distinct(Message.message_tree_id)
        if require_role:
            distinct_message_trees = distinct_message_trees.filter(Message.role == require_role)
        distinct_message_trees = distinct_message_trees.subquery()

        random_message_tree = self.db.query(distinct_message_trees).order_by(func.random()).limit(1)
        message_tree_messages = self.db.query(Message).filter(Message.message_tree_id.in_(random_message_tree)).all()
        return message_tree_messages

    def fetch_random_conversation(self, last_message_role: str = None) -> list[Message]:
        """
        Picks a random linear conversation starting from any root message
        and ending somewhere in the message_tree, possibly at the root itself.

        :param last_message_role: If set will form a conversation ending with a message
            created by this role. Necessary for the tasks like "user_reply" where
            the user should reply as a human and hence the last message of the conversation
            needs to have "assistant" role.
        """
        messages_tree = self.fetch_random_message_tree(last_message_role)
        if not messages_tree:
            raise OasstError("No message tree found", OasstErrorCode.NO_MESSAGE_TREE_FOUND)
        if last_message_role:
            conv_messages = [m for m in messages_tree if m.role == last_message_role]
            conv_messages = [random.choice(conv_messages)]
        else:
            conv_messages = [random.choice(messages_tree)]
        messages_tree = {m.id: m for m in messages_tree}

        while True:
            if not conv_messages[-1].parent_id:
                # reached the start of the conversation
                break

            parent_message = messages_tree[conv_messages[-1].parent_id]
            conv_messages.append(parent_message)

        return list(reversed(conv_messages))

    def fetch_random_initial_prompts(self, size: int = 5):
        messages = self.db.query(Message).filter(Message.parent_id.is_(None)).order_by(func.random()).limit(size).all()
        return messages

    def fetch_message_tree(self, message_tree_id: UUID):
        return self.db.query(Message).filter(Message.message_tree_id == message_tree_id).all()

    def fetch_multiple_random_replies(self, max_size: int = 5, message_role: str = None):
        parent = self.db.query(Message.id).filter(Message.children_count > 1)
        if message_role:
            parent = parent.filter(Message.role == message_role)

        parent = parent.order_by(func.random()).limit(1)
        replies = (
            self.db.query(Message).filter(Message.parent_id.in_(parent)).order_by(func.random()).limit(max_size).all()
        )
        if not replies:
            raise OasstError("No replies found", OasstErrorCode.NO_REPLIES_FOUND)

        message_tree = self.fetch_message_tree(replies[0].message_tree_id)
        message_tree = {p.id: p for p in message_tree}
        conversation = [message_tree[replies[0].parent_id]]
        while True:
            if not conversation[-1].parent_id:
                # reached start of the conversation
                break

            parent_message = message_tree[conversation[-1].parent_id]
            conversation.append(parent_message)

        conversation = reversed(conversation)

        return conversation, replies

    def fetch_message(self, message_id: UUID) -> Optional[Message]:
        return self.db.query(Message).filter(Message.id == message_id).one()

    def close_task(self, frontend_message_id: str, allow_personal_tasks: bool = False):
        self.validate_frontend_message_id(frontend_message_id)
        task = self.fetch_task_by_frontend_message_id(frontend_message_id)

        if not task:
            raise OasstError(f"Task for {frontend_message_id=} not found", OasstErrorCode.TASK_NOT_FOUND)
        if task.expired:
            raise OasstError("Task already expired", OasstErrorCode.TASK_EXPIRED)
        if not allow_personal_tasks and not task.collective:
            raise OasstError("This is not a collective task", OasstErrorCode.TASK_NOT_COLLECTIVE)
        if task.done:
            raise OasstError("Allready closed", OasstErrorCode.TASK_ALREADY_DONE)

        task.done = True
        self.db.add(task)
        self.db.commit()
