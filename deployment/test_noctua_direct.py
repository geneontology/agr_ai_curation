#!/usr/bin/env python3
"""Direct noctua-py validation script.

Tests each GO-CAM operation in isolation to identify where failures occur.
Run on the main app server or locally with a valid Barista token.

Usage:
    # On the server (uses token exchange):
    docker exec agr_ai_curation-backend-1 python /app/deployment/test_noctua_direct.py

    # Locally with uv:
    BARISTA_TOKEN=<token> uv run python deployment/test_noctua_direct.py

    # Or with the token exchange:
    GITHUB_TOKEN=<github-token> python deployment/test_noctua_direct.py
"""

import os
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Configuration
BARISTA_BASE = os.getenv("BARISTA_BASE_URL", "http://barista-dev.berkeleybop.org")
BARISTA_NAMESPACE = os.getenv("BARISTA_NAMESPACE", "minerva_public_dev")


def get_token() -> str:
    """Get a Barista token from env or via token exchange."""
    token = os.getenv("BARISTA_TOKEN")
    if token:
        return token

    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        import httpx
        exchange_url = os.getenv(
            "BARISTA_TOKEN_EXCHANGE_URL",
            f"{BARISTA_BASE}/auth/token/exchange"
        )
        logger.info("Exchanging GitHub token at %s", exchange_url)
        resp = httpx.post(exchange_url, json={"github_access_token": github_token})
        resp.raise_for_status()
        token = resp.json().get("token")
        if token:
            return token
        raise RuntimeError(f"Token exchange returned no token: {resp.json()}")

    raise RuntimeError(
        "Set BARISTA_TOKEN or GITHUB_TOKEN environment variable. "
        "Get a dev token from the GO team or use the GitHub token exchange."
    )


def test_step(name: str, fn, cleanup_fn=None):
    """Run a test step, print result, return the value or None on failure."""
    try:
        result = fn()
        logger.info("PASS: %s", name)
        return result
    except Exception as exc:
        logger.error("FAIL: %s — %s", name, exc)
        if cleanup_fn:
            try:
                cleanup_fn()
            except Exception:
                pass
        return None


