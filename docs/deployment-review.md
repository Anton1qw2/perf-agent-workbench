# Deployment review: two iterations

Date: 2026-10-18. Scope: deployment by a new developer, with an independent critic.
This is a historical review record; test counts describe that review, not the current
suite. Use [the deployment guide](deployment.md) for current setup instructions.

## Iteration 1

The critic identified a fixed API listener, Windows-only setup instructions,
missing worker verification, incomplete non-Windows encryption setup, an undeclared
Git executable prerequisite and remote HTTP examples incompatible with API tokens.
Credential tests also depended on a persistent key and an optional dependency that
were not supplied by the documented development installation on Linux/macOS.

Changes:

- Added validated `PERF_CHAT_API_HOST` and `PERF_CHAT_API_PORT` settings while retaining
  loopback defaults; documented the separate TUI destination URL.
- Added Windows and Linux/macOS installation, shared storage/configuration rules,
  API health and worker verification, and access/proxy guidance.
- Documented Git, optional extras and one-time persistent Fernet key generation.
- Replaced remote MCP HTTP examples with HTTPS.
- Added cryptography to development dependencies and updated the lock file.
- Isolated credential tests with ephemeral test keys; added a Fernet round-trip check.
- Added regression coverage for listener configuration and invalid ports/empty hosts.

## Iteration 2

The critic reviewed the changes and found no remaining major deployment blockers.
Two minor observations were addressed: use a nondefault host in the entry-point
test and include the new development dependency in the lock file.

## Verification and limits

A new Windows Python 3.12 virtual environment successfully installed the locked
requirements and editable package with dev, MCP and secrets extras. Dependency
consistency, Ruff and all eight Web regression tests passed. All 97 Python tests
passed with a project-local temporary directory. The initial default pytest temp
location was inaccessible due to Windows permissions in the review environment;
this was an environment issue, not an application test failure.

A live API and a separate worker processed a full demo conversation and run:
custom listener port, health, Web assets, task intake, clarification, resume and
synthetic result. The check used a temporary database and stopped both processes.

Linux/macOS instructions and Fernet behavior were reviewed, but those operating
systems were not executed. Private MCP/GitLab services and real model providers
were not exercised. This remains a single-user local MVP without built-in authentication.
