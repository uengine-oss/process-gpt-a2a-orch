# A2A Agent Executor

A proxy component that forwards requests to an A2A agent and shows intermediate results. This component follows the Agent2Agent (A2A) Protocol.

## Features

- Acts as an A2A server that can be called by clients
- Internally forwards requests to another A2A agent using A2A client functionality
- Provides intermediate results during execution
- Follows the Agent2Agent (A2A) Protocol specification
- Supports ProcessGPTAgentSimulator for testing

## Installation

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) package manager

### Install with uv

```bash
# Install from source
git clone https://github.com/uengine-oss/process-gpt-a2a-orch.git
cd process-gpt-a2a-orch
uv pip install -e .

# Or use the provided script
./run_simulator.sh
```

## Usage

### Command Line

```bash
# Start the agent executor with default settings
a2a-agent-executor --target-agent http://localhost:10002 --port 8000

# Show help
a2a-agent-executor --help
```

### Python API

```python
import asyncio
from a2a_agent_executor import A2AAgentExecutor, A2AAgentServer

async def main():
    # Create the executor with a single target agent endpoint
    agent_executor = A2AAgentExecutor(
        agent_endpoint="http://localhost:10002",
        name="AgentProxy",
        description="Proxies requests to a target A2A agent"
    )
    
    # Create the server
    server = A2AAgentServer(
        agent_executors=[agent_executor],
        host="0.0.0.0",
        port=8000
    )
    
    # Start the server
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())
```

### Message Format

The A2AAgentExecutor forwards any message format to the target agent. You can send any valid A2A message:

```json
{
  "query": "What is the weather today?",
  "parameters": {
    "location": "New York",
    "units": "metric"
  }
}
```

## Testing with ProcessGPTAgentSimulator

The package now supports testing with ProcessGPTAgentSimulator:

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from process_gpt_agent_sdk.simulator import ProcessGPTAgentSimulator

# Create a custom executor
class CustomAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 사용자 정의 로직 구현
        message = context.message
        
        # 중간 결과 발행
        await self._publish_progress(
            context,
            event_queue,
            "Processing request...",
            1,
            2
        )
        
        # 비즈니스 로직 수행...
        
        # 최종 결과 발행
        final_task = Task(
            id=context.task_id,
            status=TaskStatus.completed,
            output={"result": "Success"}
        )
        await event_queue.publish_task(final_task)
        
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 취소 로직 구현
        pass

# 시뮬레이터 사용
executor = CustomAgentExecutor()
simulator = ProcessGPTAgentSimulator(executor=executor)
result = await simulator.run_simulation("테스트 프롬프트")
```

## Running the Simulator

To run the simulator with uv:

```bash
# Run the simulator script
./run_simulator.sh
```

This will:
1. Create a virtual environment using uv
2. Install the package and dependencies
3. Run the simulation with test prompts

## License

Apache 2.0
