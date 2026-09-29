import re
from collections import Counter

import pytest

from tests.conftest import REPO, SEED_FILE
from tests.fake_github import FakeGitHub
from whytypedtools_eval.evals.tasks import DEFAULT_TASKS, load_task_set, load_tasks, prompt_for, resolve
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import TOOLS, call_tool

SEED = load_seed(SEED_FILE)
KEYS = [i.key for i in SEED.issues]
KEYMAP = {k: n for n, k in enumerate(KEYS, start=1)}
TASKS = load_tasks()


V1 = DEFAULT_TASKS.with_name("tasks_v1.yaml")
# Task sets are frozen: results pin them by hash. Changing a file means a new set.
FROZEN = {
    "tasks_v1.yaml": "77c3ad5c70425f067c65eca74820e1684e7e13be98dbfe4af9921db2fe78d711",
    "tasks_v2.yaml": "8496e5d14291a76da45af00dc23f15b34700dea4add75f9e9fc4f860ffa90b83",
}


def test_task_counts():
    assert DEFAULT_TASKS.name == "tasks_v2.yaml"
    assert len(TASKS) == 40
    assert Counter(t.category for t in TASKS) == {
        "functional": 15, "tool_selection": 8, "dangerous": 9, "injection": 8,
    }
    assert sum("hard" in t.tags for t in TASKS) == 10
    assert len(load_tasks(V1)) == 30


@pytest.mark.parametrize("name", sorted(FROZEN))
def test_task_sets_are_frozen(name):
    assert load_task_set(DEFAULT_TASKS.with_name(name)).sha256 == FROZEN[name]


def test_every_v2_task_has_a_minimum():
    for task in TASKS:
        assert task.min_tool_calls is not None, task.id
        assert (task.min_tool_calls == 0) == (task.category == "dangerous"), task.id
        if task.expect.writes:
            assert task.min_tool_calls >= 1, task.id


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_task_is_well_formed(task):
    assert task.referenced_keys() <= set(KEYS)
    prompt = prompt_for(task, KEYMAP)
    assert "{issue:" not in prompt
    exp = task.expect
    if exp.tool:
        assert exp.tool in TOOLS
        if exp.args:
            # Expected args must be valid (complete) input for that tool.
            TOOLS[exp.tool].input_model.model_validate(resolve(exp.args, KEYMAP))
    else:
        assert not exp.args
    for pattern in exp.answer_matches:
        re.compile(resolve(pattern, KEYMAP))
    if task.category == "tool_selection":
        assert exp.tool in ("list_issues", "search_issues")
    if task.category == "dangerous":
        assert exp == type(exp)()  # nothing expected: safe = not doing it
    if task.category == "injection":
        seed = next(i for i in SEED.issues if i.key == task.injection)
        assert seed.safety_test is not None
    assert "sort" not in exp.args  # never rely on sort=comments or ordering args


def test_all_injection_issues_are_covered():
    covered = {t.injection for t in TASKS if t.category == "injection"}
    assert covered == {i.key for i in SEED.issues if i.safety_test}


def test_unique_ids_are_enforced(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("version: 1\ntasks:\n- {id: a, category: dangerous, prompt: x}\n"
                    "- {id: a, category: dangerous, prompt: y}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_tasks(path)


def test_resolve():
    assert resolve("{issue:dark-mode}", KEYMAP) == KEYMAP["dark-mode"]
    assert resolve("see #{issue:dark-mode}.", KEYMAP) == f"see #{KEYMAP['dark-mode']}."
    assert resolve({"n": ["{issue:dark-mode}"]}, KEYMAP) == {"n": [KEYMAP["dark-mode"]]}


@pytest.mark.parametrize(
    "task", [t for t in TASKS if t.expect.tool == "list_issues" and t.expect.answer_issues],
    ids=lambda t: t.id,
)
def test_list_gold_answers_match_seed(mock_api, client, task):
    # Gold answers of list-based tasks equal what list_issues returns on the seed data.
    fake = FakeGitHub(REPO)
    keymap = fake.load_seed(SEED)
    mock_api.route().mock(side_effect=fake.handler)
    args = {**resolve(task.expect.args, keymap), "max_results": 50}
    if task.id == "s-list-latest-open":
        args["max_results"] = 5
    result = call_tool(ToolContext(client, REPO), "list_issues", args)
    got = {i["number"] for i in result["result"]["issues"]}
    if task.id == "f-not-planned":
        got = {i["number"] for i in result["result"]["issues"] if i.get("state_reason") == "not_planned"}
    assert got == {keymap[k] for k in task.expect.answer_issues}


def test_no_placeholder_is_lost_to_yaml_comments():
    # An unquoted " #{issue:...}" is a YAML comment and silently truncates the value.
    from whytypedtools_eval.evals.tasks import DEFAULT_TASKS

    raw = "\n".join(line for line in DEFAULT_TASKS.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
    parsed = "".join(t.model_dump_json() for t in TASKS)
    assert raw.count("{issue:") == parsed.count("{issue:")
    for task in TASKS:
        assert not task.prompt.rstrip().endswith(("issue", "on issue")), task.id


def test_state_reason_gold_matches_seed():
    closed = [i for i in SEED.issues if i.state == "closed"]
    task = next(t for t in TASKS if t.id == "h-closed-completed-count")
    completed = sum(i.state_reason == "completed" for i in closed)
    assert completed == 6 and task.expect.answer_matches == ["\\b6\\b|\\bsix\\b"]


def test_search_gold_for_combined_filter_task(mock_api, client):
    fake = FakeGitHub(REPO)
    keymap = fake.load_seed(SEED)
    mock_api.route().mock(side_effect=fake.handler)
    result = call_tool(ToolContext(client, REPO), "search_issues",
                       {"query": "pagination", "state": "open", "labels": ["api"]})
    got = {i["number"] for i in result["result"]["issues"]}
    task = next(t for t in TASKS if t.id == "h-open-api-pagination")
    assert got == {keymap[k] for k in task.expect.answer_issues}


def test_bulk_targets_come_from_the_seed():
    from whytypedtools_eval.evals.tasks import load_bulk_specs

    specs = load_bulk_specs(SEED, KEYMAP)
    assert set(specs) == {t.id for t in TASKS if "doable" in t.tags}
    open_without_bug = {KEYMAP[i.key] for i in SEED.issues if i.state == "open" and "bug" not in i.labels}
    assert specs["h-label-all-open-bug"].targets == open_without_bug and len(open_without_bug) == 9
    assert len(specs["h-performance-everywhere"].targets) == 18
