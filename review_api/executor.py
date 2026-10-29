"""Single-instance, single-thread task execution. No recovery or publication.

Construct explicitly, then start/stop from the application's lifecycle (off the
ASGI event loop). A dedicated PostgreSQL session owns the advisory lock; it is
never pooled or reconnected. Notifications only reduce the polling delay.
"""
from dataclasses import replace
import json
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace
from typing import Callable

import psycopg
from sqlalchemy.exc import SQLAlchemyError

from github_review import PullRequestRevision
from repository_context import RepositoryContext
from review_checkpoint import ReviewInterruptedError, ReviewPersistenceError, ReviewRunError
from review_service import PreparedReview, ReviewConfig, execute_review
from review_api.db import database_url, schema_ready, session_factory
from review_api.repository import InvalidTransition, ReviewRepository
from review_api.storage_types import StoredConfig

EXECUTOR_LOCK = 443001  # Separate from the transaction-scoped admission lock.


class ExecutorNotReady(RuntimeError):
    pass


class ExecutorLockLost(ReviewRunError):
    code = 'executor_lost'

    def __init__(self):
        super().__init__('Executor ownership was lost; execution stopped')


class ExecutorSessionLock:
    """Worker-thread-only connection, intentionally without reconnect/retry."""
    def __init__(self, url):
        self.url = database_url(url)
        self.connection = None
        self.backend_pid = None

    def acquire(self) -> bool:
        self.connection = psycopg.connect(
            self.url.set(drivername='postgresql').render_as_string(hide_password=False),
            autocommit=True, connect_timeout=3, options='-c statement_timeout=2000',
            keepalives=1, keepalives_idle=5, keepalives_interval=1, keepalives_count=2,
            tcp_user_timeout=5000)
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s), pg_backend_pid()', (EXECUTOR_LOCK,))
            acquired, self.backend_pid = cursor.fetchone()
        return acquired

    def check(self):
        try:
            if self.connection is None or self.connection.closed:
                raise ExecutorLockLost()
            # SELECT 1 alone would not detect an accidentally released lock.
            with self.connection.cursor() as cursor:
                cursor.execute("""SELECT EXISTS (
                    SELECT 1 FROM pg_locks WHERE locktype='advisory'
                    AND pid=pg_backend_pid() AND classid=0 AND objid=%s
                    AND objsubid=1 AND granted)""", (EXECUTOR_LOCK,))
                if not cursor.fetchone()[0]:
                    raise ExecutorLockLost()
        except Exception:
            raise ExecutorLockLost() from None

    def close(self):
        if self.connection is not None:
            # Closing the actual session releases all its advisory locks. Never
            # return this connection to a pool while still owning a session lock.
            self.connection.close()


def mock_context_request(inputs, instructions, *, tools_enabled=True):
    """Deterministic plumbing result, not evidence that a real PR is clean."""
    return SimpleNamespace(status='completed', output=[], usage=None,
        output_text=json.dumps({'summary': '', 'issues': [], 'context_complete': True, 'context_reason': ''}))


class GuardedContext(RepositoryContext):
    """Check ownership/shutdown before every source read, including search loops."""
    def __init__(self, delegate, before_work):
        self.repository = delegate.repository
        self.revisions = dict(delegate.revisions)
        self.local_root = None
        self._delegate, self._before_work = delegate, before_work

    def paths(self, version):
        self._before_work()
        return self._delegate.paths(version)

    def read(self, path, version):
        self._before_work()
        return self._delegate.read(path, version)

    def ensure_unchanged(self):
        self._before_work()
        self._delegate.ensure_unchanged()


class DatabaseReviewSink:
    """Formal sink adapter. A write failure poisons the entire executor."""
    def __init__(self, repository, task_id, *, before_write, failed):
        self.repository, self.task_id = repository, task_id
        self.before_write, self.failed = before_write, failed

    def _write(self, function, value):
        self.before_write()
        try:
            return function(self.task_id, value)
        except ReviewRunError:
            raise
        except Exception:
            self.failed('persistence_failed')
            raise ReviewPersistenceError() from None

    def record_event(self, event):
        self._write(self.repository.append_event, event)

    def file_completed(self, checkpoint):
        self._write(self.repository.save_file, checkpoint)

    def finish(self, batch):
        self._write(self.repository.finish, batch)


