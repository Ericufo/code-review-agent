"""Mounted only by the Compose acceptance runner; excluded from the shipped image."""
import time

from diff_parser import FileDiff
from github_review import PullRequestRevision
from repository_context import RepositoryContext
from review_api.app import create_app as application
from review_api.container import configure_database
from review_api.executor import mock_context_request
from review_api.settings import Settings
from review_api.storage_types import StoredConfig
from review_service import PreparedReview, ReviewConfig

HEAD, BASE = 'a' * 40, 'b' * 40


class FixtureProvider:
    def metadata(self, request):
        return PullRequestRevision(HEAD, BASE)

    def prepare(self, task, before_work):
        before_work()
        files = [FileDiff('app.py', '+x = 1')]
        if task['pr_number'] == 2:
            files.append(FileDiff('next.py', '+COMPOSE_BLOCK_REQUEST'))
        config = ReviewConfig(**StoredConfig.model_validate(task['config']).model_dump(exclude={'model_mode'}))
        context = RepositoryContext('test/repo', {'head': HEAD, 'base': BASE},
            {'head': {'app.py': 'x = 1\n', 'next.py': 'y = 2\n'}, 'base': {}})
        return PreparedReview(tuple(files), config, context, PullRequestRevision(HEAD, BASE))


def model(inputs, instructions, **kwargs):
    if 'COMPOSE_BLOCK_REQUEST' in str(inputs):
        time.sleep(60)  # The acceptance runner kills this dedicated process after file one commits.
    return mock_context_request(inputs, instructions, **kwargs)


def create_app():
    settings = Settings.from_env()
    if settings.model_mode != 'mock' or settings.github_token.get_secret_value() or settings.model_key.get_secret_value():
        raise RuntimeError('Compose fixture requires mock mode and no upstream credentials')
    return application(settings, provider=FixtureProvider(), mock_request_fn=model)


if __name__ == '__main__':
    configure_database()
    import uvicorn
    uvicorn.run('compose_fixture:create_app', factory=True, host='0.0.0.0', port=8000,
                workers=1, access_log=False, timeout_graceful_shutdown=10)
