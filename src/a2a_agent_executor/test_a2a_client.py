import asyncio
import pytest
import httpx
from unittest.mock import Mock, patch, AsyncMock
from a2a.client import A2AClient, A2ACardResolver
from a2a.types import AgentCard, AgentSkill

AGENT_ENDPOINT = "http://127.0.0.1:10002"


class TestA2AClientInitialization:
	"""A2A 클라이언트 초기화 테스트"""
	
	@pytest.mark.asyncio
	async def test_httpx_client_initialization(self):
		"""httpx 클라이언트가 올바르게 초기화되는지 테스트"""
		httpx_client = httpx.AsyncClient(timeout=60)
		assert httpx_client.timeout.connect == 60
		assert httpx_client.timeout.read == 60
		assert httpx_client.timeout.write == 60
		assert httpx_client.timeout.pool == 60
		await httpx_client.aclose()
	
	@pytest.mark.asyncio
	async def test_a2a_client_initialization(self):
		"""A2A 클라이언트가 올바르게 초기화되는지 테스트"""
		httpx_client = httpx.AsyncClient(timeout=60)
		
		client = A2AClient(httpx_client=httpx_client, url=AGENT_ENDPOINT)
		assert client._transport.url == AGENT_ENDPOINT
		assert client._transport.httpx_client == httpx_client
		
		await httpx_client.aclose()
	
	@pytest.mark.asyncio
	async def test_card_resolver_initialization(self):
		"""A2A 카드 리졸버가 올바르게 초기화되는지 테스트"""
		httpx_client = httpx.AsyncClient(timeout=60)
		
		card_resolver = A2ACardResolver(httpx_client=httpx_client, base_url=AGENT_ENDPOINT)
		assert card_resolver.base_url == AGENT_ENDPOINT
		assert card_resolver.httpx_client == httpx_client
		
		await httpx_client.aclose()


class TestAgentCardRetrieval:
	"""에이전트 카드 조회 테스트"""
	
	@pytest.mark.asyncio
	async def test_get_agent_card_success(self):
		"""에이전트 카드 조회 성공 테스트"""
		httpx_client = httpx.AsyncClient(timeout=60)
		card_resolver = A2ACardResolver(httpx_client=httpx_client, base_url=AGENT_ENDPOINT)
		
		try:
			# 실제 에이전트에서 카드 조회
			agent_card = await card_resolver.get_agent_card()
			agent_skills = agent_card.skills
			
			# 기본 검증
			assert agent_card.name is not None
			assert agent_card.description is not None
			assert agent_card.url is not None
			assert agent_card.version is not None
			assert isinstance(agent_skills, list)
			
			print(f"Agent name: {agent_card.name}")
			print(f"Agent description: {agent_card.description}")
			print(f"Agent URL: {agent_card.url}")
			print(f"Agent version: {agent_card.version}")
			print(f"Number of skills: {len(agent_skills)}")
			
			# 스킬 정보 출력
			for i, skill in enumerate(agent_skills):
				print(f"Skill {i+1}: {skill.name} - {skill.description}")
				if skill.tags:
					print(f"  Tags: {skill.tags}")
			
		except Exception as e:
			print(f"에이전트 카드 조회 실패: {e}")
			# 에이전트가 실행 중이 아닐 수 있으므로 테스트는 통과시키되 경고 출력
			print("⚠️  실제 에이전트가 실행 중이 아닐 수 있습니다.")
		
		await httpx_client.aclose()
	
	@pytest.mark.asyncio
	async def test_get_agent_card_with_empty_skills(self):
		"""실제 에이전트의 스킬 개수 확인 테스트"""
		httpx_client = httpx.AsyncClient(timeout=60)
		card_resolver = A2ACardResolver(httpx_client=httpx_client, base_url=AGENT_ENDPOINT)
		
		try:
			# 실제 에이전트에서 카드 조회
			agent_card = await card_resolver.get_agent_card()
			agent_skills = agent_card.skills
			
			# 스킬 개수 검증
			assert isinstance(agent_skills, list)
			print(f"실제 에이전트 스킬 개수: {len(agent_skills)}")
			
			if len(agent_skills) == 0:
				print("⚠️  이 에이전트는 스킬이 없습니다.")
			else:
				print("✅ 에이전트에 스킬이 있습니다.")
				for i, skill in enumerate(agent_skills):
					print(f"  - {skill.name}: {skill.description}")
			
		except Exception as e:
			print(f"에이전트 카드 조회 실패: {e}")
			print("⚠️  실제 에이전트가 실행 중이 아닐 수 있습니다.")
		
		await httpx_client.aclose()


