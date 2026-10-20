"""Validate model requests and enforce budgets before executing read-only tools."""
from dataclasses import dataclass
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from repository_context import ContextError, RepositoryContext, validate_path
from review_models import ContextEvidence


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    version: Literal['head', 'base'] = 'head'

    @model_validator(mode='after')
    def valid_range(self):
        validate_path(self.path)
        if self.end_line < self.start_line:
            raise ContextError('Line range must be ordered')
        return self


class SearchCodeArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=120)
    version: Literal['head', 'base'] = 'head'
    path: str | None = None
    offset: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid_query(self):
        if not self.query.strip() or '\n' in self.query or '\r' in self.query:
            raise ContextError('Search requires a nonempty single-line literal')
        if self.path is not None:
            validate_path(self.path)
        return self


ARGUMENT_MODELS = {'read_file': ReadFileArgs, 'search_code': SearchCodeArgs}
TOOL_SCHEMAS = [
    {'type': 'function', 'name': name, 'parameters': model.model_json_schema(),
     'description': description}
    for name, model, description in (
        ('read_file', ReadFileArgs, 'Read source lines at head or base. Each page contains at most 120 lines and 12000 characters, even if a larger range is requested. Check has_more and pass next_start_line to continue; end_line clamps at EOF. Cite only returned lines.'),
        ('search_code', SearchCodeArgs, 'Literal case-sensitive source search at head or base. Optional path restricts to one file. Results page at 20 matches or 12000 characters; pass next_offset as offset to continue. has_more means results are NOT exhaustive. No regex or shell.'),
    )
]


class ModelBudgetError(ContextError):
    """A request was rejected before dispatch; counters were not incremented."""


MAX_FILE_ROUNDS = 6
MAX_FILE_TOOL_CALLS = 8
MAX_TOOL_RESULT_CHARS = 12_000


@dataclass
class ContextBudget:
    max_model_calls: int = 64
    max_tool_calls: int = 160
    max_context_chars: int = 256_000
    max_input_chars: int = 2_000_000
    model_calls: int = 0
    tool_calls: int = 0
    context_chars: int = 0
    input_chars: int = 0

    def model_request(self, inputs: list, *, tools: list | None = None) -> None:
        size = len(json.dumps({'input': inputs, 'tools': tools or []}, ensure_ascii=False))
        if self.model_calls >= self.max_model_calls:
            raise ModelBudgetError(f'Batch model call budget exhausted: used={self.model_calls}, limit={self.max_model_calls}')
        if self.input_chars + size > self.max_input_chars:
            raise ModelBudgetError(f'Batch input character budget exhausted: used={self.input_chars}, '
                                   f'next_request={size}, limit={self.max_input_chars}')
        self.model_calls += 1
        self.input_chars += size


