"""
Confirm-before-execute approval gate, implemented as a Strands hook.

This is the single enforcement point for the "ask the user before running
execute_code / install_package" behavior. It replaces the old cosmetic
PENDING_USER_APPROVAL sentinel the tools used to return.

Mechanism (verified against strands-agents 1.61.1):
  - We register a BeforeToolCallEvent callback. The event mixes in the
    _Interruptible protocol, so calling event.interrupt(name, reason=...) raises
    an InterruptException the FIRST time (halting the agent loop BEFORE the tool
    runs) and, on resume, RETURNS the stored response instead of raising.
  - event.cancel_tool (bool | str) lets us deny: when set, Strands injects an
    error tool-result and the agent continues the turn without running the tool.

Decision flow in _gate (fail-safe: anything unrecognized denies):
  1. Non-gated tool            -> pass through (tool runs).
  2. Auto-execute ON (env)     -> pass through (gate disabled entirely).
  3. Approval channel == False -> pass through (the /ai/chat/sync endpoint keeps
                                   auto-executing; absent defaults to gated).
  4. Session already trusts    -> pass through (a prior "trust" response).
     this tool
  5. Otherwise                 -> event.interrupt(...) to ask the user. The
                                   returned decision is normalized ONCE here:
                                     y/yes/a/approve -> run this one call
                                     t/trust         -> run + persist trust
                                     anything else   -> deny (cancel_tool)

Auto-decline mode (invocation_state[AUTO_DECLINE_STATE_KEY] set by
notebook_agent._auto_decline_pending when a NEW chat message arrives while an
approval is still pending):
  - _gate skips bypasses 2-4 and ALWAYS denies. This matters: the resume that
    clears the stale interrupt can arrive on the /ai/chat/sync path (approval
    channel False) or after AI_AGENT_AUTO_EXECUTE was flipped on — honoring the
    bypasses there would RUN a tool the user never approved.
  - _end_declined_turn sets AfterToolsEvent.end_turn so the declined turn ends
    with a short assistant text (the notice) and NO extra model call. History
    stays valid: the replayed toolUse gets its "User denied" toolResult, followed
    by that assistant text, before the new user message is appended.
Explicit Approve/Reject resumes never carry the flag, so they are unchanged.
"""

from strands.hooks import AfterToolsEvent, BeforeToolCallEvent, HookProvider

from .execution_tools import _auto_execute_enabled, _get_context

# Tools that require user confirmation before they run.
GATED_TOOLS = {"execute_code", "install_package"}

# invocation_state key marking an auto-decline resume pass. Value: the notice
# sentence used as the declined turn's final assistant text.
AUTO_DECLINE_STATE_KEY = "_mvmnb_auto_decline"

# Tool-result text for a denied call (explicit Reject and auto-decline alike).
DENY_MESSAGE = "User denied the request; the tool was not run."


class ExecutionApprovalHook(HookProvider):
    """Gates execute_code / install_package behind a native Strands interrupt."""

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self._gate)
        registry.add_callback(AfterToolsEvent, self._end_declined_turn)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in GATED_TOOLS:
            return  # non-gated tool: run as usual

        # Auto-decline pass: ignore every bypass and deny. event.interrupt returns
        # the "n" response chat_stream supplied for this interrupt id.
        inv = getattr(event, "invocation_state", None) or {}
        if inv.get(AUTO_DECLINE_STATE_KEY):
            tool_input = event.tool_use.get("input") or {}
            event.interrupt(f"approve:{name}", reason={"tool": name, "input": tool_input})
            event.cancel_tool = DENY_MESSAGE
            return

        if _auto_execute_enabled():
            # Env gate OFF (auto-execute ON) → bypass the gate entirely.
            return

        # Only the interactive streaming path carries an approval channel. The flag
        # rides the per-invocation execution context (set by chat_stream BEFORE
        # set_execution_context snapshots a copy). Absent defaults to True (gated)
        # so a missed set fails safe. An explicit False is the sync endpoint.
        ctx = _get_context()
        if ctx.get("_approval_channel", True) is False:
            return

        # Session trust (persisted in agent.state via the session manager). A
        # defensive .get keeps a malformed map from crashing the turn — it just
        # degrades to prompting.
        agent = event.agent
        trust = agent.state.get("exec_trust") or {}
        if isinstance(trust, dict) and trust.get(name) == "trusted":
            return

        # Ask the user. The input dict is fully assembled at hook time (the hook
        # fires after argument streaming completes), so the UI card can render the
        # exact proposed code/package from reason["input"].
        tool_input = event.tool_use.get("input") or {}
        reason = {"tool": name, "input": tool_input}
        decision = event.interrupt(f"approve:{name}", reason=reason)  # halts the loop

        # Normalize the decision in exactly ONE place (the proxy forwards it
        # verbatim). Fail-safe default is denial.
        d = str(decision).strip().lower()
        if d in ("t", "trust"):
            if not isinstance(trust, dict):
                trust = {}
            trust[name] = "trusted"
            agent.state.set("exec_trust", trust)  # persisted via the session manager
            return  # run this call, and future ones this session
        if d in ("y", "yes", "a", "approve"):
            return  # run this one call
        # Anything else = denial. The tool never runs; the agent continues the turn
        # with this message as the tool result.
        event.cancel_tool = DENY_MESSAGE

    def _end_declined_turn(self, event: AfterToolsEvent) -> None:
        """End an auto-decline pass right after the denied tool results, without
        calling the model again. No-op for every other turn."""
        inv = getattr(event, "invocation_state", None) or {}
        v = inv.get(AUTO_DECLINE_STATE_KEY)
        if v:
            event.end_turn = v if isinstance(v, str) else True
