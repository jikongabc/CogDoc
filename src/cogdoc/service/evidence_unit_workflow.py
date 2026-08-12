from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cogdoc.agents.evidence_unit_verifier import (
    EvidenceUnitStructuredClient,
    EvidenceUnitVerificationBatchResult,
    EvidenceUnitVerificationResult,
    verify_evidence_unit_batch,
)
from cogdoc.graph.state import EvidenceLedgerEntry, RetrievedDoc
from cogdoc.service.evidence_unit_pipeline import (
    EvidenceUnitBatchResult,
    EvidenceUnitDerivedRetriever,
    EvidenceUnitExecutionResult,
    EvidenceUnitExecutionStatus,
    EvidenceUnitFeedbackStore,
    EvidenceUnitPipelinePolicy,
    EvidenceUnitRetrievalEngine,
    evidence_unit_plan_state,
    retrieve_evidence_units,
)
from cogdoc.service.evidence_unit_gate import (
    EvidenceUnitGateAction,
    EvidenceUnitGateBatchResult,
    EvidenceUnitGatePolicy,
    evaluate_evidence_unit_gate,
)
from cogdoc.service.evidence_unit_retry import (
    EvidenceUnitRetryRunner,
    retry_evidence_units,
)
from cogdoc.service.evidence_units import (
    EvidenceClosureStatus,
    EvidenceUnit,
    EvidenceUnitBudget,
    EvidenceUnitClosure,
)
from cogdoc.tools.evidence_rendering import evidence_block_char_count


EvidenceUnitBatchVerifier = Callable[
    [EvidenceUnitBatchResult], EvidenceUnitVerificationBatchResult
]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _evidence_id(doc: Mapping[str, Any]) -> str:
    return str(_mapping(doc.get("retrieval")).get("evidence_id") or "").strip()


def _closed_result(
    execution: EvidenceUnitExecutionResult,
    status: EvidenceClosureStatus,
    *,
    reason_code: str,
    error_class: str = "",
) -> EvidenceUnitVerificationResult:
    return EvidenceUnitVerificationResult(
        unit=execution.unit,
        status=status,
        closure=EvidenceUnitClosure(
            unit_id=execution.unit.unit_id,
            status=status,
            retrieval_round=execution.retrieval_round,
            reason_code=reason_code,
        ),
        candidate_evidence_ids=(
            tuple(_evidence_id(doc) for doc in execution.selected_docs)
            if execution.status is EvidenceUnitExecutionStatus.READY
            else ()
        ),
        reason_code=reason_code,
        error_class=error_class,
    )


def _safe_verify(
    batch: EvidenceUnitBatchResult,
    verifier: EvidenceUnitBatchVerifier,
) -> EvidenceUnitVerificationBatchResult:
    try:
        result = verifier(batch)
        expected = [execution.unit for execution in batch.results]
        actual = [outcome.unit for outcome in result.results]
        if actual != expected:
            raise ValueError("verifier results must preserve the exact batch order")
        return result
    except Exception as exc:
        status_by_execution = {
            EvidenceUnitExecutionStatus.READY: EvidenceClosureStatus.VERIFICATION_ERROR,
            EvidenceUnitExecutionStatus.NO_EVIDENCE: EvidenceClosureStatus.NO_EVIDENCE,
            EvidenceUnitExecutionStatus.RETRIEVAL_ERROR: (
                EvidenceClosureStatus.RETRIEVAL_ERROR
            ),
            EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED: (
                EvidenceClosureStatus.BUDGET_EXHAUSTED
            ),
        }
        outcomes = []
        for execution in batch.results:
            status = status_by_execution[execution.status]
            outcomes.append(
                _closed_result(
                    execution,
                    status,
                    reason_code=(
                        "verification_orchestration_error"
                        if status is EvidenceClosureStatus.VERIFICATION_ERROR
                        else execution.reason_code or status.value
                    ),
                    error_class=(
                        type(exc).__name__
                        if status is EvidenceClosureStatus.VERIFICATION_ERROR
                        else execution.error_class
                    ),
                )
            )
        return EvidenceUnitVerificationBatchResult(
            results=tuple(outcomes),
            error_class=type(exc).__name__,
        )


