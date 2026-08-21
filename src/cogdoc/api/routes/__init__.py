from cogdoc.api.routes.agent import router as agent_router
from cogdoc.api.routes.access import router as access_router
from cogdoc.api.routes.auth import router as auth_router
from cogdoc.api.routes.audit_exports import router as audit_exports_router
from cogdoc.api.routes.chat import router as chat_router
from cogdoc.api.routes.claim_verification import router as claim_verification_router
from cogdoc.api.routes.connector_credentials import (
    router as connector_credentials_router,
)
from cogdoc.api.routes.connector_oauth import router as connector_oauth_router
from cogdoc.api.routes.connections import router as connections_router
from cogdoc.api.routes.documents import router as documents_router
from cogdoc.api.routes.feedback import router as feedback_router
from cogdoc.api.routes.health import router as health_router
from cogdoc.api.routes.index_migrations import router as index_migrations_router
from cogdoc.api.routes.knowledge import router as knowledge_router
from cogdoc.api.routes.oidc import router as oidc_router
from cogdoc.api.routes.scim import router as scim_router
from cogdoc.api.routes.service_accounts import (
    policy_router as service_account_policy_router,
)
from cogdoc.api.routes.service_accounts import router as service_accounts_router
from cogdoc.api.routes.retrieval_eval_drafts import (
    router as retrieval_eval_drafts_router,
)
from cogdoc.api.routes.retrieval_diagnostics import (
    router as retrieval_diagnostics_router,
)
from cogdoc.api.routes.research import router as research_router
from cogdoc.api.routes.source_operations import router as source_operations_router
from cogdoc.api.routes.traces import router as traces_router

__all__ = [
    "agent_router",
    "access_router",
    "auth_router",
    "audit_exports_router",
    "chat_router",
    "claim_verification_router",
    "connector_credentials_router",
    "connector_oauth_router",
    "connections_router",
    "documents_router",
    "feedback_router",
    "health_router",
    "index_migrations_router",
    "knowledge_router",
    "oidc_router",
    "scim_router",
    "service_accounts_router",
    "service_account_policy_router",
    "retrieval_eval_drafts_router",
    "retrieval_diagnostics_router",
    "research_router",
    "source_operations_router",
    "traces_router",
]
