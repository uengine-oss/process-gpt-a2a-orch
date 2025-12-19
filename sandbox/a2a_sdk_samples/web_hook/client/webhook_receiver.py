# client/webhook_receiver.py
"""
Webhook 수신 서버 모듈 (HITL 지원)
A2A 서버로부터 Push Notification을 수신하는 FastAPI 서버입니다.
Human-in-the-Loop (HITL) 상태도 감지하여 처리합니다.

사용법:
    단독 실행: python webhook_receiver.py --port 9000
    모듈로 임포트: from webhook_receiver import WebhookReceiver

HITL 지원:
- input_required 상태 감지
- 상태별 이벤트 구분 (완료, HITL, 진행 중)
"""

import asyncio
import json
from pathlib import Path as _Path
from datetime import datetime
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum

import uvicorn
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

# SmartLogger 임포트
import sys
sys.path.insert(0, str(_Path(__file__).parent.parent))
from logger_config import get_logger, LogCategory

# SmartLogger 인스턴스 가져오기
logger = get_logger()
CATEGORY = LogCategory.WEBHOOK_RECEIVER


class TaskNotificationType(str, Enum):
    """알림 유형"""
    COMPLETED = "completed"      # 작업 완료
    INPUT_REQUIRED = "input_required"  # HITL - 사용자 입력 필요
    WORKING = "working"          # 작업 진행 중
    FAILED = "failed"            # 작업 실패
    CANCELED = "canceled"        # 작업 취소
    OTHER = "other"              # 기타


@dataclass
class TaskNotification:
    """수신된 Task 알림 정보"""
    task_id: str
    context_id: str
    state: str
    notification_type: TaskNotificationType
    received_at: datetime
    raw_data: dict
    result_text: Optional[str] = None
    requires_input: bool = False
    input_prompt: Optional[str] = None  # HITL 프롬프트 메시지


@dataclass
class WebhookReceiverState:
    """Webhook 수신 서버의 상태"""
    received_notifications: list[TaskNotification] = field(default_factory=list)
    expected_token: Optional[str] = None
    completion_event: Optional[asyncio.Event] = None
    input_required_event: Optional[asyncio.Event] = None  # HITL 이벤트
    any_significant_event: Optional[asyncio.Event] = None  # HITL 또는 완료 이벤트
    

