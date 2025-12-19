# A2A Agent Executor

A proxy component that forwards requests to an A2A agent and shows intermediate results. This component follows the Agent2Agent (A2A) Protocol and integrates with ProcessGPT Agent SDK for simulation and testing.

## Features

- **A2A Protocol Compliance**: Acts as an A2A server that can be called by clients
- **Request Forwarding**: Internally forwards requests to another A2A agent using A2A client functionality
- **Real-time Progress**: Provides intermediate results and progress updates during execution
- **ProcessGPT Integration**: Supports ProcessGPTAgentSimulator for comprehensive testing
- **Flexible Configuration**: Supports dynamic agent endpoint configuration through context
- **Error Handling**: Robust error handling with graceful fallbacks for simulation mode
- **Docker Support**: Containerized deployment with Docker

## Webhook Receiver (separate Pod)

When the target A2A agent supports `push_notifications=True`, this project uses non-blocking requests and receives results via webhook callbacks.

- **Executor Pod** (`a2a_agent_executor`): sends the A2A request with `push_notification_config.url` and records a first event (`event_type=webhook_accepted`).
- **Receiver Pod** (`a2a_agent_webhook_receiver`): exposes `POST /webhook/a2a/todolist/{todolist_id}` and records completion/failure events + artifact when callbacks arrive.

Important: In non-blocking mode, terminal callbacks (`completed`) may not arrive depending on the upstream A2A server/SDK behavior. This design ensures the first “accepted/submitted” event is always recorded, and completion is recorded only if callback arrives.

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

### ProcessGPT Server Mode

The primary usage is as a ProcessGPT Agent Server that integrates with Supabase:

```python
import asyncio
import os
from processgpt_agent_sdk import ProcessGPTAgentServer
from a2a_agent_executor import A2AAgentExecutor
from dotenv import load_dotenv

async def main():
    load_dotenv(override=True)
    
    # Ensure Supabase configuration
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_KEY"):
        print("Error: SUPABASE_URL and SUPABASE_KEY environment variables are required.")
        return

    executor = A2AAgentExecutor()
    
    server = ProcessGPTAgentServer(
        agent_executor=executor,
        agent_type="a2a"
    )
    
    await server.run()

if __name__ == "__main__":
    asyncio.run(main())
```

### Docker Deployment

```bash
# Build the Docker image
docker build -t a2a-agent-executor .

# Run with environment variables
docker run -p 8000:8000 \
  -e SUPABASE_URL=your_supabase_url \
  -e SUPABASE_KEY=your_supabase_key \
  a2a-agent-executor
```

### Agent Configuration

The executor dynamically configures target agents through the request context. Agent endpoints are specified in the context data:

```python
# Agent configuration in context
context_data = {
    "extras": {
        "agents": [
            {
                "endpoint": "http://localhost:10002",
                "name": "WeatherAgent",
                "goal": "Provide weather information",
                "role": "weather_assistant"
            }
        ]
    }
}
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

The package supports comprehensive testing with ProcessGPTAgentSimulator:

```python
import asyncio
import logging
from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator
from a2a_agent_executor import A2AAgentExecutor

# Configure logging
logging.basicConfig(level=logging.INFO)

async def main():
    # Create the A2A agent executor
    executor = A2AAgentExecutor()
    
    # Create the simulator
    simulator = ProcessGPTAgentSimulator(
        executor=executor,
        agent_orch="a2a"
    )
    
    # Run simulation with test prompts
    await simulator.run_simulation(
        prompt="인천에 괜찮은 숙소를 알려줘",
        activity_name="report_generation",
        user_id="user123",
        tenant_id="tenant456"
    )

if __name__ == "__main__":
    asyncio.run(main())
```

### Simulation Features

- **Mock Responses**: When no A2A agent is running, the executor provides simulated responses
- **Progress Tracking**: Real-time progress updates during execution
- **Error Handling**: Graceful handling of connection errors and timeouts
- **Context Management**: Proper task and context ID management for simulation

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

## Project Structure

```
process-gpt-a2a-orch/
├── src/
│   ├── a2a_agent_executor/
│   │   ├── __init__.py          # Package initialization
│   │   ├── executor.py          # Main A2A agent executor implementation
│   │   ├── server.py            # ProcessGPT server integration
│   │   ├── starlette_a2a_server.py  # Starlette-based A2A server
│   │   └── test_a2a_client.py   # A2A client testing utilities
│   └── simulation.py            # Simulation script for testing
├── Dockerfile                   # Docker container configuration
├── pyproject.toml              # Python project configuration
├── requirements.txt            # Python dependencies
├── run_simulator.sh            # Simulation runner script
└── README.md                   # This documentation
```

## Dependencies

- **a2a-sdk**: Agent2Agent protocol SDK
- **process-gpt-agent-sdk**: ProcessGPT agent integration
- **pydantic**: Data validation and settings management
- **httpx**: HTTP client for A2A communication
- **uvicorn**: ASGI server for web applications
- **starlette**: Web framework for building APIs

## Environment Variables

The following environment variables are required for ProcessGPT integration:

- `SUPABASE_URL`: Supabase project URL
- `SUPABASE_KEY`: Supabase project API key

### Executor Pod

- `WEBHOOK_PUBLIC_BASE_URL`: Public base URL of the receiver pod
  - Example: `http://a2a-webhook-receiver:9000`

### Receiver Pod

- `WEBHOOK_RECEIVER_HOST`: bind host (default `0.0.0.0`)
- `WEBHOOK_RECEIVER_PORT`: bind port (default `9000`)
- (no token validation) the receiver accepts webhook callbacks without `X-A2A-Notification-Token`

Create a `.env` file in the project root:

```bash
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_anon_key
```

## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/uengine-oss/process-gpt-a2a-orch.git
cd process-gpt-a2a-orch

# Create virtual environment
uv venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
uv pip install -e .
uv pip install -e ".[dev]"  # Install development dependencies
```

### Running Tests

```bash
# Run the simulation
python src/simulation.py

# Or use the provided script
./run_simulator.sh
```

## Running the Receiver Pod locally

After installing the package in editable mode:

```bash
a2a-webhook-receiver
```

## License

Apache 2.0
