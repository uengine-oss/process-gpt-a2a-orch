import asyncio
import logging
import uuid
import json
from typing import Any, Dict, List, Optional

import httpx
from a2a.client import A2AClient
from a2a.client.errors import A2AClientHTTPError, A2AClientJSONError
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import AgentCard, Message, Task, TaskState, TaskStatus, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, Artifact, Part, TextPart, DataPart, Role
from a2a.utils import new_agent_text_message, new_text_artifact
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AgentEndpoint(BaseModel):
    """Model representing an agent endpoint configuration."""
    url: str
    headers: Dict[str, str] = Field(default_factory=dict)


class A2AAgentExecutor(AgentExecutor):
    """
    An A2A Agent Executor that acts as a proxy between clients and another A2A agent.
    
    It receives requests as an A2A server, forwards them to another agent via A2A client,
    and returns intermediate results during execution.
    """

    def __init__(
        self,
        name: str = "A2AAgentExecutor",
        description: str = "A proxy agent that forwards requests to another A2A agent",
        version: str = "0.1.0",
    ):
        """
        Initialize the A2AAgentExecutor.
        
        Args:
            agent_endpoint: The endpoint of the target A2A agent.
                Can be a simple string URL or a dictionary with more configuration.
            name: The name of this agent.
            description: A description of this agent.
            version: The version of this agent.
        """
        self.name = name
        self.description = description
        self.version = version
        

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Execute the agent's logic for a given request context.
        
        Args:
            context: The request context containing the message, task ID, etc.
            event_queue: The queue to publish events to.
        """
        context_data: Dict[str, Any] = getattr(context, "get_context_data", lambda: {})() or {}
        extras = context_data.get("extras", {})
        if extras and extras.get("agents"):
            agent_list = extras.get("agents")
        else:
            agent_list = context_data.get("agent_list")
        
        if len(agent_list) > 0 and agent_list[0].get("endpoint"):
            agent_endpoint = agent_list[0].get("endpoint")
        else:
            logger.error("No agent endpoint found")
            return

        # Initialize the client
        httpx_client = httpx.AsyncClient(timeout=60)
        client = A2AClient(httpx_client=httpx_client, url=agent_endpoint)

        # Get task_id safely, handling SimulatorRequestContext which might not have _task_id
        task_id = getattr(context, "task_id", None)
        if task_id:
            logger.info(f"Executing request: {task_id}")
        else:
            task_row = getattr(context, "row", {})
            task_id = task_row.get("id", "unknown")
            logger.info(f"Executing request: {task_id}")
        
        # Extract message from the context
        message = context.message
        if not message:
            await self._publish_error(context, event_queue, "No message content provided")
            return
            
        # Generate a fresh job_id for this run (must not equal todo_id)
        run_job_id = str(uuid.uuid4())
        try:
            # Send intermediate result that we're starting
            await self._publish_progress(
                context, 
                event_queue, 
                "Forwarding request to target agent",
                1,  # Current step
                2,  # Total steps (1 for forwarding, 1 for receiving)
                job_id=run_job_id,
            )
            
            # Forward the message to the target agent
            from a2a.types import (
                SendMessageRequest, 
                MessageSendParams, 
                MessageSendConfiguration,
                Message,
                TextPart,
                Role,
                Part
            )
            # Create a Message object from the string message
            a2a_message = Message(
                message_id=str(uuid.uuid4()),
                parts=[
                    Part(root=TextPart(
                        text=message,
                        kind="text"
                    ))
                ],
                role=Role.user
            )
            
            # Create a SendMessageRequest (non-streaming)
            request = SendMessageRequest(
                id=str(uuid.uuid4()),
                params=MessageSendParams(
                    message=a2a_message,
                    configuration=MessageSendConfiguration(
                        acceptedOutputModes=["text"],  # Accept text output
                        blocking=True,  # Blocking request
                    )
                )
            )
            
            # Send the request - this returns a response directly
            update_count = 0
            final_response = None
            
            try:
                # Send the request and get the response
                response = await client.send_message(request)
                update_count = 1
                
                # Forward progress update
                await self._publish_progress(
                    context,
                    event_queue,
                    f"Received response from target agent",
                    2,  # Current step
                    2,  # Total steps
                    job_id=run_job_id,
                )
                
                # Store the response
                final_response = response
                
            except httpx.ConnectError as e:
                logger.error(f"Connection error: {e}. Is the A2A agent running at {agent_endpoint}?")
                # Create a mock response for simulation purposes
                await self._publish_progress(
                    context,
                    event_queue,
                    f"Failed to connect to A2A agent at {agent_endpoint}. This is expected in simulation mode if no agent is running.",
                    2,  # Current step
                    2,  # Total steps
                    job_id=run_job_id,
                )
            except Exception as e:
                logger.exception(f"Error processing response: {e}")
                # Don't re-raise, just log the error and continue with a mock response
            
            # Create the final task with the response from the target agent
            # If we didn't get any updates, create a default final task
            if final_response is None:
                # Get task_id safely, handling SimulatorRequestContext
                task_id = getattr(context, "task_id", None)
                if not task_id:
                    task_row = getattr(context, "row", {})
                    task_id = task_row.get("id", str(uuid.uuid4()))
                
                final_task = Task(
                    id=task_id,
                    contextId=str(uuid.uuid4()),
                    status=TaskStatus(state=TaskState.completed),
                    output={
                        "message": f"Simulation completed without actual A2A agent response from {agent_endpoint}",
                        "simulation_note": "This is a simulated response since no A2A agent was running at the specified endpoint.",
                        "original_query": message
                    }
                )
            else:
                # Get task_id safely, handling SimulatorRequestContext
                task_id = getattr(context, "task_id", None)
                if not task_id:
                    task_row = getattr(context, "row", {})
                    task_id = task_row.get("id", str(uuid.uuid4()))
                
                root = final_response.root
                if root and root.result:
                    final_task = root.result
            
            if hasattr(event_queue, 'enqueue_event'):
                try:
                    # Resolve contextId from final_task or context row
                    context_id = None
                    try:
                        if final_task is not None:
                            context_id = getattr(final_task, 'contextId', None) or getattr(final_task, 'context_id', None)
                    except Exception:
                        context_id = None
                    if not context_id:
                        try:
                            context_data: Dict[str, Any] = getattr(context, "get_context_data", lambda: {})() or {}
                            row = (context_data.get("row") or {})
                            context_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
                        except Exception:
                            context_id = None
                    if not context_id:
                        context_id = str(uuid.uuid4())

                    # Extract state and last agent text from response
                    task_state = None
                    last_agent_text = None
                    try:
                        if final_task is not None and getattr(final_task, 'status', None) is not None:
                            task_state = final_task.status.state
                            status_msg = getattr(final_task.status, 'message', None)
                            if status_msg and getattr(status_msg, 'parts', None):
                                texts: List[str] = []
                                for p in status_msg.parts:
                                    try:
                                        root = getattr(p, 'root', None)
                                        if isinstance(root, TextPart) and getattr(root, 'text', None):
                                            texts.append(root.text)
                                    except Exception:
                                        continue
                                if texts:
                                    last_agent_text = "".join(texts)
                        if not last_agent_text and final_task is not None and getattr(final_task, 'history', None):
                            for m in reversed(final_task.history):
                                try:
                                    if getattr(m, 'role', None) == Role.agent and getattr(m, 'parts', None):
                                        texts: List[str] = []
                                        for p in m.parts:
                                            root = getattr(p, 'root', None)
                                            if isinstance(root, TextPart) and getattr(root, 'text', None):
                                                texts.append(root.text)
                                        if texts:
                                            last_agent_text = "".join(texts)
                                            break
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    if not task_state:
                        task_state = TaskState.completed
                    
                    
                    history_compact: List[Dict[str, Any]] = []
                    try:
                        if final_task is not None and getattr(final_task, 'history', None):
                            for m in final_task.history:
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

                    event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            status=TaskStatus(
                                state=TaskState.completed,
                                message=self._to_a2a_message({
                                    "result": str(result_text or "")
                                }),
                            ),
                            final=True,
                            contextId=context_id,
                            taskId=task_id,
                            metadata={
                                "crew_type": "task",
                                "event_type": "task_completed",
                                "job_id": run_job_id,
                            },
                        )
                    )

                    artifact_payload: Dict[str, Any] = {
                        'last_agent_message': last_agent_text,
                        'task_state': getattr(task_state, 'value', str(task_state)),
                        'context_id': context_id,
                        'task_id': task_id,
                        'history_compact': history_compact,
                    }

                    artifact = new_text_artifact(
                        name="current_result",
                        description="Result of request to agent (structured).",
                        text=json.dumps(artifact_payload, ensure_ascii=False),
                    )
                    event_queue.enqueue_event(
                        TaskArtifactUpdateEvent(
                            artifact=artifact,
                            lastChunk=True,
                            contextId=context_id,
                            taskId=task_id,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to enqueue final events: {e}")
            else:
                # Fallback to original behavior for non-DB queues
                if hasattr(event_queue, 'publish_task'):
                    await event_queue.publish_task(final_task)
                else:
                    try:
                        print(f"Enqueuing final task: {final_task}")
                        event_queue.enqueue_event(final_task)
                    except Exception as e:
                        logger.warning(f"Failed to enqueue event to simulator: {e}")
                        print(f"Failed to enqueue event to simulator: {e}")
            
            # Get task_id safely for logging
            task_id = getattr(context, "task_id", None)
            if not task_id:
                task_row = getattr(context, "row", {})
                task_id = task_row.get("id", str(uuid.uuid4()))
            
            logger.info(f"Request {task_id} completed with {update_count} updates")
            
        except Exception as e:
            logger.exception(f"Error executing request: {e}")
            # Try to emit a failure status with a job_id as well
            await self._publish_error(context, event_queue, f"Error executing request: {str(e)}", job_id=self._generate_job_id(context=context))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Request the agent to cancel an ongoing task.
        
        Args:
            context: The request context containing the task ID to cancel.
            event_queue: The queue to publish the cancellation status update to.
        """
        logger.info(f"Cancelling task: {context.task_id}")
        
        # Create a cancelled task
        task = Task(
            id=context.task_id,
            contextId=str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.canceled),
            output={"message": "Task cancelled"}
        )
        
        # Publish the cancelled task
        # Handle both standard EventQueue and SimulatorEventQueue
        if hasattr(event_queue, 'publish_task'):
            await event_queue.publish_task(task)
        else:
            try:
                # For SimulatorEventQueue, use enqueue_event with typed event
                print(f"Enqueuing cancelled task: {task}")
                event_queue.enqueue_event(task)
            except Exception as e:
                logger.warning(f"Failed to enqueue event to simulator: {e}")
                print(f"Failed to enqueue event to simulator: {e}")

    async def _publish_error(self, context: RequestContext, event_queue: EventQueue, error_message: str, *, job_id: Optional[str] = None) -> None:
        """
        Publish an error task.
        
        Args:
            context: The request context.
            event_queue: The queue to publish the error to.
            error_message: The error message.
        """
        # Get task_id safely, handling SimulatorRequestContext
        task_id = getattr(context, "task_id", None)
        if not task_id:
            task_row = getattr(context, "row", {})
            task_id = task_row.get("id", str(uuid.uuid4()))

        # Prefer DB event path
        if hasattr(event_queue, 'enqueue_event'):
            try:
                event_queue.enqueue_event(TaskStatusUpdateEvent(
                    status=TaskStatus(state=TaskState.failed),
                    final=True,
                    contextId=str(uuid.uuid4()),
                    taskId=task_id,
                    metadata={
                        'crew_type': 'task',
                        'event_type': 'task_completed',
                        'status': 'failed',
                        'job_id': job_id,
                    },
                ))
            except Exception as e:
                logger.warning(f"Failed to enqueue error event: {e}")
        else:
            # Fallback for non-DB queues
            task = Task(
                id=task_id,
                contextId=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.failed),
                output={"error": error_message}
            )
            if hasattr(event_queue, 'publish_task'):
                await event_queue.publish_task(task)
            else:
                try:
                    print(f"Enqueuing error task: {task}")
                    event_queue.enqueue_event(task)
                except Exception as e:
                    logger.warning(f"Failed to enqueue event to simulator: {e}")
                    print(f"Failed to enqueue event to simulator: {e}")

    async def _publish_progress(
        self, 
        context: RequestContext, 
        event_queue: EventQueue, 
        message: str,
        current_step: int,
        total_steps: int,
        *,
        job_id: Optional[str] = None
    ) -> None:
        """
        Publish a progress update.
        
        Args:
            context: The request context.
            event_queue: The queue to publish the progress to.
            message: The progress message.
            current_step: The current step number.
            total_steps: The total number of steps.
        """
        progress_percentage = int((current_step / total_steps) * 100)
        
        # Get task_id safely, handling SimulatorRequestContext
        task_id = getattr(context, "task_id", None)
        if not task_id:
            task_row = getattr(context, "row", {})
            task_id = task_row.get("id", str(uuid.uuid4()))
        
        # Ensure job_id present
        if not job_id:
            job_id = self._generate_job_id(context=context)

        # Prefer DB event path
        if hasattr(event_queue, 'enqueue_event'):
            try:
                event_queue.enqueue_event(TaskStatusUpdateEvent(
                    status=TaskStatus(
                        state=TaskState.working,
                        message=self._to_a2a_message(self._build_progress_data(context=context)),
                    ),
                    final=False,
                    contextId=str(uuid.uuid4()),
                    taskId=task_id,
                    metadata={
                        'crew_type': 'task',
                        'event_type': ('task_started' if current_step == 1 else ('tool_usage_finished' if current_step >= total_steps else 'tool_usage_started')),
                        'total_steps': total_steps,
                        'job_id': job_id,
                    },
                ))
            except Exception as e:
                logger.warning(f"Failed to enqueue progress event: {e}")
        else:
            # Fallback for non-DB queues
            task = Task(
                id=task_id,
                contextId=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.working),
                output={
                    'message': message,
                    'progress': progress_percentage,
                    'step': current_step,
                    'total_steps': total_steps,
                }
            )
            if hasattr(event_queue, 'publish_task_update'):
                await event_queue.publish_task_update(task)
            else:
                try:
                    print(f"Enqueuing task: {task}")
                    event_queue.enqueue_event(task)
                except Exception as e:
                    logger.warning(f"Failed to enqueue event to simulator: {e}")
                    print(f"Failed to enqueue event to simulator: {e}")



