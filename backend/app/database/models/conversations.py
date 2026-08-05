"""Conversation rows: one row per agent pair conversation, plus its messages.

A conversation is a sequence of ``talk`` messages between exactly two agents,
identified by the sorted pair (agent_a < agent_b) so lookups are unique per
world. Active conversations have ``ended_at IS NULL``; ended rows keep the
reason (leave | distance | max_turns | duplicate) for the cooldown rule and
the REST history endpoint.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Conversation(Base):
    """One conversation between two agents (sorted pair for uniqueness)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_world_agent_a", "world_id", "agent_a"),
        Index("ix_conversations_world_agent_b", "world_id", "agent_b"),
    )

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    agent_a: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_b: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)  # world_time
    ended_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Conversation({self.conversation_id!r}, {self.agent_a!r}<->{self.agent_b!r}, "
            f"turns={self.turns}, ended={self.ended_at is not None})"
        )


class ConversationMessage(Base):
    """One delivered message inside a conversation."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation_id", "conversation_id"),
        Index("ix_conversation_messages_to_read", "to_agent_id", "read"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    from_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(256), nullable=False)
    intent: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[int] = mapped_column(Integer, nullable=False)  # world_time
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ConversationMessage({self.message_id!r}, {self.from_agent_id!r}->"
            f"{self.to_agent_id!r}, intent={self.intent!r})"
        )