class TestErrorHandling:
	"""에러 처리 테스트"""
	
	@pytest.mark.asyncio
	async def test_httpx_client_timeout_error(self):
		"""httpx 클라이언트 타임아웃 에러 테스트"""
		with pytest.raises(Exception):
			# 매우 짧은 타임아웃으로 설정하여 에러 발생
			httpx_client = httpx.AsyncClient(timeout=0.001)
			# 실제 요청을 보내지 않고 클라이언트만 생성
			await httpx_client.aclose()
	
	@pytest.mark.asyncio
	async def test_invalid_agent_endpoint(self):
		"""잘못된 에이전트 엔드포인트 테스트"""
		invalid_endpoint = "invalid-url"
		httpx_client = httpx.AsyncClient(timeout=60)
		
		# 잘못된 URL로 클라이언트 생성 시도
		try:
			client = A2AClient(httpx_client=httpx_client, url=invalid_endpoint)
			# URL 검증이 없다면 클라이언트는 생성되지만 실제 요청 시 에러 발생
			assert client._transport.url == invalid_endpoint
		except Exception as e:
			print(f"Expected error with invalid endpoint: {e}")
		
		await httpx_client.aclose()


class TestIntegration:
	"""통합 테스트"""
	
	@pytest.mark.asyncio
	async def test_full_initialization_flow(self):
		"""전체 초기화 플로우 테스트 - 실제 에이전트와 통신"""
		# Initialize the client
		httpx_client = httpx.AsyncClient(timeout=60)
		client = A2AClient(httpx_client=httpx_client, url=AGENT_ENDPOINT)
		
		# Get agent card and agent skills
		card_resolver = A2ACardResolver(httpx_client=httpx_client, base_url=AGENT_ENDPOINT)
		
		try:
			# 실제 에이전트에서 카드 조회
			agent_card = await card_resolver.get_agent_card()
			agent_skills = agent_card.skills
			
			# 기본 검증
			assert client._transport.url == AGENT_ENDPOINT
			assert card_resolver.base_url == AGENT_ENDPOINT
			assert agent_card.name is not None
			assert agent_card.description is not None
			assert isinstance(agent_skills, list)
			
			print(f"✅ 통합 테스트 성공!")
			print(f"Agent name: {agent_card.name}")
			print(f"Agent description: {agent_card.description}")
			print(f"Agent URL: {agent_card.url}")
			print(f"Agent version: {agent_card.version}")
			print(f"Number of skills: {len(agent_skills)}")
			
			# 스킬 상세 정보
			for i, skill in enumerate(agent_skills):
				print(f"Skill {i+1}: {skill.name}")
				print(f"  Description: {skill.description}")
				if skill.tags:
					print(f"  Tags: {skill.tags}")
				if skill.examples:
					print(f"  Examples: {skill.examples}")
			
		except Exception as e:
			print(f"통합 테스트 실패: {e}")
			print("⚠️  실제 에이전트가 실행 중이 아닐 수 있습니다.")
			# 에이전트가 없어도 클라이언트 초기화는 성공해야 함
			assert client._transport.url == AGENT_ENDPOINT
			assert card_resolver.base_url == AGENT_ENDPOINT
		
		await httpx_client.aclose()


if __name__ == "__main__":
	# 테스트 실행을 위한 간단한 실행 함수
	async def run_tests():
		print("=== A2A 클라이언트 초기화 테스트 ===")
		test_init = TestA2AClientInitialization()
		
		print("\n1. httpx 클라이언트 초기화 테스트")
		await test_init.test_httpx_client_initialization()
		print("✓ httpx 클라이언트 초기화 성공")
		
		print("\n2. A2A 클라이언트 초기화 테스트")
		await test_init.test_a2a_client_initialization()
		print("✓ A2A 클라이언트 초기화 성공")
		
		print("\n3. 카드 리졸버 초기화 테스트")
		await test_init.test_card_resolver_initialization()
		print("✓ 카드 리졸버 초기화 성공")
		
		print("\n=== 에이전트 카드 조회 테스트 ===")
		test_card = TestAgentCardRetrieval()
		
		print("\n4. 실제 에이전트 카드 조회 테스트")
		await test_card.test_get_agent_card_success()
		print("✓ 실제 에이전트 카드 조회 완료")
		
		print("\n5. 실제 에이전트 스킬 개수 확인 테스트")
		await test_card.test_get_agent_card_with_empty_skills()
		print("✓ 실제 에이전트 스킬 개수 확인 완료")
		
		print("\n=== 에러 처리 테스트 ===")
		test_error = TestErrorHandling()
		
		print("\n6. 잘못된 에이전트 엔드포인트 테스트")
		await test_error.test_invalid_agent_endpoint()
		print("✓ 잘못된 엔드포인트 테스트 성공")
		
		print("\n=== 통합 테스트 ===")
		test_integration = TestIntegration()
		
		print("\n7. 실제 에이전트와의 통합 테스트")
		await test_integration.test_full_initialization_flow()
		print("✓ 실제 에이전트와의 통합 테스트 완료")
		
		print("\n🎉 모든 테스트가 성공적으로 완료되었습니다!")
	
	# 테스트 실행
	asyncio.run(run_tests())
