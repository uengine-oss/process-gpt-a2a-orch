# 시뮬레이터에서 사용자 정의 실행기 사용 예제
from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator
from a2a_agent_executor import A2AAgentExecutor
import asyncio
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)

async def main():
    # 사용자 정의 실행기 생성
    executor = A2AAgentExecutor(agent_endpoint={"url": "http://localhost:10002"})
    
    # 시뮬레이터 생성
    simulator = ProcessGPTAgentSimulator(
        executor=executor,
        agent_orch="a2a"
    )
    
    # 시뮬레이션 실행
    await simulator.run_simulation(
        prompt="인천에 괜찮은 숙소를 알려줘",
        activity_name="report_generation",
        user_id="user123",
        tenant_id="tenant456"
    )

# 실행
if __name__ == "__main__":
    asyncio.run(main())