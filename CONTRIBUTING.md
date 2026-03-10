# Contributing to Arkive

Thank you for your interest in contributing to Arkive. This document provides guidelines and information for contributors.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/Arkive.git`
3. Create a feature branch: `git checkout -b feat/your-feature`
4. Make your changes
5. Run tests: `make test`
6. Commit with a descriptive message
7. Push to your fork and open a Pull Request

## Development Setup

### Prerequisites

- Python 3.12+
- Node.js 22+
- Docker and Docker Compose

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Full Stack (Docker)

```bash
make dev
```

## Code Style

### Python (Backend)

- Follow PEP 8
- Use type hints for function signatures
- Format with `ruff format`
- Lint with `ruff check`
- Run `make lint` before committing

### TypeScript/Svelte (Frontend)

- Use the repository's current Svelte 5 patterns and follow the surrounding code in `src/`
- Follow existing component patterns in `src/lib/components/`
- Use Tailwind CSS utility classes for styling
- Use shadcn-svelte components from `src/lib/components/ui/`

## Testing

All changes should include appropriate tests.

```bash
# Run all backend tests
make test

# Run unit tests only
make test-unit

# Run integration tests only
make test-integration

# Lint
make lint
```

## Commit Messages

Use clear, descriptive commit messages:

```
feat: add support for InfluxDB 3.x dumps
fix: handle SQLite WAL corruption on unclean shutdown
docs: update storage backend configuration guide
test: add integration tests for restore plan generation
refactor: simplify discovery engine profile matching
```

Prefixes: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, `perf`.

## Pull Request Guidelines

- Keep PRs focused on a single change
- Include a clear description of what the PR does and why
- Reference any related issues (e.g., "Fixes #42")
- Ensure all tests pass
- Update documentation if your change affects user-facing behavior

## Adding Container Profiles

To add support for a new application:

1. Create a YAML file in `profiles/` (use an existing profile as a template)
2. Define `image_patterns` for matching the container image
3. Specify the `db_type` and detection method (env vars, ports, files)
4. Define the dump command and restore instructions
5. Add a test case in `backend/tests/unit/test_discovery.py`

## Reporting Bugs

When filing a bug report, please include:

- Arkive version (`arkive version`)
- Unraid version (or Linux distribution)
- Steps to reproduce the issue
- Expected behavior vs. actual behavior
- Relevant Arkive logs from the container or application runtime

## Feature Requests

Feature requests are welcome. Please check existing issues first to avoid duplicates. Include:

- A clear description of the feature
- The problem it solves or use case it enables
- Any implementation ideas (optional)

## Security

If you discover a security vulnerability, please do **not** open a public issue. Use GitHub Security Advisories for this repository instead: [Security Policy](https://github.com/islamdiaa/Arkive/security/policy).

## License

By contributing to Arkive, you agree that your contributions will be licensed under the MIT License.
