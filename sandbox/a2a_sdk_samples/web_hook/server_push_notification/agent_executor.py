# server/agent_executor.py
"""
AgentExecutor 구현 모듈 (Webhook + HITL 샘플용)
Human-in-the-Loop (HITL) 워크플로우를 시뮬레이션합니다.

핵심 원리:
- 작업 중 특정 조건에서 TaskState.input_required 상태로 전환
- 클라이언트는 webhook으로 input_required 알림을 받음
- 사용자(또는 자동화된 Mock) 입력이 도착하면 작업 재개
- 완료 시 BasePushNotificationSender가 등록된 webhook URL로 Task 상태 전송

HITL 시나리오:
1. 사용자가 메시지 전송
2. 서버가 작업 시작 (WORKING)
3. 서버가 추가 정보 필요 감지 → INPUT_REQUIRED 상태로 전환
4. Webhook으로 클라이언트에 알림
5. 클라이언트가 추가 정보와 함께 메시지 재전송 (같은 task_id)
6. 서버가 작업 재개 → COMPLETED
"""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
import sys
from typing import Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    Message,
    Part,
    TextPart,
    Role,
)

# SmartLogger 임포트
sys.path.insert(0, str(Path(__file__).parent.parent))
from logger_config import get_logger, LogCategory

# SmartLogger 인스턴스 가져오기
logger = get_logger()
CATEGORY = LogCategory.EXECUTOR


