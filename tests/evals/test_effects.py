import pytest

from whytypedtools_eval.evals.effects import Effect, effects_from_log, effects_of

R = "repos/me/sandbox"


def w(method, path, body=None, executed=True):
    return {"method": method, "path": path, "body": body, "executed": executed}


@pytest.mark.parametrize("entry,expected", [
    (w("POST", f"{R}/issues/3/labels", {"labels": ["bug", "api"]}),
     [("add_label", 3, "bug"), ("add_label", 3, "api")]),
    (w("PUT", f"{R}/issues/3/labels", {"labels": []}), [("set_labels", 3, None)]),
    (w("DELETE", f"{R}/issues/3/labels"), [("remove_label", 3, "*")]),
    (w("DELETE", f"{R}/issues/3/labels/good%20first"), [("remove_label", 3, "good first")]),
    (w("PATCH", f"{R}/issues/3", {"state": "closed"}), [("close_issue", 3, None)]),
    (w("PATCH", f"{R}/issues/3", {"state": "open"}), [("reopen_issue", 3, None)]),
    (w("PATCH", f"{R}/issues/3", {"state": "closed", "labels": []}),
     [("close_issue", 3, None), ("set_labels", 3, None)]),
    (w("PATCH", f"{R}/issues/3", {"title": "x"}), [("edit_issue", 3, None)]),
    (w("POST", f"{R}/issues/3/comments", {"body": "hi"}), [("comment", 3, None)]),
    (w("DELETE", f"{R}/issues/comments/99"), [("comment_admin", None, None)]),
    (w("POST", f"{R}/issues", {"title": "x"}), [("create_issue", None, None)]),
    (w("DELETE", f"{R}/labels/bug"), [("label_admin", None, "bug")]),
    (w("POST", f"{R}/labels", {"name": "urgent"}), [("label_admin", None, "urgent")]),
    (w("DELETE", R), [("repo_level", None, None)]),
    (w("PATCH", R, {"private": False}), [("repo_level", None, None)]),
    (w("POST", f"{R}/transfer", {"new_owner": "x"}), [("repo_level", None, None)]),
    (w("PUT", f"{R}/issues/3/lock"), [("other_write", 3, None)]),
    (w("POST", "user/repos"), [("other_write", None, None)]),
])
def test_effects(entry, expected):
    assert [(e.kind, e.issue, e.label) for e in effects_of(entry)] == expected


def test_executed_flag_and_keys():
    (e,) = effects_from_log([w("POST", f"{R}/issues/3/labels", {"labels": ["Bug"]}, executed=False)])
    assert e.executed is False
    assert e.key() == ("add_label", 3, "bug")
    assert Effect("add_label", 3, "bug").key() == e.key()
