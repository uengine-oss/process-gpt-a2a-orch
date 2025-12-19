from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from a2a.types import TaskState, TaskStatusUpdateEvent, TaskArtifactUpdateEvent
from a2a.utils import new_agent_text_message, new_text_artifact
from processgpt_agent_sdk.processgpt_agent_framework import ProcessGPTEventQueue

from a2a_agent_webhook_receiver.database import (
    fetch_task_started_job_id_for_todolist,
    fetch_workitem_by_id,
)
from a2a_agent_webhook_receiver.models import TaskNotification, TaskNotificationType
from a2a_agent_webhook_receiver.smart_logger import SmartLogger
from a2a_form_processor import generate_output_json


class NotificationProcessor:
    """
    Stateless webhook notification processor.

    - Looks up todolist row from DB
    - Emits events/artifacts to ProcessGPT via ProcessGPTEventQueue
    """

    def __init__(self) -> None:
        self._log_category = "A2A_WEBHOOK_RECEIVER"

    async def handle(self, notification: TaskNotification) -> None:
        if notification.notification_type == TaskNotificationType.COMPLETED:
            await self._handle_task_completed(notification)
            return

        if notification.notification_type in (
            TaskNotificationType.FAILED,
            TaskNotificationType.CANCELED,
        ):
            await self._handle_task_failed(
                notification,
                reason=f"A2A task {notification.notification_type.value}",
            )
            return

        if notification.notification_type == TaskNotificationType.INPUT_REQUIRED:
            # 정책 유지: webhook mode에서 HITL 미지원
            await self._handle_task_failed(
                notification, reason="HITL not supported in webhook mode"
            )
            return

        # WORKING/OTHER: stateless receiver에서는 저장하지 않음(로그만)
        SmartLogger.log(
            "INFO",
            "Ignoring notification (no-op)",
            category=self._log_category,
            params={
                "notification_type": notification.notification_type.value,
                "state": notification.state,
                "todolist_id": notification.todolist_id,
                "a2a_task_id": notification.task_id,
            },
        )

    async def _handle_task_completed(self, notification: TaskNotification) -> None:
        todolist_id = notification.todolist_id
        workitem_data = await fetch_workitem_by_id(todolist_id)
        if not workitem_data:
            SmartLogger.log(
                "ERROR",
                "Workitem not found for completed task",
                category=self._log_category,
                params={"todolist_id": todolist_id},
            )
            return

        row = workitem_data[0]
        proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
        event_queue = ProcessGPTEventQueue(
            todolist_id=todolist_id,
            agent_orch="a2a",
            proc_inst_id=proc_inst_id,
        )

        result = notification.result_text or "Task completed"
        started_job_id = await fetch_task_started_job_id_for_todolist(todolist_id)
        job_uuid = started_job_id or str(uuid.uuid4())
        SmartLogger.log(
            "INFO",
            "Resolved job_id for task completion event",
            category=self._log_category,
            params={
                "todolist_id": todolist_id,
                "job_id": job_uuid,
                "source": "task_started" if started_job_id else "generated",
            },
        )

        # task crew 완료
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.completed,
            message_content=result,
            proc_inst_id=proc_inst_id,
            task_id=todolist_id,
            job_uuid=job_uuid,
            event_type="task_completed",
            crew_type="task",
        )

        # result crew: form_processor 적용 (기존 정책과 동일)
        final_result: Any = result
        result_job_uuid = str(uuid.uuid4())
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.working,
            message_content={
                "role": "최종 결과 반환",
                "name": "최종 결과 반환",
                "goal": "요청된 폼 형식에 맞는 최종 결과를 반환합니다.",
                "agent_profile": "/images/chat-icon.png",
            },
            proc_inst_id=proc_inst_id,
            task_id=todolist_id,
            job_uuid=result_job_uuid,
            event_type="task_started",
            crew_type="result",
        )

        query = row.get("query", "")
        input_text = query.split("[InputData]")[1] if query and "[InputData]" in query else None
        try:
            form_output = await generate_output_json(todolist_id, result, input_text)
            if form_output:
                final_result = form_output
        except Exception as e:
            SmartLogger.log(
                "WARNING",
                "Form output generation failed",
                category=self._log_category,
                params={"todolist_id": todolist_id, "error": str(e)},
            )

        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.completed,
            message_content=final_result,
            proc_inst_id=proc_inst_id,
            task_id=todolist_id,
            job_uuid=result_job_uuid,
            event_type="task_completed",
            crew_type="result",
        )

        # artifact
        event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=new_text_artifact(
                    name="a2a_agent_result",
                    description="A2A Agent 실행 결과",
                    text=json.dumps({"final_result": final_result}, ensure_ascii=False)
                ),
                lastChunk=True,
                contextId=proc_inst_id,
                taskId=todolist_id,
            )
        )

        event_queue.task_done()

    async def _handle_task_failed(
        self, notification: TaskNotification, reason: str
    ) -> None:
        todolist_id = notification.todolist_id
        workitem_data = await fetch_workitem_by_id(todolist_id)
        if not workitem_data:
            SmartLogger.log(
                "ERROR",
                "Workitem not found for failed task",
                category=self._log_category,
                params={"todolist_id": todolist_id, "reason": reason},
            )
            return

        row = workitem_data[0]
        proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
        event_queue = ProcessGPTEventQueue(
            todolist_id=todolist_id,
            agent_orch="a2a",
            proc_inst_id=proc_inst_id,
        )

        started_job_id = await fetch_task_started_job_id_for_todolist(todolist_id)
        job_uuid = started_job_id or str(uuid.uuid4())
        SmartLogger.log(
            "INFO",
            "Resolved job_id for task failure event",
            category=self._log_category,
            params={
                "todolist_id": todolist_id,
                "job_id": job_uuid,
                "source": "task_started" if started_job_id else "generated",
                "reason": reason,
            },
        )
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.failed,
            message_content={
                "role": "시스템 오류 알림",
                "name": "시스템 오류 알림",
                "goal": "오류 원인과 대처 안내를 전달합니다.",
                "agent_profile": "/images/chat-icon.png",
                "error": reason,
            },
            proc_inst_id=proc_inst_id,
            task_id=todolist_id,
            job_uuid=job_uuid,
            event_type="task_failed",
            crew_type="task",
        )

        event_queue.task_done()

    def _enqueue_task_status_event(
        self,
        event_queue: ProcessGPTEventQueue,
        state: TaskState,
        message_content: Any,
        proc_inst_id: str,
        task_id: str,
        job_uuid: str,
        event_type: str,
        crew_type: str,
    ) -> None:
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": state,
                    "message": new_agent_text_message(
                        json.dumps(message_content, ensure_ascii=False)
                        if isinstance(message_content, dict)
                        else message_content,
                        proc_inst_id,
                        task_id,
                    ),
                },
                final=False,
                contextId=proc_inst_id,
                taskId=task_id,
                metadata={
                    "crew_type": crew_type,
                    "event_type": event_type,
                    "job_id": job_uuid,
                },
            )
        )


