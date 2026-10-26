"""Short PostgreSQL transactions for task admission, results and queries.

No network/model work belongs inside these methods. The executor owns lifecycle
and sink adaptation; callers must not retry an uncertain write as a new model run.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from review_checkpoint import FileCheckpoint
from review_models import ReviewIssue
from review_pipeline import FileProblem, ReviewBatch, finish_review_batch
from review_api.models import ReviewTask, ReviewFile, ReviewFinding, ReviewEventRecord
from review_api.storage_types import ReviewSubmission, TaskSnapshot, StoredEvent, validate_path

# Transaction-scoped admission/claim lock, not the executor's session-level lock.
ADMISSION_LOCK = 442001
TERMINAL = {'finished', 'failed', 'interrupted'}
ERROR_CODES = {'persistence_failed', 'service_failed', 'shutdown', 'interrupted', 'executor_lost', 'upstream_failed'}


class TaskNotFound(LookupError):
    pass


class IdempotencyConflict(ValueError):
    pass


class QueueFull(RuntimeError):
    pass


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_cursor: str | None


def now():
    return datetime.now(timezone.utc)


def task_view(task):
    values = {column.name: deepcopy(getattr(task, column.name)) for column in ReviewTask.__table__.columns}
    # Internal request fingerprints/keys do not belong in query responses.
    values.pop('idempotency_key')
    values.pop('request_fingerprint')
    values['partial'] = task.execution_status != 'finished'
    return values


def encode_cursor(task):
    payload = json.dumps([task.created_at.isoformat(), str(task.id)], separators=(',', ':'))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(value):
    try:
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError()
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
        stamp, identity = json.loads(raw)
        stamp = datetime.fromisoformat(stamp)
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp, UUID(identity)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('Invalid task cursor') from None


class ReviewRepository:
    def __init__(self, sessions, *, waiting_limit=8):
        if type(waiting_limit) is not int or waiting_limit < 1:
            raise ValueError('Waiting limit must be positive')
        self.sessions = sessions
        self.waiting_limit = waiting_limit

    @staticmethod
    def _admission_lock(session):
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': ADMISSION_LOCK})

    @staticmethod
    def _replay(task, fingerprint):
        if task.request_fingerprint != fingerprint:
            raise IdempotencyConflict('Idempotency key was used for a different request')
        return task_view(task)

    def find_replay(self, key: UUID, request: ReviewSubmission):
        """Call BEFORE reading GitHub metadata or resolving new server config."""
        with self.sessions() as session:
            task = session.scalar(select(ReviewTask).where(ReviewTask.idempotency_key == key))
            return self._replay(task, request.fingerprint()) if task else None

    def submit(self, key: UUID, request: ReviewSubmission, snapshot: TaskSnapshot, *, admission_check=None):
        if not isinstance(key, UUID):
            raise ValueError('Idempotency key must be a UUID')
        fingerprint = request.fingerprint()
        with self.sessions.begin() as session:
            self._admission_lock(session)
            if admission_check is not None:
                admission_check()
            old = session.scalar(select(ReviewTask).where(ReviewTask.idempotency_key == key))
            if old is not None:
                return self._replay(old, fingerprint), False
            if request.expected_head_sha is not None and request.expected_head_sha != snapshot.head_sha:
                raise IdempotencyConflict('PR head does not match the expected revision')
            waiting = session.scalar(select(func.count()).select_from(ReviewTask).where(ReviewTask.execution_status == 'queued'))
            if waiting >= self.waiting_limit:
                raise QueueFull('Review waiting queue is full')
            task_id = uuid4()
            values = dict(id=task_id, idempotency_key=key, request_fingerprint=fingerprint,
                owner=request.owner, repo=request.repo, pr_number=request.pr_number, profile=request.profile,
                base_sha=snapshot.base_sha, head_sha=snapshot.head_sha,
                config=snapshot.config.model_dump(), prompt_id=snapshot.prompt_id,
                source_id=snapshot.source_id, model_id=snapshot.model_id)
            inserted = session.scalar(insert(ReviewTask).values(**values)
                .on_conflict_do_nothing(index_elements=['idempotency_key']).returning(ReviewTask.id))
            if inserted is None:
                old = session.scalar(select(ReviewTask).where(ReviewTask.idempotency_key == key))
                return self._replay(old, fingerprint), False
            return task_view(session.get(ReviewTask, task_id)), True

    def claim_next(self):
        with self.sessions.begin() as session:
            self._admission_lock(session)
            if session.scalar(select(ReviewTask.id).where(ReviewTask.execution_status == 'running').limit(1)):
                return None
            task = session.scalar(select(ReviewTask).where(ReviewTask.execution_status == 'queued')
                .order_by(ReviewTask.created_at, ReviewTask.id).with_for_update(skip_locked=True).limit(1))
            if task is None:
                return None
            task.execution_status, task.started_at = 'running', now()
            session.flush()
            return task_view(task)

    @staticmethod
    def _task(session, task_id, *, running=False, lock=False):
        query = select(ReviewTask).where(ReviewTask.id == task_id)
        task = session.scalar(query.with_for_update() if lock else query)
        if task is None:
            raise TaskNotFound('Review task was not found')
        if running and task.execution_status != 'running':
            raise InvalidTransition('Task must be running')
        return task

    @staticmethod
    def _event(session, task, event: StoredEvent):
        task.last_event_seq += 1
        session.add(ReviewEventRecord(task_id=task.id, seq=task.last_event_seq, **event.model_dump()))
        return task.last_event_seq

    def append_event(self, task_id: UUID, event):
        validated = StoredEvent.from_event(event)
        if validated.node in {'review_file', 'skip_file', 'exclude_file'}:
            raise ValueError('File completion events must be submitted with a file checkpoint')
        with self.sessions.begin() as session:
            task = self._task(session, task_id, running=True, lock=True)
            return self._event(session, task, validated)

    def save_file(self, task_id: UUID, checkpoint: FileCheckpoint):
        path = validate_path(checkpoint.path)
        event = StoredEvent.from_event(checkpoint.event)
        expected = {'reviewed': ('review_file', 'ok'), 'failed': ('review_file', 'failed'),
                    'skipped': (None, 'skipped'), 'excluded': ('exclude_file', 'excluded')}
        if checkpoint.status not in expected:
            raise ValueError('Unknown file status')
        node, outcome = expected[checkpoint.status]
        valid_node = event.node == node if node else event.node in {'skip_file', 'review_file'}
        if not valid_node or event.outcome != outcome or event.path != path:
            raise ValueError('File completion event does not match its checkpoint')
        if checkpoint.status == 'reviewed':
            if checkpoint.reason_code is not None or event.termination_reason != 'completed':
                raise ValueError('Reviewed file cannot have a failure reason')
        elif (checkpoint.findings or checkpoint.reason_code is None
              or checkpoint.reason_code != event.termination_reason):
            raise ValueError('Incomplete file must have a safe reason and no findings')
        findings = [ReviewIssue.model_validate(item.model_dump()) for item in checkpoint.findings]
        if any(item.file != path for item in findings):
            raise ValueError('Finding path does not match file')
        with self.sessions.begin() as session:
            task = self._task(session, task_id, running=True, lock=True)
            for item in findings:
                if not item.evidence:
                    raise ValueError('Repository-mode finding requires verified evidence')
                for evidence in item.evidence:
                    validate_path(evidence.path)
                    if (evidence.repository.lower() != f'{task.owner}/{task.repo}'
                            or evidence.revision not in (task.head_sha, task.base_sha)):
                        raise ValueError('Finding evidence does not match task repository/revision')
            file_id = uuid4()
            session.add(ReviewFile(id=file_id, task_id=task.id, path=path, status=checkpoint.status,
                reason_code=checkpoint.reason_code, completed_at=event.timestamp, duration_ms=event.duration_ms))
            session.flush()
            for ordinal, item in enumerate(findings):
                session.add(ReviewFinding(file_id=file_id, ordinal=ordinal, title=item.title,
                    body=item.description, severity=item.severity, line=item.line, suggestion=item.suggestion,
                    evidence=[e.model_dump() for e in item.evidence]))
            seq = self._event(session, task, event)
            session.flush()
            return seq

    @staticmethod
    def _files(session, task_id):
        files = list(session.scalars(select(ReviewFile).where(ReviewFile.task_id == task_id)
                                    .order_by(ReviewFile.path)))
        findings = list(session.scalars(select(ReviewFinding).join(ReviewFile)
            .where(ReviewFile.task_id == task_id).order_by(ReviewFile.path, ReviewFinding.ordinal)))
        by_file = {file.id: [] for file in files}
        for finding in findings:
            by_file[finding.file_id].append(finding)
        result = []
        for file in files:
            issues = [ReviewIssue(file=file.path, severity=f.severity, line=f.line,
                title=f.title, description=f.body, suggestion=f.suggestion, evidence=f.evidence)
                for f in by_file[file.id]]
            result.append({'path': file.path, 'status': file.status, 'reason_code': file.reason_code,
                'completed_at': file.completed_at, 'duration_ms': file.duration_ms,
                'findings': [issue.model_dump() for issue in issues]})
        return result

    def finish(self, task_id: UUID, batch: ReviewBatch):
        with self.sessions.begin() as session:
            task = self._task(session, task_id, running=True, lock=True)
            files = self._files(session, task_id)
            counts = Counter(file['status'] for file in files)
            issues = [ReviewIssue.model_validate(issue) for file in files for issue in file['findings']]
            def problems(status):
                return [FileProblem(file['path'], file['reason_code']) for file in files if file['status'] == status]
            final = finish_review_batch(issues=issues, failures=problems('failed'), skipped=problems('skipped'),
                excluded=problems('excluded'), total_files=len(files), reviewed_files=counts['reviewed'])
            def issue_set(items):
                return Counter(json.dumps(i.model_dump(), sort_keys=True) for i in items)
            if ((batch.total_files, batch.reviewed_files, batch.status) !=
                    (final.total_files, final.reviewed_files, final.status)
                    or issue_set(batch.result.issues) != issue_set(final.result.issues)
                    or any(sorted(p.path for p in getattr(batch, field)) != sorted(p.path for p in getattr(final, field))
                           for field in ('failures', 'skipped', 'excluded'))):
                raise ValueError('Final batch does not match saved file checkpoints')
            task.execution_status, task.review_status = 'finished', final.status
            task.finished_at, task.summary = now(), final.result.summary
            task.total_files, task.reviewed_files = len(files), counts['reviewed']
            task.failed_files, task.skipped_files, task.excluded_files = counts['failed'], counts['skipped'], counts['excluded']
            session.flush()
            return task_view(task)

    def terminate(self, task_id: UUID, *, status='failed', error_code='service_failed'):
        if status not in {'failed', 'interrupted'} or error_code not in ERROR_CODES:
            raise ValueError('Invalid terminal status or error code')
        with self.sessions.begin() as session:
            task = self._task(session, task_id, lock=True)
            if task.execution_status in TERMINAL:
                raise InvalidTransition('Terminal task cannot be changed')
            task.execution_status, task.error_code = status, error_code
            task.finished_at, task.resource_records_complete = now(), False
            session.flush()
            return task_view(task)

    def interrupt_pending(self, *, error_code='interrupted') -> int:
        """Only the executor owning the session lock may call this operation.

        Serialize with admission so a submission which passed the local readiness
        check cannot commit behind shutdown cleanup. Never retry/resume old work.
        """
        if error_code not in ERROR_CODES:
            raise ValueError('Invalid interruption code')
        with self.sessions.begin() as session:
            self._admission_lock(session)
            tasks = list(session.scalars(select(ReviewTask).where(
                ReviewTask.execution_status.in_(('queued', 'running'))).with_for_update()))
            stamp = now()
            for task in tasks:
                task.execution_status, task.error_code = 'interrupted', error_code
                task.finished_at, task.resource_records_complete = stamp, False
            return len(tasks)

    def get_task(self, task_id: UUID):
        with self.sessions() as session:
            return task_view(self._task(session, task_id))

    def get_result(self, task_id: UUID):
        # One MVCC snapshot prevents a final task row and older partial files (or
        # a running row and newer files) from being combined in one response.
        with self.sessions.begin() as session:
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            task = self._task(session, task_id)
            files = self._files(session, task_id)
            return {'task_id': task.id, 'partial': task.execution_status != 'finished',
                    'execution_status': task.execution_status, 'model_mode': task.config['model_mode'],
                    'resource_records_complete': task.resource_records_complete,
                    'review_status': task.review_status, 'head_sha': task.head_sha, 'base_sha': task.base_sha,
                    'summary': task.summary, 'files': files,
                    'saved_file_counts': dict(Counter(file['status'] for file in files))}

    def list_tasks(self, *, limit=20, cursor=None) -> Page:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Task limit must be between 1 and 100')
        boundary = decode_cursor(cursor) if cursor is not None else None
        with self.sessions() as session:
            query = select(ReviewTask).order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc())
            if boundary is not None:
                query = query.where(tuple_(ReviewTask.created_at, ReviewTask.id) < boundary)
            rows = list(session.scalars(query.limit(limit + 1)))
            return Page([task_view(task) for task in rows[:limit]],
                        encode_cursor(rows[limit - 1]) if len(rows) > limit else None)

    def list_events(self, task_id: UUID, *, after_seq=0, limit=100):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Invalid event cursor or limit')
        with self.sessions() as session:
            self._task(session, task_id)
            rows = list(session.scalars(select(ReviewEventRecord).where(
                ReviewEventRecord.task_id == task_id, ReviewEventRecord.seq > after_seq)
                .order_by(ReviewEventRecord.seq).limit(limit + 1)))
            items = [{column.name: deepcopy(getattr(row, column.name))
                      for column in ReviewEventRecord.__table__.columns} for row in rows[:limit]]
            return {'items': items, 'has_more': len(rows) > limit,
                    'next_after_seq': items[-1]['seq'] if items else after_seq}
