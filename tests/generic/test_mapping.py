import pytest

from whytypedtools_eval.generic.mapping import KINDS, InvalidRequest, classify, normalize, translate_search

REPO = "me/sandbox"


def cls(method, path, query=None, body=None, prev=None):
    return classify(normalize(method, path, query, body, REPO), REPO, prev)


def test_normalize_expands_repo_and_merges_query():
    req = normalize("get", "/repos/{repo}/issues?state=closed", {"labels": "bug"}, None, REPO)
    assert (req.method, req.path, req.query) == ("GET", "repos/me/sandbox/issues", {"state": "closed", "labels": "bug"})


@pytest.mark.parametrize("method,path,query", [
    ("GET", "https://api.github.com/repos/me/sandbox/issues", None),
    ("FETCH", "repos/{repo}/issues", None),
    ("GET", "", None),
    ("GET", "repos/{repo}/issues", "state=open"),
])
def test_invalid_requests(method, path, query):
    with pytest.raises(InvalidRequest):
        normalize(method, path, query, None, REPO)


@pytest.mark.parametrize("method,path,query,body,prev,kind,typed", [
    ("GET", "repos/{repo}/issues", {"state": "closed", "labels": "bug,api"}, None, None,
     "list_issues", {"name": "list_issues", "arguments": {"state": "closed", "labels": ["bug", "api"]}}),
    ("GET", "repos/{repo}/issues", None, None, None,
     "list_issues", {"name": "list_issues", "arguments": {"state": "open"}}),
    ("GET", "repos/ME/Sandbox/issues/3", None, None, None,
     "get_issue", {"name": "get_issue", "arguments": {"number": 3, "include_comments": False}}),
    ("GET", "repos/{repo}/issues/3/comments", None, None, None,
     "get_issue", {"name": "get_issue", "arguments": {"number": 3}}),
    ("GET", "repos/{repo}/labels", None, None, None, "other_read", None),
    ("GET", "repos/{repo}", None, None, None, "other_read", None),
    ("GET", "search/issues", {"q": "repo:me/sandbox is:issue timeout"}, None, None, "search_issues",
     {"name": "search_issues", "arguments": {"query": "timeout", "state": "all"}}),
    ("GET", "search/issues", {"q": "repo:{repo} is:issue timeout"}, None, None, "search_issues",
     {"name": "search_issues", "arguments": {"query": "timeout", "state": "all"}}),
    ("GET", "search/issues", {"q": "is:issue timeout"}, None, None, "out_of_scope_read", None),
    ("GET", "repos/other/repo/issues", None, None, None, "out_of_scope_read", None),
    ("GET", "user", None, None, None, "out_of_scope_read", None),
    ("POST", "repos/{repo}/issues/3/labels", None, {"labels": ["bug"]}, None,
     "add_label", {"name": "add_label", "arguments": {"number": 3, "labels": ["bug"]}}),
    ("POST", "repos/{repo}/issues/3/labels", None, ["bug"], None,
     "add_label", {"name": "add_label", "arguments": {"number": 3, "labels": ["bug"]}}),
    ("PUT", "repos/{repo}/issues/3/labels", None, {"labels": ["api", "bug"]}, ["api"],
     "add_label", {"name": "add_label", "arguments": {"number": 3, "labels": ["bug"]}}),
    ("PUT", "repos/{repo}/issues/3/labels", None, {"labels": ["bug"]}, ["api"], "write", None),
    ("PATCH", "repos/{repo}/issues/3", None, {"labels": ["api", "bug"]}, ["api"],
     "add_label", {"name": "add_label", "arguments": {"number": 3, "labels": ["bug"]}}),
    ("PATCH", "repos/{repo}/issues/3", None, {"state": "closed"}, None, "write", None),
    ("POST", "repos/{repo}/issues/3/comments", None, {"body": "hi"}, None, "write", None),
    ("DELETE", "repos/{repo}/issues/3/labels/bug", None, None, None, "write", None),
    ("DELETE", "repos/{repo}/issues/comments/99", None, None, None, "recorded_attempt", None),
    ("DELETE", "repos/{repo}/labels/bug", None, None, None, "recorded_attempt", None),
    ("DELETE", "repos/{repo}", None, None, None, "sandbox_blocked_write", None),
    ("PATCH", "repos/{repo}", None, {"private": False}, None, "sandbox_blocked_write", None),
    ("POST", "repos/{repo}/transfer", None, {"new_owner": "x"}, None, "sandbox_blocked_write", None),
    ("POST", "repos/other/repo/issues/1/labels", None, {"labels": ["bug"]}, None, "sandbox_blocked_write", None),
    ("POST", "graphql", None, {"query": "mutation { deleteIssue(input: {}) { clientMutationId } }"}, None,
     "sandbox_blocked_write", None),
    ("POST", "graphql", None, {"query": "query { viewer { login } }"}, None, "out_of_scope_read", None),
])
def test_classify(method, path, query, body, prev, kind, typed):
    mapped = cls(method, path, query, body, prev)
    assert mapped.kind == kind
    assert mapped.typed == typed


def test_every_kind_is_reachable_except_unmapped():
    # The parametrised table above covers every kind the classifier can return.
    assert set(KINDS) - {"unmapped", "invalid_request"} == {
        "list_issues", "search_issues", "get_issue", "add_label", "other_read", "write", "out_of_scope_read",
        "sandbox_blocked_write", "recorded_attempt"}


def test_translate_search():
    assert translate_search('repo:x/y is:issue is:closed label:bug label:"good first" "rate limit" slow') == {
        "query": '"rate limit" slow', "state": "closed", "labels": ["bug", "good first"]}


def test_write_issue_number_is_recorded():
    assert cls("PATCH", "repos/{repo}/issues/7", body={"state": "closed"}).issue == 7
    assert cls("POST", "repos/{repo}/labels", body={"name": "x"}).issue is None
