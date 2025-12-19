"""
ProcessGPT A2A Agent Executor 서버
A2A 에이전트와의 통신을 담당하는 메인 서버입니다.

환경변수:
- SUPABASE_URL: Supabase URL (필수)
- SUPABASE_KEY: Supabase Key (필수)
- ENV: 환경 (dev, production)

웹훅 관련 환경변수:
- WEBHOOK_PUBLIC_BASE_URL: (권장) 외부 웹훅 리시버 Pod의 Public Base URL
  예) http://a2a-webhook-receiver:9000
"""

import os
from dotenv import load_dotenv

if os.getenv("ENV") != "dev" and os.getenv("ENV") != "production":
    load_dotenv(override=True)

if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_KEY"):
    print("오류: SUPABASE_URL과 SUPABASE_KEY 환경변수가 필요합니다.")

os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_ENDPOINT"] = "https://api.smith.langchain.com"

import httpx

_orig_init = httpx.Client.__init__
def _patched_init(self, *args, **kwargs):
    kwargs["http1"] = True
    kwargs["http2"] = False
    return _orig_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_init

import asyncio
from typing import Optional

from a2a_agent_executor.executor import A2AAgentExecutor
from a2a_agent_executor.smart_logger import SmartLogger
from processgpt_agent_sdk import ProcessGPTAgentServer

# 로그 카테고리 정의
LOG_CATEGORY = "SERVER"


def get_webhook_public_base_url() -> Optional[str]:
    """
    외부 웹훅 리시버 Pod의 Public Base URL을 로드합니다.
    예) http://a2a-webhook-receiver:9000
    """
    val = (os.getenv("WEBHOOK_PUBLIC_BASE_URL") or "").strip()
    return val or None


async def setup_webhook_support(executor: A2AAgentExecutor):
    """
    웹훅 지원을 설정합니다.
    
    외부(별도 Pod) 리시버를 사용할 수 있도록 webhook base URL만 주입합니다.
    
    Args:
        executor: A2AAgentExecutor 인스턴스
        
    Returns:
        (None, None) (호환성 유지)
    """
    public_base_url = get_webhook_public_base_url()
    
    SmartLogger.log(
        "INFO",
        "Setting up webhook support (external receiver mode)",
        category=LOG_CATEGORY,
        params={"public_base_url": public_base_url},
    )
    
    try:
        if not public_base_url:
            SmartLogger.log(
                "WARNING",
                "WEBHOOK_PUBLIC_BASE_URL not set; webhook mode will be disabled",
                category=LOG_CATEGORY,
            )
            executor.set_webhook_public_base_url(None)
        else:
            executor.set_webhook_public_base_url(public_base_url)
            SmartLogger.log(
                "INFO",
                "Webhook support ENABLED (external receiver)",
                category=LOG_CATEGORY,
                params={"public_base_url": public_base_url},
            )

        # 호환성: (receiver, manager) 형태 유지
        return None, None
        
    except Exception as e:
        SmartLogger.log("ERROR", "Failed to setup webhook support", category=LOG_CATEGORY,
                       params={"error": str(e)})
        executor.set_webhook_public_base_url(None)
        return None, None


async def cleanup_webhook_support(
    webhook_receiver: Optional[object],
    webhook_manager: Optional[object],
):
    """
    웹훅 리소스를 정리합니다.
    
    Args:
        webhook_receiver: (호환성용 자리) 더 이상 사용하지 않음
        webhook_manager: (호환성용 자리) 더 이상 사용하지 않음
    """
    SmartLogger.log("INFO", "Cleaning up webhook support", category=LOG_CATEGORY)
    
    # 외부 리시버 모드에서는 로컬 리소스 정리 없음 (호환 로그만 남김)
    SmartLogger.log("INFO", "Webhook support cleaned up (external receiver mode)", category=LOG_CATEGORY)


async def main():
    """ProcessGPT 서버 메인 함수"""
    
    SmartLogger.log("INFO", "ProcessGPT A2A Agent Executor STARTING", category=LOG_CATEGORY,
                   params={
                       "supabase_url": os.getenv("SUPABASE_URL"),
                       "env": os.getenv("ENV"),
                   })
    
    # Executor 초기화
    executor = A2AAgentExecutor()
    
    # 웹훅 지원 설정
    webhook_receiver, webhook_manager = await setup_webhook_support(executor)
    
    # ProcessGPT 서버 생성
    server = ProcessGPTAgentServer(
        agent_executor=executor,
        agent_type="a2a"
    )
    
    # 서버 정보 출력
    print("=" * 60)
    print("ProcessGPT A2A Agent Executor 시작...")
    print("=" * 60)
    print(f"Supabase URL: {os.getenv('SUPABASE_URL')}")
    print(f"ENV: {os.getenv('ENV')}")
    
    public_base_url = get_webhook_public_base_url()
    if public_base_url:
        print("Webhook Mode: ENABLED (external receiver)")
        print(f"Webhook Public Base URL: {public_base_url}")
    else:
        print("Webhook Mode: DISABLED")
    
    print("=" * 60)

    try:
        await server.run()
    except KeyboardInterrupt:
        SmartLogger.log("INFO", "Server shutdown requested (KeyboardInterrupt)", 
                       category=LOG_CATEGORY)
        print("\n서버 중지 요청...")
    except Exception as e:
        SmartLogger.log("ERROR", "Server error", category=LOG_CATEGORY,
                       params={"error": str(e)})
    finally:
        # 웹훅 리소스 정리
        await cleanup_webhook_support(webhook_receiver, webhook_manager)
        
        # 서버 중지
        server.stop()
        
        SmartLogger.log("INFO", "ProcessGPT A2A Agent Executor STOPPED", category=LOG_CATEGORY)
        print("서버가 정상적으로 중지되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
