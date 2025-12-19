"""
A2A Agent Executor 모듈
A2A 에이전트와의 통신을 실행합니다.

주요 기능:
- 동기 모드: 기존 방식, 완료까지 대기
- 웹훅 모드: 비동기 방식, 즉시 반환하고 외부 Webhook Receiver Pod가 알림 처리

모드 선택:
- A2A 서버의 AgentCard에서 push_notifications 지원 여부 확인
- 지원하면 웹훅 모드, 미지원하면 동기 모드

웹훅 모드 (Stateless, External Receiver):
- URL에 todolist_id 포함하여 전송
- 메모리에 상태 저장 없음
- 웹훅 응답 시 DB에서 정보 조회하여 처리
"""

import json
import uuid
from typing import Any, Dict, Optional, List

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import (
    TaskState, 
    TaskStatusUpdateEvent, 
    TaskArtifactUpdateEvent,
    TextPart,
)
from a2a.utils import new_agent_text_message, new_text_artifact

from a2a_form_processor import generate_output_json
from .a2a_client import A2AClientManager
from .smart_logger import SmartLogger

# 로그 카테고리 정의
LOG_CATEGORY = "A2A_EXECUTOR"


class A2AAgentExecutor(AgentExecutor):
    """
    A2A 에이전트 실행기
    
    동기 모드와 웹훅 모드를 지원합니다.
    - 동기 모드: A2A 서버가 웹훅을 지원하지 않는 경우, 완료까지 대기
    - 웹훅 모드: A2A 서버가 웹훅을 지원하는 경우, 즉시 반환 (Stateless)
    
    사용 예시:
        executor = A2AAgentExecutor()
        
        # 외부 웹훅 리시버 URL 설정 (server.py에서 수행)
        executor.set_webhook_public_base_url("http://a2a-webhook-receiver:9000")
        
        # 실행 (RequestContext, EventQueue는 ProcessGPTAgentServer가 전달)
        await executor.execute(context, event_queue)
    """
    
    def __init__(self, name: str = "A2AAgentExecutor", description: str = "A2A agent executor"):
        self.name = name
        self.description = description
        
        # 웹훅 관련 (server.py에서 설정)
        self._webhook_public_base_url: Optional[str] = None
        
        SmartLogger.log("DEBUG", "A2AAgentExecutor initialized", category=LOG_CATEGORY,
                       params={"name": name})
    
    def set_webhook_public_base_url(self, public_base_url: Optional[str]) -> None:
        """
        외부(별도 Pod) 웹훅 리시버의 public base url을 설정합니다.
        예) http://a2a-webhook-receiver:9000
        """
        self._webhook_public_base_url = (public_base_url or "").strip() or None
        SmartLogger.log(
            "INFO",
            "Webhook public base url configured",
            category=LOG_CATEGORY,
            params={"public_base_url": self._webhook_public_base_url},
        )
    
    @property
    def has_webhook_support(self) -> bool:
        """웹훅 지원이 설정되어 있는지 확인합니다."""
        return bool(self._webhook_public_base_url)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        메인 실행 로직
        
        A2A 서버의 웹훅 지원 여부에 따라 모드를 자동 선택합니다.
        """
        try:
            SmartLogger.log("INFO", "A2A Agent execution STARTED", category=LOG_CATEGORY)
            
            # Context에서 데이터 추출
            context_data = getattr(context, "get_context_data", lambda: {})() or {}
            row = context_data.get("row", {})
            extras = context_data.get("extras", {})
            proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
            task_id = row.get("id")
            description = row.get("description")
            form_id = extras.get("form_id")
            
            SmartLogger.log("INFO", "Context data extracted", category=LOG_CATEGORY,
                           params={
                               "form_id": form_id,
                               "task_id": task_id,
                               "proc_inst_id": proc_inst_id,
                           })
            
            # 메시지 추출
            message = self._extract_message(context)
            if not message:
                raise ValueError("No message content provided")
            
            SmartLogger.log("DEBUG", "Message extracted", category=LOG_CATEGORY,
                           params={"message_length": len(message)})
            
            # A2A 에이전트 정보 추출
            agent_info = self._extract_agent_info(extras)
            agent_endpoint = agent_info.get("endpoint")
            
            SmartLogger.log("INFO", "Agent info extracted", category=LOG_CATEGORY,
                           params={
                               "agent_endpoint": agent_endpoint,
                               "agent_name": agent_info.get("username"),
                           })
            
            # 웹훅 지원 여부 확인 및 모드 결정
            use_webhook_mode = False
            
            if self.has_webhook_support:
                async with A2AClientManager() as client_manager:
                    try:
                        card = await client_manager.get_agent_card(agent_endpoint)
                        use_webhook_mode = client_manager.supports_push_notifications(card)
                        
                        SmartLogger.log("INFO", "Agent capability checked", category=LOG_CATEGORY,
                                       params={
                                           "agent_endpoint": agent_endpoint,
                                           "push_notifications": use_webhook_mode,
                                       })
                    except Exception as e:
                        SmartLogger.log("WARNING", "Failed to check agent capabilities, using sync mode",
                                       category=LOG_CATEGORY,
                                       params={"error": str(e)})
                        use_webhook_mode = False
            
            # 모드별 실행
            if use_webhook_mode:
                SmartLogger.log("INFO", "Using WEBHOOK mode (stateless)", category=LOG_CATEGORY,
                               params={"agent_endpoint": agent_endpoint})
                await self._execute_webhook_mode(
                    context, event_queue, row, extras, 
                    agent_info, message, proc_inst_id, task_id, description
                )
            else:
                SmartLogger.log("INFO", "Using SYNC mode", category=LOG_CATEGORY,
                               params={"agent_endpoint": agent_endpoint})
                await self._execute_sync_mode(
                    context, event_queue, row, extras,
                    agent_info, message, proc_inst_id, task_id, description
                )
            
        except Exception as e:
            SmartLogger.log("ERROR", "A2A Agent execution FAILED", category=LOG_CATEGORY,
                          params={"error": str(e)})
            raise

    async def _execute_sync_mode(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        row: Dict[str, Any],
        extras: Dict[str, Any],
        agent_info: Dict[str, Any],
        message: str,
        proc_inst_id: str,
        task_id: str,
        description: str,
    ) -> None:
        """
        동기 모드 실행 (기존 로직)
        
        A2A 서버에 요청을 보내고 완료될 때까지 대기합니다.
        """
        agent_endpoint = agent_info.get("endpoint")
        
        # 에이전트 작업 시작 이벤트
        job_uuid = str(uuid.uuid4())
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.working,
            message_content={
                "role": agent_info.get("username"),
                "name": agent_info.get("username"),
                "goal": description,
                "agent_profile": agent_info.get("profile"),
            },
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=job_uuid,
            event_type="task_started",
            crew_type="task"
        )
        
        SmartLogger.log("INFO", "Sending message to A2A agent (sync)", category=LOG_CATEGORY,
                       params={"agent_endpoint": agent_endpoint})
        
        response = await self._send_message_to_agent(agent_endpoint, message)
        result = response.get("result", "")
        
        SmartLogger.log("INFO", "Received response from A2A agent (sync)", category=LOG_CATEGORY,
                       params={
                           "agent_endpoint": agent_endpoint,
                           "result_length": len(result) if result else 0,
                       })
        
        # 에이전트 작업 완료 이벤트
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.completed,
            message_content=result,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=job_uuid,
            event_type="task_completed",
            crew_type="task"
        )

        final_result = result
        
        # form_processor를 사용한 산출물 처리 및 이벤트 발송
        result_job_uuid = str(uuid.uuid4())
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.working,
            message_content={
                "role": "최종 결과 반환", 
                "name": "최종 결과 반환", 
                "goal": "요청된 폼 형식에 맞는 최종 결과를 반환합니다.", 
                "agent_profile": "/images/chat-icon.png"
            },
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=result_job_uuid,
            event_type="task_started",
            crew_type="result"
        )
        
        query = row.get("query", "")
        if query and "[InputData]" in query:
            input_text = query.split("[InputData]")[1]
        else:
            input_text = None
        
        form_output = await generate_output_json(task_id, result, input_text)
        if form_output:
            final_result = form_output
            
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.completed,
            message_content=final_result,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=result_job_uuid,
            event_type="task_completed",
            crew_type="result"
        )

        # 아티팩트 이벤트
        event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=new_text_artifact(
                    name="a2a_agent_result",
                    description="A2A Agent 실행 결과",
                    text=json.dumps({"final_result": final_result}, ensure_ascii=False),
                ),
                lastChunk=True,
                contextId=proc_inst_id,
                taskId=task_id,
            )
        )
        
        SmartLogger.log("INFO", "A2A Agent execution COMPLETED (sync mode)", category=LOG_CATEGORY,
                       params={"task_id": task_id})

    async def _execute_webhook_mode(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        row: Dict[str, Any],
        extras: Dict[str, Any],
        agent_info: Dict[str, Any],
        message: str,
        proc_inst_id: str,
        task_id: str,
        description: str,
    ) -> None:
        """
        웹훅 모드 실행 (Stateless)
        
        URL에 todolist_id를 포함하여 메시지를 전송합니다.
        메모리에 상태를 저장하지 않으며, 완료 처리는 외부 Webhook Receiver Pod가
        웹훅 알림을 받아 DB에서 정보를 조회하여 수행합니다.
        """
        agent_endpoint = agent_info.get("endpoint")
        web_hook_accepted_job_uuid = str(uuid.uuid4())
        web_hook_callback_waiting_job_uuid = str(uuid.uuid4())
        
        # 웹훅 접수 요청 작업 시작 이벤트
        self._enqueue_task_status_event(
            event_queue=event_queue,
            state=TaskState.working,
            message_content={
                "role": agent_info.get("username"),
                "name": agent_info.get("username"),
                "goal": description,
                "agent_profile": agent_info.get("profile"),
            },
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=web_hook_accepted_job_uuid,
            event_type="task_started",
            crew_type="task"
        )
        
        # 웹훅 URL 생성 (todolist_id 포함)
        webhook_url = self._get_webhook_url(str(task_id))
        webhook_token = None  # 정책: 토큰 검증 제거
        
        SmartLogger.log("INFO", "Sending message to A2A agent (webhook, stateless)", category=LOG_CATEGORY,
                       params={
                           "agent_endpoint": agent_endpoint,
                           "webhook_url": webhook_url,
                           "todolist_id": task_id,
                       })
        
        # 웹훅 모드로 메시지 전송
        async with A2AClientManager() as client_manager:
            try:
                response = await client_manager.send_message_with_webhook(
                    agent_endpoint=agent_endpoint,
                    message=message,
                    webhook_url=webhook_url,
                    webhook_token=webhook_token or "",
                )
                
                # 응답에서 A2A task_id 추출 (로깅용)
                a2a_task_id = client_manager.extract_task_id_from_response(response)
                # 응답에서 서버 첫 메시지(working status message) 텍스트 추출 (goal에 포함)
                a2a_first_msg = client_manager.extract_first_agent_message_text_from_response(response)

                # 1차 응답을 events에 기록: webhook_accepted
                accepted_goal = "A2A non-blocking 요청이 접수되었습니다. 완료는 웹훅 콜백으로 처리됩니다."
                if a2a_first_msg:
                    accepted_goal = f"{accepted_goal}\n\n[A2A 서버 메시지]\n{a2a_first_msg}"
                self._enqueue_task_status_event(
                    event_queue=event_queue,
                    state=TaskState.working,
                    message_content={
                        "role": "A2A Webhook Accepted",
                        "name": "A2A Webhook Accepted",
                        "goal": accepted_goal,
                        "agent_profile": "/images/chat-icon.png",
                        "subtype": "webhook_accepted",
                        "a2a_task_id": a2a_task_id,
                        "webhook_url": webhook_url,
                    },
                    proc_inst_id=proc_inst_id,
                    task_id=task_id,
                    job_uuid=web_hook_accepted_job_uuid,
                    event_type="task_completed",
                    crew_type="task",
                )

                self._enqueue_task_status_event(
                    event_queue=event_queue,
                    state=TaskState.working,
                    message_content={
                        "role": "A2A Webhook Callback Waiting",
                        "name": "A2A Webhook Callback Waiting",
                        "goal": "콜백 호출 결과를 기다리고 있습니다.",
                        "agent_profile": "/images/chat-icon.png",
                        "subtype": "webhook_callback_waiting",
                        "a2a_task_id": a2a_task_id,
                        "webhook_url": webhook_url,
                    },
                    proc_inst_id=proc_inst_id,
                    task_id=task_id,
                    job_uuid=web_hook_callback_waiting_job_uuid,
                    event_type="task_started",
                    crew_type="task",
                )

                # IMPORTANT:
                # webhook(non-blocking) 모드는 여기서 execute()가 즉시 리턴되므로,
                # SDK(ProcessGPTAgentServer)가 execute() 반환 직후 호출하는 event_queue.task_done()
                # 때문에 'crew_completed'가 접수 시점에 기록될 수 있습니다.
                # 동기(sync) 모드에는 영향이 없도록, webhook 모드에서만(그리고 이 요청의 event_queue에서만)
                # task_done을 no-op으로 무력화합니다.
                try:
                    event_queue.task_done = lambda: None  # type: ignore[assignment]
                    SmartLogger.log(
                        "DEBUG",
                        "Webhook mode: suppressed task_done/crew_completed at submission time",
                        category=LOG_CATEGORY,
                        params={"todolist_id": task_id, "a2a_task_id": a2a_task_id},
                    )
                except Exception:
                    # best-effort: if event_queue is not mutable, do nothing
                    pass
                
                SmartLogger.log("INFO", "Webhook message sent successfully (stateless)", 
                               category=LOG_CATEGORY,
                               params={
                                   "todolist_id": task_id,
                                   "a2a_task_id": a2a_task_id,
                               })
                
            except Exception as e:
                SmartLogger.log("ERROR", "Failed to send webhook message", 
                              category=LOG_CATEGORY,
                              params={
                                  "agent_endpoint": agent_endpoint,
                                  "error": str(e),
                              })
                raise
        
        # 웹훅 모드에서는 여기서 즉시 반환
        # 완료 처리는 외부 Webhook Receiver Pod가 웹훅 알림을 받아 DB에서 조회하여 수행
        SmartLogger.log("INFO", "A2A Agent task submitted (webhook mode, awaiting callback)",
                       category=LOG_CATEGORY,
                       params={
                           "task_id": task_id,
                           "mode": "webhook_stateless",
                       })

    def _get_webhook_url(self, todolist_id: str) -> str:
        """
        외부 리시버 Pod로 향하는 웹훅 URL을 생성합니다.
        """
        if not self._webhook_public_base_url:
            raise RuntimeError(
                "Webhook receiver not configured (missing WEBHOOK_PUBLIC_BASE_URL)"
            )
        return f"{self._webhook_public_base_url}/webhook/a2a/todolist/{todolist_id}"

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """작업 취소"""
        SmartLogger.log("INFO", "Task cancellation requested", category=LOG_CATEGORY)
        return

    def _extract_message(self, context: RequestContext) -> str:
        """메시지 추출"""
        message = getattr(context, 'message', '') or ''
        
        # 피드백 처리
        if hasattr(context, 'row') and context.row:
            feedback_data = context.row.get('feedback', [])
            if feedback_data and isinstance(feedback_data, list) and len(feedback_data) > 0:
                try:
                    latest_feedback = sorted(feedback_data, key=lambda x: x.get('time', ''), reverse=True)[0]
                    if isinstance(latest_feedback, dict) and 'content' in latest_feedback:
                        feedback_content = latest_feedback['content']
                        if feedback_content:
                            message = f"{message}\n\n[Feedback]\n{feedback_content}"
                except Exception as e:
                    SmartLogger.log("WARNING", "Error processing feedback", category=LOG_CATEGORY,
                                   params={"error": str(e)})
        
        return message

    def _extract_agent_info(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        """extras에서 에이전트 정보 추출"""
        agent_list = extras.get("agents", [])
        if not agent_list or not agent_list[0]:
            raise ValueError("No agent info found")
        
        agent_info = agent_list[0]
        SmartLogger.log("DEBUG", "Agent info extracted", category=LOG_CATEGORY,
                       params={
                           "agent_name": agent_info.get("username"),
                           "has_endpoint": bool(agent_info.get("endpoint")),
                       })
        return agent_info

    async def _send_message_to_agent(self, agent_endpoint: str, message: str) -> Dict[str, Any]:
        """클라이언트 매니저를 사용하여 에이전트에 메시지 전송 (동기 모드)"""
        async with A2AClientManager() as client_manager:
            try:
                response = await client_manager.send_message(agent_endpoint, message)
                
                # 응답에서 결과 추출
                if response and response.root and response.root.result:
                    task = response.root.result
                    return self._extract_result_from_task(task)
                else:
                    return {"result": "No response from agent", "status": "completed"}
                    
            except Exception as e:
                SmartLogger.log("ERROR", "Failed to send message to agent", category=LOG_CATEGORY,
                              params={"error": str(e)})
                raise

    def _extract_result_from_task(self, task: Any) -> Dict[str, Any]:
        """태스크에서 결과 추출"""
        history_compact: List[Dict[str, Any]] = []
        try:
            if task is not None and getattr(task, 'history', None):
                for m in task.history:
                    try:
                        role_val = getattr(m, 'role', None)
                        role_name = role_val.value if hasattr(role_val, 'value') else str(role_val)
                    except Exception:
                        role_name = None
                    texts: List[str] = []
                    try:
                        for p in getattr(m, 'parts', []) or []:
                            root = getattr(p, 'root', None)
                            if isinstance(root, TextPart) and getattr(root, 'text', None):
                                texts.append(root.text)
                    except Exception:
                        pass
                    history_compact.append({
                        'role': role_name,
                        'text': "".join(texts) if texts else None,
                    })
        except Exception:
            history_compact = []
        
        result_text = ""
        for m in history_compact:
            if m['role'] != "user":
                result_text += m['text'] or ""

        return {
            "result": result_text or "Task completed",
            "status": "completed",
            "task_id": getattr(task, 'id', None)
        }

    def _enqueue_task_status_event(
        self, 
        event_queue: EventQueue, 
        state: TaskState, 
        message_content: Any, 
        proc_inst_id: str, 
        task_id: str, 
        job_uuid: str, 
        event_type: str,
        crew_type: str
    ) -> None:
        """TaskStatusUpdateEvent를 큐에 추가하는 범용 함수"""
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": state,
                    "message": new_agent_text_message(
                        json.dumps(message_content, ensure_ascii=False) if isinstance(message_content, dict) else message_content,
                        proc_inst_id,
                        task_id,
                    ),
                },
                final=False,
                contextId=proc_inst_id,
                taskId=task_id,
                metadata={
                    "crew_type": crew_type,
                    "event_type": event_type,
                    "job_id": job_uuid,
                },
            )
        )
