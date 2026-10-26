"""Read-only GitHub adapter and explicitly enabled, bounded model transport."""
import json
from time import monotonic
from urllib.parse import quote

import requests
from openai import OpenAI

from github_client import GitHubAPIError
from github_diff import github_files_to_diff
from github_review import PullRequestRevision, get_review_revision
from repository_context import GitHubRepositoryContext
from review_api.storage_types import StoredConfig, validate_path
from review_service import PreparedReview, ReviewConfig
from review_tools import TOOL_SCHEMAS


class UpstreamTimeout(GitHubAPIError):
    pass


class ReadOnlyGitHub:
    base_url = 'https://api.github.com'

    def __init__(self, owner, repo, settings, before_work=lambda: None):
        self.owner, self.repo, self.settings, self.before_work = owner, repo, settings, before_work
        self.prefix = f'{self.base_url}/repos/{quote(owner, safe="")}/{quote(repo, safe="")}/'

    def _get(self, url):
        # No caller/model-supplied URLs, redirects, pagination links or writes.
        if not url.startswith(self.prefix):
            raise GitHubAPIError('Repository request is outside the allowed scope')
        self.before_work()
        headers = {'Accept': 'application/vnd.github+json'}
        token = self.settings.github_token.get_secret_value()
        if token:
            headers['Authorization'] = f'Bearer {token}'
        timeout = self.settings.github_timeout
        start = monotonic()
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise GitHubAPIError('GitHub metadata or source is unavailable')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    if monotonic() - start > timeout:
                        raise UpstreamTimeout('GitHub request timed out')
                    size += len(chunk)
                    if size > 8_000_000:
                        raise GitHubAPIError('GitHub response exceeds the byte limit')
                    chunks.append(chunk)
                data = json.loads(b''.join(chunks))
                if not isinstance(data, dict):
                    raise ValueError()
                return data
        except requests.Timeout:
            raise UpstreamTimeout('GitHub request timed out') from None
        except (requests.RequestException, ValueError):
            raise GitHubAPIError('GitHub response could not be read') from None

    def get_pull_request(self, number):
        return self._get(self.prefix + f'pulls/{number}')


class GitHubProvider:
    def __init__(self, settings):
        self.settings = settings

    def metadata(self, request):
        return get_review_revision(ReadOnlyGitHub(request.owner, request.repo, self.settings), request.pr_number)

    def prepare(self, task, before_work):
        client = ReadOnlyGitHub(task['owner'], task['repo'], self.settings, before_work)
        revision = PullRequestRevision(task['head_sha'], task['base_sha'])
        # Immutable commits, never pulls/{number}/files or mutable refs.
        data = client._get(client.prefix + f'compare/{revision.base_sha}...{revision.head_sha}?per_page=1&page=1')
        files = data.get('files')
        # GitHub caps comparison files at 300 without an authoritative total.
        # Reject the ambiguous boundary instead of silently reviewing a prefix.
        if not isinstance(files, list) or len(files) >= 300:
            raise GitHubAPIError('Comparison file list is unavailable or reaches the 300-file limit')
        paths = set()
        for file in files:
            if not isinstance(file, dict):
                raise GitHubAPIError('Invalid comparison file')
            path = validate_path(file.get('filename', ''))
            if path in paths or (file.get('patch') is not None and not isinstance(file['patch'], str)):
                raise GitHubAPIError('Invalid or duplicate comparison file')
            paths.add(path)
        config = ReviewConfig(**StoredConfig.model_validate(task['config']).model_dump(exclude={'model_mode'}))
        context = GitHubRepositoryContext(client, revision) if files else None
        return PreparedReview(tuple(github_files_to_diff(files)), config, context, revision, len(files))


class LiveModelTransport:
    def __init__(self, settings):
        if settings.model_mode != 'live':
            raise ValueError('Live model mode must be explicitly enabled')
        self.settings = settings

    def __call__(self, inputs, instructions, *, tools_enabled=True):
        with OpenAI(api_key=self.settings.model_key.get_secret_value(), base_url='https://api.deepseek.com',
                    timeout=self.settings.model_timeout, max_retries=0) as client:
            return client.responses.create(model='deepseek-flash', instructions=instructions, input=inputs,
                tools=TOOL_SCHEMAS if tools_enabled else [], max_output_tokens=self.settings.max_output_tokens,
                reasoning={'effort': 'low'}, text={'format': {'type': 'json_object'}})
