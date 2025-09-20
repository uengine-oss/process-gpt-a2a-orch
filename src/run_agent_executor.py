#!/usr/bin/env python3
import asyncio
import logging
import sys
from typing import List

from a2a_agent_executor.executor import A2AAgentExecutor
from a2a_agent_executor.server import A2AAgentServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("a2a_agent_executor")

async def main():
    """Main function to run the A2A agent executor."""
    # Create the A2A agent executor with localhost:1002 endpoint
    executor = A2AAgentExecutor(
        agent_endpoint="http://localhost:1002",
        name="ProcessGPTOrchestrator",
        description="A2A Agent Executor that forwards requests to ProcessGPT agent",
        version="0.1.0"
    )
    
    # Create the server with the executor
    server = A2AAgentServer(
        agent_executors=[executor],
        host="0.0.0.0",
        port=8000
    )
    
    try:
        # Start the server
        logger.info("Starting A2A Agent Executor server...")
        await server.start()
    except KeyboardInterrupt:
        logger.info("Server interrupted by user")
    except Exception as e:
        logger.exception(f"Error running server: {e}")
    finally:
        # Stop the server
        if hasattr(server, "stop"):
            await server.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Error during execution: {e}")
