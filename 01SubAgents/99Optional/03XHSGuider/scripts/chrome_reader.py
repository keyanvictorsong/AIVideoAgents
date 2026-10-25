"""Sanitized public XHS reader; no arbitrary JavaScript/raw-state CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dyxhs_guider.chrome_reader import ChromeReadError, ChromeTab


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tab-id", type=int, help="Use only this fixed Chrome tab ID")
    target.add_argument("--create", metavar="XHS_URL", help="Create one new XHS research tab")
    parser.add_argument("--window-id", type=int, help="Fixed window ID; default is the front window only")
    parser.add_argument("--navigate", metavar="XHS_URL", help="Navigate the fixed XHS tab before reading")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--poll-ms", type=int, default=500)
    args = parser.parse_args()
    if args.create and (args.window_id is not None or args.navigate):
        parser.error("--create cannot be combined with --window-id or --navigate")
    try:
        tab = ChromeTab.create(args.create) if args.create else ChromeTab(args.tab_id, args.window_id)
        if args.navigate:
            tab.navigate(args.navigate)
        result = {"tab_id": tab.tab_id, "window_id": tab.window_id,
                  **tab.read_public_state(timeout_ms=args.timeout_ms, poll_ms=args.poll_ms)}
    except ChromeReadError as exc:
        result = {"status": exc.status, "reason": exc.reason, "rows": [], "links": []}
    except ValueError:
        # Do not echo possibly signed URLs from invalid inputs.
        result = {"status": "invalid_argument", "reason": "请使用有效的小红书 HTTPS URL、正整数标签 ID 和等待预算。", "rows": [], "links": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"links_present", "state_received", "no_results"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
