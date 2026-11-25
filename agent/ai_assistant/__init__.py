"""
AI Chat Assistant — the interactive Strands chat agent extracted into its own
service (sibling of proxy/, pool_manager/, app/).

Runs as a standalone BedrockAgentCoreApp process on 127.0.0.1:$AI_PORT (8082 from
scripts/config.sh), exposing the AgentCore contract (POST /invocations + GET /ping). Started
locally by local_install.sh as `python -m ai_assistant.agent_runtime` and
deployable unchanged to Amazon Bedrock AgentCore Runtime.

The chat agent carries NO top-level proxy.* import so it can run standalone on the
cloud path. The only coupling is a LAZY `proxy.storage` import on the local SQLite
memory path (ai_assistant/memory.DbSessionRepository + notebook_agent.new_thread),
which resolves because local_install.sh runs the agent from the repo .venv. The
container image (ai_assistant/Dockerfile) has no proxy/ and targets
MEMORY_BACKEND=agentcore.
"""