class WebhookReceiver:
    """
    Webhook 수신 서버 (HITL 지원)
    
    A2A 서버로부터 Push Notification을 수신하고 처리합니다.
    input_required 상태를 감지하여 HITL 워크플로우를 지원합니다.
    
    사용 예시:
        receiver = WebhookReceiver(port=9000, token="my-secret-token")
        
        # 서버 시작 (백그라운드)
        await receiver.start()
        
        # HITL 또는 완료 알림 대기
        notification = await receiver.wait_for_notification(timeout=30)
        
        if notification.notification_type == TaskNotificationType.INPUT_REQUIRED:
            # HITL 처리
            print(f"Input required: {notification.input_prompt}")
        
        # 서버 중지
        await receiver.stop()
    """
    
    def __init__(
        self,
        port: int = 9000,
        host: str = "0.0.0.0",
        token: Optional[str] = None,
        on_notification: Optional[Callable[[TaskNotification], None]] = None,
        on_input_required: Optional[Callable[[TaskNotification], None]] = None,
    ):
        """
        Args:
            port: 수신 서버 포트
            host: 수신 서버 호스트
            token: 인증 토큰 (A2A 서버가 보내는 토큰과 일치해야 함)
            on_notification: 알림 수신 시 호출할 콜백 함수
            on_input_required: HITL 상태 수신 시 호출할 콜백 함수
        """
        self.port = port
        self.host = host
        self.state = WebhookReceiverState(
            expected_token=token,
            completion_event=asyncio.Event(),
            input_required_event=asyncio.Event(),
            any_significant_event=asyncio.Event(),  # HITL 또는 완료 시 설정
        )
        self.on_notification = on_notification
        self.on_input_required = on_input_required
        self._server_task: Optional[asyncio.Task] = None
        self._server: Optional[uvicorn.Server] = None
        self._app = self._create_app()
    
    @property
    def webhook_url(self) -> str:
        """Webhook URL을 반환합니다."""
        return f"http://localhost:{self.port}/webhook/a2a"
    
    def _determine_notification_type(self, state: str) -> TaskNotificationType:
        """상태값으로부터 알림 유형을 결정합니다."""
        state_mapping = {
            "completed": TaskNotificationType.COMPLETED,
            "input-required": TaskNotificationType.INPUT_REQUIRED,
            "working": TaskNotificationType.WORKING,
            "failed": TaskNotificationType.FAILED,
            "canceled": TaskNotificationType.CANCELED,
        }
        return state_mapping.get(state, TaskNotificationType.OTHER)
    
    def _extract_text_from_message(self, message: dict) -> Optional[str]:
        """메시지에서 텍스트를 추출합니다."""
        if not message:
            return None
        
        parts = message.get("parts", [])
        for part in parts:
            # A2A SDK 구조: part.root.text 또는 part.text
            root = part.get("root", {})
            if isinstance(root, dict) and "text" in root:
                return root["text"]
            if "text" in part:
                return part["text"]
        return None
    
    def _create_app(self) -> FastAPI:
        """FastAPI 앱을 생성합니다."""
        app = FastAPI(
            title="A2A Webhook Receiver (HITL Support)",
            description="A2A Push Notification 수신 서버 - HITL 지원",
        )
        
        @app.post("/webhook/a2a")
        async def receive_notification(request: Request):
            """
            A2A 서버로부터 Push Notification을 수신합니다.
            input_required 상태도 감지하여 처리합니다.
            """
            receive_time = datetime.now()
            client_host = request.client.host if request.client else "unknown"
            
            # ========== WEBHOOK RECEIVE START ==========
            logger.log("INFO", "Webhook notification RECEIVED", category=CATEGORY,
                      params={
                          "receive_time": receive_time.isoformat(),
                          "client_ip": client_host,
                      })
            
            # 1. 토큰 검증
            received_token = request.headers.get("X-A2A-Notification-Token")
            
            if self.state.expected_token:
                if received_token != self.state.expected_token:
                    logger.log("WARNING", "Token validation FAILED", category=CATEGORY,
                              params={"action": "Rejecting request"})
                    raise HTTPException(status_code=401, detail="Invalid token")
                logger.log("DEBUG", "Token validation PASSED", category=CATEGORY)
            
            # 2. 요청 바디 파싱
            try:
                body = await request.json()
            except Exception as e:
                logger.log("ERROR", "Request body parsing FAILED", category=CATEGORY,
                          params={"error": str(e)})
                raise HTTPException(status_code=400, detail="Invalid JSON")
            
            # 3. Task 정보 추출
            task_id = body.get("id", "unknown")
            context_id = body.get("contextId", "unknown")
            status = body.get("status", {})
            state = status.get("state", "unknown")
            
            # 알림 유형 결정
            notification_type = self._determine_notification_type(state)
            is_hitl = notification_type == TaskNotificationType.INPUT_REQUIRED
            
            logger.log("INFO", "Task data extracted", category=CATEGORY,
                      params={
                          "task_id": task_id,
                          "context_id": context_id,
                          "state": state,
                          "notification_type": notification_type.value,
                          "is_hitl": is_hitl,
                      })
            
            # 4. 텍스트 추출 (history 또는 status.message에서)
            result_text = None
            input_prompt = None
            
            # history에서 추출
            history = body.get("history", [])
            for msg in history:
                if msg.get("role") != "user":
                    text = self._extract_text_from_message(msg)
                    if text:
                        result_text = text
            
            # status.message에서 추출 (HITL의 경우 이게 프롬프트)
            status_msg = status.get("message", {})
            if status_msg:
                msg_text = self._extract_text_from_message(status_msg)
                if msg_text:
                    if is_hitl:
                        input_prompt = msg_text
                    if not result_text:
                        result_text = msg_text
            
            # 실제 메시지 내용 로깅 (디버깅용)
            if result_text:
                logger.log("INFO", "Message content received from server", category=CATEGORY,
                          params={
                              "task_id": task_id,
                              "state": state,
                              "result_text": result_text,
                          })
            
            if is_hitl and input_prompt:
                logger.log("INFO", "HITL prompt extracted", category=CATEGORY,
                          params={
                              "task_id": task_id,
                              "prompt_preview": input_prompt[:80] + "..." if len(input_prompt) > 80 else input_prompt,
                          })
            
            # 5. 알림 객체 생성
            notification = TaskNotification(
                task_id=task_id,
                context_id=context_id,
                state=state,
                notification_type=notification_type,
                received_at=receive_time,
                raw_data=body,
                result_text=result_text,
                requires_input=is_hitl,
                input_prompt=input_prompt,
            )
            
            # 6. 알림 저장
            self.state.received_notifications.append(notification)
            
            # 7. 콜백 호출
            if self.on_notification:
                try:
                    self.on_notification(notification)
                except Exception as e:
                    logger.log("ERROR", "Notification callback FAILED", category=CATEGORY,
                              params={"error": str(e)})
            
            # 8. HITL 콜백 호출
            if is_hitl and self.on_input_required:
                try:
                    logger.log("INFO", "Executing HITL callback", category=CATEGORY,
                              params={"task_id": task_id})
                    self.on_input_required(notification)
                except Exception as e:
                    logger.log("ERROR", "HITL callback FAILED", category=CATEGORY,
                              params={"error": str(e)})
            
            # 9. 이벤트 설정
            if is_hitl:
                # HITL 이벤트 설정
                self.state.input_required_event.set()
                self.state.any_significant_event.set()  # HITL도 significant event
                logger.log("INFO", "INPUT_REQUIRED event SET", category=CATEGORY,
                          params={"task_id": task_id, "awaiting_user_input": True})
            
            # 터미널 상태인 경우 완료 이벤트 설정
            is_terminal_state = state in ("completed", "failed", "canceled")
            if is_terminal_state:
                self.state.completion_event.set()
                self.state.any_significant_event.set()  # 완료도 significant event
                logger.log("INFO", "Completion event SET", category=CATEGORY,
                          params={"task_id": task_id, "terminal_state": state})
            
            # ========== WEBHOOK RECEIVE END ==========
            process_duration = (datetime.now() - receive_time).total_seconds()
            logger.log("INFO", "Webhook notification processed", category=CATEGORY,
                      params={
                          "task_id": task_id,
                          "state": state,
                          "notification_type": notification_type.value,
                          "process_duration_sec": round(process_duration, 3),
                      })
            
            return JSONResponse(
                content={
                    "status": "received", 
                    "task_id": task_id,
                    "notification_type": notification_type.value,
                },
                status_code=200,
            )
        
        @app.get("/")
        async def root():
            """서버 상태 확인"""
            return {
                "status": "running",
                "webhook_endpoint": "/webhook/a2a",
                "notifications_received": len(self.state.received_notifications),
                "hitl_supported": True,
            }
        
        @app.get("/notifications")
        async def list_notifications():
            """수신된 알림 목록"""
            return {
                "count": len(self.state.received_notifications),
                "notifications": [
                    {
                        "task_id": n.task_id,
                        "context_id": n.context_id,
                        "state": n.state,
                        "notification_type": n.notification_type.value,
                        "requires_input": n.requires_input,
                        "received_at": n.received_at.isoformat(),
                        "result_text": n.result_text,
                        "input_prompt": n.input_prompt,
                    }
                    for n in self.state.received_notifications
                ]
            }
        
        return app
    
    async def start(self):
        """서버를 백그라운드에서 시작합니다."""
        start_time = datetime.now()
        
        logger.log("INFO", "Webhook receiver server STARTING", category=CATEGORY,
                  params={
                      "host": self.host,
                      "port": self.port,
                      "webhook_url": self.webhook_url,
                      "hitl_support": "ENABLED",
                  })
        
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        
        await asyncio.sleep(0.5)
        
        startup_duration = (datetime.now() - start_time).total_seconds()
        logger.log("INFO", "Webhook receiver server STARTED", category=CATEGORY,
                  params={
                      "startup_duration_sec": round(startup_duration, 3),
                      "ready_to_receive": True,
                  })
    
    async def stop(self):
        """서버를 graceful하게 중지합니다."""
        stop_start = datetime.now()
        
        logger.log("INFO", "Webhook receiver server STOPPING", category=CATEGORY)
        
        if self._server:
            self._server.should_exit = True
            
            if self._server_task:
                try:
                    await asyncio.wait_for(self._server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.log("WARNING", "Graceful shutdown TIMEOUT", category=CATEGORY)
                    self._server_task.cancel()
                    try:
                        await self._server_task
                    except asyncio.CancelledError:
                        pass
                except asyncio.CancelledError:
                    pass
        
        stop_duration = (datetime.now() - stop_start).total_seconds()
        logger.log("INFO", "Webhook receiver server STOPPED", category=CATEGORY,
                  params={"stop_duration_sec": round(stop_duration, 3)})
    
    async def wait_for_notification(
        self, 
        timeout: float = 1800.0,
        wait_for_terminal: bool = False,
    ) -> Optional[TaskNotification]:
        """
        알림을 대기합니다.
        
        Args:
            timeout: 대기 시간(초)
            wait_for_terminal: True면 터미널 상태만 대기, False면 HITL 포함 모든 중요 알림 대기
        
        Returns:
            수신된 알림, 타임아웃 시 None
        """
        wait_start = datetime.now()
        logger.log("INFO", "Waiting for notification", category=CATEGORY,
                  params={
                      "timeout_sec": timeout,
                      "wait_for_terminal": wait_for_terminal,
                  })
        
        try:
            # wait_for_terminal이 True면 completion_event만 대기
            # False면 HITL 포함 모든 중요 이벤트 대기
            if wait_for_terminal:
                event_to_wait = self.state.completion_event
            else:
                event_to_wait = self.state.any_significant_event
            
            await asyncio.wait_for(
                event_to_wait.wait(),
                timeout=timeout,
            )
            wait_duration = (datetime.now() - wait_start).total_seconds()
            
            if self.state.received_notifications:
                notification = self.state.received_notifications[-1]
                logger.log("INFO", "Notification received", category=CATEGORY,
                          params={
                              "wait_duration_sec": round(wait_duration, 2),
                              "task_id": notification.task_id,
                              "state": notification.state,
                              "notification_type": notification.notification_type.value,
                          })
                return notification
            
            return None
            
        except asyncio.TimeoutError:
            logger.log("WARNING", "Notification wait TIMEOUT", category=CATEGORY,
                      params={"timeout_sec": timeout})
            return None
    
    async def wait_for_input_required(
        self, 
        timeout: float = 1800.0,
    ) -> Optional[TaskNotification]:
        """
        HITL (input_required) 상태를 대기합니다.
        
        Args:
            timeout: 대기 시간(초)
        
        Returns:
            HITL 알림, 타임아웃 시 None
        """
        wait_start = datetime.now()
        logger.log("INFO", "Waiting for INPUT_REQUIRED state", category=CATEGORY,
                  params={"timeout_sec": timeout})
        
        try:
            await asyncio.wait_for(
                self.state.input_required_event.wait(),
                timeout=timeout,
            )
            wait_duration = (datetime.now() - wait_start).total_seconds()
            
            # HITL 알림 찾기
            for notification in reversed(self.state.received_notifications):
                if notification.notification_type == TaskNotificationType.INPUT_REQUIRED:
                    logger.log("INFO", "INPUT_REQUIRED notification found", category=CATEGORY,
                              params={
                                  "wait_duration_sec": round(wait_duration, 2),
                                  "task_id": notification.task_id,
                                  "prompt": notification.input_prompt[:50] if notification.input_prompt else None,
                              })
                    return notification
            
            return None
            
        except asyncio.TimeoutError:
            logger.log("WARNING", "INPUT_REQUIRED wait TIMEOUT", category=CATEGORY,
                      params={"timeout_sec": timeout})
            return None
    
    def reset_events(self):
        """이벤트를 초기화합니다. 새 대기 사이클을 시작할 때 호출합니다."""
        self.state.completion_event.clear()
        self.state.input_required_event.clear()
        self.state.any_significant_event.clear()
        logger.log("DEBUG", "Events reset", category=CATEGORY)
    
    def get_latest_notification(self) -> Optional[TaskNotification]:
        """가장 최근 알림을 반환합니다."""
        if self.state.received_notifications:
            return self.state.received_notifications[-1]
        return None
    
    def get_hitl_notifications(self) -> list[TaskNotification]:
        """HITL 알림만 반환합니다."""
        return [
            n for n in self.state.received_notifications 
            if n.notification_type == TaskNotificationType.INPUT_REQUIRED
        ]


def create_standalone_app(token: Optional[str] = None) -> FastAPI:
    """단독 실행용 FastAPI 앱을 생성합니다."""
    receiver = WebhookReceiver(token=token)
    return receiver._app


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="A2A Webhook Receiver (HITL Support)")
    parser.add_argument("--port", type=int, default=9000, help="수신 서버 포트")
    parser.add_argument("--host", default="0.0.0.0", help="수신 서버 호스트")
    parser.add_argument("--token", default=None, help="인증 토큰")
    
    args = parser.parse_args()
    
    logger.log("INFO", "A2A Webhook Receiver starting (standalone mode)", category=CATEGORY,
              params={
                  "host": args.host,
                  "port": args.port,
                  "webhook_endpoint": f"http://localhost:{args.port}/webhook/a2a",
                  "hitl_support": "ENABLED",
              })
    
    app = create_standalone_app(token=args.token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
