from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from processgpt_agent_sdk import initialize_db

from a2a_agent_webhook_receiver.models import TaskNotification, TaskNotificationType
from a2a_agent_webhook_receiver.processor import NotificationProcessor
from a2a_agent_webhook_receiver.smart_logger import SmartLogger

LOG_CATEGORY = "A2A_WEBHOOK_RECEIVER"


def _determine_notification_type(state: str) -> TaskNotificationType:
    # A2A SDK uses 'input-required' in payload for input_required
    mapping = {
        "completed": TaskNotificationType.COMPLETED,
        "input-required": TaskNotificationType.INPUT_REQUIRED,
        "working": TaskNotificationType.WORKING,
        "failed": TaskNotificationType.FAILED,
        "canceled": TaskNotificationType.CANCELED,
    }
    return mapping.get(state, TaskNotificationType.OTHER)


def _extract_text_from_message(message: dict) -> Optional[str]:
    if not message:
        return None
    parts = message.get("parts", []) or []
    for part in parts:
        root = part.get("root", {}) if isinstance(part, dict) else {}
        if isinstance(root, dict) and "text" in root:
            return root.get("text")
        if isinstance(part, dict) and "text" in part:
            return part.get("text")
    return None


def create_app() -> FastAPI:
    app = FastAPI(
        title="A2A Webhook Receiver",
        description="A2A Push Notification receiver (separate pod, stateless)",
    )

    processor = NotificationProcessor()

    @app.post("/webhook/a2a/todolist/{todolist_id}")
    async def receive_notification(
        request: Request,
        todolist_id: str = Path(..., description="ProcessGPT todolist id"),
    ):
        receive_time = datetime.now()
        client_host = request.client.host if request.client else "unknown"

        try:
            body = await request.json()
        except Exception as e:
            SmartLogger.log(
                "ERROR",
                "Invalid JSON payload",
                category=LOG_CATEGORY,
                params={"todolist_id": todolist_id, "client_ip": client_host, "error": str(e)},
            )
            raise HTTPException(status_code=400, detail="Invalid JSON")

        task_id = body.get("id", "unknown")
        context_id = body.get("contextId", "unknown")
        status = body.get("status", {}) or {}
        state = status.get("state", "unknown")
        notification_type = _determine_notification_type(state)
        is_hitl = notification_type == TaskNotificationType.INPUT_REQUIRED

        SmartLogger.log(
            "INFO",
            "Webhook notification received",
            category=LOG_CATEGORY,
            params={
                "todolist_id": todolist_id,
                "a2a_task_id": task_id,
                "context_id": context_id,
                "state": state,
                "notification_type": notification_type.value,
            },
        )

        # Extract message content
        result_text = None
        input_prompt = None

        history = body.get("history", []) or []
        for msg in history:
            if isinstance(msg, dict) and msg.get("role") != "user":
                text = _extract_text_from_message(msg)
                if text:
                    result_text = text

        status_msg = status.get("message", {}) or {}
        msg_text = _extract_text_from_message(status_msg) if isinstance(status_msg, dict) else None
        if msg_text:
            if is_hitl:
                input_prompt = msg_text
            if not result_text:
                result_text = msg_text

        notification = TaskNotification(
            task_id=task_id,
            todolist_id=todolist_id,
            context_id=context_id,
            state=state,
            notification_type=notification_type,
            received_at=receive_time,
            raw_data=body,
            result_text=result_text,
            requires_input=is_hitl,
            input_prompt=input_prompt,
        )

        try:
            await processor.handle(notification)
        except Exception as e:
            # do not fail webhook request hard unless needed
            SmartLogger.log(
                "ERROR",
                "Notification processing failed",
                category=LOG_CATEGORY,
                params={"todolist_id": todolist_id, "a2a_task_id": task_id, "error": str(e)},
            )

        return JSONResponse(
            content={
                "status": "received",
                "todolist_id": todolist_id,
                "a2a_task_id": task_id,
                "notification_type": notification_type.value,
            },
            status_code=200,
        )

    @app.get("/health")
    async def health():
        return {"status": "healthy", "timestamp": datetime.now().isoformat()}

    return app


def main() -> None:
    initialize_db()
    
    host = os.getenv("WEBHOOK_RECEIVER_HOST", "0.0.0.0")
    port = int(os.getenv("WEBHOOK_RECEIVER_PORT", "9000"))

    SmartLogger.log(
        "INFO",
        "A2A webhook receiver starting",
        category=LOG_CATEGORY,
        params={"host": host, "port": port},
    )

    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level=os.getenv("WEBHOOK_RECEIVER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()


