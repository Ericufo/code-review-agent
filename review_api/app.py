"""Application factory. Uvicorn: review_api.app:create_app --factory --workers 1."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from github_client import GitHubAPIError
from github_review import ReviewSupersededError
from review_api.db import create_database_engine, session_factory
from review_api.executor import ReviewExecutor, ExecutorNotReady, mock_context_request
from review_api.http_boundary import HTTPBoundary, APIError, error_response
from review_api.providers import GitHubProvider, LiveModelTransport, UpstreamTimeout
from review_api.repository import ReviewRepository, TaskNotFound, IdempotencyConflict, QueueFull
from review_api.routes import router
from review_api.settings import Settings, code_identifiers


def create_app(settings=None, *, engine=None, provider=None, mock_request_fn=mock_context_request):
    settings = settings or Settings.from_env()
    owned_engine = engine is None

    @asynccontextmanager
    async def lifespan(app):
        state = app.state
        state.engine = engine if engine is not None else create_database_engine(settings.database_url.get_secret_value())
        state.repository = ReviewRepository(session_factory(state.engine), waiting_limit=settings.waiting_limit)
        state.provider = provider if provider is not None else GitHubProvider(settings)
        state.prompt_id, state.source_id = code_identifiers()
        state.executor = ReviewExecutor(state.engine, state.provider.prepare, repository=state.repository,
            shutdown_grace=settings.shutdown_grace, mock_request_fn=mock_request_fn,
            allow_live=settings.model_mode == 'live',
            live_request_fn=LiveModelTransport(settings) if settings.model_mode == 'live' else None)
        try:
            await run_in_threadpool(state.executor.start)
            # Keep liveness available on startup failure; readiness and admission fail closed.
            yield
        finally:
            state.shutdown_complete = await run_in_threadpool(state.executor.stop)
            if state.shutdown_complete and owned_engine:
                await run_in_threadpool(state.engine.dispose)
            if not state.shutdown_complete:
                # Do not dispose the still-active worker's resources or start a replacement.
                # Uvicorn exits after failed lifespan shutdown; its worker is a daemon.
                raise RuntimeError('Review shutdown grace expired; process exit required')

    app = FastAPI(title='Review task API', version='4.4.4', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(HTTPBoundary, token=settings.api_token.get_secret_value())
    app.include_router(router)

    async def api_error(request: Request, exc: APIError):
        return error_response(request.scope, exc.status, exc.code, exc.message)
    app.add_exception_handler(APIError, api_error)

    def handler(status, code, message, *, storage_failure=False):
        async def respond(request, exc):
            if storage_failure and hasattr(request.app.state, 'executor'):
                request.app.state.executor.storage_failed()
            return error_response(request.scope, status, code, message)
        return respond
    for kind, status, code, message in (
        (RequestValidationError, 422, 'invalid_input', 'Request fields or JSON are invalid'),
        (TaskNotFound, 404, 'not_found', 'Review task was not found'),
        (IdempotencyConflict, 409, 'idempotency_conflict', 'Idempotency key or expected revision conflicts'),
        (ReviewSupersededError, 409, 'pr_not_reviewable', 'PR is closed, draft, or superseded'),
        (QueueFull, 429, 'queue_full', 'Review waiting queue is full'),
        (ExecutorNotReady, 503, 'not_ready', 'Review service is not ready'),
        (UpstreamTimeout, 504, 'upstream_timeout', 'GitHub metadata request timed out'),
        (GitHubAPIError, 502, 'upstream_failed', 'GitHub metadata is unavailable'),
        (ValueError, 422, 'invalid_input', 'Request fields or cursor are invalid'),
    ):
        app.add_exception_handler(kind, handler(status, code, message))
    app.add_exception_handler(SQLAlchemyError, handler(503, 'not_ready', 'Review storage is unavailable', storage_failure=True))

    async def http_error(request, exc):
        return error_response(request.scope, exc.status_code, 'http_error', 'HTTP request is not supported')
    app.add_exception_handler(HTTPException, http_error)
    return app
