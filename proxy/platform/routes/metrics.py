"""
Image tier routes and the image-version lookup used by the VM detail card.
Part of: proxy.platform (Smart MicroVM Service layer)
Endpoints:
  GET /image-tiers                     - Available MicroVM memory/vCPU tiers
"""

import copy
import time
import asyncio
import logging

from fastapi import APIRouter, Request

from proxy.platform.microvm_manager import (
    IMAGE_ARN, IMAGE_BASE_NAME, DEFAULT_IMAGE_ARCH, IMAGE_ARCHES,
)
from proxy.platform.defaults import DEFAULT_MEMORY_MIB

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


def _tier(mem: int, arch: str, name: str) -> dict:
    vcpu = mem / 2048
    return {
        "memory_mib": mem,
        "memory_gb": mem / 1024,
        "vcpu": vcpu,
        "arch": arch,
        "label": f"\U0001f9e0 {mem / 1024:.1f} GB \u00b7 \u26a1 {vcpu} vCPU",
        "image_name": name,
    }


# /image-tiers success-only cache: the image list changes only when images are
# (re)built, so a successful computed result is served for this long. Failures and
# the IMAGE_ARN-unset path are never cached.
_IMAGE_TIERS_CACHE_SECONDS = 300
_IMAGE_TIERS_CACHE: dict = {"value": None, "ts": 0.0}


@router.get("/image-tiers")
async def list_image_tiers(request: Request):
    """Discover available MicroVM image tiers, grouped by CPU architecture.

    Images are named {IMAGE_BASE_NAME}-{arch}-{mem}. The UI uses this to drive the
    arch selector (only arches that actually have built images appear) and the
    size dropdown (per selected arch). Only arches in IMAGE_ARCHES and sizes in
    that arch's MICROVM_SIZES_JSON list are offered.

    Returns:
      arches:       [arch, ...] in IMAGE_ARCHES with ≥1 CREATED image (real availability)
      defaultArch:  the arch to pre-select (config default if built, else first)
      tiersByArch:  {arch: [tier, ...]} sorted by memory
      tiers:        flat list for the default arch (the launch dialog's fallback
                    when tiersByArch is missing)
      default_memory_mib: the tier to pre-select (DEFAULT_MEMORY_MIB, config.sh)
    """
    empty = {"arches": [], "defaultArch": DEFAULT_IMAGE_ARCH, "tiersByArch": {}, "tiers": [],
             "default_memory_mib": DEFAULT_MEMORY_MIB}
    if not IMAGE_ARN:
        return empty

    cached = _IMAGE_TIERS_CACHE["value"]
    if cached is not None and time.monotonic() - _IMAGE_TIERS_CACHE["ts"] < _IMAGE_TIERS_CACHE_SECONDS:
        return copy.deepcopy(cached)

    try:
        vm_manager = request.app.state.vm_manager
        # POLL client: the image-tier list is a low-frequency poll, not
        # the latency-sensitive launch path.
        client = vm_manager.get_lambda_poll_client()

        # Blocking control-plane SDK call → worker thread.
        response = await asyncio.to_thread(client.list_microvm_images)
        # Lazy import: the per-arch size table lives with the launch route, and
        # routes/microvm.py imports latest_image_versions from this module (also
        # lazily, in get_instance_detail), so neither side imports at load time.
        from proxy.platform.routes import microvm as microvm_routes
        sizes_by_arch = microvm_routes._SIZES_BY_ARCH or {}
        tiers_by_arch: dict[str, list] = {}
        for img in response.get("items", []):
            name = img.get("name", "")
            state = img.get("state", "")
            if state not in ("CREATED", "UPDATED"):
                continue
            if not name.startswith(f"{IMAGE_BASE_NAME}-"):
                continue
            # {IMAGE_BASE_NAME}-{arch}-{mem} → split off the trailing "-{mem}",
            # then the "-{arch}" before it. arch may itself contain no dash.
            suffix = name[len(IMAGE_BASE_NAME) + 1:]  # "{arch}-{mem}"
            parts = suffix.rsplit("-", 1)
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            arch, mem = parts[0], int(parts[1])
            # Offer only what this proxy is configured to launch: an arch not in
            # IMAGE_ARCHES (e.g. x86_64 dropped by the config.sh region guard,
            # or opted out) and a size not in that arch's MICROVM_SIZES_JSON
            # list are hidden even if a matching image exists.
            if arch not in IMAGE_ARCHES:
                continue
            allowed = sizes_by_arch.get(arch)
            if allowed and mem not in allowed:
                continue
            tiers_by_arch.setdefault(arch, []).append(_tier(mem, arch, name))

        for arch in tiers_by_arch:
            tiers_by_arch[arch].sort(key=lambda t: t["memory_mib"])

        # Arches with real availability, ordered to prefer the configured default.
        arches = sorted(tiers_by_arch.keys(),
                        key=lambda a: (a != DEFAULT_IMAGE_ARCH, a))
        default_arch = DEFAULT_IMAGE_ARCH if DEFAULT_IMAGE_ARCH in tiers_by_arch else (
            arches[0] if arches else DEFAULT_IMAGE_ARCH)

        result = {
            "arches": arches,
            "defaultArch": default_arch,
            "tiersByArch": tiers_by_arch,
            "tiers": tiers_by_arch.get(default_arch, []),
            "default_memory_mib": DEFAULT_MEMORY_MIB,
        }
        # Cache only this SUCCESSFUL computed result (a private deep copy).
        _IMAGE_TIERS_CACHE["value"] = copy.deepcopy(result)
        _IMAGE_TIERS_CACHE["ts"] = time.monotonic()
        return result
    except Exception as e:
        logger.warning(f"Failed to list image tiers: {e}")
        return empty


# Latest ACTIVE version per image (VM card "older image" note). Success-only
# cache, separate from _IMAGE_TIERS_CACHE so /image-tiers is unchanged.
_IMAGE_VERSIONS_CACHE_SECONDS = 300
_IMAGE_VERSIONS_CACHE: dict = {"value": None, "ts": 0.0}


async def latest_image_versions(vm_manager) -> dict:
    """{image_arn: version, image_name: version} from list_microvm_images
    (latestActiveImageVersion). Read-only control-plane call on the poll client,
    off the loop, cached 300 s on success. Any failure returns {} (uncached)."""
    cached = _IMAGE_VERSIONS_CACHE["value"]
    if cached is not None and time.monotonic() - _IMAGE_VERSIONS_CACHE["ts"] < _IMAGE_VERSIONS_CACHE_SECONDS:
        return dict(cached)
    try:
        client = vm_manager.get_lambda_poll_client()
        response = await asyncio.to_thread(client.list_microvm_images)
        result: dict = {}
        for img in response.get("items", []) or []:
            version = img.get("latestActiveImageVersion")
            if version is None or version == "":
                continue
            version = str(version)
            if img.get("imageArn"):
                result[img["imageArn"]] = version
            if img.get("name"):
                result[img["name"]] = version
        _IMAGE_VERSIONS_CACHE["value"] = dict(result)
        _IMAGE_VERSIONS_CACHE["ts"] = time.monotonic()
        return result
    except Exception as e:
        logger.debug(f"latest_image_versions unavailable: {type(e).__name__}")
        return {}