def main():
    from noctua.barista import BaristaClient

    token = get_token()
    logger.info("Token: %s...%s", token[:8], token[-4:])
    logger.info("Barista: %s", BARISTA_BASE)
    logger.info("Namespace: %s", BARISTA_NAMESPACE)

    client = BaristaClient(
        token=token,
        base_url=BARISTA_BASE,
        namespace=BARISTA_NAMESPACE,
        timeout=30.0,
    )

    model_id = None

    # Step 1: Create model
    def step_create():
        nonlocal model_id
        resp = client.create_model(title="noctua-py validation test")
        logger.info("  Raw response OK: %s", resp.ok)
        logger.info("  Model ID: %s", resp.model_id)
        logger.info("  Error: %s", resp.error)
        if resp.raw:
            logger.info("  Signal: %s", resp.signal)
        if not resp.ok:
            raise RuntimeError(f"create_model failed: {resp.error}")
        model_id = resp.model_id
        return resp

    result = test_step("Create model", step_create)
    if not result or not model_id:
        logger.error("Cannot continue without a model. Aborting.")
        sys.exit(1)

    # Step 2: Add individual (molecular function)
    def step_add_mf():
        resp = client.add_individual(
            model_id, "GO:0003924", assign_var="gtpase_activity"
        )
        logger.info("  Response OK: %s", resp.ok)
        logger.info("  Error: %s", resp.error)
        logger.info("  Variables: %s", resp.model_vars)
        logger.info("  Individuals: %d", len(resp.individuals))
        if not resp.ok:
            raise RuntimeError(f"add_individual failed: {resp.error}")
        return resp

    test_step("Add individual (GO:0003924 GTPase activity)", step_add_mf)

    # Step 3: Add another individual (biological process)
    def step_add_bp():
        resp = client.add_individual(
            model_id, "GO:0007264", assign_var="signaling"
        )
        logger.info("  Response OK: %s", resp.ok)
        logger.info("  Error: %s", resp.error)
        logger.info("  Variables: %s", resp.model_vars)
        if not resp.ok:
            raise RuntimeError(f"add_individual failed: {resp.error}")
        return resp

    test_step("Add individual (GO:0007264 small GTPase signaling)", step_add_bp)

    # Step 4: Add fact (causal relationship) — this is where the agent fails
    def step_add_fact():
        resp = client.add_fact(
            model_id,
            subject_id="gtpase_activity",
            object_id="signaling",
            predicate_id="RO:0002211",  # regulates
        )
        logger.info("  Response OK: %s", resp.ok)
        logger.info("  Error: %s", resp.error)
        logger.info("  Facts: %d", len(resp.facts))
        if resp.raw:
            logger.info("  Raw message-type: %s", resp.raw.get("message-type"))
            if not resp.ok:
                logger.info("  Raw response: %s", json.dumps(resp.raw, indent=2)[:500])
        if not resp.ok:
            raise RuntimeError(f"add_fact failed: {resp.error}")
        return resp

    test_step("Add fact (gtpase_activity --[regulates]--> signaling)", step_add_fact)

    # Step 5: Add fact with evidence
    def step_add_evidence():
        resp = client.add_fact_with_evidence(
            model_id,
            subject_id="gtpase_activity",
            object_id="signaling",
            predicate_id="BFO:0000050",  # part of
            eco_id="ECO:0000314",  # direct assay evidence
            sources=["PMID:12345678"],
        )
        logger.info("  Response OK: %s", resp.ok)
        logger.info("  Error: %s", resp.error)
        if not resp.ok:
            logger.info("  Raw response: %s", json.dumps(resp.raw, indent=2)[:500])
            raise RuntimeError(f"add_fact_with_evidence failed: {resp.error}")
        return resp

    test_step("Add fact with evidence (part_of + ECO + PMID)", step_add_evidence)

    # Step 6: Get model (verify everything is there)
    def step_get_model():
        resp = client.get_model(model_id)
        logger.info("  Response OK: %s", resp.ok)
        if resp.ok and resp.data:
            logger.info("  Individuals: %d", len(resp.data.individuals))
            logger.info("  Facts: %d", len(resp.data.facts))
            for ind in resp.data.individuals:
                label = ind.root_type.label if ind.root_type else "?"
                logger.info("    Individual: %s (%s)", ind.id, label)
            for fact in resp.data.facts:
                pred = fact.property_label or fact.property
                logger.info("    Fact: %s --[%s]--> %s", fact.subject, pred, fact.object)
        if not resp.ok:
            raise RuntimeError(f"get_model failed: {resp.error}")
        return resp

    test_step("Get model (verify contents)", step_get_model)

    # Step 7: Export as markdown
    def step_export():
        resp = client.export_model(model_id, format="markdown")
        logger.info("  Response OK: %s", resp.ok)
        if resp.ok:
            export_data = resp.raw.get("data", "")
            logger.info("  Export length: %d chars", len(str(export_data)))
            logger.info("  First 200 chars: %s", str(export_data)[:200])
        if not resp.ok:
            raise RuntimeError(f"export failed: {resp.error}")
        return resp

    test_step("Export model (markdown)", step_export)

    # Cleanup: delete the test model
    logger.info("")
    logger.info("Test model: %s", model_id)
    logger.info("View at: %s", f"http://noctua-dev.berkeleybop.org/editor/graph/{model_id}")
    logger.info("")
    logger.info("To delete: docker exec agr_ai_curation-backend-1 python -c \"")
    logger.info("  from noctua.barista import BaristaClient")
    logger.info("  c = BaristaClient(token='%s...', base_url='%s', namespace='%s')", token[:8], BARISTA_BASE, BARISTA_NAMESPACE)
    logger.info("  # c.delete_model('%s')  # uncomment to delete", model_id)
    logger.info("\"")


if __name__ == "__main__":
    main()
