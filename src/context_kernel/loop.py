from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from .models import utc_now
from .storage import Workspace
from .tasks import TaskStore
from .tools import ToolExecutor
from .loop_actions import (
    ALLOWED_ACTIONS,
    TOOL_ACTIONS,
    parse_agent_action,
    repeated_agent_action,
    summarize_tool_result,
)
from .loop_execution import (
    attach_trace_outputs,
    parse_agent_step_action,
    response_token_counts,
    run_provider_agent_step,
)
from .loop_progress import emit_agent_progress
from .loop_reports import (
    add_tokens,
    compact_saved_agent_report,
    write_agent_run_memory,
)
from .loop_planning import prepare_agent_step_plan
from .loop_routing import (
    AUX_REVIEW_MODES,
    MODEL_ROUTING_MODES,
    resolve_role_model,
    run_auxiliary_review,
    select_model_role,
)
from .loop_step_handlers import handle_respond_action, handle_tool_action


class AgentLoop:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.tasks = TaskStore(workspace)
        self.tools = ToolExecutor(workspace)

    def run(
        self,
        request: str,
        *,
        provider_name: str,
        budget: int | None,
        profile: str = "balanced",
        model: str | None = None,
        aux_model: str | None = None,
        model_routing: str = "primary",
        aux_review: str = "auto",
        base_url: str | None = None,
        task_id: str | None = None,
        max_steps: int = 5,
        remember: bool = True,
        allow_over_budget: bool = False,
        expect_json: bool = False,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if model_routing not in MODEL_ROUTING_MODES:
            raise ValueError(f"model_routing must be one of: {', '.join(sorted(MODEL_ROUTING_MODES))}")
        if aux_review not in AUX_REVIEW_MODES:
            raise ValueError(f"aux_review must be one of: {', '.join(sorted(AUX_REVIEW_MODES))}")

        task = self._task_for_request(request, task_id)
        report = {
            "id": uuid4().hex[:12],
            "created_at": utc_now(),
            "request": request,
            "task_id": task["id"],
            "status": "running",
            "max_steps": max_steps,
            "steps": [],
            "final_response": None,
            "model_routing": {
                "mode": model_routing,
                "primary_model": resolve_role_model(provider_name, model, aux_model, "primary"),
                "auxiliary_model": resolve_role_model(provider_name, model, aux_model, "auxiliary"),
                "aux_review": aux_review,
            },
            "diagnostic": None,
            "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
            "totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "materialized_files": [],
        }

        for index in range(1, max_steps + 1):
            emit_agent_progress(
                progress_callback,
                {
                    "event": "step_start",
                    "step": index,
                    "max_steps": max_steps,
                    "message": "building minimal context and selecting model",
                },
            )
            step = self._run_step(
                request,
                task["id"],
                index=index,
                max_steps=max_steps,
                prior_steps=report["steps"],
                provider_name=provider_name,
                budget=budget,
                profile=profile,
                model=model,
                aux_model=aux_model,
                model_routing=model_routing,
                aux_review=aux_review,
                base_url=base_url,
                allow_over_budget=allow_over_budget,
                expect_json=expect_json,
                progress_callback=progress_callback,
            )
            report["steps"].append(step)
            emit_agent_progress(
                progress_callback,
                {
                    "event": "step_end",
                    "step": index,
                    "max_steps": max_steps,
                    "status": step.get("status"),
                    "action": (step.get("action") or {}).get("action"),
                    "model_role": step.get("model_role"),
                    "model": step.get("model"),
                    "tokens": step.get("tokens", {}).get("total_tokens", 0),
                },
            )
            add_tokens(report["totals"], step.get("tokens", {}))
            add_tokens(report["totals"], step.get("aux_review", {}).get("tokens", {}))
            if step.get("final_response") is not None:
                report["final_response"] = step["final_response"]
            if step.get("materialized_files"):
                report["materialized_files"].extend(step["materialized_files"])

            if not step["continue"]:
                report["status"] = step["status"]
                report["diagnostic"] = step.get("diagnostic")
                self.tasks.step(task["id"], step["stop_reason"], kind="agent_stop")
                break
        else:
            report["status"] = "stopped"
            self.tasks.step(task["id"], f"Agent loop stopped after {max_steps} step(s).", kind="agent_stop")

        if remember:
            report["state"] = write_agent_run_memory(self.workspace, self.tasks, report)
        report["completed_at"] = utc_now()
        self.workspace.agent_runs_dir.mkdir(parents=True, exist_ok=True)
        Workspace.write_json(self.workspace.agent_runs_dir / f"{report['id']}.json", compact_saved_agent_report(report))
        return report

    def _task_for_request(self, request: str, task_id: str | None) -> dict[str, Any]:
        if task_id:
            task = self.tasks.get(task_id)
            if task.get("status") == "completed":
                raise ValueError(f"Task is completed and cannot receive agent loop steps: {task_id}")
            return task
        return self.tasks.start(request, goal=request)

    def _run_step(
        self,
        request: str,
        task_id: str,
        *,
        index: int,
        max_steps: int,
        prior_steps: list[dict[str, Any]],
        provider_name: str,
        budget: int | None,
        profile: str,
        model: str | None,
        aux_model: str | None,
        model_routing: str,
        aux_review: str,
        base_url: str | None,
        allow_over_budget: bool,
        expect_json: bool,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        plan, effective_budget, budget_block = prepare_agent_step_plan(
            self.workspace,
            request=request,
            task_id=task_id,
            budget=budget,
            profile=profile,
            index=index,
            max_steps=max_steps,
            allow_over_budget=allow_over_budget,
            progress_callback=progress_callback,
        )
        if budget_block is not None:
            return budget_block

        emit_agent_progress(
            progress_callback,
            {
                "event": "context_ready",
                "step": index,
                "max_steps": max_steps,
                "estimated_used": plan["budget"]["estimated_used"],
                "budget_total": plan["budget"]["total"],
                "memory_count": len(plan.get("selection", {}).get("memory", [])),
                "skills": [
                    {"id": item.get("id", ""), "level": item.get("level", "")}
                    for item in plan.get("selection", {}).get("skills", [])[:5]
                ],
            },
        )
        selected_role, routing_reason = select_model_role(
            model_routing=model_routing,
            plan=plan,
            step_index=index,
            prior_steps=prior_steps,
            profile=profile,
        )
        selected_model = resolve_role_model(provider_name, model, aux_model, selected_role)
        emit_agent_progress(
            progress_callback,
            {
                "event": "provider_start",
                "step": index,
                "max_steps": max_steps,
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
            },
        )
        review = run_auxiliary_review(
            self.workspace,
            request=request,
            provider_name=provider_name,
            plan=plan,
            selected_role=selected_role,
            selected_model=selected_model,
            aux_model=aux_model,
            base_url=base_url,
            budget=effective_budget,
            profile=profile,
            task_id=task_id,
            allow_over_budget=allow_over_budget,
            aux_review=aux_review,
            routing_reason=routing_reason,
        )

        trace, provider_failure = run_provider_agent_step(
            self.workspace,
            self.tasks,
            request=request,
            task_id=task_id,
            index=index,
            max_steps=max_steps,
            provider_name=provider_name,
            budget=effective_budget,
            profile=profile,
            model=selected_model,
            base_url=base_url,
            allow_over_budget=allow_over_budget,
            expect_json=expect_json,
            plan=plan,
            selected_role=selected_role,
            routing_reason=routing_reason,
            aux_review=review,
        )
        if provider_failure is not None:
            return provider_failure
        attach_trace_outputs(self.tasks, task_id, trace)
        tokens = response_token_counts(trace)

        verifier_ok = bool(trace.get("verifier", {}).get("ok"))
        action, contract_recovered, action_failure = parse_agent_step_action(
            self.tasks,
            task_id=task_id,
            index=index,
            trace=trace,
            expect_json=expect_json,
            verifier_ok=verifier_ok,
            prior_steps=prior_steps,
            plan=plan,
            model_role=selected_role,
            model=selected_model,
            routing_reason=routing_reason,
            aux_review=review,
            tokens=tokens,
        )
        if action_failure is not None:
            return action_failure

        if action["action"] == "respond":
            return handle_respond_action(
                tasks=self.tasks,
                tools=self.tools,
                request=request,
                task_id=task_id,
                index=index,
                max_steps=max_steps,
                plan=plan,
                trace=trace,
                selected_role=selected_role,
                selected_model=selected_model,
                routing_reason=routing_reason,
                review=review,
                action=action,
                tokens=tokens,
                verifier_ok=verifier_ok,
                contract_recovered=contract_recovered,
                progress_callback=progress_callback,
            )

        return handle_tool_action(
            tasks=self.tasks,
            tools=self.tools,
            request=request,
            task_id=task_id,
            index=index,
            max_steps=max_steps,
            plan=plan,
            trace=trace,
            selected_role=selected_role,
            selected_model=selected_model,
            routing_reason=routing_reason,
            review=review,
            action=action,
            tokens=tokens,
            verifier_ok=verifier_ok,
            contract_recovered=contract_recovered,
            progress_callback=progress_callback,
        )
