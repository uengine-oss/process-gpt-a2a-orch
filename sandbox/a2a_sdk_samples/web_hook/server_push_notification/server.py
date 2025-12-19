# server/server.py
"""
FastAPI 서버 실행 모듈 (HITL + Webhook 지원)
Human-in-the-Loop 워크플로우와 Push Notification(Webhook)을 지원하는 A2A 서버입니다.

핵심 컴포넌트:
- InMemoryPushNotificationConfigStore: 태스크별 webhook 설정 저장
- BasePushNotificationSender: webhook URL로 HTTP POST 알림 전송
- HITLDemoAgentExecutor: input_required 상태를 통한 HITL 지원

HITL 워크플로우:
1. 클라이언트가 HITL 키워드 포함 메시지 전송
2. 서버가 input_required 상태로 전환
3. Webhook으로 클라이언트에 알림
4. 클라이언트가 사용자 응답과 함께 메시지 재전송
5. 서버가 작업 완료 후 최종 알림 전송
"""

import uvicorn
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.events import InMemoryQueueManager
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.server.tasks import BasePushNotificationSender
from a2a.server.events import EventConsumer
from a2a.server.tasks import ResultAggregator
from a2a.types import Task, TaskState, Message, MessageSendParams
from a2a.utils.errors import ServerError

# 로컬 모듈 임포트를 위한 경로 설정
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_card import create_agent_card
from agent_executor import HITLDemoAgentExecutor
from logger_config import get_logger, LogCategory

# SmartLogger 인스턴스 가져오기
logger = get_logger()
CATEGORY = LogCategory.SERVER


# 글로벌 httpx 클라이언트 (lifespan에서 관리)
_httpx_client: httpx.AsyncClient | None = None


