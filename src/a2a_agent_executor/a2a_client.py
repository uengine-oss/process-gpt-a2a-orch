"""
A2A 클라이언트 관리 모듈
A2A 서버와의 통신을 담당하며, 동기/웹훅 모드를 지원합니다.

주요 기능:
- AgentCard 조회 및 push_notifications 지원 여부 확인
- 동기 모드 (blocking=True) 메시지 전송
- 웹훅 모드 (blocking=False, push_notification_config) 메시지 전송
"""

import uuid
from typing import Any, Dict, Optional

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
    TextPart as A2ATextPart,
)

from .smart_logger import SmartLogger

# 로그 카테고리 정의
LOG_CATEGORY = "A2A_CLIENT"


class A2AClientManager:
    """
    A2A 클라이언트 관리 클래스
    
    동기 모드와 웹훅 모드를 모두 지원합니다.
    - 동기 모드: blocking=True로 요청, 완료까지 대기
    - 웹훅 모드: blocking=False, push_notification_config 포함하여 요청
    
    사용 예시:
        async with A2AClientManager() as client_manager:
            # AgentCard 조회
            card = await client_manager.get_agent_card(agent_endpoint)
            
            if client_manager.supports_push_notifications(card):
                # 웹훅 모드
                response = await client_manager.send_message_with_webhook(
                    agent_endpoint, message, webhook_url, webhook_token
                )
            else:
                # 동기 모드
                response = await client_manager.send_message(agent_endpoint, message)
    """
    
    def __init__(self, timeout: int = 120):
        """
        Args:
            timeout: HTTP 요청 타임아웃 (초)
        """
        self.timeout = timeout
        self._client: Optional[A2AClient] = None
        self._httpx_client: Optional[httpx.AsyncClient] = None
        self._agent_cards: Dict[str, AgentCard] = {}  # 캐시
    
    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        self._httpx_client = httpx.AsyncClient(timeout=self.timeout)
        SmartLogger.log("DEBUG", "A2AClientManager initialized", category=LOG_CATEGORY,
                       params={"timeout": self.timeout})
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        if self._httpx_client:
            await self._httpx_client.aclose()
            SmartLogger.log("DEBUG", "A2AClientManager closed", category=LOG_CATEGORY)
    
    def _get_httpx_client(self) -> httpx.AsyncClient:
        """httpx 클라이언트 반환 (초기화 확인)"""
        if not self._httpx_client:
            raise RuntimeError("Client manager not initialized. Use async context manager.")
        return self._httpx_client
    
    def create_client(self, agent_endpoint: str) -> A2AClient:
        """A2A 클라이언트 생성"""
        self._client = A2AClient(
            httpx_client=self._get_httpx_client(), 
            url=agent_endpoint
        )
        return self._client
    
    async def get_agent_card(self, agent_endpoint: str) -> AgentCard:
        """
        에이전트 카드를 조회합니다.
        
        Args:
            agent_endpoint: A2A 에이전트 서버 URL
            
        Returns:
            AgentCard 객체
        """
        # 캐시 확인
        if agent_endpoint in self._agent_cards:
            SmartLogger.log("DEBUG", "AgentCard retrieved from cache", category=LOG_CATEGORY,
                           params={"agent_endpoint": agent_endpoint})
            return self._agent_cards[agent_endpoint]
        
        SmartLogger.log("INFO", "Fetching AgentCard", category=LOG_CATEGORY,
                       params={"agent_endpoint": agent_endpoint})
        
        try:
            resolver = A2ACardResolver(self._get_httpx_client(), agent_endpoint)
            card = await resolver.get_agent_card()
            
            # 캐시에 저장
            self._agent_cards[agent_endpoint] = card
            
            SmartLogger.log("INFO", "AgentCard fetched successfully", category=LOG_CATEGORY,
                           params={
                               "agent_endpoint": agent_endpoint,
                               "agent_name": card.name,
                               "push_notifications": card.capabilities.push_notifications if card.capabilities else None,
                           })
            
            return card
            
        except Exception as e:
            SmartLogger.log("ERROR", "Failed to fetch AgentCard", category=LOG_CATEGORY,
                          params={
                              "agent_endpoint": agent_endpoint,
                              "error": str(e),
                          })
            raise
    
    def supports_push_notifications(self, card: AgentCard) -> bool:
        """
        에이전트가 Push Notifications (웹훅)를 지원하는지 확인합니다.
        
        Args:
            card: AgentCard 객체
            
        Returns:
            웹훅 지원 여부
        """
        if not card or not card.capabilities:
            return False
        return bool(card.capabilities.push_notifications)
    
    def create_message_request(
        self, 
        message: str,
        blocking: bool = True,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        webhook_token: Optional[str] = None,
    ) -> SendMessageRequest:
        """
        메시지 요청을 생성합니다.
        
        Args:
            message: 전송할 메시지
            blocking: 블로킹 요청 여부 (동기 모드: True, 웹훅 모드: False)
            task_id: 기존 태스크 ID (HITL 응답 시)
            context_id: 컨텍스트 ID
            webhook_url: 웹훅 URL (웹훅 모드에서만 사용)
            webhook_token: 웹훅 인증 토큰 (웹훅 모드에서만 사용)
            
        Returns:
            SendMessageRequest 객체
        """
        # Message 객체 생성
        a2a_message = Message(
            message_id=str(uuid.uuid4()),
            parts=[
                Part(root=TextPart(
                    text=message,
                    kind="text"
                ))
            ],
            role=Role.user,
            task_id=task_id,
            context_id=context_id,
        )
        
        # Configuration 생성
        config_params: Dict[str, Any] = {
            "acceptedOutputModes": ["text"],
            "blocking": blocking,
        }
        
        # 웹훅 설정 추가
        if webhook_url and not blocking:
            config_params["push_notification_config"] = PushNotificationConfig(
                url=webhook_url,
                token=webhook_token,
            )
        
        configuration = MessageSendConfiguration(**config_params)
        
        return SendMessageRequest(
            id=str(uuid.uuid4()),
            params=MessageSendParams(
                message=a2a_message,
                configuration=configuration,
            )
        )
    
    async def send_message(
        self, 
        agent_endpoint: str, 
        message: str,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Any:
        """
        동기 모드로 메시지를 전송합니다 (blocking=True).
        완료될 때까지 대기합니다.
        
        Args:
            agent_endpoint: A2A 에이전트 서버 URL
            message: 전송할 메시지
            task_id: 기존 태스크 ID (HITL 응답 시)
            context_id: 컨텍스트 ID
            
        Returns:
            A2A 서버 응답
        """
        client = self.create_client(agent_endpoint)
        request = self.create_message_request(
            message=message,
            blocking=True,
            task_id=task_id,
            context_id=context_id,
        )
        
        SmartLogger.log("INFO", "Sending message (sync mode)", category=LOG_CATEGORY,
                       params={
                           "agent_endpoint": agent_endpoint,
                           "message_length": len(message),
                           "blocking": True,
                       })
        
        try:
            response = await client.send_message(request)
            
            SmartLogger.log("INFO", "Message sent successfully (sync mode)", category=LOG_CATEGORY,
                           params={
                               "agent_endpoint": agent_endpoint,
                               "has_result": bool(response and response.root and response.root.result),
                           })
            
            return response
            
        except httpx.ConnectError as e:
            SmartLogger.log("ERROR", "Connection error", category=LOG_CATEGORY,
                          params={
                              "agent_endpoint": agent_endpoint,
                              "error": str(e),
                          })
            raise
        except Exception as e:
            SmartLogger.log("ERROR", "Error sending message", category=LOG_CATEGORY,
                          params={
                              "agent_endpoint": agent_endpoint,
                              "error": str(e),
                          })
            raise
    
    async def send_message_with_webhook(
        self,
        agent_endpoint: str,
        message: str,
        webhook_url: str,
        webhook_token: str,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Any:
        """
        웹훅 모드로 메시지를 전송합니다 (blocking=False).
        즉시 반환되며, 결과는 웹훅으로 전달됩니다.
        
        Args:
            agent_endpoint: A2A 에이전트 서버 URL
            message: 전송할 메시지
            webhook_url: 결과를 받을 웹훅 URL
            webhook_token: 웹훅 인증 토큰
            task_id: 기존 태스크 ID (HITL 응답 시)
            context_id: 컨텍스트 ID
            
        Returns:
            A2A 서버 초기 응답 (task_id 포함)
        """
        client = self.create_client(agent_endpoint)
        request = self.create_message_request(
            message=message,
            blocking=False,
            task_id=task_id,
            context_id=context_id,
            webhook_url=webhook_url,
            webhook_token=webhook_token,
        )
        
        SmartLogger.log("INFO", "Sending message (webhook mode)", category=LOG_CATEGORY,
                       params={
                           "agent_endpoint": agent_endpoint,
                           "message_length": len(message),
                           "webhook_url": webhook_url,
                           "blocking": False,
                       })
        
        try:
            response = await client.send_message(request)
            
            # 응답에서 task_id 추출
            result_task_id = None
            if response and response.root and response.root.result:
                result_task_id = response.root.result.id
            
            SmartLogger.log("INFO", "Message sent successfully (webhook mode)", category=LOG_CATEGORY,
                           params={
                               "agent_endpoint": agent_endpoint,
                               "a2a_task_id": result_task_id,
                           })
            
            return response
            
        except httpx.ConnectError as e:
            SmartLogger.log("ERROR", "Connection error", category=LOG_CATEGORY,
                          params={
                              "agent_endpoint": agent_endpoint,
                              "error": str(e),
                          })
            raise
        except Exception as e:
            SmartLogger.log("ERROR", "Error sending message (webhook mode)", category=LOG_CATEGORY,
                          params={
                              "agent_endpoint": agent_endpoint,
                              "error": str(e),
                          })
            raise
    
    def extract_task_id_from_response(self, response: Any) -> Optional[str]:
        """
        응답에서 A2A task_id를 추출합니다.
        
        Args:
            response: A2A 서버 응답
            
        Returns:
            task_id 또는 None
        """
        if response and response.root and response.root.result:
            return response.root.result.id
        return None

    def extract_first_agent_message_text_from_response(self, response: Any) -> Optional[str]:
        """
        초기 응답(Task)에서 서버가 넣어준 agent 메시지 텍스트를 추출합니다.

        우선순위:
        1) task.status.message (첫 상태 이벤트에 포함된 메시지)
        2) task.history 에서 role != user 인 첫 메시지
        """
        task = None
        try:
            if response and getattr(response, "root", None) and getattr(response.root, "result", None):
                task = response.root.result
        except Exception:
            task = None

        if not task:
            return None

        # 1) status.message 우선
        try:
            status = getattr(task, "status", None)
            status_msg = getattr(status, "message", None) if status is not None else None
            txt = self._extract_text_from_message_obj(status_msg)
            if txt:
                return txt
        except Exception:
            pass

        # 2) history fallback
        try:
            history = getattr(task, "history", None) or []
            for m in history:
                role_val = getattr(m, "role", None)
                role_name = None
                try:
                    role_name = role_val.value if hasattr(role_val, "value") else str(role_val)
                except Exception:
                    role_name = str(role_val) if role_val is not None else None
                if role_name == "user":
                    continue
                txt = self._extract_text_from_message_obj(m)
                if txt:
                    return txt
        except Exception:
            pass

        return None

    def _extract_text_from_message_obj(self, message_obj: Any) -> Optional[str]:
        """
        A2A Message(또는 dict 유사 payload)에서 parts[].text를 추출합니다.
        """
        if not message_obj:
            return None

        # dict 형태 payload 처리
        if isinstance(message_obj, dict):
            parts = message_obj.get("parts", []) or []
            for part in parts:
                if isinstance(part, dict):
                    root = part.get("root", part) if isinstance(part.get("root", part), dict) else part.get("root", part)
                    if isinstance(root, dict):
                        text = root.get("text") or root.get("content") or root.get("data")
                        if isinstance(text, str) and text.strip():
                            return text.strip()
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            return None

        # pydantic/model 객체 처리
        parts = getattr(message_obj, "parts", None) or []
        for p in parts:
            root = getattr(p, "root", None)
            # a2a.types.TextPart
            if isinstance(root, A2ATextPart) and getattr(root, "text", None):
                return (root.text or "").strip() or None
            # root가 dict인 경우
            if isinstance(root, dict):
                text = root.get("text") or root.get("content") or root.get("data")
                if isinstance(text, str) and text.strip():
                    return text.strip()
            # part 자체가 dict/텍스트일 수 있음
            if isinstance(p, dict):
                text = p.get("text") or (p.get("root", {}) or {}).get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
            if hasattr(p, "text") and isinstance(getattr(p, "text"), str) and getattr(p, "text").strip():
                return getattr(p, "text").strip()

        # Message가 dict처럼 model_dump 가능하면 한 번 더 시도
        try:
            if hasattr(message_obj, "model_dump") and callable(getattr(message_obj, "model_dump")):
                return self._extract_text_from_message_obj(message_obj.model_dump())
        except Exception:
            pass

        return None
    
    def clear_agent_card_cache(self, agent_endpoint: Optional[str] = None) -> None:
        """
        AgentCard 캐시를 삭제합니다.
        
        Args:
            agent_endpoint: 특정 에이전트만 삭제 (None이면 전체 삭제)
        """
        if agent_endpoint:
            if agent_endpoint in self._agent_cards:
                del self._agent_cards[agent_endpoint]
                SmartLogger.log("DEBUG", "AgentCard cache cleared for endpoint", 
                               category=LOG_CATEGORY,
                               params={"agent_endpoint": agent_endpoint})
        else:
            self._agent_cards.clear()
            SmartLogger.log("DEBUG", "All AgentCard cache cleared", category=LOG_CATEGORY)
