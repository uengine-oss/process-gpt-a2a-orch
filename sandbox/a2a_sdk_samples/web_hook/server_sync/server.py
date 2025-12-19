# server_sync/server.py
"""
FastAPI 서버 실행 모듈 (동기 방식 HITL 지원)
Push Notification 없이 동기 방식으로 Human-in-the-Loop를 지원하는 A2A 서버입니다.

핵심 컴포넌트:
- HITLSyncAgentExecutor: input_required 상태를 즉시 응답으로 반환
- Push Notification 관련 컴포넌트 없음 (동기 방식)

동기 HITL 워크플로우:
1. 클라이언트가 blocking 요청으로 HITL 키워드 포함 메시지 전송
2. 서버가 input_required 상태를 즉시 응답으로 반환
3. 클라이언트가 응답 확인 후 같은 task_id로 사용자 응답 재전송
4. 서버가 작업 완료 후 최종 응답 반환

Push Notification 서버(server_push_notification)와의 차이점:
- InMemoryPushNotificationConfigStore 없음
- BasePushNotificationSender 없음
- 클라이언트가 webhook 대신 직접 응답에서 상태 확인
"""

import uvicorn
from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.events import InMemoryQueueManager

# 로컬 모듈 임포트를 위한 경로 설정
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_card import create_agent_card
from agent_executor import HITLSyncAgentExecutor
from logger_config import get_logger, LogCategory

# SmartLogger 인스턴스 가져오기
logger = get_logger()
CATEGORY = LogCategory.SERVER


def create_app(
    host: str = "localhost", 
    port: int = 8000, 
    task_duration: int = 3,
    hitl_keywords: list[str] | None = None,
) -> FastAPI:
    """
    FastAPI 애플리케이션을 생성합니다. (동기 방식)
    
    Push Notification 관련 컴포넌트 없이 동기 방식으로 동작합니다.
    
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
    logger.log("INFO", "HITL Sync Server initialization STARTED", category=CATEGORY,
              params={
                  "host": host,
                  "port": port,
                  "task_duration": task_duration,
                  "init_time": init_time.isoformat(),
                  "mode": "SYNC",
              })
    
    # 1. AgentCard 생성 (push_notifications=False)
    agent_card = create_agent_card(host=host, port=port)
    logger.log("INFO", "AgentCard created (sync mode)", category=CATEGORY,
              params={
                  "name": agent_card.name,
                  "url": agent_card.url,
                  "push_notifications": agent_card.capabilities.push_notifications,
                  "skills_count": len(agent_card.skills),
                  "mode": "SYNC",
              })
    
    # 2. HITL Sync AgentExecutor 생성
    agent_executor = HITLSyncAgentExecutor(
        task_duration=task_duration,
        hitl_keywords=hitl_keywords,
    )
    logger.log("INFO", "HITLSyncAgentExecutor created", category=CATEGORY,
              params={
                  "class": agent_executor.__class__.__name__,
                  "task_duration_sec": task_duration,
                  "hitl_keywords": agent_executor.hitl_keywords[:5],  # 처음 5개만 로그
                  "mode": "SYNC",
              })
    
    # 3. TaskStore, QueueManager 생성 (메모리 기반)
    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    logger.log("DEBUG", "TaskStore and QueueManager created", category=CATEGORY,
              params={"mode": "SYNC"})
    
    # 4. RequestHandler 생성 (Push Notification 관련 컴포넌트 없음)
    # 동기 방식이므로 push_config_store, push_sender 없이 생성
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=task_store,
        queue_manager=queue_manager,
        # push_config_store 없음
        # push_sender 없음
    )
    logger.log("INFO", "RequestHandler created (sync mode - no push notification)", category=CATEGORY,
              params={
                  "push_notification_enabled": False,
                  "hitl_enabled": True,
                  "mode": "SYNC",
              })
    
    # 5. A2A FastAPI 애플리케이션 생성
    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    
    # 6. FastAPI 앱 가져오기
    app = a2a_app.build()
    
    # 7. CORS 설정
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # ========== SERVER INITIALIZATION END ==========
    init_duration = (datetime.now() - init_time).total_seconds()
    logger.log("INFO", "HITL Sync Server initialization COMPLETED", category=CATEGORY,
              params={
                  "init_duration_sec": round(init_duration, 3),
                  "hitl_support": "ENABLED (sync mode)",
                  "push_notifications": "DISABLED",
                  "status": "READY",
                  "mode": "SYNC",
              })
    
    # 8. 루트 엔드포인트 추가
    @app.get("/")
    async def root():
        return {
            "message": "A2A HITL Sync Demo Server is running!",
            "agent": agent_card.name,
            "version": agent_card.version,
            "mode": "SYNC",
            "capabilities": {
                "streaming": agent_card.capabilities.streaming,
                "push_notifications": agent_card.capabilities.push_notifications,
                "hitl": True,
            },
            "hitl_info": {
                "mode": "sync",
                "description": "input_required state is returned immediately in response",
                "trigger_keywords": agent_executor.hitl_keywords,
                "client_action": "Re-request with same task_id when input_required",
            },
            "endpoints": {
                "agent_card": "/.well-known/agent.json",
                "rpc": "/",
            }
        }
    
    # 9. 헬스 체크 엔드포인트
    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "mode": "sync",
            "push_notifications_enabled": False,
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
    서버를 실행합니다. (동기 방식)
    
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
    
    logger.log("INFO", "A2A HITL Sync Server starting", category=CATEGORY,
              params={
                  "host": host,
                  "port": port,
                  "api_docs_url": f"http://localhost:{port}/docs",
                  "agent_card_url": f"http://localhost:{port}/.well-known/agent.json",
                  "hitl_enabled": True,
                  "hitl_keywords": keywords_to_log,
                  "push_notifications": "DISABLED",
                  "mode": "SYNC",
              })
    
    print("\n" + "="*60)
    print("🚀 A2A HITL Sync Demo Server (동기 방식)")
    print("="*60)
    print(f"📍 Server URL: http://localhost:{port}")
    print(f"📄 Agent Card: http://localhost:{port}/.well-known/agent.json")
    print(f"📚 API Docs: http://localhost:{port}/docs")
    print(f"\n⚙️  Mode: SYNC (No Push Notifications)")
    print(f"📨 Push Notifications: DISABLED")
    print(f"\n🔑 HITL Trigger Keywords:")
    for kw in keywords_to_log:
        print(f"   - {kw}")
    print("\n💡 Tip: Send a message containing any keyword above to trigger HITL")
    print("💡 Sync Mode: Client receives input_required state immediately in response")
    print("💡 Client Action: Re-request with same task_id to continue")
    print("="*60 + "\n")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="A2A HITL Sync Demo Server (동기 방식)")
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