class HITLDemoAgentExecutor(AgentExecutor):
    """
    Human-in-the-Loop 데모용 에이전트 실행기
    
    작업 중 추가 정보가 필요한 상황을 시뮬레이션하고,
    input_required 상태를 통해 HITL 워크플로우를 구현합니다.
    
    HITL 트리거 조건:
    - 메시지에 "approval" 또는 "승인"이 포함된 경우
    - 메시지에 "confirm" 또는 "확인"이 포함된 경우
    - 메시지에 "budget" 또는 "예산"이 포함된 경우
    
    재개 조건:
    - 같은 task_id로 새 메시지가 도착하면 작업 재개
    """
    
    # 진행 중인 HITL 작업을 추적하는 클래스 변수
    _pending_hitl_tasks: dict[str, dict] = {}
    
    def __init__(
        self,
        task_duration: int = 3,
        hitl_keywords: Optional[list[str]] = None,
    ):
        """
        Args:
            task_duration: 태스크 처리에 소요되는 시간(초). 기본값 3초.
            hitl_keywords: HITL을 트리거하는 키워드 목록
        """
        self.task_duration = task_duration
        self.hitl_keywords = hitl_keywords or [
            "approval", "승인",
            "confirm", "확인", 
            "budget", "예산",
            "hitl", "human",
        ]
    
    def _should_require_input(self, user_input: str) -> bool:
        """
        사용자 입력에 HITL 트리거 키워드가 있는지 확인합니다.
        
        Args:
            user_input: 사용자 입력 문자열
        
        Returns:
            bool: HITL이 필요하면 True
        """
        lower_input = user_input.lower()
        for keyword in self.hitl_keywords:
            if keyword.lower() in lower_input:
                return True
        return False
    
    def _is_hitl_response(self, task_id: str, user_input: str) -> bool:
        """
        현재 메시지가 HITL 상태에 대한 응답인지 확인합니다.
        
        Args:
            task_id: 태스크 ID
            user_input: 사용자 입력
            
        Returns:
            bool: HITL 응답이면 True
        """
        return task_id in self._pending_hitl_tasks
    
    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """
        Task를 처리하는 메인 메서드
        
        HITL 워크플로우:
        1. 새 작업: 키워드 감지 → input_required 상태
        2. HITL 응답: 작업 재개 → completed 상태
        
        Args:
            context: 요청 컨텍스트 (사용자 메시지, task ID 등 포함)
            event_queue: 이벤트를 발행할 큐
        """
        task_id = context.task_id
        context_id = context.context_id
        start_time = datetime.now()
        
        # 사용자 입력 가져오기
        user_input = context.get_user_input()
        
        # ========== EXECUTION START ==========
        logger.log("INFO", "Task execution STARTED", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "context_id": context_id,
                      "user_input": user_input,
                      "is_hitl_response": self._is_hitl_response(task_id, user_input),
                  })
        
        # HITL 응답인지 확인
        if self._is_hitl_response(task_id, user_input):
            # HITL 상태에서 재개
            await self._resume_from_hitl(
                task_id, context_id, user_input, event_queue, start_time
            )
        else:
            # 새 작업 시작
            await self._start_new_task(
                task_id, context_id, user_input, event_queue, start_time
            )
    
    async def _start_new_task(
        self,
        task_id: str,
        context_id: str,
        user_input: str,
        event_queue: EventQueue,
        start_time: datetime,
    ) -> None:
        """
        새 작업을 시작합니다.
        
        HITL 키워드가 감지되면 input_required 상태로 전환합니다.
        """
        # 1. WORKING 상태로 전환 (+ 첫 응답용 agent 메시지 포함)
        logger.log("INFO", "State transition: SUBMITTED -> WORKING", category=CATEGORY,
                  params={"task_id": task_id})

        # 첫 응답 메시지(클라이언트가 초기 non-blocking 응답에서 goal 등에 포함시키기 위함)
        # 정책: user_input을 포함한 echo 형태
        first_ack_text = (
            "✅ 요청을 접수했습니다. 처리 중입니다.\n"
            f"요청: {user_input}"
        )
        first_message_id = str(uuid.uuid4())
        first_response_message = Message(
            message_id=first_message_id,
            role=Role.agent,
            parts=[Part(root=TextPart(text=first_ack_text))],
            task_id=task_id,
            context_id=context_id,
        )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.working,
                    message=first_response_message,
                ),
                final=False,
            )
        )
        
        # 2. 초기 처리 시뮬레이션 (1초)
        logger.log("INFO", "Initial processing...", category=CATEGORY,
                  params={"task_id": task_id, "duration_sec": 1})
        await asyncio.sleep(1)
        
        # 3. HITL 필요 여부 확인
        requires_input = self._should_require_input(user_input)
        
        if requires_input:
            # ========== HITL TRIGGERED ==========
            await self._trigger_hitl(
                task_id, context_id, user_input, event_queue
            )
        else:
            # 일반 처리 완료
            await self._complete_task(
                task_id, context_id, user_input, event_queue, start_time
            )
    
    async def _trigger_hitl(
        self,
        task_id: str,
        context_id: str,
        user_input: str,
        event_queue: EventQueue,
    ) -> None:
        """
        HITL 상태를 트리거합니다.
        
        input_required 상태로 전환하고, 클라이언트에 추가 입력을 요청합니다.
        """
        logger.log("INFO", "HITL TRIGGERED - Requesting user input", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "trigger_reason": "Keyword detected in user input",
                      "original_input": user_input,
                  })
        
        # HITL 상태 저장
        self._pending_hitl_tasks[task_id] = {
            "original_input": user_input,
            "requested_at": datetime.now().isoformat(),
            "context_id": context_id,
        }
        
        # HITL 요청 메시지 생성
        hitl_message = (
            f"⏸️ 추가 확인이 필요합니다!\n\n"
            f"요청 내용: {user_input}\n\n"
            f"처리를 계속하시려면 'yes', 'approve', '승인' 중 하나로 응답해주세요.\n"
            f"취소하시려면 'no', 'cancel', '취소'로 응답해주세요."
        )
        
        message_id = str(uuid.uuid4())
        response_message = Message(
            message_id=message_id,
            role=Role.agent,
            parts=[Part(root=TextPart(text=hitl_message))],
            task_id=task_id,
            context_id=context_id,
        )
        
        # INPUT_REQUIRED 상태로 전환 (메시지 포함)
        logger.log("INFO", "State transition: WORKING -> INPUT_REQUIRED", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "message_id": message_id,
                      "awaiting_response": True,
                  })
        
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.input_required,
                    message=response_message,
                ),
                final=True,  # 현재 실행은 종료, 클라이언트 응답 대기
            )
        )
        
        logger.log("INFO", "HITL state set, awaiting client response", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "pending_tasks_count": len(self._pending_hitl_tasks),
                  })
    
    async def _resume_from_hitl(
        self,
        task_id: str,
        context_id: str,
        user_input: str,
        event_queue: EventQueue,
        start_time: datetime,
    ) -> None:
        """
        HITL 상태에서 작업을 재개합니다.
        
        사용자 응답에 따라 작업을 완료하거나 취소합니다.
        """
        hitl_info = self._pending_hitl_tasks.pop(task_id, {})
        original_input = hitl_info.get("original_input", "")
        
        logger.log("INFO", "HITL RESPONSE received - Resuming task", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "original_input": original_input,
                      "user_response": user_input,
                  })
        
        # 1. WORKING 상태로 재전환
        logger.log("INFO", "State transition: INPUT_REQUIRED -> WORKING", category=CATEGORY,
                  params={"task_id": task_id, "reason": "User response received"})
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.working),
                final=False,
            )
        )
        
        # 2. 응답 분석
        lower_response = user_input.lower()
        is_approved = any(word in lower_response for word in [
            "yes", "approve", "승인", "확인", "ok", "okay", "동의"
        ])
        is_cancelled = any(word in lower_response for word in [
            "no", "cancel", "취소", "거부", "reject"
        ])
        
        if is_cancelled:
            # 사용자가 취소 요청
            await self._cancel_by_user(task_id, context_id, event_queue)
        else:
            # 승인 또는 기타 응답 → 작업 완료
            await self._complete_hitl_task(
                task_id, context_id, original_input, user_input, 
                event_queue, start_time, is_approved
            )
    
    async def _complete_task(
        self,
        task_id: str,
        context_id: str,
        user_input: str,
        event_queue: EventQueue,
        start_time: datetime,
    ) -> None:
        """
        일반 작업을 완료합니다 (HITL 없음).
        """
        # 남은 작업 시뮬레이션
        remaining_duration = max(1, self.task_duration - 1)
        logger.log("INFO", "Processing task (no HITL required)...", category=CATEGORY,
                  params={"task_id": task_id, "remaining_sec": remaining_duration})
        
        for i in range(remaining_duration):
            await asyncio.sleep(1)
            progress = ((i + 2) / self.task_duration) * 100
            logger.log("DEBUG", f"Progress: {progress:.0f}%", category=CATEGORY,
                      params={"task_id": task_id})
        
        # 결과 메시지 생성
        result_message = (
            f"✅ 작업이 완료되었습니다!\n\n"
            f"입력: {user_input}\n"
            f"처리 시간: {self.task_duration}초\n"
            f"HITL: 필요 없음"
        )
        
        await self._send_completion(
            task_id, context_id, result_message, event_queue, start_time
        )
    
    async def _complete_hitl_task(
        self,
        task_id: str,
        context_id: str,
        original_input: str,
        user_response: str,
        event_queue: EventQueue,
        start_time: datetime,
        is_approved: bool,
    ) -> None:
        """
        HITL 작업을 완료합니다.
        """
        # 추가 처리 시뮬레이션
        logger.log("INFO", "Processing approved HITL task...", category=CATEGORY,
                  params={"task_id": task_id, "is_approved": is_approved})
        await asyncio.sleep(2)
        
        # 결과 메시지 생성
        approval_status = "✅ 승인됨" if is_approved else "⚠️ 조건부 진행"
        result_message = (
            f"🎉 HITL 작업이 완료되었습니다!\n\n"
            f"원본 요청: {original_input}\n"
            f"사용자 응답: {user_response}\n"
            f"승인 상태: {approval_status}\n"
            f"처리 결과: 성공적으로 완료됨"
        )
        
        await self._send_completion(
            task_id, context_id, result_message, event_queue, start_time
        )
    
    async def _cancel_by_user(
        self,
        task_id: str,
        context_id: str,
        event_queue: EventQueue,
    ) -> None:
        """
        사용자 요청에 의해 작업을 취소합니다.
        """
        logger.log("INFO", "User requested cancellation", category=CATEGORY,
                  params={"task_id": task_id})
        
        cancel_message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.agent,
            parts=[Part(root=TextPart(text="❌ 작업이 사용자 요청에 의해 취소되었습니다."))],
            task_id=task_id,
            context_id=context_id,
        )
        
        logger.log("INFO", "State transition: WORKING -> CANCELED", category=CATEGORY,
                  params={"task_id": task_id, "reason": "User cancelled"})
        
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.canceled,
                    message=cancel_message,
                ),
                final=True,
            )
        )
    
    async def _send_completion(
        self,
        task_id: str,
        context_id: str,
        result_text: str,
        event_queue: EventQueue,
        start_time: datetime,
    ) -> None:
        """
        완료 이벤트를 전송합니다.
        """
        message_id = str(uuid.uuid4())
        response_message = Message(
            message_id=message_id,
            role=Role.agent,
            parts=[Part(root=TextPart(text=result_text))],
            task_id=task_id,
            context_id=context_id,
        )
        
        # WORKING 상태로 메시지 전송 (history에 추가)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.working,
                    message=response_message,
                ),
                final=False,
            )
        )
        
        # COMPLETED 상태로 변경
        end_time = datetime.now()
        total_duration = (end_time - start_time).total_seconds()
        
        logger.log("INFO", "State transition: WORKING -> COMPLETED", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "total_duration_sec": round(total_duration, 2),
                  })
        
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.completed),
                final=True,
            )
        )
        
        logger.log("INFO", "Task execution COMPLETED", category=CATEGORY,
                  params={
                      "task_id": task_id,
                      "total_duration_sec": round(total_duration, 2),
                      "status": "SUCCESS",
                  })

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """
        Task 취소 처리
        """
        task_id = context.task_id
        context_id = context.context_id
        
        logger.log("INFO", "Task cancellation REQUESTED", category=CATEGORY,
                  params={"task_id": task_id})
        
        # HITL 대기 중인 작업 정리
        if task_id in self._pending_hitl_tasks:
            self._pending_hitl_tasks.pop(task_id)
            logger.log("INFO", "Removed from pending HITL tasks", category=CATEGORY,
                      params={"task_id": task_id})
        
        # 취소 상태로 변경
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.canceled),
                final=True,
            )
        )
        
        logger.log("INFO", "Task cancellation COMPLETED", category=CATEGORY,
                  params={"task_id": task_id})


# 호환성을 위해 기존 클래스 별칭 유지
WebhookDemoAgentExecutor = HITLDemoAgentExecutor


if __name__ == "__main__":
    # 테스트: AgentExecutor 생성
    executor = HITLDemoAgentExecutor(task_duration=3)
    logger.log("INFO", "HITLDemoAgentExecutor created (test mode)", category=CATEGORY,
              params={
                  "executor_class": executor.__class__.__name__,
                  "task_duration_sec": executor.task_duration,
                  "hitl_keywords": executor.hitl_keywords,
              })
