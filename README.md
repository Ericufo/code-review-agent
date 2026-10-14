# Code Review Agent

An AI-powered CLI tool that automatically reviews local Git changes and GitHub Pull Requests, produces structured, file-level code review feedback.

## Features

- Reviews tracked and untracked Git changes
- Reviews GitHub Pull Requests through GitHub API
- Parses unified diffs into individual file changes
- Performs per-file LLM code reviews
- Validates structured results with Pydantic
- Aggregates issues across multiple files
- Preserves partial results when individual reviews fail
- Skips binary and oversized diffs with explicit warnings
- Handles GitHub API failures with clear error messages
- Includes mocked unit tests and GitHub Actions CI

## Tech Stack

- Python 3.10+
- DeepSeek API (OpenAI-compatible client)
- Pydantic v2
- Git
- GitHub REST API
- unittest / unittest.mock
- GitHub Actions

## Quick Start

Clone the repository and create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and configure the required environment variables.


## Review local Git changes
Run:

```bash
python main.py
```

The tool examines local Git changes and prints a structured review containing issue severity, file paths, line numbers, explanations, and suggested improvements.

## Review a GitHub Pull Request
Run:

```bash
python main.py --github-pr <pull_request_number>
```

The target repository is configured through
`GITHUB_OWNER` and `GITHUB_REPO`.

The tool fetches changed files from the specified GitHub repository,
converts GitHub patches into internal diff representations,
and reuses the existing review pipeline.

## Architecture

```text
                 User Input
                     |
          +----------+----------+
          |                     |
          v                     v
   Local Git Changes       GitHub Pull Request
          |                     |
          v                     v
    git_utils.py        github_client.py
          |                     |
          v                     v
          +----------+----------+
                     |
                     v
              github_diff.py
              diff_parser.py

                     |
                     v

            FileDiff objects

                     |
                     v

          review_pipeline.py
          Review files and aggregate results

                     |
                     v

              reviewer.py
              LLM inference

                     |
                     v

          review_models.py
          Pydantic validation

                     |
                     v

                CLI Output
```

## Tests

Run the unit test suite:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs tests automatically on pushes and pull requests.

## Limitations

- Reviews diffs, not full repository context
- Findings can contain false positives
- Does not yet publish GitHub PR comments
- Does not maintain repository-level memory or knowledge
- Large or binary diffs may be skipped
- Certain specially quoted Git filenames are not yet supported
- Diff contents are sent to an external LLM API; do not submit secrets

## Roadmap

- V1: Local Git diff review
- V2: GitHub Pull Request integration
- V3: Automated PR comments
- V4: Context-aware agentic review
- V5: API service and deployment

## V2 API Test

This is a test pull request for GitHub API integration.
