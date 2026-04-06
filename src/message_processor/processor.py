from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class NormalizedMessage:
    """A normalized message with extracted metadata."""

    telegram_id: int
    chat_id: int
    chat_type: str
    user_id: int
    username: str | None
    first_name: str | None
    text: str
    reply_to_message_id: int | None
    is_mention_bot: bool
    is_command: bool
    command: str | None
    command_args: list[str]
    language_code: str | None
    created_at: datetime

    @property
    def is_group(self) -> bool:
        return self.chat_type in ("group", "supergroup")

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"