def _subset_batch(
    batch: EvidenceUnitBatchResult, unit_ids: Sequence[str]
) -> EvidenceUnitBatchResult:
    requested = set(unit_ids)
    results = tuple(
        result for result in batch.results if result.unit.unit_id in requested
    )
    evidence_ids = {
        _evidence_id(doc)
        for result in results
        if result.status is EvidenceUnitExecutionStatus.READY
        for doc in result.selected_docs
    }
    ledger = tuple(
        entry
        for entry in batch.evidence_ledger
        if str(entry.get("evidence_id") or "") in evidence_ids
    )
    return EvidenceUnitBatchResult(
        results=results,
        evidence_ledger=ledger,
        channel_counts=batch.channel_counts,
        ranking_count=batch.ranking_count,
        feedback_errors=batch.feedback_errors,
    )


def _merge_verification_round(
    first: EvidenceUnitVerificationBatchResult,
    second: EvidenceUnitVerificationBatchResult,
    retry_unit_ids: Sequence[str],
) -> EvidenceUnitVerificationBatchResult:
    retried = {result.unit.unit_id: result for result in second.results}
    retry_set = set(retry_unit_ids)
    if set(retried) != retry_set:
        raise ValueError("second verification must contain exactly the retried units")
    return EvidenceUnitVerificationBatchResult(
        results=tuple(
            retried.get(result.unit.unit_id, result) for result in first.results
        ),
        protocol_errors=tuple(
            dict.fromkeys((*first.protocol_errors, *second.protocol_errors))
        ),
        error_class=second.error_class or first.error_class,
    )


def _verification_contract_failure(
    batch: EvidenceUnitBatchResult,
    exc: Exception,
) -> EvidenceUnitVerificationBatchResult:
    status_by_execution = {
        EvidenceUnitExecutionStatus.READY: EvidenceClosureStatus.VERIFICATION_ERROR,
        EvidenceUnitExecutionStatus.NO_EVIDENCE: EvidenceClosureStatus.NO_EVIDENCE,
        EvidenceUnitExecutionStatus.RETRIEVAL_ERROR: EvidenceClosureStatus.RETRIEVAL_ERROR,
        EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED: (
            EvidenceClosureStatus.BUDGET_EXHAUSTED
        ),
    }
    return EvidenceUnitVerificationBatchResult(
        results=tuple(
            _closed_result(
                execution,
                status_by_execution[execution.status],
                reason_code=(
                    "verification_contract_error"
                    if execution.status is EvidenceUnitExecutionStatus.READY
                    else execution.reason_code
                    or status_by_execution[execution.status].value
                ),
                error_class=(
                    type(exc).__name__
                    if execution.status is EvidenceUnitExecutionStatus.READY
                    else execution.error_class
                ),
            )
            for execution in batch.results
        ),
        protocol_errors=("verification_contract_error",),
        error_class=type(exc).__name__,
    )