class ToolSession:
    def __init__(self, context: RepositoryContext, budget: ContextBudget, *,
                 max_calls=MAX_FILE_TOOL_CALLS, max_chars=48_000, timeout=180, clock=time.monotonic):
        self.context, self.budget = context, budget
        self.max_calls, self.max_chars = max_calls, max_chars
        self.calls = self.used_chars = 0
        self.clock = clock
        self.deadline = clock() + timeout
        self.seen = set()
        self.evidence = []

    def check_time(self):
        if self.clock() >= self.deadline:
            raise ContextError('File review time budget exhausted')

    def execute(self, name: str, arguments: str) -> str:
        self.check_time()
        if self.calls >= self.max_calls:
            raise ContextError(f'Tool call budget exhausted (file): used={self.calls}, limit={self.max_calls}')
        if self.budget.tool_calls >= self.budget.max_tool_calls:
            raise ContextError(f'Tool call budget exhausted (batch): used={self.budget.tool_calls}, limit={self.budget.max_tool_calls}')
        self.calls += 1
        self.budget.tool_calls += 1
        if name not in ARGUMENT_MODELS or len(arguments) > 4096:
            raise ContextError('Unknown tool or oversized arguments')
        # Error messages returned to the model must not echo untrusted argument values.
        try:
            args = ARGUMENT_MODELS[name].model_validate_json(arguments)
        except ContextError as exc:
            # Our path/range validators contain fixed messages, never argument values.
            raise ContextError(f'Invalid tool arguments: {exc}') from exc
        except ValidationError as exc:
            fields = ARGUMENT_MODELS[name].model_fields
            details = '; '.join(
                f"{error['loc'][0] if error['loc'] and error['loc'][0] in fields else 'arguments'}: {error['type']}"
                for error in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise ContextError(f'Invalid tool arguments ({details})') from exc
        key = (name, args.model_dump_json())
        if key in self.seen:
            raise ContextError('Repeated tool request rejected')
        self.seen.add(key)
        entries = []
        payload_data = {'scope': 'allowlisted UTF-8 source files only',
                        'repository': self.context.repository,
                        'revision': self.context.revisions[args.version],
                        'total_lines': None, 'entries': entries,
                        'has_more': False, 'next_start_line': None, 'next_offset': None}
        # Page against both the per-result limit and the remaining cumulative
        # budgets. Never return half a line or charge context that was not sent.
        result_limit = min(MAX_TOOL_RESULT_CHARS, self.max_chars - self.used_chars,
                           self.budget.max_context_chars - self.budget.context_chars)

        def page_too_small(kind):
            if result_limit < MAX_TOOL_RESULT_CHARS:
                return ContextError(
                    f'Remaining context character budget cannot fit {kind}: '
                    f'file_remaining={self.max_chars - self.used_chars}, '
                    f'batch_remaining={self.budget.max_context_chars - self.budget.context_chars}')
            return ContextError(f'A single {kind} exceeds the tool result character limit')

        # Reserve room for continuation metadata while adding complete lines/matches.
        def fits():
            return len(json.dumps(payload_data, ensure_ascii=False)) <= result_limit - 100

        def entry(path, start, end, lines):
            evidence = ContextEvidence(repository=self.context.repository,
                revision=self.context.revisions[args.version], path=path,
                start_line=start, end_line=end)
            return {'evidence': evidence.model_dump(), 'text': '\n'.join(
                f'{line}: {text}' for line, text in zip(range(start, end + 1), lines))}

        if name == 'read_file':
            lines = self.context.read(args.path, args.version).splitlines()
            payload_data['total_lines'] = len(lines)
            if not lines and args.start_line == 1:
                pass  # Empty file: successful read, no citable lines.
            elif args.start_line > len(lines):
                raise ContextError('Requested lines are outside the source file')
            else:
                requested_end = min(args.end_line, len(lines))
                end = min(requested_end, args.start_line + 119)
                entries.append(entry(args.path, args.start_line, end, lines[args.start_line - 1:end]))
                while not fits() and end >= args.start_line:
                    end -= 1
                    entries[:] = [entry(args.path, args.start_line, end, lines[args.start_line - 1:end])] if end >= args.start_line else []
                if not entries:
                    raise page_too_small('source line')
                if end < requested_end:
                    payload_data.update(has_more=True, next_start_line=end + 1)
        else:
            paths = [args.path] if args.path is not None else self.context.paths(args.version)
            matched = 0
            for path in paths:
                self.check_time()
                for number, line in enumerate(self.context.read(path, args.version).splitlines(), 1):
                    if args.query in line:
                        matched += 1
                        if matched <= args.offset:
                            continue
                        entries.append(entry(path, number, number, [line]))
                        if len(entries) > 20 or not fits():
                            entries.pop()
                            if not entries:
                                raise page_too_small('search match')
                            payload_data.update(has_more=True, next_offset=args.offset + len(entries))
                            break
                if payload_data['has_more']:
                    break
        self.check_time()
        payload = json.dumps(payload_data, ensure_ascii=False)
        size = len(payload)
        if self.used_chars + size > self.max_chars:
            raise ContextError(f'File context character budget exceeded: used={self.used_chars}, result={size}, limit={self.max_chars}')
        if self.budget.context_chars + size > self.budget.max_context_chars:
            raise ContextError(f'Batch context character budget exceeded: used={self.budget.context_chars}, result={size}, limit={self.budget.max_context_chars}')
        self.used_chars += size
        self.budget.context_chars += size
        self.evidence.extend(ContextEvidence.model_validate(entry['evidence']) for entry in entries)
        return payload

    def validate_evidence(self, result):
        def supplied(item):
            return any(
                item.repository == known.repository and item.revision == known.revision
                and item.path == known.path
                and known.start_line <= item.start_line <= item.end_line <= known.end_line
                for known in self.evidence
            )

        for issue in result.issues:
            if not issue.evidence or any(not supplied(item) for item in issue.evidence):
                raise ContextError('Finding lacks evidence actually returned by a repository tool')
