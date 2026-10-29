"""Synchronous routes run in Starlette's threadpool; no network in DB transactions."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from review_api.db import schema_ready
from review_api.http_boundary import APIError
from review_api.schemas import (SubmitReview, SubmissionResponse, TaskResponse, TaskPage,
    ResultResponse, EventPage, ListQuery, EventQuery)
from review_api.storage_types import TaskSnapshot

router = APIRouter()


@router.get('/health/live')
def live():
    return {'status': 'live'}


@router.get('/health/ready')
def ready(request: Request):
    state = request.app.state
    worker = getattr(state, 'executor', None)
    if worker is None or not worker.ready:
        raise APIError(503, 'not_ready', 'Review service is not ready')
    try:
        matched = schema_ready(state.engine)
    except SQLAlchemyError:
        worker.storage_failed()
        raise APIError(503, 'not_ready', 'Review storage is unavailable') from None
    if not matched:
        worker.storage_failed()
        raise APIError(503, 'not_ready', 'Review service is not ready')
    return {'status': 'ready'}


@router.post('/api/v1/reviews', response_model=SubmissionResponse, status_code=202)
def submit(body: SubmitReview, request: Request, response: Response,
           idempotency_key: Annotated[UUID, Header()]):
    state = request.app.state
    if f'{body.owner}/{body.repo}' not in state.settings.allowed_repos:
        raise APIError(403, 'repository_forbidden', 'Repository is not allowed')
    # Replay precedes resolving current profiles, PR metadata, or capacity.
    task = state.repository.find_replay(idempotency_key, body)
    created = False
    if task is None:
        state.executor.require_ready()
        config = state.settings.profiles.get(body.profile)
        if config is None:
            raise APIError(422, 'invalid_profile', 'Unknown server budget profile')
        revision = state.provider.metadata(body)
        if body.expected_head_sha is not None and revision.head_sha != body.expected_head_sha:
            raise APIError(409, 'revision_conflict', 'PR head does not match the expected revision')
        snapshot = TaskSnapshot(head_sha=revision.head_sha, base_sha=revision.base_sha,
            config=config, prompt_id=state.prompt_id, source_id=state.source_id,
            model_id='mock-v1' if config.model_mode == 'mock' else 'deepseek-flash')
        task, created = state.executor.submit(idempotency_key, body, snapshot)
    response.status_code = 202 if created else 200
    url = f"/api/v1/reviews/{task['id']}"
    return {'task_id': task['id'], 'execution_status': task['execution_status'],
            'model_mode': task['config']['model_mode'],
            'head_sha': task['head_sha'], 'base_sha': task['base_sha'],
            'status_url': url, 'result_url': url + '/result', 'events_url': url + '/events'}


@router.get('/api/v1/reviews', response_model=TaskPage)
def list_tasks(request: Request, query: Annotated[ListQuery, Query()]):
    page = request.app.state.repository.list_tasks(**query.model_dump())
    return {'items': page.items, 'next_cursor': page.next_cursor}


@router.get('/api/v1/reviews/{task_id}', response_model=TaskResponse)
def task(task_id: UUID, request: Request):
    return request.app.state.repository.get_task(task_id)


@router.get('/api/v1/reviews/{task_id}/result', response_model=ResultResponse)
def result(task_id: UUID, request: Request):
    return request.app.state.repository.get_result(task_id)


@router.get('/api/v1/reviews/{task_id}/events', response_model=EventPage)
def events(task_id: UUID, request: Request, query: Annotated[EventQuery, Query()]):
    return request.app.state.repository.list_events(task_id, **query.model_dump())
