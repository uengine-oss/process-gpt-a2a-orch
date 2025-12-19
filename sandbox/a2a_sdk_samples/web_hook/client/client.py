# client/client.py
"""
A2A HITL 클라이언트 모듈 (Push Notification + Sync 모드 자동 스위칭)
Human-in-the-Loop (HITL) 워크플로우를 지원하는 A2A 클라이언트입니다.

사용법:
    python client.py --agent-url http://localhost:8000 --message "예산 승인 요청"

자동 모드 스위칭:
- AgentCard의 push_notifications capability를 확인
- push_notifications=True: Webhook 방식 (비동기)
- push_notifications=False: Sync 방식 (동기 - blocking 요청)

HITL 워크플로우 (Push Notification 모드):
1. AgentCard를 조회하여 pushNotifications capability 확인
2. Webhook 수신 서버를 백그라운드에서 시작
3. push_notification_config와 함께 메시지 전송 (non-blocking)
4. Webhook으로 상태 알림 수신
5. input_required 상태 감지 시 Mock 사용자 입력 자동 전송
6. 최종 완료 알림 수신

HITL 워크플로우 (Sync 모드):
1. AgentCard를 조회하여 pushNotifications capability 확인
2. blocking=True로 메시지 전송
3. 응답에서 직접 Task 상태 확인
4. input_required 상태면 같은 task_id로 Mock 응답 재전송
5. completed/failed/canceled면 종료

HITL 트리거 키워드:
- "approval", "승인", "confirm", "확인", "budget", "예산", "hitl", "human"
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Callable

import httpx
from a2a.client import A2AClient
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import (
    AgentCard,
    SendMessageRequest,
    MessageSendParams,
    MessageSendConfiguration,
    Message,
    TextPart,
    Role,
    Part,
    PushNotificationConfig,
    Task,
)

# 로컬 모듈 임포트
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from webhook_receiver import WebhookReceiver, TaskNotification, TaskNotificationType
from logger_config import get_logger, LogCategory

# SmartLogger 인스턴스 가져오기
logger = get_logger()
CATEGORY = LogCategory.CLIENT


class HITLMockResponder:
    """
    HITL 상황에서 Mock 사용자 응답을 생성합니다.
    
    실제 환경에서는 이 클래스를 UI 또는 사용자 입력 시스템으로 대체합니다.
    """
    
    DEFAULT_RESPONSES = {
        "approve": "yes, 승인합니다.",
        "reject": "no, 거부합니다.",
        "auto": "approve - 자동 승인 (Mock Response)",
    }
    
    def __init__(
        self, 
        mode: str = "auto",
        custom_response: Optional[str] = None,
        response_delay: float = 1.0,
    ):
        """
        Args:
            mode: 응답 모드 ("approve", "reject", "auto", "custom")
            custom_response: 커스텀 응답 (mode="custom"일 때 사용)
            response_delay: 응답 전 대기 시간(초) - 실제 사용자 입력 시뮬레이션
        """
        self.mode = mode
        self.custom_response = custom_response
        self.response_delay = response_delay
    
    async def get_response(self, prompt: Optional[str] = None) -> str:
        """
        Mock 사용자 응답을 생성합니다.
        
        Args:
            prompt: HITL 프롬프트 메시지
            
        Returns:
            Mock 사용자 응답
        """
        # 사용자 입력 시간 시뮬레이션
        if self.response_delay > 0:
            logger.log("INFO", f"Simulating user input delay: {self.response_delay}s", 
                      category=CATEGORY)
            await asyncio.sleep(self.response_delay)
        
        if self.mode == "custom" and self.custom_response:
            response = self.custom_response
        elif self.mode in self.DEFAULT_RESPONSES:
            response = self.DEFAULT_RESPONSES[self.mode]
        else:
            response = self.DEFAULT_RESPONSES["auto"]
        
        logger.log("INFO", "Mock user response generated", category=CATEGORY,
                  params={
                      "mode": self.mode,
                      "response": response,
                      "prompt_preview": prompt[:50] if prompt else None,
                  })
        
        return response


class A2AHITLClient:
    """
    A2A HITL 클라이언트 (자동 모드 스위칭 지원)
    
    Human-in-the-Loop 워크플로우를 완벽히 지원하는 클라이언트입니다.
    서버의 push_notifications capability에 따라 자동으로 모드를 선택합니다.
    
    - push_notifications=True: Webhook 방식 (기존)
    - push_notifications=False: Sync 방식 (blocking 요청)
    
    사용 예시:
        async with A2AHITLClient("http://localhost:8000") as client:
            result = await client.send_with_hitl_support("예산 승인이 필요합니다.")
            print(f"최종 결과: {result}")
    """
    
    def __init__(
        self,
        agent_url: str,
        webhook_port: int = 9000,
        webhook_token: Optional[str] = None,
        timeout: int = 120,
        mock_responder: Optional[HITLMockResponder] = None,
        max_hitl_iterations: int = 5,
    ):
        """
        Args:
            agent_url: A2A 에이전트 서버 URL
            webhook_port: Webhook 수신 서버 포트
            webhook_token: Webhook 인증 토큰
            timeout: 요청 타임아웃(초)
            mock_responder: HITL Mock 응답 생성기
            max_hitl_iterations: 최대 HITL 반복 횟수
        """
        self.agent_url = agent_url
        self.timeout = timeout
        self.webhook_port = webhook_port
        self.webhook_token = webhook_token or str(uuid.uuid4())
        self.mock_responder = mock_responder or HITLMockResponder()
        self.max_hitl_iterations = max_hitl_iterations
        
        self._httpx_client: Optional[httpx.AsyncClient] = None
        self._a2a_client: Optional[A2AClient] = None
        self._agent_card: Optional[AgentCard] = None
        self._webhook_receiver: Optional[WebhookReceiver] = None
    
    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        self._httpx_client = httpx.AsyncClient(timeout=self.timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        if self._webhook_receiver:
            await self._webhook_receiver.stop()
        if self._httpx_client:
            await self._httpx_client.aclose()
    
    async def get_agent_card(self) -> AgentCard:
        """에이전트 카드를 조회합니다."""
        if self._agent_card:
            return self._agent_card
        
        logger.log("INFO", "Fetching AgentCard", category=CATEGORY,
                  params={"agent_url": self.agent_url})
        
        resolver = A2ACardResolver(self._httpx_client, self.agent_url)
        self._agent_card = await resolver.get_agent_card()
        
        logger.log("INFO", "AgentCard fetched", category=CATEGORY,
                  params={
                      "name": self._agent_card.name,
                      "push_notifications": self._agent_card.capabilities.push_notifications,
                  })
        
        return self._agent_card
    
    def supports_push_notifications(self) -> bool:
        """에이전트가 Push Notifications를 지원하는지 확인합니다."""
        if not self._agent_card:
            raise RuntimeError("AgentCard를 먼저 로드하세요.")
        return bool(self._agent_card.capabilities.push_notifications)
    
    async def start_webhook_receiver(self) -> str:
        """Webhook 수신 서버를 시작합니다."""
        self._webhook_receiver = WebhookReceiver(
            port=self.webhook_port,
            token=self.webhook_token,
        )
        await self._webhook_receiver.start()
        return self._webhook_receiver.webhook_url
    
    def _create_a2a_client(self) -> A2AClient:
        """A2A 클라이언트를 생성합니다."""
        if not self._a2a_client:
            self._a2a_client = A2AClient(
                httpx_client=self._httpx_client,
                url=self.agent_url,
            )
        return self._a2a_client
    
    def _create_message_request(
        self,
        message: str,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        blocking: bool = False,
    ) -> SendMessageRequest:
        """
        메시지 요청을 생성합니다.
        
        Args:
            message: 전송할 메시지
            task_id: 기존 태스크 ID (HITL 응답 시)
            context_id: 컨텍스트 ID
            webhook_url: Webhook URL (Push Notification 모드)
            blocking: blocking 요청 여부 (Sync 모드에서 True)
        """
        # Message 객체 생성
        a2a_message = Message(
            message_id=str(uuid.uuid4()),
            parts=[Part(root=TextPart(text=message, kind="text"))],
            role=Role.user,
            task_id=task_id,
            context_id=context_id,
        )
        
        # Configuration 생성
        config_params = {
            "acceptedOutputModes": ["text"],
            "blocking": blocking,
        }
        
        if webhook_url:
            config_params["push_notification_config"] = PushNotificationConfig(
                url=webhook_url,
                token=self.webhook_token,
            )
        
        configuration = MessageSendConfiguration(**config_params)
        
        return SendMessageRequest(
            id=str(uuid.uuid4()),
            params=MessageSendParams(
                message=a2a_message,
                configuration=configuration,
            )
        )
    
    async def send_with_hitl_support(
        self,
        message: str,
        wait_timeout: float = 1800.0,
    ) -> Dict[str, Any]:
        """
        메시지를 전송하고 HITL 워크플로우를 자동으로 처리합니다.
        
        서버의 push_notifications capability에 따라 자동으로 모드를 선택합니다.
        - push_notifications=True: Webhook 방식 (비동기)
        - push_notifications=False: Sync 방식 (동기)
        
        Args:
            message: 전송할 메시지
            wait_timeout: 각 단계별 대기 타임아웃(초)
        
        Returns:
            최종 결과 딕셔너리
        """
        process_start = datetime.now()
        
        # 1. AgentCard 조회
        await self.get_agent_card()
        
        # 2. Push Notifications 지원 여부에 따라 모드 선택
        if self.supports_push_notifications():
            logger.log("INFO", "Using PUSH NOTIFICATION mode (webhook)", category=CATEGORY,
                      params={
                          "agent_url": self.agent_url,
                          "mode": "PUSH_NOTIFICATION",
                      })
            return await self._send_with_push_notification_mode(
                message, wait_timeout, process_start
            )
        else:
            logger.log("INFO", "Using SYNC mode (blocking)", category=CATEGORY,
                      params={
                          "agent_url": self.agent_url,
                          "mode": "SYNC",
                          "reason": "Server does not support push_notifications",
                      })
            return await self._send_with_sync_mode(
                message, wait_timeout, process_start
            )
    
    async def _send_with_push_notification_mode(
        self,
        message: str,
        wait_timeout: float,
        process_start: datetime,
    ) -> Dict[str, Any]:
        """
        Push Notification 모드로 HITL 워크플로우를 처리합니다.
        
        기존 방식: Webhook으로 상태 알림을 수신합니다.
        """
        # ========== HITL WORKFLOW START (PUSH NOTIFICATION) ==========
        logger.log("INFO", "HITL workflow STARTED (push notification mode)", category=CATEGORY,
                  params={
                      "agent_url": self.agent_url,
                      "message_length": len(message),
                      "max_hitl_iterations": self.max_hitl_iterations,
                      "mode": "PUSH_NOTIFICATION",
                  })
        
        # 1. Webhook 수신 서버 시작
        webhook_url = await self.start_webhook_receiver()
        logger.log("INFO", "Webhook receiver started", category=CATEGORY,
                  params={"webhook_url": webhook_url})
        
        # 2. A2A 클라이언트 생성
        client = self._create_a2a_client()
        
        # 3. 초기 메시지 전송
        current_task_id = None
        current_context_id = None
        hitl_iteration = 0
        
        logger.log("INFO", "Sending initial message (non-blocking)", category=CATEGORY,
                  params={"message_preview": message[:50]})
        
        request = self._create_message_request(
            message=message,
            webhook_url=webhook_url,
            blocking=False,  # Push Notification 모드는 non-blocking
        )
        
        response = await client.send_message(request)
        
        if response and response.root and response.root.result:
            task = response.root.result
            current_task_id = task.id
            current_context_id = task.context_id
            logger.log("INFO", "Initial task created", category=CATEGORY,
                      params={
                          "task_id": current_task_id,
                          "context_id": current_context_id,
                      })
        
        # 4. HITL 루프 - input_required 상태 처리
        while hitl_iteration < self.max_hitl_iterations:
            logger.log("INFO", f"Waiting for notification (iteration {hitl_iteration})", 
                      category=CATEGORY,
                      params={"task_id": current_task_id})
            
            # 이벤트 초기화
            self._webhook_receiver.reset_events()
            
            # 다음 알림 대기
            notification = await self._webhook_receiver.wait_for_notification(
                timeout=wait_timeout
            )
            
            if not notification:
                logger.log("WARNING", "Notification wait TIMEOUT", category=CATEGORY)
                return self._create_timeout_result(
                    current_task_id, process_start, hitl_iteration, mode="PUSH_NOTIFICATION"
                )
            
            logger.log("INFO", "Notification received", category=CATEGORY,
                      params={
                          "task_id": notification.task_id,
                          "state": notification.state,
                          "type": notification.notification_type.value,
                          "result_text": notification.result_text,
                      })
            
            # 상태에 따른 처리
            if notification.notification_type == TaskNotificationType.COMPLETED:
                # 작업 완료
                return self._create_success_result_from_notification(
                    notification, process_start, hitl_iteration, mode="PUSH_NOTIFICATION"
                )
            
            elif notification.notification_type == TaskNotificationType.INPUT_REQUIRED:
                # HITL 처리
                hitl_iteration += 1
                logger.log("INFO", f"HITL detected (iteration {hitl_iteration})", 
                          category=CATEGORY,
                          params={
                              "task_id": notification.task_id,
                              "prompt": notification.input_prompt[:80] if notification.input_prompt else None,
                          })
                
                # Mock 사용자 응답 생성
                mock_response = await self.mock_responder.get_response(
                    notification.input_prompt
                )
                
                # 응답 메시지 전송 (같은 task_id로)
                logger.log("INFO", "Sending HITL response", category=CATEGORY,
                          params={
                              "task_id": notification.task_id,
                              "response": mock_response,
                          })
                
                hitl_request = self._create_message_request(
                    message=mock_response,
                    task_id=notification.task_id,
                    context_id=notification.context_id,
                    webhook_url=webhook_url,
                    blocking=False,
                )
                
                await client.send_message(hitl_request)
                
                # 다음 알림 대기로 계속
                continue
            
            elif notification.notification_type in (
                TaskNotificationType.FAILED, 
                TaskNotificationType.CANCELED
            ):
                # 실패 또는 취소
                return self._create_failure_result_from_notification(
                    notification, process_start, hitl_iteration, mode="PUSH_NOTIFICATION"
                )
            
            else:
                # 진행 중 등 기타 상태 - 계속 대기
                logger.log("DEBUG", f"Intermediate state: {notification.state}", 
                          category=CATEGORY)
                continue
        
        # 최대 HITL 반복 초과
        logger.log("WARNING", "Max HITL iterations exceeded", category=CATEGORY,
                  params={"max_iterations": self.max_hitl_iterations})
        
        return {
            "status": "max_hitl_exceeded",
            "mode": "PUSH_NOTIFICATION",
            "task_id": current_task_id,
            "hitl_iterations": hitl_iteration,
            "total_duration_sec": (datetime.now() - process_start).total_seconds(),
        }
    
    async def _send_with_sync_mode(
        self,
        message: str,
        wait_timeout: float,
        process_start: datetime,
    ) -> Dict[str, Any]:
        """
        Sync 모드로 HITL 워크플로우를 처리합니다.
        
        blocking=True로 요청하고 응답에서 직접 상태를 확인합니다.
        input_required 상태면 같은 task_id로 재요청합니다.
        """
        # ========== HITL WORKFLOW START (SYNC) ==========
        logger.log("INFO", "HITL workflow STARTED (sync mode)", category=CATEGORY,
                  params={
                      "agent_url": self.agent_url,
                      "message_length": len(message),
                      "max_hitl_iterations": self.max_hitl_iterations,
                      "mode": "SYNC",
                  })
        
        # 1. A2A 클라이언트 생성
        client = self._create_a2a_client()
        
        # 2. 초기 메시지 전송 (blocking)
        current_task_id = None
        current_context_id = None
        hitl_iteration = 0
        
        logger.log("INFO", "Sending initial message (blocking)", category=CATEGORY,
                  params={"message_preview": message[:50], "mode": "SYNC"})
        
        request = self._create_message_request(
            message=message,
            blocking=True,  # Sync 모드는 blocking
        )
        
        response = await client.send_message(request)
        
        # 3. 응답에서 Task 추출
        task = self._extract_task_from_response(response)
        if not task:
            logger.log("ERROR", "Failed to get task from response", category=CATEGORY)
            return {
                "status": "error",
                "mode": "SYNC",
                "error": "Failed to get task from response",
                "total_duration_sec": (datetime.now() - process_start).total_seconds(),
            }
        
        current_task_id = task.id
        current_context_id = task.context_id
        
        # Task에서 결과 텍스트 추출
        initial_result_text = self._extract_result_from_task(task)
        
        logger.log("INFO", "Task received from sync response", category=CATEGORY,
                  params={
                      "task_id": current_task_id,
                      "context_id": current_context_id,
                      "state": task.status.state.value if task.status else "unknown",
                      "result_text": initial_result_text,
                  })
        
        # 4. HITL 루프 - 응답에서 직접 상태 확인
        while hitl_iteration < self.max_hitl_iterations:
            state = task.status.state.value if task.status else "unknown"
            
            logger.log("INFO", f"Processing task state (iteration {hitl_iteration})", 
                      category=CATEGORY,
                      params={
                          "task_id": current_task_id,
                          "state": state,
                          "mode": "SYNC",
                      })
            
            # 상태에 따른 처리
            if state == "completed":
                # 작업 완료
                return self._create_success_result_from_task(
                    task, process_start, hitl_iteration, mode="SYNC"
                )
            
            elif state == "input-required":
                # HITL 처리
                hitl_iteration += 1
                
                # Task에서 프롬프트 추출
                input_prompt = self._extract_prompt_from_task(task)
                
                logger.log("INFO", f"HITL detected (iteration {hitl_iteration})", 
                          category=CATEGORY,
                          params={
                              "task_id": current_task_id,
                              "prompt_preview": input_prompt[:80] if input_prompt else None,
                              "mode": "SYNC",
                          })
                
                # Mock 사용자 응답 생성
                mock_response = await self.mock_responder.get_response(input_prompt)
                
                # 응답 메시지 전송 (같은 task_id로, blocking)
                logger.log("INFO", "Sending HITL response (blocking)", category=CATEGORY,
                          params={
                              "task_id": current_task_id,
                              "response": mock_response,
                              "mode": "SYNC",
                          })
                
                hitl_request = self._create_message_request(
                    message=mock_response,
                    task_id=current_task_id,
                    context_id=current_context_id,
                    blocking=True,
                )
                
                response = await client.send_message(hitl_request)
                
                # 새 응답에서 Task 추출
                task = self._extract_task_from_response(response)
                if not task:
                    logger.log("ERROR", "Failed to get task from HITL response", category=CATEGORY)
                    return {
                        "status": "error",
                        "mode": "SYNC",
                        "task_id": current_task_id,
                        "error": "Failed to get task from HITL response",
                        "hitl_iterations": hitl_iteration,
                        "total_duration_sec": (datetime.now() - process_start).total_seconds(),
                    }
                
                # 다음 루프로 계속
                continue
            
            elif state in ("failed", "canceled"):
                # 실패 또는 취소
                return self._create_failure_result_from_task(
                    task, process_start, hitl_iteration, mode="SYNC"
                )
            
            elif state == "working":
                # 아직 작업 중 - Sync 모드에서는 이 상태가 오면 안됨
                logger.log("WARNING", "Received 'working' state in sync mode, retrying", 
                          category=CATEGORY,
                          params={"task_id": current_task_id})
                await asyncio.sleep(1)
                
                # 다시 요청 (같은 task_id로)
                retry_request = self._create_message_request(
                    message="",  # 빈 메시지로 상태 확인
                    task_id=current_task_id,
                    context_id=current_context_id,
                    blocking=True,
                )
                response = await client.send_message(retry_request)
                task = self._extract_task_from_response(response)
                if not task:
                    break
                continue
            
            else:
                # 기타 상태
                logger.log("DEBUG", f"Unknown state: {state}", category=CATEGORY)
                break
        
        # 최대 HITL 반복 초과 또는 루프 종료
        logger.log("WARNING", "Max HITL iterations exceeded or loop ended", category=CATEGORY,
                  params={"max_iterations": self.max_hitl_iterations, "mode": "SYNC"})
        
        return {
            "status": "max_hitl_exceeded",
            "mode": "SYNC",
            "task_id": current_task_id,
            "hitl_iterations": hitl_iteration,
            "total_duration_sec": (datetime.now() - process_start).total_seconds(),
        }
    
    def _extract_task_from_response(self, response) -> Optional[Task]:
        """응답에서 Task를 추출합니다."""
        if response and response.root and response.root.result:
            return response.root.result
        return None
    
    def _extract_prompt_from_task(self, task: Task) -> Optional[str]:
        """Task에서 HITL 프롬프트를 추출합니다."""
        # 1. status.message에서 추출 시도
        if task.status and task.status.message:
            message = task.status.message
            if message.parts:
                for part in message.parts:
                    if hasattr(part, 'root') and hasattr(part.root, 'text'):
                        return part.root.text
                    if hasattr(part, 'text'):
                        return part.text
        
        # 2. history에서 마지막 agent 메시지 추출
        if task.history:
            for msg in reversed(task.history):
                if msg.role != Role.user and msg.parts:
                    for part in msg.parts:
                        if hasattr(part, 'root') and hasattr(part.root, 'text'):
                            return part.root.text
                        if hasattr(part, 'text'):
                            return part.text
        
        return None
    
    def _extract_result_from_task(self, task: Task) -> Optional[str]:
        """Task에서 결과 텍스트를 추출합니다."""
        # history에서 agent 메시지 추출
        if task.history:
            for msg in reversed(task.history):
                if msg.role != Role.user and msg.parts:
                    for part in msg.parts:
                        if hasattr(part, 'root') and hasattr(part.root, 'text'):
                            return part.root.text
                        if hasattr(part, 'text'):
                            return part.text
        return None
    
    def _create_success_result_from_notification(
        self,
        notification: TaskNotification,
        start_time: datetime,
        hitl_iterations: int,
        mode: str = "PUSH_NOTIFICATION",
    ) -> Dict[str, Any]:
        """Notification에서 성공 결과 생성"""
        total_duration = (datetime.now() - start_time).total_seconds()
        
        logger.log("INFO", "HITL workflow COMPLETED successfully", category=CATEGORY,
                  params={
                      "task_id": notification.task_id,
                      "hitl_iterations": hitl_iterations,
                      "total_duration_sec": round(total_duration, 2),
                      "mode": mode,
                      "result_text": notification.result_text,
                  })
        
        return {
            "status": "completed",
            "mode": mode,
            "task_id": notification.task_id,
            "state": notification.state,
            "result": notification.result_text,
            "hitl_iterations": hitl_iterations,
            "received_at": notification.received_at.isoformat(),
            "total_duration_sec": total_duration,
        }
    
    def _create_success_result_from_task(
        self,
        task: Task,
        start_time: datetime,
        hitl_iterations: int,
        mode: str = "SYNC",
    ) -> Dict[str, Any]:
        """Task에서 성공 결과 생성"""
        total_duration = (datetime.now() - start_time).total_seconds()
        result_text = self._extract_result_from_task(task)
        
        logger.log("INFO", "HITL workflow COMPLETED successfully", category=CATEGORY,
                  params={
                      "task_id": task.id,
                      "hitl_iterations": hitl_iterations,
                      "total_duration_sec": round(total_duration, 2),
                      "mode": mode,
                      "result_text": result_text,
                  })
        
        return {
            "status": "completed",
            "mode": mode,
            "task_id": task.id,
            "state": task.status.state.value if task.status else "unknown",
            "result": result_text,
            "hitl_iterations": hitl_iterations,
            "total_duration_sec": total_duration,
        }
    
    def _create_failure_result_from_notification(
        self,
        notification: TaskNotification,
        start_time: datetime,
        hitl_iterations: int,
        mode: str = "PUSH_NOTIFICATION",
    ) -> Dict[str, Any]:
        """Notification에서 실패/취소 결과 생성"""
        total_duration = (datetime.now() - start_time).total_seconds()
        
        logger.log("WARNING", f"HITL workflow ended with {notification.state}", 
                  category=CATEGORY,
                  params={
                      "task_id": notification.task_id,
                      "hitl_iterations": hitl_iterations,
                      "mode": mode,
                      "result_text": notification.result_text,
                  })
        
        return {
            "status": notification.state,
            "mode": mode,
            "task_id": notification.task_id,
            "result": notification.result_text,
            "hitl_iterations": hitl_iterations,
            "total_duration_sec": total_duration,
        }
    
    def _create_failure_result_from_task(
        self,
        task: Task,
        start_time: datetime,
        hitl_iterations: int,
        mode: str = "SYNC",
    ) -> Dict[str, Any]:
        """Task에서 실패/취소 결과 생성"""
        total_duration = (datetime.now() - start_time).total_seconds()
        state = task.status.state.value if task.status else "unknown"
        result_text = self._extract_result_from_task(task)
        
        logger.log("WARNING", f"HITL workflow ended with {state}", 
                  category=CATEGORY,
                  params={
                      "task_id": task.id,
                      "hitl_iterations": hitl_iterations,
                      "mode": mode,
                      "result_text": result_text,
                  })
        
        return {
            "status": state,
            "mode": mode,
            "task_id": task.id,
            "result": result_text,
            "hitl_iterations": hitl_iterations,
            "total_duration_sec": total_duration,
        }
    
    def _create_timeout_result(
        self,
        task_id: Optional[str],
        start_time: datetime,
        hitl_iterations: int,
        mode: str = "PUSH_NOTIFICATION",
    ) -> Dict[str, Any]:
        """타임아웃 결과 생성"""
        total_duration = (datetime.now() - start_time).total_seconds()
        
        logger.log("WARNING", "HITL workflow TIMEOUT", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "hitl_iterations": hitl_iterations,
                      "mode": mode,
                  })
        
        return {
            "status": "timeout",
            "mode": mode,
            "task_id": task_id,
            "hitl_iterations": hitl_iterations,
            "total_duration_sec": total_duration,
        }


# 호환성을 위한 별칭
A2AWebhookClient = A2AHITLClient


async def main(
    agent_url: str, 
    message: str, 
    mock_mode: str = "auto",
    custom_response: Optional[str] = None,
):
    """
    메인 함수 - HITL 워크플로우 실행 (자동 모드 스위칭)
    
    Args:
        agent_url: A2A 에이전트 URL
        message: 전송할 메시지
        mock_mode: Mock 응답 모드 ("auto", "approve", "reject", "custom")
        custom_response: 커스텀 응답 (mock_mode="custom"일 때)
    """
    session_start = datetime.now()
    session_id = str(uuid.uuid4())[:8]
    
    logger.log("INFO", "HITL Client session STARTED (auto mode switching)", category=CATEGORY,
              params={
                  "session_id": session_id,
                  "agent_url": agent_url,
                  "message_length": len(message),
                  "mock_mode": mock_mode,
              })
    
    # Mock 응답 생성기 설정
    mock_responder = HITLMockResponder(
        mode=mock_mode,
        custom_response=custom_response,
        response_delay=2.0,  # 2초 대기 (사용자 입력 시뮬레이션)
    )
    
    async with A2AHITLClient(
        agent_url,
        mock_responder=mock_responder,
    ) as client:
        result = await client.send_with_hitl_support(
            message=message,
            wait_timeout=1800.0,
        )
        
        session_duration = (datetime.now() - session_start).total_seconds()
        
        logger.log("INFO", "HITL Client session COMPLETED", category=CATEGORY,
                  params={
                      "session_id": session_id,
                      "status": result.get("status"),
                      "mode": result.get("mode"),
                      "hitl_iterations": result.get("hitl_iterations", 0),
                      "session_duration_sec": round(session_duration, 2),
                  })
        
        # 결과 출력
        print("\n" + "="*60)
        print("📋 HITL 워크플로우 결과")
        print("="*60)
        print(f"모드: {result.get('mode', 'unknown')}")
        print(f"상태: {result.get('status')}")
        print(f"Task ID: {result.get('task_id')}")
        print(f"HITL 반복: {result.get('hitl_iterations', 0)}회")
        print(f"총 소요 시간: {result.get('total_duration_sec', 0):.2f}초")
        if result.get('result'):
            print(f"\n결과:\n{result.get('result')}")
        print("="*60 + "\n")
        
        return result


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="A2A HITL Client (Auto Mode Switching)")
    parser.add_argument(
        "--agent-url",
        default="http://localhost:8000",
        help="A2A 에이전트 서버 URL",
    )
    parser.add_argument(
        "--message",
        default="예산 승인이 필요합니다. 100만원을 사용해도 될까요?",
        help="전송할 메시지 (HITL 트리거 키워드: 승인, 확인, 예산 등)",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=9000,
        help="Webhook 수신 서버 포트 (Push Notification 모드)",
    )
    parser.add_argument(
        "--mock-mode",
        choices=["auto", "approve", "reject", "custom"],
        default="auto",
        help="HITL Mock 응답 모드",
    )
    parser.add_argument(
        "--custom-response",
        default=None,
        help="커스텀 Mock 응답 (--mock-mode=custom일 때)",
    )
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("🚀 A2A HITL 클라이언트 시작 (Auto Mode Switching)")
    print("="*60)
    print(f"에이전트 URL: {args.agent_url}")
    print(f"메시지: {args.message}")
    print(f"Mock 모드: {args.mock_mode}")
    print("💡 서버의 push_notifications 지원 여부에 따라 자동으로 모드 선택")
    print("="*60 + "\n")
    
    asyncio.run(main(
        agent_url=args.agent_url,
        message=args.message,
        mock_mode=args.mock_mode,
        custom_response=args.custom_response,
    ))
