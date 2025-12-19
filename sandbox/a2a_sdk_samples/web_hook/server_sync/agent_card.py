# server_sync/agent_card.py
"""
AgentCard 정의 모듈 (동기 방식 HITL 지원)
Human-in-the-Loop를 지원하지만 Push Notification 없이 동기 방식으로 동작하는 에이전트 정보를 정의합니다.

지원 기능:
- Human-in-the-Loop (input_required 상태) - 동기 방식
- Push Notifications: 비활성화 (클라이언트가 폴링/재요청 방식 사용)

동기 방식 HITL 워크플로우:
1. 클라이언트가 blocking 요청 전송
2. 서버가 input_required 상태로 즉시 응답 반환
3. 클라이언트가 같은 task_id로 사용자 응답과 함께 재요청
4. 서버가 작업 완료 후 응답 반환
"""

from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill
)


def create_agent_card(host: str = "localhost", port: int = 8000) -> AgentCard:
    """
    동기 방식 HITL을 지원하는 에이전트 카드를 생성합니다.
    
    Push Notification이 비활성화되어 있으므로, 클라이언트는
    blocking 요청 후 응답에서 직접 상태를 확인해야 합니다.
    
    Args:
        host: 서버 호스트
        port: 서버 포트
    
    Returns:
        AgentCard: 에이전트 정보
    """
    
    # 1. HITL 스킬 정의 (동기 방식)
    hitl_skill = AgentSkill(
        id="hitl_approval_sync",
        name="Human-in-the-Loop 승인 (동기 방식)",
        description=(
            "사용자 승인이 필요한 작업을 동기 방식으로 처리합니다. "
            "특정 키워드(승인, 확인, 예산 등)가 포함된 요청 시 "
            "input_required 상태를 즉시 응답으로 반환합니다. "
            "클라이언트는 같은 task_id로 재요청하여 작업을 계속합니다."
        ),
        tags=["hitl", "approval", "human-in-the-loop", "sync", "blocking"],
        examples=[
            "예산 승인이 필요합니다",
            "이 작업을 확인해주세요",
            "approval request for budget increase",
        ],
    )
    
    # 2. 일반 작업 스킬 정의
    general_skill = AgentSkill(
        id="general_task_sync",
        name="일반 작업 처리 (동기 방식)",
        description=(
            "일반 작업을 동기 방식으로 처리합니다. "
            "HITL 키워드가 없는 요청은 바로 처리되어 응답됩니다."
        ),
        tags=["sync", "blocking", "general"],
        examples=[
            "데이터 처리 요청",
            "보고서 생성",
        ],
    )
    
    # 3. 에이전트의 기능(Capabilities) 정의
    # 핵심: push_notifications=False
    capabilities = AgentCapabilities(
        streaming=False,  # 동기 방식 사용
        push_notifications=False,  # 푸시 알림 비활성화 (클라이언트가 재요청 방식 사용)
        state_transition_history=True,  # 상태 전환 히스토리 지원
    )
    
    # 4. AgentCard 생성
    card = AgentCard(
        name="HITL 동기 에이전트",
        description=(
            "Human-in-the-Loop 워크플로우를 동기 방식으로 지원하는 에이전트입니다. "
            "Push Notification 대신 blocking 요청/응답 방식을 사용합니다. "
            "승인이 필요한 작업 시 input_required 상태를 즉시 응답으로 반환하고, "
            "클라이언트가 같은 task_id로 재요청하면 작업을 계속합니다. "
            "HITL 트리거 키워드: 승인, 확인, 예산, approval, confirm, budget, hitl"
        ),
        url=f"http://{host}:{port}",
        version="2.0.0",
        capabilities=capabilities,
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[hitl_skill, general_skill],
    )
    
    return card


if __name__ == "__main__":
    # 테스트: AgentCard 생성 및 출력
    card = create_agent_card()
    print("✅ AgentCard 생성 성공! (동기 방식)")
    print(f"📝 이름: {card.name}")
    print(f"📝 설명: {card.description}")
    print(f"📝 버전: {card.version}")
    print(f"📝 스킬 개수: {len(card.skills)}")
    print("\n📝 스킬 목록:")
    for skill in card.skills:
        print(f"  - {skill.name}: {skill.description[:50]}...")
    print(f"\n📝 스트리밍 지원: {card.capabilities.streaming}")
    print(f"📝 Push Notifications 지원: {card.capabilities.push_notifications}")
    print(f"📝 HITL 지원: ✅ (동기 방식 - 클라이언트 재요청)")

