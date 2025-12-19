# server/agent_card.py
"""
AgentCard 정의 모듈 (HITL + Webhook 지원)
Human-in-the-Loop 및 Push Notification(Webhook) capability가 활성화된 에이전트 정보를 정의합니다.

지원 기능:
- Push Notifications (Webhook)
- Human-in-the-Loop (input_required 상태)
- 장시간 작업 처리
"""

from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill
)


def create_agent_card(host: str = "localhost", port: int = 8000) -> AgentCard:
    """
    HITL 및 Webhook을 지원하는 에이전트 카드를 생성합니다.
    
    Args:
        host: 서버 호스트
        port: 서버 포트
    
    Returns:
        AgentCard: 에이전트 정보
    """
    
    # 1. HITL 스킬 정의
    hitl_skill = AgentSkill(
        id="hitl_approval",
        name="Human-in-the-Loop 승인",
        description=(
            "사용자 승인이 필요한 작업을 처리합니다. "
            "특정 키워드(승인, 확인, 예산 등)가 포함된 요청 시 "
            "input_required 상태로 전환하여 사용자 확인을 요청합니다."
        ),
        tags=["hitl", "approval", "human-in-the-loop", "confirmation"],
        examples=[
            "예산 승인이 필요합니다",
            "이 작업을 확인해주세요",
            "approval request for budget increase",
        ],
    )
    
    # 2. 장시간 작업 스킬 정의
    long_running_skill = AgentSkill(
        id="long_running_task",
        name="장시간 작업 처리",
        description=(
            "장시간 실행되는 작업을 처리하고 완료 시 webhook으로 알림을 보냅니다. "
            "HITL 키워드가 없는 일반 요청은 바로 처리됩니다."
        ),
        tags=["webhook", "async", "long-running"],
        examples=[
            "데이터 처리 요청",
            "보고서 생성",
        ],
    )
    
    # 3. 에이전트의 기능(Capabilities) 정의
    capabilities = AgentCapabilities(
        streaming=False,  # non-blocking 모드 사용
        push_notifications=True,  # Webhook 활성화 (HITL 알림에도 사용)
        state_transition_history=True,  # 상태 전환 히스토리 지원
    )
    
    # 4. AgentCard 생성
    card = AgentCard(
        name="HITL 지원 에이전트",
        description=(
            "Human-in-the-Loop 워크플로우를 지원하는 에이전트입니다. "
            "승인이 필요한 작업 시 input_required 상태로 전환하여 "
            "사용자 확인을 요청하고, webhook을 통해 상태 변화를 알립니다. "
            "HITL 트리거 키워드: 승인, 확인, 예산, approval, confirm, budget, hitl"
        ),
        url=f"http://{host}:{port}",
        version="2.0.0",
        capabilities=capabilities,
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[hitl_skill, long_running_skill],
    )
    
    return card


if __name__ == "__main__":
    # 테스트: AgentCard 생성 및 출력
    card = create_agent_card()
    print("✅ AgentCard 생성 성공!")
    print(f"📝 이름: {card.name}")
    print(f"📝 설명: {card.description}")
    print(f"📝 버전: {card.version}")
    print(f"📝 스킬 개수: {len(card.skills)}")
    print("\n📝 스킬 목록:")
    for skill in card.skills:
        print(f"  - {skill.name}: {skill.description[:50]}...")
    print(f"\n📝 스트리밍 지원: {card.capabilities.streaming}")
    print(f"📝 Push Notifications 지원: {card.capabilities.push_notifications}")
    print(f"📝 HITL 지원: ✅ (via input_required state)")
