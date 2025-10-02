import json
import logging
import uuid
from typing import Any, Dict, Optional, List

import httpx
from a2a.client import A2AClient
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import (
    TaskState, 
    TaskStatusUpdateEvent, 
    TaskArtifactUpdateEvent,
    SendMessageRequest, 
    MessageSendParams, 
    MessageSendConfiguration,
    Message,
    TextPart,
    Role,
    Part
)
from a2a.utils import new_agent_text_message, new_text_artifact

from .form_processor import generate_output_json
from .a2a_client import A2AClientManager

logger = logging.getLogger(__name__)


class A2AAgentExecutor(AgentExecutor):
    def __init__(self, name: str = "A2AAgentExecutor", description: str = "A2A agent executor"):
        self.name = name
        self.description = description

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """메인 실행 로직"""
        try:
            logger.info("🎯 A2A Agent 실행 시작")
            
            # Context에서 데이터 추출
            context_data = getattr(context, "get_context_data", lambda: {})() or {}
            row = context_data.get("row", {})
            extras = context_data.get("extras", {})
            proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
            task_id = row.get("id")
            description = row.get("description")
            form_id = extras.get("form_id")
            
            logger.info(f"🔍 form_id: {form_id}, task_id: {task_id}, proc_inst_id: {proc_inst_id}")
            
            # 메시지 추출
            message = self._extract_message(context)
            if not message:
                raise ValueError("No message content provided")
            
            logger.info(f"📝 Message: {message}")
            
            # A2A 에이전트 엔드포인트 추출
            agent_info = self._extract_agent_info(extras)
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
            
            response = await self._send_message_to_agent(agent_endpoint, message)
            result = response.get("result", "")
            
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
            artifact_data = final_result
            event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    artifact=new_text_artifact(
                        name="a2a_agent_result",
                        description="A2A Agent 실행 결과",
                        text=json.dumps(artifact_data, ensure_ascii=False),
                    ),
                    lastChunk=True,
                    contextId=proc_inst_id,
                    taskId=task_id,
                )
            )
            
            logger.info("🎉 A2A Agent 실행 완료")
            
        except Exception as e:
            logger.error(f"❌ A2A Agent 실행 중 오류 발생: {e}", exc_info=True)
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """작업 취소"""
        logger.info("🛑 작업 취소 요청됨")
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
                    logger.warning(f"피드백 처리 중 오류: {e}")
        
        return message

    def _extract_agent_info(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        """extras에서 에이전트 정보 추출"""
        agent_list = extras.get("agents", [])
        if not agent_list or not agent_list[0]:
            raise ValueError("No agent info found")
        
        agent_info = agent_list[0]
        logger.info(f"🔗 Agent info: {agent_info}")
        return agent_info

    async def _send_message_to_agent(self, agent_endpoint: str, message: str) -> Dict[str, Any]:
        """클라이언트 매니저를 사용하여 에이전트에 메시지 전송"""
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
                logger.error(f"Failed to send message to agent: {e}")
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
                result_text += m['text']

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
                        json.dumps(message_content, ensure_ascii=False),
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