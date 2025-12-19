from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskNotificationType(str, Enum):
    """A2A task notification type derived from task.status.state."""

    COMPLETED = "completed"
    INPUT_REQUIRED = "input_required"
    WORKING = "working"
    FAILED = "failed"
    CANCELED = "canceled"
    OTHER = "other"


@dataclass
class TaskNotification:
    """Parsed webhook payload from A2A server."""

    task_id: str  # A2A task ID
    todolist_id: str  # ProcessGPT todo id (from URL)
    context_id: str
    state: str
    notification_type: TaskNotificationType
    received_at: datetime
    raw_data: dict
    result_text: Optional[str] = None
    requires_input: bool = False
    input_prompt: Optional[str] = None


