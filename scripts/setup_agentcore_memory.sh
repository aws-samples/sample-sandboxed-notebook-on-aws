#!/usr/bin/env bash
# ============================================================
# Amazon Bedrock AgentCore Memory — provisioning (STANDALONE)
#
# Creates (or reuses) a short-term AgentCore Memory resource for the chat agent's
# conversation history, then prints the memoryId + the exact config.sh lines to
# enable it.
#
# DELIBERATELY NOT wired into local_install.sh: AgentCore Memory is a billable,
# managed cloud resource and the local default is MEMORY_BACKEND=sqlite. Run this
# ONCE by hand when you want to switch the agent to AgentCore Memory:
#
#     bash scripts/setup_agentcore_memory.sh
#
# then set in scripts/config.sh (the script prints these):
#     export MEMORY_BACKEND="agentcore"
#     export AGENTCORE_MEMORY_ID="<printed id>"
# and restart the stack. Flip MEMORY_BACKEND back to "sqlite" to go local again.
#
# Notes:
#   • Short-term memory only (no long-term semantic strategies) — that's all the
#     chat SessionManager needs (conversational event store + retrieve).
#   • event_expiry bounds storage/cost. The runtime does NOT garbage-collect events.
#   • Idempotent: create-or-get by name. Safe to re-run.
#   • To remove it later: use MemoryClient.delete_memory(memory_id) or the console.
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/config.sh"

# Tunables (override via env when running):
MEMORY_NAME="${AGENTCORE_MEMORY_NAME:-microvm_notebook_chat_memory}"
MEMORY_EXPIRY_DAYS="${AGENTCORE_MEMORY_EXPIRY_DAYS:-7}"

PYTHON="${PYTHON:-python3}"

echo "============================================"
echo "  AgentCore Memory — Provisioning"
echo "============================================"
echo "  Region:      $BEDROCK_REGION"
echo "  Name:        $MEMORY_NAME"
echo "  Event expiry: ${MEMORY_EXPIRY_DAYS} day(s)"
echo "  Strategies:  (none — short-term memory only)"
echo ""

AGENTCORE_MEMORY_NAME="$MEMORY_NAME" \
AGENTCORE_MEMORY_EXPIRY_DAYS="$MEMORY_EXPIRY_DAYS" \
BEDROCK_REGION="$BEDROCK_REGION" \
  "$PYTHON" "$ROOT_DIR/scripts/setup_agentcore_memory.py"
