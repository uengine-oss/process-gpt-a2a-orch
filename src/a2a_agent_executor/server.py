import socket

_orig_getaddrinfo = socket.getaddrinfo

def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # family=0 은 AF_UNSPEC (IPv4+IPv6 모두)
    if family == 0:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _ipv4_only_getaddrinfo


import asyncio
import os
from processgpt_agent_sdk import ProcessGPTAgentServer
from a2a_agent_executor.executor import A2AAgentExecutor
from dotenv import load_dotenv

async def main():
    """ProcessGPT 서버 메인 함수"""
    
    load_dotenv(override=True)
    
    # 1. 환경변수 확인
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_ANON_KEY"):
        print("오류: SUPABASE_URL과 SUPABASE_ANON_KEY 환경변수가 필요합니다.")
        return
    
    # 2. 사용자 정의 실행기 생성
    executor = A2AAgentExecutor()
    
    # 3. ProcessGPT 서버 생성
    server = ProcessGPTAgentServer(
        agent_executor=executor,
        agent_type="a2a"  # 에이전트 타입 식별자
    )
    
    print("ProcessGPT 서버 시작...")
    print(f"에이전트 타입: a2a_agent")
    print(f"폴링 간격: 5초")
    print("Ctrl+C로 서버를 중지할 수 있습니다.")
    
    try:
        # 4. 서버 실행 (무한 루프)
        await server.run()
    except KeyboardInterrupt:
        print("\n서버 중지 요청...")
        server.stop()
        print("서버가 정상적으로 중지되었습니다.")

if __name__ == "__main__":
    asyncio.run(main())