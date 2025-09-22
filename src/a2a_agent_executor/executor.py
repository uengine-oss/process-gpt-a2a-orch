import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx
from a2a.client import A2AClient
from a2a.client.errors import A2AClientHTTPError, A2AClientJSONError
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import AgentCard, Message, Task, TaskState, TaskStatus
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
        agent_endpoint: str | Dict[str, Any],
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

        agent_list = context_data.get("agent_list")
        agent_endpoint = agent_list[0].endpoint


        # Initialize the client
        httpx_client = httpx.AsyncClient()
        client = A2AClient(httpx_client=self.httpx_client, url=agent_endpoint)


        # Get task_id safely, handling SimulatorRequestContext which might not have _task_id
        task_id = getattr(context, "task_id", None)
        if task_id:
            logger.info(f"Executing request: {task_id}")
        else:
            # For SimulatorRequestContext, try to get task_id from prepared_data
            prepared_data = getattr(context, "_prepared_data", {})
            task_id = prepared_data.get("task_id", "unknown")
            logger.info(f"Executing request: {task_id}")
        
        # Extract message from the context
        message = context.message
        if not message:
            await self._publish_error(context, event_queue, "No message content provided")
            return
            
        try:
            # Send intermediate result that we're starting
            await self._publish_progress(
                context, 
                event_queue, 
                "Forwarding request to target agent",
                1,  # Current step
                2   # Total steps (1 for forwarding, 1 for receiving)
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
            import uuid
            
            # Create a Message object from the string message
            a2a_message = Message(
                messageId=str(uuid.uuid4()),
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
                response = await self.client.send_message(request)
                update_count = 1
                
                # Forward progress update
                await self._publish_progress(
                    context,
                    event_queue,
                    f"Received response from target agent",
                    2,  # Current step
                    2   # Total steps
                )
                
                # Store the response
                final_response = response
                
            except httpx.ConnectError as e:
                logger.error(f"Connection error: {e}. Is the A2A agent running at {self.agent_endpoint.url}?")
                # Create a mock response for simulation purposes
                await self._publish_progress(
                    context,
                    event_queue,
                    f"Failed to connect to A2A agent at {self.agent_endpoint.url}. This is expected in simulation mode if no agent is running.",
                    2,  # Current step
                    2   # Total steps
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
                    # For SimulatorRequestContext, try to get task_id from prepared_data
                    prepared_data = getattr(context, "_prepared_data", {})
                    task_id = prepared_data.get("task_id", str(uuid.uuid4()))
                
                final_task = Task(
                    id=task_id,
                    contextId=str(uuid.uuid4()),
                    status=TaskStatus(state=TaskState.completed),
                    output={
                        "message": f"Simulation completed without actual A2A agent response from {self.agent_endpoint.url}",
                        "simulation_note": "This is a simulated response since no A2A agent was running at the specified endpoint.",
                        "original_query": message
                    }
                )
            else:
                # Get task_id safely, handling SimulatorRequestContext
                task_id = getattr(context, "task_id", None)
                if not task_id:
                    # For SimulatorRequestContext, try to get task_id from prepared_data
                    prepared_data = getattr(context, "_prepared_data", {})
                    task_id = prepared_data.get("task_id", str(uuid.uuid4()))
                
                final_task = Task(
                    id=task_id,
                    contextId=final_response.task.contextId if final_response.task else str(uuid.uuid4()),
                    status=final_response.task.status if final_response.task else TaskStatus(state=TaskState.completed),
                    output=final_response.task.output if final_response.task else {}
                )
            
            # Publish the final task
            # Handle both standard EventQueue and SimulatorEventQueue
            if hasattr(event_queue, 'publish_task'):
                await event_queue.publish_task(final_task)
            else:
                # For SimulatorEventQueue, use enqueue_event
                # Convert Task to dict to make it JSON serializable
                task_dict = final_task.model_dump() if hasattr(final_task, 'model_dump') else final_task.dict()
                event_queue.enqueue_event(task_dict)
            
            # Get task_id safely for logging
            task_id = getattr(context, "task_id", None)
            if not task_id:
                prepared_data = getattr(context, "_prepared_data", {})
                task_id = prepared_data.get("task_id", "unknown")
            
            logger.info(f"Request {task_id} completed with {update_count} updates")
            
        except Exception as e:
            logger.exception(f"Error executing request: {e}")
            await self._publish_error(context, event_queue, f"Error executing request: {str(e)}")

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
            # For SimulatorEventQueue, use enqueue_event
            # Convert Task to dict to make it JSON serializable
            task_dict = task.model_dump() if hasattr(task, 'model_dump') else task.dict()
            event_queue.enqueue_event(task_dict)

    async def _publish_error(self, context: RequestContext, event_queue: EventQueue, error_message: str) -> None:
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
            # For SimulatorRequestContext, try to get task_id from prepared_data
            prepared_data = getattr(context, "_prepared_data", {})
            task_id = prepared_data.get("task_id", str(uuid.uuid4()))
            
        task = Task(
            id=task_id,
            contextId=str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.failed),
            output={"error": error_message}
        )
        
        # Handle both standard EventQueue and SimulatorEventQueue
        if hasattr(event_queue, 'publish_task'):
            await event_queue.publish_task(task)
        else:
            # For SimulatorEventQueue, use enqueue_event
            # Convert Task to dict to make it JSON serializable
            task_dict = task.model_dump() if hasattr(task, 'model_dump') else task.dict()
            event_queue.enqueue_event(task_dict)

    async def _publish_progress(
        self, 
        context: RequestContext, 
        event_queue: EventQueue, 
        message: str,
        current_step: int,
        total_steps: int
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
            # For SimulatorRequestContext, try to get task_id from prepared_data
            prepared_data = getattr(context, "_prepared_data", {})
            task_id = prepared_data.get("task_id", str(uuid.uuid4()))
        
        task = Task(
            id=task_id,
            contextId=str(uuid.uuid4()),  # Generate a context ID if not available
            status=TaskStatus(state=TaskState.working),
            output={
                "message": message,
                "progress": progress_percentage,
                "step": current_step,
                "total_steps": total_steps
            }
        )
        
        # Handle both standard EventQueue and SimulatorEventQueue
        if hasattr(event_queue, 'publish_task_update'):
            await event_queue.publish_task_update(task)
        else:
            # For SimulatorEventQueue, use enqueue_event
            # Convert Task to dict to make it JSON serializable
            task_dict = task.model_dump() if hasattr(task, 'model_dump') else task.dict()
            event_queue.enqueue_event(task_dict)  #TaskStatusUpdateEvent or ArtifactUpdateEvent 



# sample :  must be corrected as follows:

#  async for event in self.agent.stream(query, task.context_id):
#             if event['is_task_complete']:
#                 await event_queue.enqueue_event(
#                     TaskArtifactUpdateEvent(
#                         append=False,
#                         context_id=task.context_id,
#                         task_id=task.id,
#                         last_chunk=True,
#                         artifact=new_text_artifact(
#                             name='current_result',
#                             description='Result of request to agent.',
#                             text=event['content'],
#                         ),
#                     )
#                 )
#                 await event_queue.enqueue_event(
#                     TaskStatusUpdateEvent(
#                         status=TaskStatus(state=TaskState.completed),
#                         final=True,
#                         context_id=task.context_id,
#                         task_id=task.id,
#                     )
#                 )
#             elif event['require_user_input']:
#                 await event_queue.enqueue_event(
#                     TaskStatusUpdateEvent(
#                         status=TaskStatus(
#                             state=TaskState.input_required,
#                             message=new_agent_text_message(
#                                 event['content'],
#                                 task.context_id,
#                                 task.id,
#                             ),
#                         ),
#                         final=True,
#                         context_id=task.context_id,
#                         task_id=task.id,
#                     )
#                 )
#             else:
#                 await event_queue.enqueue_event(
#                     TaskStatusUpdateEvent(
#                         status=TaskStatus(
#                             state=TaskState.working,
#                             message=new_agent_text_message(
#                                 event['content'],
#                                 task.context_id,
#                                 task.id,
#                             ),
#                         ),
#                         final=False,
#                         context_id=task.context_id,
#                         task_id=task.id,
#                     )
#                 )