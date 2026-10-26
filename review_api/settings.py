"""Explicit server configuration. Never reads .env or secrets at import time."""
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from review_api.db import database_url
from review_api.storage_types import ReviewSubmission, StoredConfig


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    database_url: SecretStr
    api_token: SecretStr
    allowed_repos: frozenset[str]
    github_token: SecretStr = SecretStr('')
    model_mode: Literal['mock', 'live'] = 'mock'
    model_key: SecretStr = SecretStr('')
    profiles: dict[str, StoredConfig] = Field(default_factory=lambda: {'default': StoredConfig()})
    waiting_limit: int = Field(default=8, ge=1, le=8)
    github_timeout: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    model_timeout: float = Field(default=45, gt=0, le=60, allow_inf_nan=False)
    max_output_tokens: int = Field(default=16384, ge=1, le=16384)
    shutdown_grace: float = Field(default=10, ge=0, le=60, allow_inf_nan=False)

    @model_validator(mode='after')
    def check(self):
        database_url(self.database_url.get_secret_value())
        token = self.api_token.get_secret_value()
        if not token.strip() or len(token) > 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('A nonempty ASCII bearer token without whitespace is required')
        repos = set()
        for value in self.allowed_repos:
            parts = value.split('/')
            if len(parts) != 2:
                raise ValueError('Allowed repositories must be owner/repo pairs')
            request = ReviewSubmission(owner=parts[0], repo=parts[1], pr_number=1)
            repos.add(f'{request.owner}/{request.repo}')
        if not repos:
            raise ValueError('At least one allowed repository is required')
        object.__setattr__(self, 'allowed_repos', frozenset(repos))
        if not self.profiles or len(self.profiles) > 16:
            raise ValueError('Declare between 1 and 16 profiles')
        ceiling = StoredConfig().model_dump()
        for name, config in self.profiles.items():
            ReviewSubmission(owner='test', repo='repo', pr_number=1, profile=name)
            for key, value in config.model_dump().items():
                if key.startswith('max_') and value > ceiling[key]:
                    raise ValueError('Profile exceeds server hard limits')
            if config.model_mode != self.model_mode:
                raise ValueError('Profiles must use the server model mode')
        if self.model_mode == 'live' and not self.model_key.get_secret_value().strip():
            raise ValueError('Live mode requires a server model key')
        return self

    @classmethod
    def from_env(cls):
        mode = os.getenv('REVIEW_MODEL_MODE', 'mock')
        try:
            profiles = json.loads(os.getenv('REVIEW_PROFILES_JSON') or '{}')
            if not isinstance(profiles, dict):
                raise ValueError()
            profiles = {name: StoredConfig(**{**values, 'model_mode': mode}) for name, values in profiles.items()}
            return cls(database_url=os.getenv('DATABASE_URL', ''), api_token=os.getenv('REVIEW_API_TOKEN', ''),
                allowed_repos=frozenset(x.strip() for x in os.getenv('REVIEW_ALLOWED_REPOS', '').split(',') if x.strip()),
                github_token=os.getenv('GITHUB_TOKEN', ''), model_mode=mode,
                model_key=os.getenv('DEEPSEEK_API_KEY', ''),
                profiles=profiles or {'default': StoredConfig(model_mode=mode)},
                waiting_limit=os.getenv('REVIEW_WAITING_LIMIT', '8'),
                github_timeout=os.getenv('REVIEW_GITHUB_TIMEOUT', '10'),
                model_timeout=os.getenv('REVIEW_MODEL_TIMEOUT', '45'),
                max_output_tokens=os.getenv('REVIEW_MAX_OUTPUT_TOKENS', '16384'),
                shutdown_grace=os.getenv('REVIEW_SHUTDOWN_GRACE', '10'))
        except Exception:
            # Validation errors can echo secret inputs. Startup errors must not.
            raise ValueError('Invalid review API configuration; check server environment') from None


def code_identifiers():
    root = Path(__file__).resolve().parents[1]
    def digest(paths):
        value = hashlib.sha256()
        for path in sorted(paths):
            value.update(path.relative_to(root).as_posix().encode())
            value.update(path.read_bytes())
        return 'sha256:' + value.hexdigest()
    prompt = digest([root / name for name in ('reviewer.py', 'context_review.py', 'review_models.py', 'review_tools.py')])
    source = digest([*root.glob('*.py'), *(root / 'review_api').glob('*.py')])
    return prompt, source
