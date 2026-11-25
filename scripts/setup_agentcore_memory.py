"""
Create-or-get an Amazon Bedrock AgentCore Memory resource for the chat agent.

Invoked by scripts/setup_agentcore_memory.sh (standalone — NOT part of
local_install.sh). Idempotent: create_or_get_memory keys on the name, so re-running
returns the existing resource instead of creating a duplicate.

Short-term memory only (no long-term strategies) — the chat agent's Strands
AgentCoreMemorySessionManager just needs conversational event storage + retrieval.

Env (set by the .sh wrapper):
  AGENTCORE_MEMORY_NAME         resource name (default microvm_notebook_chat_memory)
  AGENTCORE_MEMORY_EXPIRY_DAYS  short-term event expiry in days (default 7)
  BEDROCK_REGION / AWS_REGION   region
"""

import os
import sys

from bedrock_agentcore.memory.client import MemoryClient

NAME = os.environ.get("AGENTCORE_MEMORY_NAME", "microvm_notebook_chat_memory")
EXPIRY_DAYS = int(os.environ.get("AGENTCORE_MEMORY_EXPIRY_DAYS", "7"))
REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2"))


def main() -> int:
    client = MemoryClient(region_name=REGION)

    print(f">> create-or-get AgentCore Memory '{NAME}' in {REGION} (expiry={EXPIRY_DAYS}d)...")
    try:
        mem = client.create_or_get_memory(
            name=NAME,
            strategies=[],  # short-term only
            description="Local dev: chat agent conversation history (Sandboxed Notebook on AWS)",
            event_expiry_days=EXPIRY_DAYS,
        )
    except Exception as e:
        print(f"\n[ERROR] Failed to create/get AgentCore Memory: {e}", file=sys.stderr)
        print("  - Confirm your AWS principal has bedrock-agentcore memory permissions "
              "(CreateMemory/GetMemory/ListEvents/CreateEvent/RetrieveMemoryRecords).", file=sys.stderr)
        print(f"  - Confirm AgentCore Memory is available in region {REGION}.", file=sys.stderr)
        return 1

    # The SDK returns the memory dict; id key has varied across versions, so probe.
    memory_id = mem.get("memoryId") or mem.get("id") or mem.get("MemoryId")
    if not memory_id:
        print(f"\n[ERROR] Created/fetched memory but couldn't find its id in: {list(mem.keys())}",
              file=sys.stderr)
        return 1

    try:
        status = client.get_memory_status(memory_id)
    except Exception:
        status = "(unknown)"

    print("\n============================================")
    print("  AgentCore Memory ready")
    print(f"  memoryId: {memory_id}")
    print(f"  status:   {status}")
    print(f"  region:   {REGION}")
    print("  Set these in scripts/config.sh to use it:")
    print('    export MEMORY_BACKEND="agentcore"')
    print(f'    export AGENTCORE_MEMORY_ID="{memory_id}"')
    print("  Then restart the stack. (Flip MEMORY_BACKEND back to \"sqlite\" for local DB.)")
    print("  To remove later: python3 -c \"from bedrock_agentcore.memory.client import "
          f"MemoryClient; MemoryClient(region_name='{REGION}').delete_memory('{memory_id}')\"")
    print("============================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
