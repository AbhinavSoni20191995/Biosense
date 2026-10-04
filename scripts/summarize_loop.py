"""Print the decision trail of a finished loop directory.

    uv run --frozen python scripts/summarize_loop.py runs/cart-demo
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biosense.production import orchestrator as OR  # noqa: E402


def main(loop_dir):
    state = OR.load_state(loop_dir)
    print(f'  loop {state["loop_id"]} (request {state["request_id"]})')
    print(f'  iterations used: {OR.iterations_used(state)} / {state["max_iterations"]}')
    print(f'  autonomy: {state["autonomy"]["mode"]}, '
          f'bioreactor {state["autonomy"]["bioreactor_source"]}')
    print(f'  verdicts: {[i["verdict"] for i in state["iterations"]]}')
    for p in sorted(Path(loop_dir).glob('decisions/decision-*.json')):
        d = json.loads(p.read_text())
        print(f'  {p.stem}: it{d["iteration"]} {d["type"]:24s} '
              f'authored_by={d.get("authored_by", "-")} '
              f'advances={d.get("advances_iteration")} route={d.get("route_to")}')
    for c in state['consults']:
        print(f'  {c["consult_id"]}: {c["status"]} (blocking={c["blocking"]})')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
