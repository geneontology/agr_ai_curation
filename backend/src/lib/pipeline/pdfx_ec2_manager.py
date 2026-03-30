"""Optional EC2 lifecycle manager for the PDFX instance.

When PDFX_EC2_INSTANCE_ID is set, this module provides start-on-demand
and stop-when-idle behavior for the PDF extraction service's EC2 instance.

If the env var is not set, all functions are no-ops and the PDFX service
is assumed to be always running (the upstream default behavior).

Environment variables:
    PDFX_EC2_INSTANCE_ID: EC2 instance ID (e.g., i-0e82df32732b76879).
        If unset, on-demand management is disabled entirely.
    PDFX_EC2_REGION: AWS region (default: us-east-1).
    PDFX_EC2_STARTUP_TIMEOUT: Max seconds to wait for instance + service
        to become healthy (default: 300).
    PDFX_EC2_HEALTH_POLL_INTERVAL: Seconds between health checks during
        startup (default: 10).
    PDFX_EC2_IDLE_MINUTES: Minutes of idle time before auto-stop
        (default: 15). Set to 0 to disable auto-stop.
    PDF_EXTRACTION_SERVICE_URL: Used to health-check the PDFX service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_instance_id: Optional[str] = None
_region: str = "us-east-1"
_startup_timeout: int = 300
_health_poll_interval: int = 10
_idle_minutes: int = 15
_last_activity: float = 0.0


def _load_config() -> None:
    """Load configuration from environment variables (once)."""
    global _instance_id, _region, _startup_timeout, _health_poll_interval, _idle_minutes
    _instance_id = os.getenv("PDFX_EC2_INSTANCE_ID", "").strip() or None
    _region = os.getenv("PDFX_EC2_REGION", "us-east-1")
    _startup_timeout = int(os.getenv("PDFX_EC2_STARTUP_TIMEOUT", "300"))
    _health_poll_interval = int(os.getenv("PDFX_EC2_HEALTH_POLL_INTERVAL", "10"))
    _idle_minutes = int(os.getenv("PDFX_EC2_IDLE_MINUTES", "15"))


_load_config()


def is_enabled() -> bool:
    """Return True if on-demand EC2 management is configured."""
    return _instance_id is not None


def _get_ec2_client():
    """Create a boto3 EC2 client. Import boto3 lazily to avoid hard dependency."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            "boto3 is required for PDFX on-demand EC2 management. "
            "Install it with: pip install boto3"
        )
    return boto3.client("ec2", region_name=_region)


def _get_instance_state() -> str:
    """Return the current EC2 instance state (e.g., 'running', 'stopped')."""
    ec2 = _get_ec2_client()
    response = ec2.describe_instances(InstanceIds=[_instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise RuntimeError(f"PDFX EC2 instance {_instance_id} not found")
    return reservations[0]["Instances"][0]["State"]["Name"]


def _start_instance() -> None:
    """Start the EC2 instance if it is stopped."""
    ec2 = _get_ec2_client()
    logger.info("Starting PDFX EC2 instance %s", _instance_id)
    ec2.start_instances(InstanceIds=[_instance_id])


def stop_instance() -> None:
    """Stop the EC2 instance. Called by the idle watchdog."""
    if not is_enabled():
        return
    state = _get_instance_state()
    if state == "running":
        ec2 = _get_ec2_client()
        logger.info("Stopping idle PDFX EC2 instance %s", _instance_id)
        ec2.stop_instances(InstanceIds=[_instance_id])


async def _check_pdfx_health() -> bool:
    """Return True if the PDFX service health endpoint responds OK."""
    service_url = os.getenv("PDF_EXTRACTION_SERVICE_URL", "").rstrip("/")
    if not service_url:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{service_url}/api/v1/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "ok"
    except Exception:
        pass
    return False


async def ensure_running() -> None:
    """Ensure the PDFX EC2 instance is running and the service is healthy.

    If the instance is stopped, starts it and waits for the health endpoint
    to respond. If the instance is already running, just checks health.

    This is a no-op when PDFX_EC2_INSTANCE_ID is not set.
    """
    global _last_activity
    _last_activity = time.monotonic()

    if not is_enabled():
        return

    # Check if service is already healthy (fast path)
    if await _check_pdfx_health():
        logger.debug("PDFX service is healthy, no EC2 action needed")
        return

    # Service not healthy — check instance state
    state = _get_instance_state()
    logger.info("PDFX EC2 instance %s is in state: %s", _instance_id, state)

    # Reset idle timer now — prevents watchdog from stopping a freshly started instance
    _last_activity = time.monotonic()

    if state == "stopped":
        _start_instance()
    elif state == "running":
        # Instance running but service not healthy — may still be starting
        logger.info("PDFX instance running but service not healthy, waiting...")
    elif state in ("pending", "stopping"):
        logger.info("PDFX instance in transitional state '%s', waiting...", state)
    else:
        raise RuntimeError(
            f"PDFX EC2 instance {_instance_id} is in unexpected state: {state}"
        )

    # Poll until healthy or timeout
    deadline = time.monotonic() + _startup_timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(_health_poll_interval)
        if await _check_pdfx_health():
            logger.info("PDFX service is now healthy")
            return
        logger.info(
            "Waiting for PDFX service... (%.0fs remaining)",
            deadline - time.monotonic(),
        )

    raise RuntimeError(
        f"PDFX service did not become healthy within {_startup_timeout}s "
        f"after starting instance {_instance_id}"
    )


def record_activity() -> None:
    """Record that PDFX was used, resetting the idle timer."""
    global _last_activity
    _last_activity = time.monotonic()


def should_stop_idle() -> bool:
    """Return True if the instance has been idle long enough to stop.

    Called by the idle watchdog (cron, background task, or Lambda).
    Only returns True if:
    - On-demand management is enabled
    - Activity has been recorded at least once
    - Idle time exceeds the configured threshold
    - The instance is actually running (avoid stopping already-stopped instances)
    """
    if not is_enabled() or _idle_minutes <= 0:
        return False
    if _last_activity == 0.0:
        return False
    idle_seconds = time.monotonic() - _last_activity
    if idle_seconds <= (_idle_minutes * 60):
        return False
    # Only stop if actually running
    try:
        state = _get_instance_state()
        return state == "running"
    except Exception:
        return False


def get_status() -> dict:
    """Return current manager status for health/debug endpoints."""
    if not is_enabled():
        return {"enabled": False}

    try:
        state = _get_instance_state()
    except Exception as exc:
        state = f"error: {exc}"

    idle_seconds = time.monotonic() - _last_activity if _last_activity else None

    return {
        "enabled": True,
        "instance_id": _instance_id,
        "region": _region,
        "instance_state": state,
        "idle_seconds": round(idle_seconds, 1) if idle_seconds else None,
        "idle_stop_after_minutes": _idle_minutes,
        "startup_timeout": _startup_timeout,
    }