def _evaluate_verified_gate(
    batch: EvidenceUnitBatchResult,
    verification: EvidenceUnitVerificationBatchResult,
    *,
    policy: EvidenceUnitGatePolicy,
) -> tuple[EvidenceUnitVerificationBatchResult, EvidenceUnitGateBatchResult]:
    try:
        return verification, evaluate_evidence_unit_gate(
            batch,
            verification,
            policy=policy,
        )
    except Exception as exc:
        failed = _verification_contract_failure(batch, exc)
        return failed, evaluate_evidence_unit_gate(
            batch,
            failed,
            policy=policy,
        )


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceUnitBatchResult:
    execution: EvidenceUnitBatchResult
    verification: EvidenceUnitVerificationBatchResult | None = None
    gate: EvidenceUnitGateBatchResult | None = None
    retry_unit_ids: tuple[str, ...] = ()
    retry_history: tuple[tuple[str, ...], ...] = ()
    verification_rounds: int = 0

    @property
    def generation_unit_ids(self) -> tuple[str, ...]:
        if self.gate is None:
            return tuple(
                result.unit.unit_id
                for result in self.execution.results
                if result.status is EvidenceUnitExecutionStatus.READY
            )
        if (
            self.gate.policy.require_all_required_units
            and not self.gate.batch_can_generate
        ):
            return ()
        return self.gate.generate_unit_ids

    @property
    def grounded_docs(self) -> tuple[RetrievedDoc, ...]:
        generation_ids = set(self.generation_unit_ids)
        if self.verification is None:
            unverified_docs: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
            for execution in self.execution.results:
                if execution.unit.unit_id not in generation_ids:
                    continue
                for doc in execution.selected_docs:
                    evidence_id = _evidence_id(doc)
                    if evidence_id:
                        unverified_docs.setdefault(evidence_id, doc)
            return tuple(unverified_docs.values())
        executions = {
            result.unit.unit_id: result for result in self.execution.results
        }
        unique: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
        for outcome in self.verification.results:
            if outcome.unit.unit_id not in generation_ids:
                continue
            allowed = set(outcome.grounding_evidence_ids)
            for doc in executions[outcome.unit.unit_id].selected_docs:
                evidence_id = _evidence_id(doc)
                if evidence_id in allowed:
                    unique.setdefault(evidence_id, doc)
        return tuple(unique.values())

    @property
    def grounded_docs_by_source(self) -> dict[str, list[RetrievedDoc]]:
        grouped: dict[str, list[RetrievedDoc]] = {}
        for doc in self.grounded_docs:
            source = str(_mapping(doc.get("meta")).get("source") or "").strip()
            if source:
                grouped.setdefault(source, []).append(doc)
        return grouped

    @property
    def evidence_ledger(self) -> tuple[EvidenceLedgerEntry, ...]:
        if self.verification is None and self.gate is None:
            return self.execution.evidence_ledger
        grounded_ids = {_evidence_id(doc) for doc in self.grounded_docs}
        return tuple(
            entry
            for entry in self.execution.evidence_ledger
            if str(entry.get("evidence_id") or "") in grounded_ids
        )

    @property
    def metrics(self) -> dict[str, Any]:
        if self.verification is None:
            grounded = len(self.generation_unit_ids)
            planned = len(self.execution.results)
            return {
                **self.execution.metrics,
                "supported_count": 0,
                "contradictory_count": 0,
                "verification_error_count": 0,
                "protocol_error_count": 0,
                "verification_enabled": False,
                "verification_rounds": 0,
                "targeted_retry_count": sum(
                    len(item) for item in self.retry_history
                ),
                "targeted_retry_unit_count": len(self.retry_unit_ids),
                "targeted_retry_round_count": len(self.retry_history),
                "ready_count": grounded,
                "grounded_doc_count": len(self.grounded_docs),
                "grounded_chars": sum(
                    evidence_block_char_count(doc, str(doc.get("text") or ""))
                    for doc in self.grounded_docs
                ),
                "coverage_rate": grounded / planned if planned else 0.0,
                **(self.gate.metrics if self.gate is not None else {}),
            }
        grounded = len(self.generation_unit_ids)
        planned = len(self.verification.results)
        return {
            **self.execution.metrics,
            **self.verification.metrics,
            "verification_enabled": True,
            "verification_rounds": self.verification_rounds,
            "targeted_retry_count": sum(len(item) for item in self.retry_history),
            "targeted_retry_unit_count": len(self.retry_unit_ids),
            "targeted_retry_round_count": len(self.retry_history),
            "ready_count": grounded,
            "grounded_doc_count": len(self.grounded_docs),
            "grounded_chars": sum(
                evidence_block_char_count(doc, str(doc.get("text") or ""))
                for doc in self.grounded_docs
            ),
            "coverage_rate": grounded / planned if planned else 0.0,
            **(self.gate.metrics if self.gate is not None else {}),
        }

    def _state_results(self) -> list[dict[str, Any]]:
        gate_decisions = (
            {decision.unit_id: decision for decision in self.gate.decisions}
            if self.gate is not None
            else {}
        )
        generation_ids = set(self.generation_unit_ids)
        if self.verification is None:
            unverified_rows: list[dict[str, Any]] = []
            retry_ids = set(self.retry_unit_ids)
            for result in self.execution.results:
                row = result.to_state()
                decision = gate_decisions.get(result.unit.unit_id)
                if result.unit.unit_id not in generation_ids:
                    row.update(
                        {
                            "selected_docs": [],
                            "selected_count": 0,
                            "selected_chars": 0,
                        }
                    )
                row.update(
                    {
                        "gate_action": decision.action.value if decision else "",
                        "gate_reason_code": decision.reason_code if decision else "",
                        "retry_attempted": result.unit.unit_id in retry_ids,
                    }
                )
                unverified_rows.append(row)
            return unverified_rows
        outcomes = {
            result.unit.unit_id: result for result in self.verification.results
        }
        retry_ids = set(self.retry_unit_ids)
        rows: list[dict[str, Any]] = []
        for execution in self.execution.results:
            outcome = outcomes[execution.unit.unit_id]
            grounded_ids = (
                set(outcome.grounding_evidence_ids)
                if execution.unit.unit_id in generation_ids
                else set()
            )
            selected_docs = [
                doc
                for doc in execution.selected_docs
                if _evidence_id(doc) in grounded_ids
            ]
            row = execution.to_state()
            row.update(outcome.to_state())
            decision = gate_decisions.get(execution.unit.unit_id)
            row.update(
                {
                    "selected_docs": selected_docs,
                    "selected_count": len(selected_docs),
                    "selected_chars": sum(
                        evidence_block_char_count(doc, str(doc.get("text") or ""))
                        for doc in selected_docs
                    ),
                    "retry_attempted": execution.unit.unit_id in retry_ids,
                    "gate_action": decision.action.value if decision else "",
                    "gate_reason_code": decision.reason_code if decision else "",
                }
            )
            rows.append(row)
        return rows

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "evidence_units": [
                evidence_unit_plan_state(result.unit)
                for result in self.execution.results
            ],
            "evidence_unit_results": self._state_results(),
            "evidence_unit_metrics": self.metrics,
            "evidence_ledger": list(self.evidence_ledger),
            "evidence_unit_retry_history": [
                list(unit_ids) for unit_ids in self.retry_history
            ],
        }
        if self.verification is not None:
            state.update(self.verification.to_state())
        if self.gate is not None:
            state.update(self.gate.to_state())
        return state


