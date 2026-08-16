from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import (
    RetrievalDiagnosticLabelRequest,
    RetrievalDiagnosticRequest,
)
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    retrieval_scope_for_request,
    resolve_kb_scope,
)
from cogdoc.service.index_provenance import current_index_provenance
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.retrieval_diagnostics import run_retrieval_diagnostics
from cogdoc.service.retrieval_pipeline import build_retrieval_queries
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.eval.retrieval_eval_drafts import create_pending_draft


router = APIRouter(prefix="/v1/retrieval-diagnostics", tags=["retrieval-eval"])


def _run(body, *, request: Request, storage_id: str, scope):
    requirements = [item.model_dump(mode="json") for item in body.requirements]
    queries = build_retrieval_queries(
        body.query,
        evidence_requirements=requirements,
        max_queries=max(1, min(25, len(requirements) * 2 + 1)),
    )
    with kb_read_lease(storage_id):
        return run_retrieval_diagnostics(
            engine=RetrieverFactory.get_engine(storage_id),
            derived_knowledge_retriever=(
                request.app.state.state_runtime.derived_knowledge_retriever
            ),
            retrieval_feedback_store=(
                request.app.state.state_runtime.retrieval_feedback_store
            ),
            kb_id=storage_id,
            query=body.query,
            queries=queries,
            top_k=body.top_k,
            scope=scope,
            rerank=body.rerank,
            rerank_top_n=body.rerank_top_n,
            route_weights=body.route_weights,
            route_min_candidates=body.route_min_candidates,
            requirement_ids=[item.requirement_id for item in body.requirements],
        )


@router.post("")
async def diagnose_retrieval(
    body: RetrievalDiagnosticRequest,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    kb_scope = resolve_kb_scope(request, body.doc_id)
    if kb_scope is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    result = await run_sync(
        request.app.state.offload_executor,
        _run,
        body,
        request=request,
        storage_id=kb_scope.storage_id,
        scope=retrieval_scope_for_request(request, kb_scope),
    )
    return {
        "schema_version": "v1",
        "kb_id": body.doc_id,
        "query": body.query,
        **result,
    }


@router.post("/labels")
async def save_diagnostic_label(
    body: RetrievalDiagnosticLabelRequest,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    kb_scope = resolve_kb_scope(request, body.doc_id)
    if kb_scope is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    store = getattr(request.app.state, "retrieval_eval_draft_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="证据评测草稿存储不可用")
    provenance = current_index_provenance(kb_scope.storage_id)
    acceptable = [item.model_dump(mode="json") for item in body.acceptable_evidence]
    negatives = [
        item.model_dump(mode="json") for item in body.hard_negative_evidence
    ]
    draft = create_pending_draft(
        kb_id=kb_scope.storage_id,
        query=body.query,
        no_answer=body.no_answer,
        units=[
            {
                "unit_id": body.requirement_id,
                "task_kind": "qa_requirement",
                "label": body.requirement_label or body.query,
                "retrieval_query": body.query,
                "expected_status": "no_evidence" if body.no_answer else "supported",
                "acceptable_evidence": acceptable,
                "hard_negative_chunks": negatives,
            }
        ],
        **provenance,
    )
    row = store.ensure(draft)
    return {
        "schema_version": "v1",
        "draft": externalize_kb_fields(row, request),
    }
