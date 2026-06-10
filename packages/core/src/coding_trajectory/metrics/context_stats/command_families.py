"""Shared command-family token sets used to classify shell commands.

These sets are the single source of truth for bucketing ``RunCommand`` tool
outputs (and equivalent shell activity) into high-level command families such
as tests, builds, package management, and external interaction. Both the
context-category attribution in :mod:`inferred_categories` and the dashboard
plugin's overview rendering import from here so that new tools (e.g.
``bun test``, ``deno task``) stay classified consistently across views.
"""

from __future__ import annotations


TEST_TOKENS: frozenset[str] = frozenset({
    "pytest", "jest", "vitest", "mocha", "rspec", "phpunit",
    "unittest", "tox", "ctest", "test", "deno",
})

BUILD_TOKENS: frozenset[str] = frozenset({
    "tsc", "mypy", "ruff", "eslint", "flake8", "pylint", "black", "isort", "prettier",
    "make", "cmake", "webpack", "rollup", "vite", "esbuild", "clippy",
    "build", "compile", "lint", "typecheck", "check", "vet",
})

PACKAGE_MANAGERS: frozenset[str] = frozenset({
    "npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pipenv",
    "cargo", "gem", "bundle", "brew", "conda", "apt", "apt-get",
})

DEPENDENCY_TOKENS: frozenset[str] = frozenset({
    "install", "add", "ci", "sync", "get", "lock", "update", "upgrade", "remove",
})

COMMAND_RUNNERS: frozenset[str] = frozenset({
    "uv", "poetry", "pdm", "pipenv", "rye", "hatch",
    "npx", "bunx", "pnpm", "yarn", "bun", "deno",
})

RUNNER_SUBWORDS: frozenset[str] = frozenset({"run", "exec", "dlx", "tool", "task"})