def verify_and_retry_evidence_units(
    initial: EvidenceUnitBatchResult,
    *,
    budget: EvidenceUnitBudget,
    verifier: EvidenceUnitBatchVerifier | None,
    retry_runner: EvidenceUnitRetryRunner | None = None,
    gate_policy: EvidenceUnitGatePolicy | None = None,
) -> VerifiedEvidenceUnitBatchResult:
    """Verify a batch and retry only units semantically judged unsupported."""

    policy = gate_policy or EvidenceUnitGatePolicy(
        allow_unverified_ready=verifier is None,
        contradictory_action=EvidenceUnitGateAction.GENERATE,
        require_all_required_units=False,
    )

    def _unavailable_retry_runner(
        _units: tuple[EvidenceUnit, ...],
    ) -> EvidenceUnitBatchResult:
        raise RuntimeError("targeted retry runner is unavailable")

    if verifier is None:
        execution = initial
        gate = evaluate_evidence_unit_gate(execution, policy=policy)
        unverified_retry_history: list[tuple[str, ...]] = []
        unverified_retried_unit_ids: list[str] = []
        while gate.retry_unit_ids:
            retry_ids = gate.retry_unit_ids
            unverified_retry_history.append(retry_ids)
            for unit_id in retry_ids:
                if unit_id not in unverified_retried_unit_ids:
                    unverified_retried_unit_ids.append(unit_id)
            execution = retry_evidence_units(
                execution,
                retry_ids,
                budget=budget,
                runner=retry_runner or _unavailable_retry_runner,
            )
            gate = evaluate_evidence_unit_gate(execution, policy=policy)
        return VerifiedEvidenceUnitBatchResult(
            execution=execution,
            gate=gate,
            retry_unit_ids=tuple(unverified_retried_unit_ids),
            retry_history=tuple(unverified_retry_history),
        )

    execution = initial
    verification = _safe_verify(execution, verifier)
    verification, gate = _evaluate_verified_gate(
        execution,
        verification,
        policy=policy,
    )
    retry_history: list[tuple[str, ...]] = []
    retried_unit_ids: list[str] = []

    while gate.retry_unit_ids:
        retry_ids = gate.retry_unit_ids
        retry_history.append(retry_ids)
        for unit_id in retry_ids:
            if unit_id not in retried_unit_ids:
                retried_unit_ids.append(unit_id)
        execution = retry_evidence_units(
            execution,
            retry_ids,
            budget=budget,
            runner=retry_runner or _unavailable_retry_runner,
        )
        retry_batch = _subset_batch(execution, retry_ids)
        retry_verification = _safe_verify(retry_batch, verifier)
        verification = _merge_verification_round(
            verification,
            retry_verification,
            retry_ids,
        )
        verification, gate = _evaluate_verified_gate(
            execution,
            verification,
            policy=policy,
        )

    return VerifiedEvidenceUnitBatchResult(
        execution=execution,
        verification=verification,
        gate=gate,
        retry_unit_ids=tuple(retried_unit_ids),
        retry_history=tuple(retry_history),
        verification_rounds=1 + len(retry_history),
    )


