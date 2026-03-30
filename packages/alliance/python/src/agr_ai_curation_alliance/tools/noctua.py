"""Noctua GO-CAM tool factories for the AGR Alliance package.

Wraps noctua-py's BaristaClient to expose GO-CAM model operations as
tools that the AI agent system can invoke. Tools require a barista_token
in their execution context, obtained via the Barista token exchange at
login time.

Vibe coded by Claude (Opus 4.6) with SJC, 2026-03-21.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents import function_tool

logger = logging.getLogger(__name__)


def _require_context_value(context: dict[str, Any], key: str) -> str:
    value = context.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing required context value '{key}'")
    return value


def _summarize_response(resp: Any, highlight_new: str = "") -> str:
    """Convert a BaristaResponse to a concise string for the agent.

    Returns actual Minerva IDs so the LLM can use them in subsequent calls
    (important because each tool call runs in a fresh subprocess — variable
    tracking does not persist between calls).
    """
    if not resp.ok:
        error_msg = resp.error or "unknown error"
        # Include the raw API message for debugging
        raw_message = resp.raw.get("message", "") if resp.raw else ""
        if raw_message and raw_message != error_msg:
            return f"Error: {error_msg} — {raw_message}"
        return f"Error: {error_msg}"
    parts = []
    if resp.model_id:
        parts.append(f"model_id: {resp.model_id}")
    if resp.individuals:
        parts.append(f"{len(resp.individuals)} individual(s)")
        # Show actual IDs so LLM can use them in add_fact calls
        for ind in resp.individuals:
            ind_id = ind.get("id", "") if isinstance(ind, dict) else getattr(ind, "id", "")
            types = ind.get("type", []) if isinstance(ind, dict) else []
            label = ""
            for t in types:
                if isinstance(t, dict) and t.get("label"):
                    label = t["label"]
                    break
            if ind_id:
                parts.append(f"  individual: {ind_id} ({label})")
    if resp.facts:
        parts.append(f"{len(resp.facts)} fact(s)")
    return "; ".join(parts) if parts else "OK"


def _model_summary(resp: Any) -> str:
    """Build a structured summary of a full model response."""
    if not resp.ok:
        return f"Error: {resp.error or 'unknown error'}"
    data = resp.data
    if not data:
        return f"Model {resp.model_id}: no data returned"

    parts = [f"Model: {resp.model_id}"]
    if data.annotations:
        for ann in data.annotations:
            if ann.key == "title":
                parts.append(f"Title: {ann.value}")
    if data.individuals:
        parts.append(f"\nIndividuals ({len(data.individuals)}):")
        for ind in data.individuals[:20]:
            label = ""
            if ind.root_type and ind.root_type.label:
                label = ind.root_type.label
            parts.append(f"  - {ind.id}: {label}")
    if data.facts:
        parts.append(f"\nFacts ({len(data.facts)}):")
        for fact in data.facts[:20]:
            pred = fact.property_label or fact.property
            parts.append(f"  - {fact.subject} --[{pred}]--> {fact.object}")
    return "\n".join(parts)


def create_noctua_tool(context: dict[str, Any]):
    """Create the noctua GO-CAM tool with the user's Barista token."""
    barista_token = _require_context_value(context, "barista_token")

    # Lazy import so noctua-py is only required when the tool is used
    from noctua.barista import BaristaClient

    import os
    barista_base = os.getenv(
        "BARISTA_BASE_URL", "http://barista-dev.berkeleybop.org"
    )
    barista_namespace = os.getenv("BARISTA_NAMESPACE", "minerva_public_dev")

    client = BaristaClient(
        token=barista_token,
        base_url=barista_base,
        namespace=barista_namespace,
        timeout=30.0,
    )

    @function_tool
    def noctua_gocam(
        action: str,
        model_id: str = "",
        title: str = "",
        class_curie: str = "",
        assign_var: str = "",
        subject: str = "",
        object_: str = "",
        predicate: str = "",
        eco_id: str = "",
        sources: str = "",
        individual_id: str = "",
        format: str = "markdown",
        limit: int = 10,
    ) -> str:
        """Query and edit Gene Ontology Causal Activity Models (GO-CAMs) via Noctua.

        Actions:
          list_models     - List available GO-CAM models (optional: title, limit)
          get_model       - Get full model details (required: model_id)
          create_model    - Create a new model (optional: title)
          add_individual  - Add a molecular entity to a model
                           (required: model_id, class_curie; optional: assign_var)
          add_fact        - Add a relationship between two entities
                           (required: model_id, subject, object_, predicate)
          add_evidence    - Add a fact with evidence
                           (required: model_id, subject, object_, predicate,
                            eco_id, sources as comma-separated PMIDs)
          remove_fact     - Remove a relationship
                           (required: model_id, subject, object_, predicate)
          export_model    - Export model in a format (required: model_id;
                           optional: format = owl|ttl|json-ld|gaf|markdown)

        IMPORTANT: Each tool call runs independently. Variable names (assign_var)
        do NOT persist between calls. After add_individual, the response includes
        the actual individual ID (e.g., "gomodel:xxx/individual-123"). You MUST
        use these actual IDs (not variable names) as subject/object_ in add_fact.

        GO-CAM models represent biological pathways as causal activity models.
        Use GO terms (GO:NNNNNNN) for molecular functions/processes,
        relation ontology terms (RO:NNNNNNN) for predicates,
        and ECO terms (ECO:NNNNNNN) for evidence codes.
        """
        try:
            if action == "list_models":
                result = client.list_models(
                    limit=limit,
                    title=title or None,
                )
                models = result.get("models", [])
                if not models:
                    return "No models found."
                lines = [f"Found {len(models)} model(s):"]
                for m in models[:limit]:
                    mid = m.get("id", "?")
                    mtitle = m.get("title", "(untitled)")
                    lines.append(f"  - {mid}: {mtitle}")
                return "\n".join(lines)

            elif action == "get_model":
                if not model_id:
                    return "Error: model_id is required for get_model"
                resp = client.get_model(model_id)
                return _model_summary(resp)

            elif action == "create_model":
                resp = client.create_model(title=title or None)
                return f"Created model: {_summarize_response(resp)}"

            elif action == "add_individual":
                if not model_id or not class_curie:
                    return "Error: model_id and class_curie are required"
                resp = client.add_individual(
                    model_id,
                    class_curie,
                    assign_var=assign_var or "x1",
                )
                return f"Added individual: {_summarize_response(resp)}"

            elif action == "add_fact":
                if not all([model_id, subject, object_, predicate]):
                    return "Error: model_id, subject, object_, and predicate are required"
                resp = client.add_fact(model_id, subject, object_, predicate)
                return f"Added fact: {_summarize_response(resp)}"

            elif action == "add_evidence":
                if not all([model_id, subject, object_, predicate, eco_id, sources]):
                    return "Error: model_id, subject, object_, predicate, eco_id, and sources are required"
                source_list = [s.strip() for s in sources.split(",") if s.strip()]
                resp = client.add_fact_with_evidence(
                    model_id, subject, object_, predicate,
                    eco_id=eco_id, sources=source_list,
                )
                return f"Added fact with evidence: {_summarize_response(resp)}"

            elif action == "remove_fact":
                if not all([model_id, subject, object_, predicate]):
                    return "Error: model_id, subject, object_, and predicate are required"
                resp = client.remove_fact(model_id, subject, object_, predicate)
                return f"Removed fact: {_summarize_response(resp)}"

            elif action == "export_model":
                if not model_id:
                    return "Error: model_id is required for export_model"
                resp = client.export_model(model_id, format=format or "markdown")
                if resp.ok:
                    return str(resp.raw.get("data", resp.raw))
                return f"Export failed: {resp.error}"

            else:
                return (
                    f"Unknown action '{action}'. Available actions: "
                    "list_models, get_model, create_model, add_individual, "
                    "add_fact, add_evidence, remove_fact, export_model"
                )

        except Exception as exc:
            logger.exception("Noctua tool error (action=%s)", action)
            return f"Error: {exc}"

    return noctua_gocam


__all__ = ["create_noctua_tool"]
