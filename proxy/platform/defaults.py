"""
Product defaults shared across the proxy. Leaf module: imports only the stdlib,
so any proxy module can import it without a cycle.

DEFAULT_MEMORY_MIB is the memory tier used when a request, a DB row or an
in-memory record does not carry one (launch without memoryMiB, cost records,
log-group resolution, pricing). scripts/config.sh owns the value and exports it
(default 2048, validated against the image size tiers); local_install.sh passes
it to the proxy, which reads it once here at import. The proxy also forwards it
to each VM in launch_env_vars() so the VM helpers use the same tier.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

# Used only when DEFAULT_MEMORY_MIB is unset or invalid (proxy started without
# scripts/config.sh). Matches the config.sh default.
_FALLBACK_MEMORY_MIB = 2048


def _read_default_memory_mib() -> int:
    raw = os.environ.get("DEFAULT_MEMORY_MIB", "").strip()
    if not raw:
        return _FALLBACK_MEMORY_MIB
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(f"DEFAULT_MEMORY_MIB={raw!r} is not a positive integer; "
                       f"using {_FALLBACK_MEMORY_MIB}")
        return _FALLBACK_MEMORY_MIB
    return value


DEFAULT_MEMORY_MIB = _read_default_memory_mib()


# MicroVM image names are "{base}-{arch}-{memory_mib}", e.g.
# notebook-microvm-x86_64-1024 (the ARN's last ':'-segment).
_IMAGE_ARCH_SIZE_RE = re.compile(r"-(arm64|x86_64)-(\d+)$")


def arch_size_from_image(image_arn) -> tuple:
    """(architecture, memory_mib) encoded in a MicroVM image ARN or name, or
    (None, None) when the name does not follow {base}-{arch}-{mem}. The image a
    VM runs is the ground truth for its arch and tier, so callers prefer this
    over a default whenever a VM record lacks them."""
    if not image_arn or not isinstance(image_arn, str):
        return None, None
    m = _IMAGE_ARCH_SIZE_RE.search(image_arn.rsplit(":", 1)[-1])
    if not m:
        return None, None
    return m.group(1), int(m.group(2))
