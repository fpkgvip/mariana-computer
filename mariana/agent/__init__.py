"""Mariana agent mode — general-purpose autonomous execution.

This package sits alongside :mod:`mariana.orchestrator` (which handles the
research investigation loop) and provides a separate, simpler loop for
*computer tasks*: writing and running code, browsing the web, creating
files, analysing data, and self-testing.

Public surface:

* :mod:`mariana.agent.tools`       — HTTP clients for sandbox / browser services
* :mod:`mariana.agent.dispatcher`  — tool-name → callable mapping
* :mod:`mariana.agent.state`       — agent state machine
* :mod:`mariana.agent.loop`        — the agent event loop
* :mod:`mariana.agent.models`      — Pydantic models for tasks, steps, artifacts
"""
