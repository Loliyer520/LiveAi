from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import ast
from typing import Any



@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolved(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _contains_import(path: str, names: set[str]) -> bool:
    try:
        tree = ast.parse(Path(path).read_text(encoding='utf-8'), filename=path)
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in names for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module in names:
            return True
    return False


def _source(path: str) -> str:
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        return ''


def run_preflight(*, config: Any, repo_root: str = '.') -> list[CheckResult]:
    """Read-only preflight. It does not acquire leases or change queues."""
    # Accept the old config shape for migration/preflight callers, but the production
    # AppConfig no longer exposes receiver/transport ownership settings.
    transport = getattr(config, 'transport', None)
    receiver = getattr(config, 'receiver', None)
    outbox_path = _resolved(getattr(transport, 'outbox_path', 'data/ipc/napcat_outbox.sqlite3'))
    inbox_path = _resolved(getattr(receiver, 'inbox_path', outbox_path))
    transport_mode = getattr(transport, 'mode', 'legacy')
    receiver_mode = getattr(receiver, 'mode', 'legacy')
    results: list[CheckResult] = []

    results.append(CheckResult(
        'default_modes_are_legacy',
        transport_mode == 'legacy' and receiver_mode == 'legacy',
        f'transport={transport_mode}, receiver={receiver_mode}',
    ))
    if transport is None and receiver is None:
        results.append(CheckResult(
            'single_process_runtime', True,
            'AppConfig has no receiver/transport ownership settings',
        ))
        return results

    results.append(CheckResult(
        'outbox_and_inbox_share_sqlite', outbox_path == inbox_path,
        f'outbox={outbox_path}; inbox={inbox_path}',
    ))

    main_src = _source(str(Path(repo_root) / 'main.py'))


def preflight_ok(results: list[CheckResult]) -> bool:
    return all(result.ok for result in results)
