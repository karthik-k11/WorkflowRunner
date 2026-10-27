# WorkflowRunner

WorkflowRunner is a local workflow automation engine built with Python and Flask.

It is designed to help developers define workflows, manage task dependencies, execute tasks in the correct order, monitor execution status, and inspect execution history.

## Technology Stack

- Python 3.12
- Flask
- SQLite
- HTML, CSS, and JavaScript
- pytest
- Git and GitHub

## Project Status

Day 1: Initial project setup.

Core workflow functionality has not yet been implemented.

## Planned Development Phases

### Basic
- Workflow and task management
- Dependency validation
- Dependency-aware execution
- Execution status tracking
- SQLite execution history
- Dashboard and automated tests

### Intermediate
- Conditional tasks
- Retry policies
- Reusable workflow templates
- Improved execution monitoring

### Advanced
- Scheduled workflows
- Workflow dependency graphs
- Controlled parallel execution
- Execution recovery and reporting

## Local Development Setup

Create a virtual environment:

    py -3.12 -m venv .venv

Activate it in Windows PowerShell:

    .\.venv\Scripts\Activate.ps1

Install dependencies:

    python -m pip install -r requirements.txt

Run tests:

    python -m pytest

## Security

WorkflowRunner is intended for local development. Task execution will use validated configurations, restricted file operations, subprocess timeouts, and safe command execution practices.

## License

Not yet specified.
