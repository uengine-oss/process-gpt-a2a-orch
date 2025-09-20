#!/bin/bash

# Exit on error
set -e

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Please run setup first."
    exit 1
fi

# Activate the virtual environment
source venv/bin/activate

# Install the package in development mode if needed
if [ "$1" == "--install" ]; then
    echo "Installing package in development mode..."
    pip install -e .
fi

# Run the agent executor
echo "Starting A2A Agent Executor with localhost:1002 endpoint..."
python src/run_agent_executor.py