def retrieve_verified_evidence_units(
    units: Sequence[EvidenceUnit],
    *,
    kb_id: str,
    original_query: str,
    engine: EvidenceUnitRetrievalEngine,
    derived_knowledge_retriever: EvidenceUnitDerivedRetriever,
    retrieval_feedback_store: EvidenceUnitFeedbackStore | None,
    budget: EvidenceUnitBudget,
    policy: EvidenceUnitPipelinePolicy,
    rrf_k: float,
    fallback_docs_by_source: Mapping[str, Sequence[RetrievedDoc]] | None = None,
    verification_enabled: bool = True,
    is_local: bool = False,
    max_chars_per_verification_doc: int = 1600,
    max_units_per_verification_batch: int = 8,
    structured_client: EvidenceUnitStructuredClient | None = None,
    gate_policy: EvidenceUnitGatePolicy | None = None,
    authorization_scope=None,
) -> VerifiedEvidenceUnitBatchResult:
    """Run the shared retrieval, closed-set verification, and retry workflow."""

    initial = retrieve_evidence_units(
        units,
        kb_id=kb_id,
        original_query=original_query,
        engine=engine,
        derived_knowledge_retriever=derived_knowledge_retriever,
        retrieval_feedback_store=retrieval_feedback_store,
        budget=budget,
        policy=policy,
        rrf_k=rrf_k,
        fallback_docs_by_source=fallback_docs_by_source,
        authorization_scope=authorization_scope,
    )

    batch_verifier: EvidenceUnitBatchVerifier | None = None
    if verification_enabled:

        def _verify(
            batch: EvidenceUnitBatchResult,
        ) -> EvidenceUnitVerificationBatchResult:
            return verify_evidence_unit_batch(
                batch,
                is_local=is_local,
                max_chars_per_doc=max_chars_per_verification_doc,
                max_units_per_batch=max_units_per_verification_batch,
                structured_client=structured_client,
            )

        batch_verifier = _verify

    def retry_runner(retry_units: tuple[EvidenceUnit, ...]) -> EvidenceUnitBatchResult:
        return retrieve_evidence_units(
            retry_units,
            kb_id=kb_id,
            original_query=original_query,
            engine=engine,
            derived_knowledge_retriever=derived_knowledge_retriever,
            retrieval_feedback_store=retrieval_feedback_store,
            budget=budget,
            policy=policy,
            rrf_k=rrf_k,
            fallback_docs_by_source=fallback_docs_by_source,
            query_phase="recovery",
            retrieval_round=1,
            authorization_scope=authorization_scope,
        )

    return verify_and_retry_evidence_units(
        initial,
        budget=budget,
        verifier=batch_verifier,
        retry_runner=retry_runner,
        gate_policy=gate_policy,
    )


__all__ = [
    "EvidenceUnitBatchVerifier",
    "VerifiedEvidenceUnitBatchResult",
    "retrieve_verified_evidence_units",
    "verify_and_retry_evidence_units",
]
