"""Workflow Planner for translating natural language requests into validated multi-step executable workflows via Gemini."""

import json
import logging
import re
from typing import Any

from src.core.workflow_engine import WorkflowEngine
from src.core.workflow_manager import WorkflowManager, WorkflowValidationError
from src.llm.base import ModelTier
from src.llm.manager import LLMManager

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class WorkflowPlanner:
    """Translates natural-language goals into validated structured multi-step execution graphs."""

    def __init__(
        self,
        workflow_manager: WorkflowManager,
        workflow_engine: WorkflowEngine,
        llm_manager: LLMManager | None = None,
        gemini_client: Any | None = None,
    ) -> None:
        self.workflow_manager = workflow_manager
        self.workflow_engine = workflow_engine
        self.llm_manager = llm_manager
        self.gemini_client = gemini_client

    def _get_available_tool_catalog(self, user_id: int) -> list[dict[str, Any]]:
        """Return catalog of tools accessible by the specified user."""
        registry = self.workflow_manager.tool_registry
        policy = self.workflow_manager.permission_policy

        if not registry:
            return []

        catalog: list[dict[str, Any]] = []
        for defn in registry.list_tools():
            t_name = defn["name"]
            tool_obj = registry.get_definition(t_name)
            if tool_obj and policy:
                allowed, _ = policy.check_permission(user_id, tool_obj)
                if not allowed:
                    continue  # Do not expose tools user is unauthorized to use

            catalog.append({
                "name": defn["name"],
                "description": defn["description"],
                "parameters": defn.get("parameters", {}),
                "permission": defn.get("permission", "read_only"),
            })
        return catalog

    def _extract_json_payload(self, text: str) -> dict[str, Any]:
        """Extract and parse JSON object from LLM output, handling markdown fences and raw JSON."""
        # 1. Try markdown code fence
        match = _JSON_BLOCK_PATTERN.search(text)
        if match:
            return json.loads(match.group(1))

        # 2. Try raw substring from first '{' to last '}'
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])

        # 3. Direct parse
        return json.loads(text)

    async def plan_workflow(self, user_id: int, natural_language_request: str) -> dict[str, Any]:
        """Convert natural language goal into a validated persistent workflow definition."""
        clean_request = natural_language_request.strip()
        if not clean_request:
            raise WorkflowValidationError("Request text cannot be empty.")

        tools_catalog = self._get_available_tool_catalog(user_id)
        if not tools_catalog:
            raise WorkflowValidationError("No registered tools are available to construct a workflow.")

        system_prompt = (
            "You are an expert Workflow Architect. Your role is to convert a user's natural language request "
            "into a structured, sequential, multi-step execution plan using ONLY the provided tools.\n\n"
            "STRICT RULES:\n"
            "1. ONLY use tools listed in the Available Tools catalog. Do NOT hallucinate tool names.\n"
            "2. Tool names must match exactly.\n"
            "3. Step names must be alphanumeric/underscores (e.g. 'step_search', 'step_summarize').\n"
            "4. Each dependent step can reference outputs of previous steps using '{step_name.field}' or '{step_name.output.field}'.\n"
            "5. If a step should only run conditionally, include a 'condition' object with: 'field', 'operator' (e.g. 'gt', 'eq', 'exists'), and 'value'.\n"
            "6. Output ONLY valid JSON matching this schema:\n"
            "{\n"
            '  "name": "Short Descriptive Title",\n'
            '  "description": "Explanation of workflow",\n'
            '  "steps": [\n'
            '    {\n'
            '      "name": "step_1",\n'
            '      "tool_name": "tool_name_here",\n'
            '      "arguments": { "param": "value" },\n'
            '      "dependencies": [],\n'
            '      "condition": null,\n'
            '      "max_retries": 1\n'
            '    }\n'
            '  ]\n'
            "}\n"
        )

        user_prompt = (
            f"User Request:\n{clean_request}\n\n"
            f"Available Tools Catalog:\n{json.dumps(tools_catalog, indent=2)}\n\n"
            "Generate the structured workflow plan in JSON format:"
        )

        raw_response = ""
        if self.llm_manager:
            resp = await self.llm_manager.generate(
                prompt=user_prompt,
                system_instruction=system_prompt,
                tier=ModelTier.MEDIUM,
                temperature=0.1,
            )
            raw_response = resp.content
        elif self.gemini_client:
            raw_response = await self.gemini_client.generate_response(
                user_id=user_id,
                message_text=f"{system_prompt}\n\n{user_prompt}",
                history=[],
                temperature=0.1,
            )
        else:
            raise RuntimeError("Neither LLMManager nor GeminiClient is configured on WorkflowPlanner.")

        try:
            plan_data = self._extract_json_payload(raw_response)
        except Exception as exc:
            logger.error("Failed parsing LLM workflow plan JSON: %s. Response was: %s", exc, raw_response[:300])
            raise WorkflowValidationError(f"Generated workflow plan is not valid JSON: {exc}")

        wf_name = plan_data.get("name") or f"Workflow for {clean_request[:40]}"
        wf_desc = plan_data.get("description") or clean_request
        steps = plan_data.get("steps") or []

        # Validate against WorkflowManager constraints (tools, permissions, DAG)
        self.workflow_manager.validate_workflow_definition(
            name=wf_name,
            steps=steps,
            user_id=user_id,
        )

        # Persist valid workflow
        created = await self.workflow_manager.create_workflow(
            user_id=user_id,
            name=wf_name,
            steps=steps,
            description=wf_desc,
            is_one_time=True,
        )

        created["plan"] = {
            "name": wf_name,
            "description": wf_desc,
            "steps": steps,
        }
        return created

    async def plan_and_execute(self, user_id: int, natural_language_request: str) -> dict[str, Any]:
        """Convert natural language goal into a workflow, persist it, and immediately execute."""
        planned = await self.plan_workflow(user_id, natural_language_request)
        wf_id = planned["workflow_id"]

        exec_res = await self.workflow_engine.execute_workflow(
            workflow_id=wf_id,
            user_id=user_id,
            trigger_type="agent",
        )

        return {
            "workflow_id": wf_id,
            "name": planned["name"],
            "description": planned.get("description"),
            "steps": planned.get("plan", {}).get("steps", []),
            "execution": exec_res,
        }

