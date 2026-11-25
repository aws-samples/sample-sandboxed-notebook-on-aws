"""modelcaps — canonical Bedrock model-capability catalog + runtime resolver.

This package is the SINGLE SOURCE of the per-model Bedrock inference-parameter
resolver and its generated capability catalog. Layout:

  - ``params.py``       — the RUNTIME resolver (pure stdlib). Public API below.
  - ``model_caps.json`` — the generated catalog data (keyed on BASE model id).
  - ``refresh.py``      — the BUILD/DEV-ONLY generator (boto3 + LiteLLM). NOT
                          imported here, so importing ``modelcaps`` never pulls in
                          boto3/third-party deps.

The runtime trio (``params.py``, ``__init__.py``, ``model_caps.json``) is vendored
into the isolated service contexts (agent/ai_assistant/, app/notebook/helpers/) by
``scripts/sync_modelcaps.sh``. The proxy imports this package directly.
"""

from .params import (
    supports_temperature,
    supports_prompt_caching,
    supports_vision,
    supports_document,
    minimal_thinking_field,
    build_inference_config,
    build_additional_request_fields,
    resolve_model_id,
    list_models,
    top_models_for_agent,
    fallback_chain,
)

__all__ = [
    "supports_temperature",
    "supports_prompt_caching",
    "supports_vision",
    "supports_document",
    "minimal_thinking_field",
    "build_inference_config",
    "build_additional_request_fields",
    "resolve_model_id",
    "list_models",
    "top_models_for_agent",
    "fallback_chain",
]