TERMINAL_STATES = {
    TaskState.completed,
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


class WebhookFriendlyRequestHandler(DefaultRequestHandler):
    """
    DefaultRequestHandler 개선 버전:
    - blocking=False(non-blocking) 요청은 즉시 응답을 반환
    - 이후 task가 terminal 상태에 도달하면 push notification(webhook)을 한 번 더 전송

    왜 필요한가?
    - a2a-sdk의 기본 DefaultRequestHandler는 non-blocking에서 첫 이벤트(대개 working)까지만 push를 트리거하고
      나머지 이벤트(terminal 포함)는 백그라운드에서 consume만 하며 push를 다시 보내지 않습니다.
    """

    async def _send_terminal_push_when_ready(self, task_id: str) -> None:
        # terminal 상태가 될 때까지 task_store에서 폴링 후 push 전송
        # (background consumer가 task_store 업데이트를 수행하므로 eventual consistency)
        max_wait_sec = 120.0
        interval = 0.5
        deadline = asyncio.get_running_loop().time() + max_wait_sec

        while asyncio.get_running_loop().time() < deadline:
            try:
                task = await self.task_store.get(task_id)
            except Exception as e:
                logger.log(
                    "WARNING",
                    "Failed to fetch task while waiting terminal state",
                    category=CATEGORY,
                    params={"task_id": task_id, "error": str(e)},
                )
                task = None

            state = getattr(getattr(task, "status", None), "state", None)
            if task and state in TERMINAL_STATES:
                try:
                    if self._push_sender:
                        await self._push_sender.send_notification(task)
                        logger.log(
                            "INFO",
                            "Terminal push notification sent",
                            category=CATEGORY,
                            params={"task_id": task_id, "terminal_state": str(state)},
                        )
                    # in-memory store cleanup (best-effort)
                    if self._push_config_store:
                        await self._push_config_store.delete_info(task_id)
                except Exception as e:
                    logger.log(
                        "ERROR",
                        "Terminal push notification failed",
                        category=CATEGORY,
                        params={"task_id": task_id, "error": str(e)},
                    )
                return

            await asyncio.sleep(interval)

        logger.log(
            "WARNING",
            "Terminal push wait timeout (no terminal state observed)",
            category=CATEGORY,
            params={"task_id": task_id, "timeout_sec": max_wait_sec},
        )

    async def on_message_send(
        self,
        params: MessageSendParams,
        context=None,
    ) -> Message | Task:
        # DefaultRequestHandler.on_message_send를 기반으로,
        # non-blocking일 때 terminal push를 백그라운드에서 추가로 전송한다.
        (
            task_manager,
            task_id,
            queue,
            result_aggregator,
            producer_task,
        ) = await self._setup_message_execution(params, context)

        consumer = EventConsumer(queue)
        producer_task.add_done_callback(consumer.agent_task_callback)

        blocking = True
        if params.configuration and params.configuration.blocking is False:
            blocking = False

        interrupted_or_non_blocking = False
        try:
            (
                result,
                interrupted_or_non_blocking,
            ) = await result_aggregator.consume_and_break_on_interrupt(
                consumer, blocking=blocking
            )
            if not result:
                raise ServerError()

            if isinstance(result, Task):
                self._validate_task_id_match(task_id, result.id)

            # 1) 기존 동작: 첫 이벤트 시점의 push (보통 working)
            await self._send_push_notification_if_needed(task_id, result_aggregator)

            # 2) 추가 동작: non-blocking이면 terminal 도달 후 push를 한 번 더 전송
            if interrupted_or_non_blocking and not blocking:
                latest = await result_aggregator.current_result
                latest_state = (
                    latest.status.state if isinstance(latest, Task) and latest.status else None
                )
                if latest_state not in TERMINAL_STATES:
                    asyncio.create_task(self._send_terminal_push_when_ready(task_id))
                else:
                    # 이미 terminal이면(매우 짧은 작업) 추가 전송 필요 없음
                    logger.log(
                        "DEBUG",
                        "Non-blocking: already terminal at first response, skip terminal push scheduling",
                        category=CATEGORY,
                        params={"task_id": task_id, "terminal_state": str(latest_state)},
                    )

        except Exception as e:
            logger.log(
                "ERROR",
                "Agent execution failed in WebhookFriendlyRequestHandler",
                category=CATEGORY,
                params={"error": str(e)},
            )
            raise
        finally:
            if interrupted_or_non_blocking:
                asyncio.create_task(self._cleanup_producer(producer_task, task_id))
            else:
                await self._cleanup_producer(producer_task, task_id)

        return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 앱의 수명 주기 관리
    httpx 클라이언트를 생성하고 종료 시 정리합니다.
    """
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=30.0)
    logger.log("DEBUG", "httpx client created", category=CATEGORY)
    yield
    await _httpx_client.aclose()
    logger.log("DEBUG", "httpx client closed", category=CATEGORY)


def create_app(
    host: str = "localhost", 
    port: int = 8000, 
    task_duration: int = 3,
    hitl_keywords: list[str] | None = None,
) -> FastAPI:
    """
    FastAPI 애플리케이션을 생성합니다.
    
    Args:
        host: 서버 호스트
        port: 서버 포트
        task_duration: 태스크 처리 시간(초)
        hitl_keywords: HITL 트리거 키워드 목록
    
    Returns:
        FastAPI: FastAPI 앱 인스턴스
    """
    init_time = datetime.now()
    
    # ========== SERVER INITIALIZATION START ==========
    logger.log("INFO", "HITL Server initialization STARTED", category=CATEGORY,
              params={
                  "host": host,
                  "port": port,
                  "task_duration": task_duration,
                  "init_time": init_time.isoformat(),
              })
    
    # 1. AgentCard 생성 (HITL + push_notifications 지원)
    agent_card = create_agent_card(host=host, port=port)
    logger.log("INFO", "AgentCard created", category=CATEGORY,
              params={
                  "name": agent_card.name,
                  "url": agent_card.url,
                  "push_notifications": agent_card.capabilities.push_notifications,
                  "skills_count": len(agent_card.skills),
              })
    
    # 2. HITL AgentExecutor 생성
    agent_executor = HITLDemoAgentExecutor(
        task_duration=task_duration,
        hitl_keywords=hitl_keywords,
    )
    logger.log("INFO", "HITLDemoAgentExecutor created", category=CATEGORY,
              params={
                  "class": agent_executor.__class__.__name__,
                  "task_duration_sec": task_duration,
                  "hitl_keywords": agent_executor.hitl_keywords[:5],  # 처음 5개만 로그
              })
    
    # 3. TaskStore, QueueManager 생성
    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    logger.log("DEBUG", "TaskStore and QueueManager created", category=CATEGORY)
    
    # 4. Push Notification 컴포넌트 생성
    push_config_store = InMemoryPushNotificationConfigStore()
    
    # httpx 클라이언트 생성
    httpx_client = httpx.AsyncClient(timeout=30.0)
    push_sender = BasePushNotificationSender(
        httpx_client=httpx_client,
        config_store=push_config_store,
    )
    logger.log("INFO", "Push Notification components created", category=CATEGORY,
              params={
                  "purpose": "Send webhook notifications for HITL and completion",
              })
    
    # 5. RequestHandler 생성
    # - non-blocking(blocking=False) 요청에서 terminal(completed/failed 등) 상태까지 webhook push가 가도록 커스텀 핸들러 사용
    request_handler = WebhookFriendlyRequestHandler(
        agent_executor=agent_executor,
        task_store=task_store,
        queue_manager=queue_manager,
        push_config_store=push_config_store,
        push_sender=push_sender,
    )
    logger.log("INFO", "RequestHandler created", category=CATEGORY,
              params={
                  "push_notification_enabled": True,
                  "hitl_enabled": True,
              })
    
    # 6. A2A FastAPI 애플리케이션 생성
    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    
    # 7. FastAPI 앱 가져오기
    app = a2a_app.build()
    
    # 8. CORS 설정
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # ========== SERVER INITIALIZATION END ==========
    init_duration = (datetime.now() - init_time).total_seconds()
    logger.log("INFO", "HITL Server initialization COMPLETED", category=CATEGORY,
              params={
                  "init_duration_sec": round(init_duration, 3),
                  "hitl_support": "ENABLED",
                  "push_notifications": "ENABLED",
                  "status": "READY",
              })
    
    # 9. 루트 엔드포인트 추가
    @app.get("/")
    async def root():
        return {
            "message": "A2A HITL + Webhook Demo Server is running!",
            "agent": agent_card.name,
            "version": agent_card.version,
            "capabilities": {
                "streaming": agent_card.capabilities.streaming,
                "push_notifications": agent_card.capabilities.push_notifications,
                "hitl": True,
            },
            "hitl_info": {
                "trigger_keywords": agent_executor.hitl_keywords,
                "description": "Messages containing these keywords will trigger input_required state",
            },
            "endpoints": {
                "agent_card": "/.well-known/agent.json",
                "rpc": "/",
            }
        }
    
    # 10. 헬스 체크 엔드포인트
    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "push_notifications_enabled": True,
            "hitl_enabled": True,
        }
    
    return app


def run_server(
    host: str = "0.0.0.0", 
    port: int = 8000, 
    task_duration: int = 3,
    hitl_keywords: list[str] | None = None,
):
    """
    서버를 실행합니다.
    
    Args:
        host: 서버 호스트 (기본: 0.0.0.0)
        port: 서버 포트 (기본: 8000)
        task_duration: 태스크 처리 시간(초) (기본: 3)
        hitl_keywords: HITL 트리거 키워드 목록
    """
    app = create_app(
        host="localhost", 
        port=port, 
        task_duration=task_duration,
        hitl_keywords=hitl_keywords,
    )
    
    default_keywords = [
        "approval", "승인", "confirm", "확인", 
        "budget", "예산", "hitl", "human"
    ]
    keywords_to_log = hitl_keywords or default_keywords
    
    logger.log("INFO", "A2A HITL Server starting", category=CATEGORY,
              params={
                  "host": host,
                  "port": port,
                  "api_docs_url": f"http://localhost:{port}/docs",
                  "agent_card_url": f"http://localhost:{port}/.well-known/agent.json",
                  "hitl_enabled": True,
                  "hitl_keywords": keywords_to_log,
                  "push_notifications": "ENABLED",
              })
    
    print("\n" + "="*60)
    print("🚀 A2A HITL + Webhook Demo Server")
    print("="*60)
    print(f"📍 Server URL: http://localhost:{port}")
    print(f"📄 Agent Card: http://localhost:{port}/.well-known/agent.json")
    print(f"📚 API Docs: http://localhost:{port}/docs")
    print(f"\n🔑 HITL Trigger Keywords:")
    for kw in keywords_to_log:
        print(f"   - {kw}")
    print("\n💡 Tip: Send a message containing any keyword above to trigger HITL")
    print("="*60 + "\n")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="A2A HITL + Webhook Demo Server")
    parser.add_argument("--host", default="0.0.0.0", help="서버 호스트")
    parser.add_argument("--port", type=int, default=8000, help="서버 포트")
    parser.add_argument("--task-duration", type=int, default=3, help="태스크 처리 시간(초)")
    parser.add_argument(
        "--hitl-keywords", 
        nargs="+", 
        default=None, 
        help="HITL 트리거 키워드 (공백으로 구분)"
    )
    
    args = parser.parse_args()
    run_server(
        host=args.host, 
        port=args.port, 
        task_duration=args.task_duration,
        hitl_keywords=args.hitl_keywords,
    )
