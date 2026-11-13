from __future__ import annotations

import argparse

from . import SCENARIO_IDS, build_all


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m incident_agents.scenarios", description="Build the synthetic incident dataset.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="generate scenario data, repositories and answer keys")
    build.add_argument("--out", default=".data/scenarios", help="output directory (default .data/scenarios)")
    build.add_argument("--only", action="append", choices=SCENARIO_IDS, help="build only this scenario (repeatable)")
    sub.add_parser("list", help="list scenario ids")
    args = parser.parse_args()
    if args.command == "list":
        print("\n".join(SCENARIO_IDS))
        return
    for path in build_all(args.out, args.only):
        print(f"built {path}")


if __name__ == "__main__":
    main()