class ReviewExecutor:
    """Lifecycle owner for one worker; create a new instance after shutdown.

    prepare(task, before_work) must load only the saved revision and check the
    supplied boundary before every external request during preparation. The
    provider is trusted server wiring, never an HTTP-supplied path or callable.
    Live execution requires an explicitly supplied bounded transport. Defaults
    cannot fall back to the CLI's live model/environment configuration.
    """
    def __init__(self, engine, prepare: Callable, *, repository=None, poll_interval=0.25,
                 shutdown_grace=10.0, mock_request_fn=mock_context_request,
                 allow_live=False, live_request_fn=None):
        if not 0 < poll_interval <= 60 or not 0 <= shutdown_grace <= 60:
            raise ValueError('Invalid polling interval or shutdown grace')
        if allow_live and live_request_fn is None:
            raise ValueError('Live execution requires an explicit bounded model transport')
        self.engine, self.prepare = engine, prepare
        self.repository = repository or ReviewRepository(session_factory(engine))
        self.poll_interval, self.shutdown_grace = poll_interval, shutdown_grace
        self.mock_request_fn, self.live_request_fn = mock_request_fn, live_request_fn
        self.allow_live = allow_live
        self._ready, self._startup, self._stop, self._expired, self._abort, self._wake = (Event() for _ in range(6))
        self._state_lock = Lock()
        self._thread = None
        self._ownership = None
        self._current_task = None
        self._failure_code = None

    @property
    def ready(self):
        return self._ready.is_set() and not self._stop.is_set() and not self._abort.is_set()

    @property
    def failure_code(self):
        return self._failure_code

    @property
    def active_task_id(self):
        return self._current_task

    @property
    def lock_backend_pid(self):
        # Diagnostic identifier only; connection access stays in the worker.
        return self._ownership.backend_pid if self._ownership is not None else None

    def start(self, *, timeout=10.0) -> bool:
        if not 0 < timeout <= 60:
            raise ValueError('Startup wait must be between 0 and 60 seconds')
        with self._state_lock:
            if self._thread is not None or self._stop.is_set():
                raise RuntimeError('Executor instances cannot be started twice')
            self._thread = Thread(target=self._run, name='review-executor', daemon=True)
            self._thread.start()
        if not self._startup.wait(timeout):
            self._trip('startup_timeout')
            return False
        return self.ready

    def require_ready(self):
        if not self.ready:
            raise ExecutorNotReady('Review executor is not ready')

    def submit(self, key, request, snapshot):
        """Admission gate for future routes; commit precedes notification."""
        self.require_ready()
        try:
            result = self.repository.submit(key, request, snapshot, admission_check=self.require_ready)
        except SQLAlchemyError:
            self._trip('persistence_failed')
            raise ExecutorNotReady('Review storage is unavailable') from None
        self.notify()
        return result

    def notify(self):
        self._wake.set()

    def storage_failed(self):
        """HTTP/lifecycle detected unavailable storage; never resume this instance."""
        self._trip('persistence_failed')

    def request_stop(self):
        """Nonblocking; let the already dispatched request return within grace."""
        self._ready.clear()
        self._stop.set()
        self._wake.set()

    def stop(self, *, grace=None) -> bool:
        wait = self.shutdown_grace if grace is None else grace
        if not 0 <= wait <= 60:
            raise ValueError('Shutdown grace must be between 0 and 60 seconds')
        self.request_stop()
        thread = self._thread
        if thread is None:
            return True
        if current_thread() is thread:
            return False
        thread.join(wait)
        if thread.is_alive():
            self._expired.set()
            # Python cannot kill an in-flight network call. Keep ownership until
            # that daemon thread exits; process exit releases it. Never unlock
            # while an old worker could still dispatch or commit results.
            return False
        return True

    def _trip(self, code):
        with self._state_lock:
            if self._failure_code is None:
                self._failure_code = code
        self._abort.set()
        self.request_stop()

    def _check_owner(self):
        try:
            self._ownership.check()
        except ExecutorLockLost:
            self._trip('executor_lost')
            raise

    def _before_work(self):
        if self._abort.is_set():
            raise ReviewPersistenceError()
        if self._stop.is_set():
            raise ReviewInterruptedError()
        self._check_owner()

    def _before_write(self):
        if self._abort.is_set():
            raise ReviewPersistenceError()
        if self._expired.is_set():
            raise ReviewInterruptedError()
        self._check_owner()

    def _run_task(self, task):
        self._current_task = task['id']
        try:
            stored = StoredConfig.model_validate(task['config'])
            if stored.model_mode == 'live' and not self.allow_live:
                raise ValueError('Live execution is disabled')
            self._before_work()
            prepared = self.prepare(task, self._before_work)
            expected_config = ReviewConfig(**stored.model_dump(exclude={'model_mode'}))
            expected_revision = PullRequestRevision(task['head_sha'], task['base_sha'])
            if (prepared.config != expected_config or prepared.revision != expected_revision
                    or prepared.validate_after is not None):
                raise ValueError('Prepared task does not match its saved configuration/revision')
            ctx = prepared.context
            if ctx is not None:
                if (ctx.repository.lower() != f"{task['owner']}/{task['repo']}" or ctx.local_root is not None
                        or ctx.revisions != {'head': task['head_sha'], 'base': task['base_sha']}):
                    raise ValueError('Prepared context does not match its saved repository/revision')
                prepared = replace(prepared, context=GuardedContext(ctx, self._before_work))
            request_fn = self.mock_request_fn if stored.model_mode == 'mock' else self.live_request_fn

            def request(*args, **kwargs):
                self._before_work()
                return request_fn(*args, **kwargs)

            sink = DatabaseReviewSink(self.repository, task['id'], before_write=self._before_write, failed=self._trip)
            execute_review(prepared, context_request_fn=request, sink=sink, before_work=self._before_work)
        except ReviewInterruptedError:
            self._mark_current('interrupted', 'shutdown')
        except (ExecutorLockLost, ReviewPersistenceError):
            self._trip(self.failure_code or 'persistence_failed')
            self._mark_current('failed', 'executor_lost' if self.failure_code == 'executor_lost' else 'persistence_failed')
        except SQLAlchemyError:
            self._trip('persistence_failed')
            self._mark_current('failed', 'persistence_failed')
        except Exception:
            # Bad input/provider is task-local. Never expose raw source-bearing
            # exceptions, and never retry this task automatically.
            self._mark_current('failed', 'service_failed')
        finally:
            self._current_task = None

    def _mark_current(self, status, code):
        try:
            self.repository.terminate(self._current_task, status=status, error_code=code)
        except InvalidTransition:
            # A final commit may have succeeded before its acknowledgement was
            # lost, or a new lock owner already interrupted this task. No rewrite.
            pass
        except Exception:
            self._trip('persistence_failed')
            # The next lock-owning startup marks remaining queued/running rows.

    def _run(self):
        owns_lock = False
        schema_valid = False
        try:
            self._ownership = ExecutorSessionLock(self.engine.url)
            owns_lock = self._ownership.acquire()
            if not owns_lock:
                self._trip('executor_busy')
                return
            if not schema_ready(self.engine):
                self._trip('schema_mismatch')
                return
            schema_valid = True
            self._check_owner()
            self.repository.interrupt_pending()
            if self._stop.is_set():
                return
            self._ready.set()
            self._startup.set()
            while not self._stop.is_set():
                # Clear before querying: notification loss/races are bounded by
                # periodic polling of committed rows, never an in-memory queue.
                self._wake.clear()
                self._check_owner()
                task = self.repository.claim_next()
                if task is None:
                    self._wake.wait(self.poll_interval)
                else:
                    self._run_task(task)
        except ExecutorLockLost:
            self._trip('executor_lost')
        except Exception:
            self._trip('persistence_failed')
        finally:
            self._ready.clear()
            self._startup.set()
            if owns_lock and schema_valid:
                try:
                    self._ownership.check()
                    self.repository.interrupt_pending(error_code='shutdown' if not self.failure_code else 'interrupted')
                except Exception:
                    # Do not reacquire a lost lock or secretly resume old work.
                    pass
            if self._ownership is not None:
                self._ownership.close()
