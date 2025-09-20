import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from a2a.client import A2AClient
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import AgentCard, Message, Task, TaskStatus
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
        
        # Process agent endpoint
        if isinstance(agent_endpoint, str):
            self.agent_endpoint = AgentEndpoint(url=agent_endpoint)
        else:
            self.agent_endpoint = AgentEndpoint(**agent_endpoint)
        
        # Initialize the client
        self.client = A2AClient(self.agent_endpoint.url)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Execute the agent's logic for a given request context.
        
        Args:
            context: The request context containing the message, task ID, etc.
            event_queue: The queue to publish events to.
        """
        logger.info(f"Executing request: {context.task_id}")
        
        # Extract message from the context
        message = context.message
        if not message or not message.content:
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
            response = await self.client.send_message(
                agent_name="agent",  # Generic agent name
                message=message,     # Forward the original message
                stream=True          # Enable streaming for intermediate updates
            )
            
            # Process streaming updates
            update_count = 0
            async for update in response.stream():
                update_count += 1
                # Forward intermediate updates
                await self._publish_progress(
                    context,
                    event_queue,
                    f"Received update from target agent: {update}",
                    2,  # Current step
                    2   # Total steps
                )
            
            # Get final response
            final_response = await response.final()
            
            # Create the final task with the response from the target agent
            final_task = Task(
                id=context.task_id,
                status=final_response.task.status if final_response.task else TaskStatus.completed,
                output=final_response.task.output if final_response.task else {}
            )
            
            # Publish the final task
            await event_queue.publish_task(final_task)
            
            logger.info(f"Request {context.task_id} completed with {update_count} updates")
            
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
            status=TaskStatus.canceled,
            output={"message": "Task cancelled"}
        )
        
        # Publish the cancelled task
        await event_queue.publish_task(task)

    async def _publish_error(self, context: RequestContext, event_queue: EventQueue, error_message: str) -> None:
        """
        Publish an error task.
        
        Args:
            context: The request context.
            event_queue: The queue to publish the error to.
            error_message: The error message.
        """
        task = Task(
            id=context.task_id,
            status=TaskStatus.failed,
            output={"error": error_message}
        )
        
        await event_queue.publish_task(task)

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
        
        task = Task(
            id=context.task_id,
            status=TaskStatus.in_progress,
            output={
                "message": message,
                "progress": progress_percentage,
                "step": current_step,
                "total_steps": total_steps
            }
        )
        
        await event_queue.publish_task_update(task)