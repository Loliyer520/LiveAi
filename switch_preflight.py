from __future__ import annotations

import argparse
import json
import sys

from core.config import AppConfig
from core.switch_checks import preflight_ok, run_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only LiveAi cutover preflight')
    parser.add_argument('--config', default='config.yaml')
    args = parser.parse_args(argv)
    config = AppConfig()
    results = run_preflight(config=config)
    print(json.dumps([result.as_dict() for result in results], ensure_ascii=False, indent=2))
    return 0 if preflight_ok(results) else 2


if __name__ == '__main__':
    sys.exit(main())
