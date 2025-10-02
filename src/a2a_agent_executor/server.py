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
from a2a_agent_executor.executor import A2AAgentExecutor
from processgpt_agent_sdk import ProcessGPTAgentServer

async def main():
    """ProcessGPT 서버 메인 함수"""
    executor = A2AAgentExecutor()
    
    server = ProcessGPTAgentServer(
        agent_executor=executor,
        agent_type="a2a"
    )
    
    print("ProcessGPT A2A Agent Executor 시작...")
    print("Supabase URL: ", os.getenv("SUPABASE_URL"))
    print("ENV: ", os.getenv("ENV"))

    try:
        await server.run()
    except KeyboardInterrupt:
        print("\n서버 중지 요청...")
        server.stop()
        print("서버가 정상적으로 중지되었습니다.")

if __name__ == "__main__":
    asyncio.run(main())