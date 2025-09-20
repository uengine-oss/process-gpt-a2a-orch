#!/bin/bash

# Exit on error
set -e

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Please install it first."
    echo "Visit https://github.com/astral-sh/uv for installation instructions."
    exit 1
fi

# Create a virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment with uv..."
    uv venv .venv
fi

# Activate the virtual environment
source .venv/bin/activate

# Install the package and dependencies
echo "Installing package and dependencies with uv..."
uv pip install -e .

# Run the simulation
echo "Running the simulation..."
python src/simulation.py