# Job ID helper(s)
    def _build_progress_data(self, context: RequestContext) -> Dict[str, Any]:
        """진행 중 메타데이터 data 구성 (goal + agent profile)."""
        try:
            context_data: Dict[str, Any] = getattr(context, "get_context_data", lambda: {})() or {}
            extras = context_data.get("extras", {}) or {}
            agents = extras.get("agents", []) or []
            agent = agents[0] if isinstance(agents, list) and agents else {}
            return {
                "goal": agent.get("goal") or extras.get("activity_name") or "",
                "name": agent.get("name") or agent.get("username") or "",
                "role": agent.get("role") or "",
                "agent_profile": agent.get("profile") or agent.get("agent_profile") or "/images/chat-icon.png",
            }
        except Exception:
            return {
                "goal": "",
                "name": "",
                "role": "",
                "agent_profile": "/images/chat-icon.png",
            }

    def _to_a2a_message(self, payload: Dict[str, Any]) -> Message:
        """딕셔너리 payload를 A2A Message로 래핑하여 TaskStatus.message 스키마를 만족시킴."""
        return Message(
            message_id=str(uuid.uuid4()),
            parts=[
                Part(root=TextPart(
                    text=self._safe_json(payload),
                    kind="text"
                ))
            ],
            role=Role.agent
        )

    def _safe_json(self, value: Dict[str, Any]) -> str:
        try:
            import json
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    def _to_data_artifact(self, payload: Dict[str, Any]) -> Artifact:
        """딕셔너리 payload를 A2A Artifact(DataPart)로 변환."""
        return Artifact(
            artifact_id=str(uuid.uuid4()),
            name="result",
            description="Final structured result",
            parts=[
                Part(root=DataPart(data=payload))
            ],
        )
    def _generate_job_id(self, context: Optional[RequestContext] = None, event_obj: Any = None, source: Any = None) -> str:
        """이벤트/컨텍스트에서 job_id를 생성.
        우선순위: event_obj.task.id > source.task.id > context.task_id/row.id > 무작위 UUID
        """
        try:
            if event_obj is not None and hasattr(event_obj, 'task') and hasattr(event_obj.task, 'id'):
                return str(event_obj.task.id)
            if source is not None and hasattr(source, 'task') and hasattr(source.task, 'id'):
                return str(source.task.id)
            if context is not None:
                task_id = getattr(context, 'task_id', None)
                if not task_id:
                    task_row = getattr(context, 'row', {})
                    task_id = task_row.get('id')
                if task_id:
                    return str(task_id)
        except Exception:
            pass
        return str(uuid.uuid4())
