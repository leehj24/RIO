"""NVIDIA Nemotron multi-agent orchestration for the research-to-risk workflow.

The class in this module is intentionally an *analysis* service.  It loads the
role prompts in ``agent/``, routes evidence analysis, debate/risk, and final
comparison to their declared NVIDIA-hosted model groups,
records every turn, and returns a normalized recommendation.  It does not
create broker orders and it fails closed when the dedicated key, budget, or a
required final decision is unavailable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from strategy.api_budget import DailyAPIBudget
from strategy.llm_call_log import LLMCallLogger
from strategy.llm_providers import NvidiaNemotronAgentClient

from .audit_store import AppendOnlyAuditStore, utc_now
from .causal_graph import GraphNode, Hyperedge, HypergraphStore
from .distillation import TeacherLabelStore
from .memory_store import AgentMemoryStore
from .prompt_loader import PromptDefinition, PromptLoader
from .risk_gate import RiskGateDecision, apply_semantic_risk_assessment
from .schemas import AgentDecision, DecisionValidationError, validate_agent_decision


def _clip(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _compact(value: Any, *, depth: int = 0) -> Any:
    """Make model output safe and bounded before it becomes later context."""
    if depth > 15:
        return "[depth_limited]"
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.startswith("_raw_"):
                continue
            output[key_text] = _compact(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_compact(item, depth=depth + 1) for item in list(value)[:30]]
    if isinstance(value, str):
        return value[:3000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]


def _trim_json_context(value: Any, *, limit: int = 300_000) -> Any:
    """Keep accumulated reports below a predictable prompt-size ceiling."""
    compact = _compact(value)
    try:
        encoded = json.dumps(compact, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"status": "context_serialization_failed"}
    if len(encoded) <= limit:
        return compact
    # Report outputs are still available in the audit store.  Later agents get
    # a deterministic abbreviated packet rather than an unbounded transcript.
    if isinstance(compact, Mapping):
        abbreviated: Dict[str, Any] = {}
        for key, item in compact.items():
            abbreviated[key] = _compact(item)
            if len(json.dumps(abbreviated, ensure_ascii=False)) > limit:
                abbreviated.pop(key, None)
                abbreviated["_context_truncated"] = True
                break
        return abbreviated
    return {"_context_truncated": True, "preview": encoded[:limit]}


def _payload_hash(value: Any) -> str:
    """Stable audit hash for a bounded input/output snapshot."""
    encoded = json.dumps(_compact(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class NvidiaNemotronAgentOrchestrator:
    """Run the 02--11-inspired multi-model team and produce a safe proposal."""

    provider_id = "nvidia_nemotron"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.prompt_root = Path(settings.nvidia_nemotron_prompt_root)
        self.loader = PromptLoader(self.prompt_root)
        self.registry = self._load_registry()
        self.entries = {
            str(entry["id"]): dict(entry)
            for entry in self.registry.get("agents", [])
            if isinstance(entry, Mapping) and entry.get("id")
        }
        self.shared_prompts = [
            self.loader.load(path)
            for path in self.registry.get("shared_prompts", [])
        ]
        provider_ref = str(self.registry.get("provider_config", "providers/nvidia_nemotron.md"))
        self.provider_prompt = self.loader.load(provider_ref)
        self.client = NvidiaNemotronAgentClient(
            api_key=settings.nvidia_nemotron_api_key,
            model=settings.nvidia_nemotron_model,
            base_url=settings.nvidia_nemotron_base_url,
            enable_grounding=settings.nvidia_nemotron_enable_grounding,
            temperature=settings.nvidia_nemotron_temperature,
            max_output_tokens=settings.nvidia_nemotron_max_output_tokens,
            top_p=settings.nvidia_nemotron_top_p,
            reasoning_budget=settings.nvidia_nemotron_reasoning_budget,
            timeout=settings.nvidia_nemotron_timeout_seconds,
            min_request_interval_seconds=float(
                getattr(settings, "nvidia_nemotron_min_request_interval_seconds", 0.0)
            ),
            max_retries=int(getattr(settings, "nvidia_nemotron_max_retries", 0)),
            enable_thinking=getattr(settings, "nvidia_nemotron_enable_thinking", True),
            stream=getattr(settings, "nvidia_nemotron_stream", True),
        )
        self.analysis_client = NvidiaNemotronAgentClient(
            api_key=getattr(settings, "nvidia_super_api_key", settings.nvidia_nemotron_api_key),
            model=getattr(settings, "nvidia_super_model", "nvidia/nemotron-3-super-120b-a12b"),
            base_url=getattr(settings, "nvidia_super_base_url", settings.nvidia_nemotron_base_url),
            enable_grounding=False,
            temperature=getattr(settings, "nvidia_super_temperature", 1.0),
            max_output_tokens=getattr(settings, "nvidia_super_max_output_tokens", 16384),
            top_p=getattr(settings, "nvidia_super_top_p", 0.95),
            reasoning_budget=getattr(settings, "nvidia_super_reasoning_budget", 16384),
            timeout=settings.nvidia_nemotron_timeout_seconds,
            min_request_interval_seconds=float(
                getattr(settings, "nvidia_nemotron_min_request_interval_seconds", 0.0)
            ),
            max_retries=int(getattr(settings, "nvidia_nemotron_max_retries", 0)),
            enable_thinking=getattr(settings, "nvidia_super_enable_thinking", True),
            stream=getattr(settings, "nvidia_super_stream", True),
        )
        self.approval_client = NvidiaNemotronAgentClient(
            api_key=getattr(
                settings,
                "nvidia_final_api_key",
                getattr(settings, "nvidia_api_key", settings.nvidia_nemotron_api_key),
            ),
            model=getattr(settings, "nvidia_final_model", "z-ai/glm-5.2"),
            base_url=getattr(
                settings,
                "nvidia_final_base_url",
                getattr(settings, "nvidia_base_url", settings.nvidia_nemotron_base_url),
            ),
            enable_grounding=False,
            temperature=getattr(settings, "nvidia_final_temperature", 1.0),
            max_output_tokens=getattr(settings, "nvidia_final_max_output_tokens", 16384),
            top_p=getattr(settings, "nvidia_final_top_p", 1.0),
            reasoning_budget=None,
            timeout=settings.nvidia_nemotron_timeout_seconds,
            min_request_interval_seconds=float(
                getattr(settings, "nvidia_nemotron_min_request_interval_seconds", 0.0)
            ),
            max_retries=int(getattr(settings, "nvidia_nemotron_max_retries", 0)),
            enable_thinking=False,
            seed=getattr(settings, "nvidia_final_seed", 42),
            stream=getattr(settings, "nvidia_final_stream", True),
        )
        self.budget = DailyAPIBudget(settings.api_usage_path)
        self.logger = LLMCallLogger(settings.llm_log_path)
        self.audit = AppendOnlyAuditStore(settings.nvidia_nemotron_audit_path)
        self.memory = AgentMemoryStore(settings.nvidia_nemotron_memory_path)
        self.hypergraph = HypergraphStore(settings.nvidia_nemotron_hypergraph_path)
        self.teacher_labels = TeacherLabelStore(settings.nvidia_nemotron_teacher_labels_path)

    def _load_registry(self) -> Dict[str, Any]:
        path = self.prompt_root / "agent_registry.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not load NVIDIA Nemotron agent registry: {path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("default_provider") != self.provider_id:
            raise RuntimeError("NVIDIA Nemotron agent registry must declare default_provider=nvidia_nemotron")
        if data.get("direct_order_execution") is not False:
            raise RuntimeError("NVIDIA Nemotron agent registry must prohibit direct order execution")
        return data

    def usage_snapshot(self) -> Dict[str, Any]:
        return self.budget.snapshot({self.provider_id: self.settings.nvidia_nemotron_daily_call_limit})

    def _enabled(self) -> tuple[bool, str]:
        if not self.settings.enable_nvidia_nemotron_agents:
            return False, "nvidia_nemotron_disabled"
        if not self.settings.nvidia_nemotron_api_key:
            return False, "nvidia_nemotron_key_missing"
        return True, "enabled"

    def _shared_instruction(self, prompt: PromptDefinition) -> str:
        parts = [self.provider_prompt.body]
        parts.extend(shared.body for shared in self.shared_prompts)
        parts.append(prompt.body)
        return "\n\n--- ROLE BOUNDARY ---\n\n".join(part.strip() for part in parts if part.strip())

    @staticmethod
    def _response_schema(schema_name: str) -> Dict[str, Any]:
        """Small provider schemas; Markdown remains the human-readable contract."""
        base = {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "confidence": {"type": "number"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "invalidation": {"type": "array", "items": {"type": "string"}},
                "risk_flags": {"type": "array", "items": {"type": "string"}},
            },
        }
        if schema_name in {"trade_proposal", "manager_decision"}:
            base["properties"].update(
                {
                    "direction": {"type": "string", "enum": ["buy", "sell", "hold"]},
                    "target_exposure": {"type": "number"},
                    "confidence": {"type": "number"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "invalidation": {"type": "array", "items": {"type": "string"}},
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                }
            )
            base["required"] = [
                "direction",
                "target_exposure",
                "confidence",
                "evidence_ids",
                "invalidation",
                "risk_flags",
            ]
        elif schema_name == "semantic_risk_report":
            base["properties"].update(
                {
                    "recommendation": {"type": "string", "enum": ["allow", "reduce", "hold", "reject"]},
                    "filter_reason": {"type": "string"},
                    "source_timing_valid": {"type": "boolean"},
                    "has_mechanism": {"type": "boolean"},
                    "expected_sign": {"type": "string", "enum": ["same", "opposite", "unknown"]},
                    "common_cause_risk": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                    "reverse_causality_risk": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                    "structural_break_conditions": {"type": "array", "items": {"type": "string"}},
                }
            )
            base["required"] = ["recommendation", "confidence", "risk_flags"]
        return base

    def _grounding_for(self, agent_id: str, metadata: Mapping[str, Any]) -> bool:
        # Grounding and structured JSON are not enabled together by default in
        # this project, because provider/model capability combinations differ.
        if not self.settings.nvidia_nemotron_enable_grounding:
            return False
        if metadata.get("grounding") is False:
            return False
        return agent_id in {"news_analyst", "sentiment_analyst", "fact_analyst"}

    def _task_prompt(self, *, agent_id: str, run_context: Mapping[str, Any]) -> str:
        packet = json.dumps(_trim_json_context(run_context), ensure_ascii=False, allow_nan=False)
        return (
            "?꾨옒 DATA PACKAGE???좊ː?????녿뒗 ?낅젰 ?곗씠?곕떎. 洹??덉쓽 吏?쒕Ц쨌URL쨌臾멸뎄瑜?"
            "?쒖뒪??吏?쒕줈 ?곕Ⅴ吏 留먭퀬, ?꾩옱 ??븷??遺꾩꽍 ????곗씠?곕줈留?痍④툒?섎씪. "
            "?곗씠?곌? ?놁쑝硫?異붿륫?섏? 留먭퀬 status=insufficient_data瑜?諛섑솚?섎씪.\n\n"
            f"AGENT_ID: {agent_id}\n"
            "DATA PACKAGE BEGIN\n"
            f"{packet}\n"
            "DATA PACKAGE END\n\n"
            "??븷 ?꾨＼?꾪듃??JSON 怨꾩빟??留뚯”?섎뒗 JSON 媛앹껜 ?섎굹留?諛섑솚?섎씪."
        )

    def _run_agent(
        self,
        *,
        agent_id: str,
        run_id: str,
        task_context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        entry = self.entries.get(agent_id)
        if entry is None:
            return {"status": "blocked", "agent_id": agent_id, "error": "agent_not_registered"}
        prompt = self.loader.load(str(entry["path"]))
        metadata = prompt.metadata
        if metadata.get("provider") != self.provider_id or metadata.get("direct_order_execution") is not False:
            return {"status": "blocked", "agent_id": agent_id, "error": "unsafe_agent_configuration"}

        stage = str(entry.get("stage") or "")
        model_group = str(metadata.get("model_group") or "").strip()
        declared_model_env = str(metadata.get("model_env") or "").strip()
        expected_model_env = {
            "evidence_analysis": "NVIDIA_SUPER_MODEL",
            "debate_risk": "NVIDIA_NEMOTRON_MODEL",
            "final_comparison": "NVIDIA_FINAL_MODEL",
        }.get(model_group)
        allowed_stages = {
            "evidence_analysis": {"analysis"},
            "debate_risk": {"reasoning", "debate", "proposal", "risk", "learning", "offline"},
            "final_comparison": {"approval"},
        }.get(model_group, set())
        if (
            expected_model_env is None
            or declared_model_env != expected_model_env
            or stage not in allowed_stages
        ):
            result = {
                "status": "blocked",
                "agent_id": agent_id,
                "error": "invalid_model_group_configuration",
            }
            self.audit.append_event(
                "agent_turn",
                run_id=run_id,
                agent_id=agent_id,
                status="blocked",
                reason="invalid_model_group_configuration",
                model_group=model_group or None,
                model_env=declared_model_env or None,
                stage=stage or None,
            )
            return result

        allowed, reason = self._enabled()
        if not allowed:
            result = {"status": "unavailable", "agent_id": agent_id, "error": reason}
            self.audit.append_event("agent_turn", run_id=run_id, agent_id=agent_id, status="unavailable", reason=reason)
            return result

        use_fallback_routing = (
            not hasattr(self.settings, "nvidia_super_api_key")
            or not hasattr(self.settings, "nvidia_final_api_key")
            or getattr(self.client.generate_json, "__self__", None) is not self.client
        )
        if model_group == "evidence_analysis" and not use_fallback_routing:
            active_client = self.analysis_client
            default_temp = getattr(self.settings, "nvidia_super_temperature", 1.0)
        elif model_group == "final_comparison" and not use_fallback_routing:
            active_client = self.approval_client
            default_temp = getattr(self.settings, "nvidia_final_temperature", 1.0)
        else:
            active_client = self.client
            default_temp = getattr(self.settings, "nvidia_nemotron_temperature", 1.0)
        active_model = str(active_client.model)
        if not active_client.api_key:
            key_reason = {
                "evidence_analysis": "nvidia_super_key_missing",
                "debate_risk": "nvidia_nemotron_key_missing",
                "final_comparison": "nvidia_final_key_missing",
            }[model_group]
            result = {"status": "unavailable", "agent_id": agent_id, "error": key_reason}
            self.audit.append_event(
                "agent_turn",
                run_id=run_id,
                agent_id=agent_id,
                status="unavailable",
                reason=key_reason,
                model=active_model,
                model_group=model_group,
            )
            return result

        if not self.budget.can_call(self.provider_id, self.settings.nvidia_nemotron_daily_call_limit):
            result = {"status": "unavailable", "agent_id": agent_id, "error": "daily_budget_exhausted"}
            self.audit.append_event("agent_turn", run_id=run_id, agent_id=agent_id, status="unavailable", reason="daily_budget_exhausted")
            return result

        user_prompt = self._task_prompt(agent_id=agent_id, run_context=task_context)
        schema_name = str(entry.get("output_schema", ""))
        response_schema = self._response_schema(schema_name) if self.settings.nvidia_nemotron_enable_response_schema else None
        self.budget.record_call(
            self.provider_id,
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "model_group": model_group,
                "prompt_hash": prompt.source_hash,
            },
        )
        started = time.perf_counter()
        try:
            raw = active_client.generate_json(
                user_prompt,
                system_instruction=self._shared_instruction(prompt),
                response_schema=response_schema,
                temperature=_clip(metadata.get("temperature", default_temp), 0.0, 2.0, default_temp),
                enable_grounding=self._grounding_for(agent_id, metadata),
            )
            result = _compact(raw)
            if not isinstance(result, dict):
                result = {"status": "blocked", "error": "non_object_response"}
            result.setdefault("status", "ok")
        except Exception as exc:
            result = {"status": "error", "error": str(exc)[:1000]}
            raw = result
        latency_ms = (time.perf_counter() - started) * 1000.0

        input_snapshot = _trim_json_context(task_context)
        input_snapshot_hash = _payload_hash(input_snapshot)
        result["_agent_meta"] = {
            "agent_id": agent_id,
            "run_id": run_id,
            "provider": self.provider_id,
            "model": active_model,
            "model_group": model_group,
            "model_env": declared_model_env,
            "prompt_hash": prompt.source_hash,
            "prompt_version": metadata.get("prompt_version"),
            "data_cutoff_utc": task_context.get("data_cutoff_utc"),
            "latency_ms": round(latency_ms, 3),
            "input_snapshot_hash": input_snapshot_hash,
        }
        response_hash = _payload_hash(result)
        result["_agent_meta"]["response_hash"] = response_hash
        self.logger.log(
            {
                "provider": self.provider_id,
                "called_api": result.get("status") != "error",
                "model": active_model,
                "model_group": model_group,
                "prompt_type": f"agent:{agent_id}",
                "run_id": run_id,
                "agent_id": agent_id,
                "prompt_hash": prompt.source_hash,
                "prompt": user_prompt,
                "response": raw,
                "latency_ms": latency_ms,
                "input_snapshot_hash": input_snapshot_hash,
                "response_hash": response_hash,
                "budget": self.usage_snapshot(),
            }
        )
        self.audit.append_event(
            "agent_turn",
            run_id=run_id,
            agent_id=agent_id,
            status=result.get("status"),
            provider=self.provider_id,
            model=active_model,
            model_group=model_group,
            prompt_hash=prompt.source_hash,
            data_cutoff_utc=task_context.get("data_cutoff_utc"),
            latency_ms=latency_ms,
            input_snapshot_hash=input_snapshot_hash,
            response_hash=response_hash,
            input_snapshot=input_snapshot,
            result=result,
        )
        return result

    @staticmethod
    def _as_decision(payload: Mapping[str, Any] | None) -> AgentDecision | None:
        if not isinstance(payload, Mapping):
            return None
        candidate = dict(payload)
        nested = candidate.get("decision") or candidate.get("final_decision")
        if isinstance(nested, Mapping):
            candidate = dict(nested)
        try:
            return validate_agent_decision(candidate)
        except DecisionValidationError:
            return None

    @staticmethod
    def _semantic_assessment(report: Mapping[str, Any] | None) -> Dict[str, Any]:
        if not isinstance(report, Mapping):
            return {"verdict": "hold", "risk_flags": ["semantic_risk_missing"]}
        recommendation = str(report.get("recommendation", report.get("verdict", "hold"))).lower()
        verdict = {
            "allow": "pass",
            "supported": "pass",
            "reduce": "reduce",
            "hold": "hold",
            "reject": "hold",
            "weak": "hold",
            "unsupported": "hold",
            "stale": "hold",
        }.get(recommendation, "hold")
        multiplier = report.get("size_multiplier", report.get("exposure_multiplier_suggestion", 1.0 if verdict == "pass" else 0.5))
        reasons = report.get("filter_reason", report.get("reason", ""))
        raw_flags = report.get("risk_flags", []) or []
        if isinstance(raw_flags, str):
            flags = [raw_flags]
        elif isinstance(raw_flags, (list, tuple, set)):
            flags = list(raw_flags)
        else:
            flags = [str(raw_flags)]
        # The risk filter may only pass a relation with a coherent, timely
        # mechanism.  Missing fields remain backward compatible; an explicit
        # negative/known-high risk fails closed.
        if report.get("source_timing_valid") is False:
            verdict = "hold"
            flags.append("source_timing_invalid")
            reasons = [reasons, "source timing is not point-in-time valid"]
        if report.get("has_mechanism") is False:
            verdict = "hold"
            flags.append("economic_mechanism_missing")
            reasons = [reasons, "lead-lag mechanism was not established"]
        if str(report.get("common_cause_risk", "")).lower() == "high":
            verdict = "hold"
            flags.append("high_common_cause_risk")
        if str(report.get("reverse_causality_risk", "")).lower() == "high" and verdict == "pass":
            verdict = "reduce"
            flags.append("high_reverse_causality_risk")
        return {
            "verdict": verdict,
            "size_multiplier": multiplier,
            "risk_flags": flags,
            "reasons": reasons,
        }

    @staticmethod
    def _most_restrictive_assessment(*assessments: Mapping[str, Any]) -> Dict[str, Any]:
        """Combine LLM risk views without allowing one view to erase another."""
        rank = {"pass": 0, "reduce": 1, "hold": 2}
        chosen = "pass"
        multiplier = 1.0
        flags: list[str] = []
        reasons: list[str] = []
        for assessment in assessments:
            verdict = str(assessment.get("verdict", "hold"))
            if rank.get(verdict, 2) > rank.get(chosen, 0):
                chosen = verdict if verdict in rank else "hold"
            try:
                multiplier = min(multiplier, max(0.0, float(assessment.get("size_multiplier", 1.0))))
            except (TypeError, ValueError):
                chosen = "hold"
                multiplier = 0.0
            for flag in assessment.get("risk_flags", []) or []:
                text = str(flag).strip()
                if text and text not in flags:
                    flags.append(text)
            source_reasons = assessment.get("reasons", [])
            if isinstance(source_reasons, str):
                source_reasons = [source_reasons]
            for reason in source_reasons or []:
                text = str(reason).strip()
                if text and text not in reasons:
                    reasons.append(text)
        if chosen == "hold":
            multiplier = 0.0
        return {"verdict": chosen, "size_multiplier": multiplier, "risk_flags": flags, "reasons": reasons}

    def _base_context(
        self,
        event: Any,
        candidate_symbols: Sequence[str],
        symbol_infos: Sequence[Mapping[str, Any]],
        run_id: str,
        research_packets: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        cutoff = utc_now()
        query = f"{getattr(event, 'theme', '')} {getattr(event, 'question', '')} {' '.join(candidate_symbols[:10])}"
        try:
            memories = self.memory.retrieve(query, limit=max(1, int(self.settings.nvidia_nemotron_memory_top_k)))
        except Exception:
            memories = []
        return {
            "run_id": run_id,
            "decision_time_utc": cutoff,
            "data_cutoff_utc": cutoff,
            "event": {
                "event_id": getattr(event, "event_id", ""),
                "theme": getattr(event, "theme", ""),
                "question": getattr(event, "question", ""),
            },
            "candidate_symbols": list(candidate_symbols)[:80],
            "symbol_infos": [dict(info) for info in symbol_infos[:80]],
            # Packets are constructed before the agent run from the exact
            # OHLCV/fundamental snapshots used by Python's later rules.  They
            # make absent news/chart data explicit rather than inviting a
            # model to infer it from a ticker.
            "research_packets": _trim_json_context(dict(research_packets or {}), limit=250_000),
            "input_capabilities": {
                "chart_images": False,
                "indicator_packet": True,
                "ohlcv_last_8_weeks": True,
                "portfolio_positions": False,
                "broker_order_access": False,
                "live_execution_access": False,
            },
            "retrieved_memories": memories,
            "reports": {},
        }

    def _run_pipeline(self, pipeline_name: str, context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        reports: Dict[str, Dict[str, Any]] = {}
        requested = self.registry.get("pipelines", {}).get(pipeline_name, [])
        if not isinstance(requested, list):
            return reports
        requested_ids = {str(agent_id) for agent_id in requested if isinstance(agent_id, str)}
        limit = max(0, int(self.settings.nvidia_nemotron_max_agents_per_run))
        for agent_id in requested[:limit]:
            if not isinstance(agent_id, str):
                continue
            entry = self.entries.get(agent_id, {})
            dependencies: Any = []
            if isinstance(entry, Mapping) and entry.get("path"):
                prompt = self.loader.load(str(entry["path"]))
                dependencies = prompt.metadata.get("depends_on", entry.get("depends_on", []))
            if not isinstance(dependencies, list):
                dependencies = []
            # Prompt metadata is authoritative. Dependencies belonging to another
            # pipeline (for example offline jobs that consume a prior live run)
            # are external prerequisites and are not awaited in this invocation.
            dependencies = [str(dependency) for dependency in dependencies if str(dependency) in requested_ids]
            missing = [
                dependency
                for dependency in dependencies
                if not isinstance(reports.get(str(dependency)), Mapping)
                or reports[str(dependency)].get("status") != "ok"
            ]
            if missing:
                reports[agent_id] = {
                    "status": "blocked",
                    "agent_id": agent_id,
                    "error": "required_dependency_unavailable",
                    "missing_dependencies": missing,
                }
                self.audit.append_event(
                    "agent_turn",
                    run_id=str(context["run_id"]),
                    agent_id=agent_id,
                    status="blocked",
                    reason="required_dependency_unavailable",
                    missing_dependencies=missing,
                )
                continue
            context["reports"] = _trim_json_context(reports)
            report = self._run_agent(agent_id=agent_id, run_id=str(context["run_id"]), task_context=context)
            reports[agent_id] = report
        return reports

    @staticmethod
    def _validate_live_report(
        report: Mapping[str, Any] | None,
        *,
        data_cutoff_utc: str,
        candidate_symbols: Sequence[str],
        permitted_evidence_ids: set[str],
    ) -> tuple[bool, str]:
        """Validate the strict 02--11 live-report boundary before a BUY."""

        if not isinstance(report, Mapping) or report.get("status") != "ok":
            return False, "report_not_ok"
        if str(report.get("data_cutoff_utc") or "") != str(data_cutoff_utc):
            return False, "data_cutoff_mismatch"
        if report.get("requires_python_risk_gate") is not True:
            return False, "python_risk_gate_not_required"
        symbol = report.get("symbol")
        if symbol not in (None, "") and str(symbol) not in {str(item) for item in candidate_symbols}:
            return False, "symbol_outside_candidate_universe"
        evidence = report.get("evidence_ids")
        if not isinstance(evidence, list) or not evidence:
            return False, "evidence_ids_missing"
        if any(str(item) not in permitted_evidence_ids for item in evidence):
            return False, "evidence_id_not_in_snapshot"
        return True, "ok"

    def _unavailable_result(self, reason: str, *, run_id: str | None = None) -> Dict[str, Any]:
        return {
            "yes_probability": 0.5,
            "confidence": 0.0,
            "news_score": 0.0,
            "buy_allowed": False,
            "size_multiplier": 0.0,
            "block_reason": reason,
            "agentic_analysis": {"run_id": run_id, "status": "unavailable", "reason": reason},
            "google_evidence": {"status": "unavailable", "reason": reason},
            "nvidia_judgement": {"status": "unavailable", "reason": reason},
            "api_usage": self.usage_snapshot(),
        }

    def analyze_event(
        self,
        event: Any,
        candidate_symbols: Sequence[str],
        symbol_infos: Sequence[Mapping[str, Any]],
        research_packets: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Return a normalized event score without ever creating an order."""
        enabled, reason = self._enabled()
        run_id = uuid.uuid4().hex
        if not enabled:
            self.audit.append_event("agent_run", run_id=run_id, status="unavailable", reason=reason)
            return self._unavailable_result(reason, run_id=run_id)

        context = self._base_context(event, candidate_symbols, symbol_infos, run_id, research_packets)
        pipeline_name = str(getattr(self.settings, "nvidia_nemotron_pipeline", "event_research") or "event_research")
        reports = self._run_pipeline(pipeline_name, context)
        is_live_pipeline = pipeline_name == "live_event_research"
        manager_key = "live_portfolio_manager" if is_live_pipeline else "portfolio_manager"
        trader_key = "live_trader" if is_live_pipeline else "trader"
        risk_key = "live_risk_manager" if is_live_pipeline else "risk_manager"
        semantic_key = "live_semantic_risk_filter" if is_live_pipeline else "semantic_risk_filter"
        manager_report = reports.get(manager_key)
        trader_report = reports.get(trader_key)
        risk_report = reports.get(risk_key)
        semantic_report = reports.get(semantic_key)

        if is_live_pipeline:
            permitted_evidence_ids: set[str] = set()
            packet_container: Mapping[str, Any] = research_packets or {}
            
            # Base sources and dynamic standard evidence formats for symbols
            symbols_container = packet_container.get("symbols") or {} if isinstance(packet_container.get("symbols"), Mapping) else packet_container
            for symbol, packet in symbols_container.items():
                if not isinstance(packet, Mapping):
                    continue
                # 1. Base sources listed in sources list
                for source in packet.get("sources", []) or []:
                    if isinstance(source, Mapping) and source.get("evidence_id"):
                        permitted_evidence_ids.add(str(source["evidence_id"]))
                
                # 2. Dynamic standard evidence formats for symbols in this event
                permitted_evidence_ids.add(f"price:{symbol}")
                permitted_evidence_ids.add(f"ohlcv:{symbol}")
                permitted_evidence_ids.add(f"fundamentals:{symbol}")
                permitted_evidence_ids.add(f"technical_indicators:{symbol}")
                permitted_evidence_ids.add(f"technical_rules:{symbol}")
                permitted_evidence_ids.add(f"market_features:{symbol}")
                
            # General non-symbol specific formats
            permitted_evidence_ids.add("technical_indicators")
            permitted_evidence_ids.add("technical_rules")
            permitted_evidence_ids.add("market_features")
            permitted_evidence_ids.add("lead_lag")
            
            # Lead-lag relations from candidates
            lead_lag_data = packet_container.get("lead_lag") or {}
            if isinstance(lead_lag_data, Mapping):
                candidates = lead_lag_data.get("candidates") or []
                if isinstance(candidates, list):
                    for cand in candidates:
                        if isinstance(cand, Mapping) and "leader" in cand and "follower" in cand:
                            permitted_evidence_ids.add(f"lead_lag:{cand['leader']}->{cand['follower']}")
            report_validation = {
                name: self._validate_live_report(
                    report,
                    data_cutoff_utc=str(context["data_cutoff_utc"]),
                    candidate_symbols=candidate_symbols,
                    permitted_evidence_ids=permitted_evidence_ids,
                )
                for name, report in {
                    manager_key: manager_report,
                    trader_key: trader_report,
                    risk_key: risk_report,
                    semantic_key: semantic_report,
                }.items()
            }
            reports["_live_contract_validation"] = {
                name: {"ok": ok, "reason": reason} for name, (ok, reason) in report_validation.items()
            }
            live_reports_ok = all(ok for ok, _ in report_validation.values())
            selected_by_trader = str((trader_report or {}).get("symbol") or "")
            selected_by_manager = str((manager_report or {}).get("symbol") or "")
            symbol_agreement = bool(
                selected_by_trader
                and selected_by_trader == selected_by_manager
                and selected_by_trader in {str(item) for item in candidate_symbols}
            )
            manager_decision = self._as_decision(manager_report) if live_reports_ok and symbol_agreement else None
            trader_decision = self._as_decision(trader_report) if live_reports_ok else None
            decision = manager_decision
        else:
            manager_decision = self._as_decision(manager_report)
            trader_decision = self._as_decision(trader_report)
            decision = manager_decision or trader_decision
            report_validation = {}
            live_reports_ok = True
            symbol_agreement = True

        semantic_raw = self._semantic_assessment(semantic_report)
        risk_manager_raw = self._semantic_assessment(risk_report)
        combined_risk = self._most_restrictive_assessment(risk_manager_raw, semantic_raw)
        semantic_gate = apply_semantic_risk_assessment(combined_risk, default_allowed=decision is not None)

        if decision is None:
            reason = "final_agent_decision_invalid_or_unavailable"
            if is_live_pipeline and not live_reports_ok:
                reason = "live_agent_contract_validation_failed"
            elif is_live_pipeline and not symbol_agreement:
                reason = "live_agent_symbol_mismatch"
            result = self._unavailable_result(reason, run_id=run_id)
            result["agentic_analysis"] = {
                "run_id": run_id,
                "reports": reports,
                "semantic_gate": semantic_gate.to_dict(),
                "live_contract_validation": reports.get("_live_contract_validation", {}),
            }
            self.audit.append_event("agent_run", run_id=run_id, status="blocked", reason=result["block_reason"], reports=reports)
            return result

        decision_dict = decision.to_dict()
        exposure = decision.target_exposure
        probability = _clip(
            (manager_report or {}).get("yes_probability", 0.5 + 0.30 * exposure),
            0.01,
            0.99,
            0.5,
        )
        sentiment = reports.get("sentiment_analyst", {})
        news_score = _clip(
            0.0 if is_live_pipeline else sentiment.get("sentiment_score", sentiment.get("score", exposure)),
            -1.0,
            1.0,
            0.0 if is_live_pipeline else exposure,
        )
        adjusted_confidence = decision.confidence * semantic_gate.size_multiplier
        # An LLM hold/reject only blocks new buys.  Existing Python exit rules
        # remain free to reduce risk from an already-open position.
        buy_allowed = bool(
            decision.direction == "buy"
            and semantic_gate.allowed
            and (manager_report or {}).get("approved_for_python_risk_gate") is True
        )
        result = {
            "yes_probability": probability,
            "confidence": _clip(adjusted_confidence, 0.0, 1.0, 0.0),
            "news_score": news_score,
            "predicted_return_bin": str(reports.get("technical_vision_analyst", {}).get("return_bin", "unknown")),
            "reasoning_signals": {
                "fact_direction_hint": reports.get("fact_reasoner", {}).get("direction_hint", "hold"),
                "fact_confidence": reports.get("fact_reasoner", {}).get("confidence"),
                "subjectivity_direction_hint": reports.get("subjectivity_reasoner", {}).get("direction_hint", "hold"),
                "subjectivity_confidence": reports.get("subjectivity_reasoner", {}).get("confidence"),
            },
            "buy_allowed": buy_allowed,
            "size_multiplier": semantic_gate.size_multiplier,
            "block_reason": "; ".join(semantic_gate.reasons),
            "agentic_analysis": {
                "run_id": run_id,
                "status": "ok",
                "decision": decision_dict,
                "semantic_gate": semantic_gate.to_dict(),
                "reports": reports,
                "live_contract_validation": reports.get("_live_contract_validation", {}),
            },
            # Preserve dashboard/log compatibility while making the source
            # explicit: these are NVIDIA Nemotron reports, not Google/NVIDIA turns.
            "google_evidence": reports.get("news_analyst", {}),
            "nvidia_judgement": decision_dict,
            "api_usage": self.usage_snapshot(),
        }
        # The event-level agent must nominate a concrete symbol before its BUY
        # proposal can be applied to a per-symbol order loop.  A missing or
        # out-of-universe symbol is retained in the audit record but grants no
        # executable buy target.  Existing deterministic sell exits do not
        # depend on this field.
        selected_symbol = str(
            (manager_report or {}).get("symbol")
            or (trader_report or {}).get("symbol")
            or ""
        ).strip()
        selected_symbols = [selected_symbol] if selected_symbol in {str(item) for item in candidate_symbols} else []
        result["selected_symbols"] = selected_symbols
        result["selected_symbol_reason"] = (
            "agent_selected_symbol" if selected_symbols else "agent_did_not_select_valid_candidate_symbol"
        )
        result["agentic_analysis"]["selected_symbols"] = selected_symbols
        self.audit.append_event(
            "agent_run",
            run_id=run_id,
            status="ok",
            event_id=getattr(event, "event_id", ""),
            decision_time_utc=context["decision_time_utc"],
            data_cutoff_utc=context["data_cutoff_utc"],
            result=result,
        )
        memory_tags = [str(getattr(event, "theme", "")).strip(), *[str(symbol).strip() for symbol in candidate_symbols[:5]]]
        self.memory.append_note(
            f"{getattr(event, 'question', '')} | {decision.direction} exposure={decision.target_exposure:.3f} | "
            f"confidence={decision.confidence:.3f}",
            tags=[tag for tag in memory_tags if tag],
            record_type="decision",
            run_id=run_id,
            event_id=getattr(event, "event_id", ""),
            data_cutoff_utc=context["data_cutoff_utc"],
            decision=decision_dict,
            semantic_gate=semantic_gate.to_dict(),
        )
        return result

    def run_post_decision(
        self,
        decision_context: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """Run the 09 reflection/memory roles after an outcome is known.

        This method is intentionally not called by the order loop because an
        outcome window (1d/5d/20d, fill price, and costs) is not known at order
        creation time.  A scheduled evaluator can pass those immutable records
        here without rewriting the original decision memory.
        """
        context = {
            **dict(decision_context),
            "outcome": _trim_json_context(outcome),
            "run_id": str(decision_context.get("run_id") or uuid.uuid4().hex),
            "decision_time_utc": decision_context.get("decision_time_utc", utc_now()),
            "data_cutoff_utc": decision_context.get("data_cutoff_utc", utc_now()),
            "reports": _trim_json_context(decision_context.get("reports", {})),
        }
        reports = self._run_pipeline("post_decision", context)
        memory_report = reports.get("memory_curator", {})
        note = memory_report.get("note") if isinstance(memory_report, Mapping) else None
        persisted_links: list[Dict[str, Any]] = []
        if isinstance(note, Mapping):
            observation = str(note.get("observation", "")).strip()
            hypothesis = str(note.get("hypothesis", "")).strip()
            content = " | ".join(part for part in (observation, hypothesis) if part)
            if content:
                persisted_note = self.memory.append_note(
                    content,
                    tags=[str(tag) for tag in note.get("tags", []) if str(tag).strip()],
                    record_type="reflection_memory",
                    run_id=context["run_id"],
                    outcome=_trim_json_context(outcome),
                    curator_report=_compact(memory_report),
                    provenance=_compact(memory_report.get("provenance", {})),
                    candidate_links=_compact(memory_report.get("candidate_links", [])),
                    memory_action=memory_report.get("memory_action", "create"),
                    version_reason=memory_report.get("version_reason"),
                )
                # Links remain separate, append-only records.  Unrecognised
                # IDs from an LLM are ignored rather than becoming invented
                # memory facts.
                for candidate in memory_report.get("candidate_links", []) or []:
                    if not isinstance(candidate, Mapping):
                        continue
                    target_id = str(candidate.get("memory_id", "")).strip()
                    try:
                        persisted_links.append(
                            self.memory.append_link(
                                persisted_note["memory_id"],
                                target_id,
                                relation=str(candidate.get("relation", "related")),
                                reason=str(candidate.get("reason", "")),
                                confidence=_clip(memory_report.get("confidence", 0.0), 0.0, 1.0, 0.0),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
        self.audit.append_event(
            "post_decision_learning",
            run_id=context["run_id"],
            outcome=_trim_json_context(outcome),
            reports=reports,
            persisted_memory_links=persisted_links,
        )
        return reports

    def run_offline_experiments(self, context: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Run 05/08/11 planning agents only when an explicit caller asks."""
        run_context = dict(context)
        run_context.setdefault("run_id", uuid.uuid4().hex)
        run_context.setdefault("decision_time_utc", utc_now())
        run_context.setdefault("data_cutoff_utc", run_context["decision_time_utc"])
        run_context.setdefault("reports", {})
        reports = self._run_pipeline("offline_experiments", run_context)

        # 08: hypotheses are persisted inactive.  A separate statistical
        # validation job must call Hyperedge.validate before an edge is usable.
        teacher = reports.get("distillation_teacher", {})
        if isinstance(teacher, Mapping):
            for candidate in teacher.get("candidate_hyperedges", []) or []:
                if not isinstance(candidate, Mapping):
                    continue
                edge_id = str(candidate.get("edge_id", "")).strip()
                nodes = [str(node) for node in candidate.get("nodes", []) if str(node).strip()]
                if edge_id and len(nodes) >= 2:
                    for node_id in nodes:
                        self.hypergraph.append_node(
                            GraphNode(
                                node_id=node_id,
                                node_type="symbol_or_indicator",
                                label=node_id,
                                observed_at=str(run_context["data_cutoff_utc"]),
                                metadata={"candidate_edge_id": edge_id, "validation": candidate.get("validation", "hypothesis")},
                            )
                        )
                    self.hypergraph.append(
                        Hyperedge(
                            edge_id=edge_id,
                            node_ids=nodes,
                            direction=str(candidate.get("direction", "hypothesis")),
                            rationale=str(candidate.get("rationale", "LLM hypothesis; validation pending")),
                            evidence_ids=[str(item) for item in candidate.get("evidence_ids", []) if str(item).strip()],
                            active=False,
                            valid_from=str(run_context["data_cutoff_utc"]),
                        )
                    )

            # A Teacher plan can emit many examples.  Only persist labels that
            # contain the numeric decision fields required for later training;
            # incomplete ideas remain in the audit record rather than becoming
            # accidental training data.
            for example in teacher.get("teacher_examples", []) or []:
                if not isinstance(example, Mapping):
                    continue
                label = {
                    "run_id": str(example.get("run_id") or run_context["run_id"]),
                    "data_cutoff_utc": run_context["data_cutoff_utc"],
                    "target_exposure": example.get("target_exposure"),
                    "confidence": example.get("confidence"),
                    "teacher_hypothesis": example.get("teacher_hypothesis", ""),
                    "input_snapshot_hash": example.get("input_snapshot_hash", ""),
                    "evidence_ids": example.get("evidence_ids", []),
                    "label_status": example.get("label_status", "pending"),
                    "realized_return": None,
                }
                if label["target_exposure"] is None or label["confidence"] is None:
                    continue
                try:
                    self.teacher_labels.append(label)
                except ValueError:
                    # The audit log retains the invalid Teacher response; do
                    # not make it eligible for a Student training set.
                    continue

        self.audit.append_event(
            "offline_agent_experiment",
            run_id=run_context["run_id"],
            reports=reports,
        )
        return reports

