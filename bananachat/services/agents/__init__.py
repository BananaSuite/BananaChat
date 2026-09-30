"""Coding agents in cloud sandboxes, and agent swarms.

* ``settings`` - administrator settings (off by default) and which models may drive agents;
* ``runner`` - the HTTP client for the sandbox runner on the compute host;
* ``tools`` - tool schemas, strict argument validation and execution through the runner;
* ``loop`` - the agent loop (one background thread per task run, leases, budgets, swarms);
* ``uploads`` - files added to a task until its sandbox exists;
* ``gitfetch`` - strict parsing of public Git repository addresses and the SSRF-safe archive download;
* ``gitrepo`` - the pending import, the Git scripts run inside the sandbox (import, patch export) and the
  import record;
* ``service`` - starting, follow-ups, stopping, deleting, the workspace and periodic jobs.

The web server never runs agent code; see docs/agents.md for the threat model.
"""
