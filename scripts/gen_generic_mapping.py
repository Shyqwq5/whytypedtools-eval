"""Write evals/generic_mapping_v2.yaml from the task set and the classifier rules.

    uv run python scripts/gen_generic_mapping.py

The file is committed before any generic run; a test checks it is up to date.
"""

from whytypedtools_eval.evals.generic_mapping import DEFAULT_MAPPING, build_mapping, render
from whytypedtools_eval.evals.tasks import load_task_set

if __name__ == "__main__":
    DEFAULT_MAPPING.write_text(render(build_mapping(load_task_set())), encoding="utf-8", newline="\n")
    print(f"wrote {DEFAULT_MAPPING}")
