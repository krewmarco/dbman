"""GitHub provider: a read-only window onto one repository's pull requests and
issues, over GitHub's GraphQL API. See CLAUDE.md for the architecture writeup.

GitHub has no separate "bugs" or "features" list - those are issues carrying
a label (`bug`, `enhancement`). So the provider exposes two tables
(`pull_requests`, `issues`) plus a fixed set of saved-query views (`bugs`,
`features`, `open_prs`, `needs_review`, ...), each just a GitHub search
qualifier string. Every table and view is fetched through the one
`search(type: ISSUE)` GraphQL field, which gives native cursor paging and
returns labels, review decision and CI status in the same round trip (the
REST API needs extra calls per PR for the latter two).
"""
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .base import Provider, Column, RowKey, RowPage, Capabilities, DiagramModel

API_URL = "https://api.github.com/graphql"

# GraphQL search caps `first` at 100 per request; get_page loops to fill a
# larger page_size. Search as a whole is capped at 1000 results per query.
_MAX_PER_REQUEST = 100
_SEARCH_RESULT_CAP = 1000

# Longest a description is carried in a cell. The grid truncates further for
# display; this just keeps a pathological PR body from bloating every page.
_BODY_LIMIT = 500
_STALE_DAYS = 30

_TABLES = {
    "pull_requests": ("pr", "is:pr"),
    "issues": ("issue", "is:issue"),
}

# name -> (kind, search qualifiers, description). "{stale}" is filled with a
# date at query time.
_VIEWS = {
    "open_prs": ("pr", "is:pr is:open draft:false", "Open pull requests, not drafts"),
    "draft_prs": ("pr", "is:pr is:open draft:true", "Open draft pull requests"),
    "merged_prs": ("pr", "is:pr is:merged", "Merged pull requests (a changelog)"),
    "needs_review": ("pr", "is:pr is:open review:required", "Open PRs still awaiting a review"),
    "failing_ci": ("pr", "is:pr is:open status:failure", "Open PRs whose checks are failing"),
    "stale_prs": ("pr", "is:pr is:open updated:<{stale}", f"Open PRs untouched for {_STALE_DAYS}+ days"),
    "bugs": ("issue", "is:issue is:open label:bug", "Open issues labeled 'bug'"),
    "unassigned_bugs": ("issue", "is:issue is:open label:bug no:assignee", "Open bugs nobody owns"),
    "features": ("issue", "is:issue is:open label:enhancement", "Open issues labeled 'enhancement'"),
    "stale_issues": ("issue", "is:issue is:open updated:<{stale}", f"Open issues untouched for {_STALE_DAYS}+ days"),
}

_PR_COLUMNS = [
    Column("#", "number", primary_key=True, read_only=True),
    Column("title", "text", read_only=True),
    Column("state", "text", read_only=True),
    Column("author", "text", read_only=True),
    Column("review", "text", read_only=True),
    Column("ci", "text", read_only=True),
    Column("labels", "text", read_only=True),
    Column("branch", "text", read_only=True),
    Column("+/-", "text", read_only=True),
    Column("updated", "datetime", read_only=True),
    Column("created", "datetime", read_only=True),
    Column("body", "text", read_only=True),
]

_ISSUE_COLUMNS = [
    Column("#", "number", primary_key=True, read_only=True),
    Column("title", "text", read_only=True),
    Column("state", "text", read_only=True),
    Column("labels", "text", read_only=True),
    Column("assignees", "text", read_only=True),
    Column("milestone", "text", read_only=True),
    Column("author", "text", read_only=True),
    Column("comments", "number", read_only=True),
    Column("updated", "datetime", read_only=True),
    Column("created", "datetime", read_only=True),
    Column("body", "text", read_only=True),
]

_SEARCH_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on PullRequest {
        number title url state isDraft merged body createdAt updatedAt
        author { login }
        reviewDecision
        headRefName baseRefName additions deletions
        labels(first: 20) { nodes { name } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
      ... on Issue {
        number title url state body createdAt updatedAt
        author { login }
        comments { totalCount }
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        milestone { title }
      }
    }
  }
}
"""


def _resolve_token(url_token):
    """URL userinfo, then $GITHUB_TOKEN / $GH_TOKEN, then the `gh` CLI's own
    login. GraphQL rejects anonymous requests, so one is required."""
    if url_token:
        return url_token
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise ValueError(
        "No GitHub token found. Provide one via github://<token>@github.com/<owner>/<repo>, "
        "the GITHUB_TOKEN environment variable, or `gh auth login`."
    )


def _cell_text(value, limit=None) -> str:
    """Flatten to one line: a newline inside a cell breaks the DataTable row."""
    text = re.sub(r"\s+", " ", value or "").strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _stamp(iso) -> str:
    return (iso or "").replace("T", " ").rstrip("Z")[:16]


def _names(connection, key) -> str:
    return ", ".join(n[key] for n in (connection or {}).get("nodes", []) if n)


def _pr_row(node) -> list:
    if node.get("merged"):
        state = "merged"
    elif node.get("state") == "CLOSED":
        state = "closed"
    else:
        state = "draft" if node.get("isDraft") else "open"

    rollup = None
    commits = (node.get("commits") or {}).get("nodes") or []
    if commits:
        rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")

    return [
        node["number"],
        node.get("title") or "",
        state,
        (node.get("author") or {}).get("login", ""),
        (node.get("reviewDecision") or "").lower().replace("_", " "),
        (rollup or "").lower(),
        _names(node.get("labels"), "name"),
        f"{node.get('headRefName', '')} → {node.get('baseRefName', '')}",
        f"+{node.get('additions', 0)}/-{node.get('deletions', 0)}",
        _stamp(node.get("updatedAt")),
        _stamp(node.get("createdAt")),
        _cell_text(node.get("body"), _BODY_LIMIT),
    ]


def _issue_row(node) -> list:
    return [
        node["number"],
        node.get("title") or "",
        (node.get("state") or "").lower(),
        _names(node.get("labels"), "name"),
        _names(node.get("assignees"), "login"),
        (node.get("milestone") or {}).get("title", ""),
        (node.get("author") or {}).get("login", ""),
        (node.get("comments") or {}).get("totalCount", 0),
        _stamp(node.get("updatedAt")),
        _stamp(node.get("createdAt")),
        _cell_text(node.get("body"), _BODY_LIMIT),
    ]


class GitHubProvider(Provider):
    """Read-only. Every column is read_only and every write capability is off:
    this observes a shared, real repository, and mutating one (closing
    issues, relabeling) is a separate, deliberate decision."""

    def __init__(self, db_url: str):
        import requests  # lazy: only GitHub users need this dependency

        parts = urlsplit(db_url)
        segments = [p for p in parts.path.split("/") if p]
        # owner/repo live in the URL *path*, like CouchDB's dbname and
        # Notion's page id, so derive_db_name() yields a secret-free "repo".
        if len(segments) != 2:
            raise ValueError(
                "GitHub URL must be github://[<token>@]github.com/<owner>/<repo>"
            )
        self.owner, self.repo = segments
        token = _resolve_token(parts.username)

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })

        # Fail fast on a bad token or an unknown/private repo.
        self._graphql(
            "query($o: String!, $n: String!) { repository(owner: $o, name: $n) { nameWithOwner } }",
            {"o": self.owner, "n": self.repo},
        )

        self.capabilities = Capabilities(
            definition_pane=True,
            create_definition=False,
            diagram=False,
            lookup_plugin=False,
            truncate_column=False,
            whole_row_edit=False,
            open_in_browser=True,
            delete_item=False,
            add_row=False,
            delete_row=False,
            reorder_row=False,
            sort_column=False,
        )

    # -- HTTP -----------------------------------------------------------------

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self.session.post(
            API_URL, json={"query": query, "variables": variables}, timeout=20,
        )
        if resp.status_code == 401:
            raise RuntimeError("GitHub rejected the token (401) - check it or run `gh auth login`")
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "unknown error") for e in body["errors"]))
        data = body["data"]
        if "repository" in data and data["repository"] is None:
            raise RuntimeError(f"Repository {self.owner}/{self.repo} not found, or the token can't see it")
        return data

    # -- name resolution ------------------------------------------------------

    @staticmethod
    def _kind(name: str) -> str:
        if name in _TABLES:
            return _TABLES[name][0]
        return _VIEWS[name][0]

    def _search_string(self, name: str) -> str:
        qualifiers = _TABLES[name][1] if name in _TABLES else _VIEWS[name][1]
        stale = (datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)).strftime("%Y-%m-%d")
        qualifiers = qualifiers.replace("{stale}", stale)
        # Newest activity first; search's default is "best match", which is
        # meaningless with no free-text term.
        return f"repo:{self.owner}/{self.repo} {qualifiers} sort:updated-desc"

    # -- Provider interface ---------------------------------------------------

    def list_tables(self) -> list:
        return list(_TABLES)

    def list_views(self) -> list:
        return list(_VIEWS)

    def is_editable(self, item_type: str) -> bool:
        return False

    def is_filterable(self, item_type: str) -> bool:
        return False  # v1: no column-filter -> search-qualifier translation yet

    def get_schema(self, name, item_type) -> list:
        return list(_PR_COLUMNS if self._kind(name) == "pr" else _ISSUE_COLUMNS)

    def get_page(self, name, item_type, filters, cursor, page_size, sort=None) -> RowPage:
        kind = self._kind(name)
        columns = self.get_schema(name, item_type)
        query = self._search_string(name)
        want = page_size or _MAX_PER_REQUEST

        rows, keys = [], []
        after = cursor
        has_more = False
        while len(rows) < want:
            data = self._graphql(_SEARCH_QUERY, {
                "q": query, "first": min(_MAX_PER_REQUEST, want - len(rows)), "after": after,
            })["search"]
            for node in data["nodes"]:
                # search(type: ISSUE) can return either kind; the query
                # already pins is:pr / is:issue, so this is just a guard
                # against empty inline-fragment nodes.
                if not node or "number" not in node:
                    continue
                rows.append(_pr_row(node) if kind == "pr" else _issue_row(node))
                keys.append(RowKey(node["url"]))
            page_info = data["pageInfo"]
            has_more = page_info["hasNextPage"]
            after = page_info["endCursor"]
            if not has_more:
                break

        # GraphQL search stops serving results past 1000 per query; without
        # this a "more" indicator would page forward into an error.
        if has_more and data["issueCount"] > _SEARCH_RESULT_CAP and len(rows) + self._offset(cursor) >= _SEARCH_RESULT_CAP:
            has_more = False
        return RowPage(columns, rows, keys, next_cursor=after if has_more else None, has_more=has_more)

    @staticmethod
    def _offset(cursor) -> int:
        """GitHub cursors are opaque base64 of `cursor:<n>`, where n is the
        offset - decoded only to enforce the 1000-result cap."""
        if not cursor:
            return 0
        import base64
        try:
            return int(base64.b64decode(cursor).decode().split(":")[-1])
        except (ValueError, UnicodeDecodeError):
            return 0

    def get_definition(self, name, item_type) -> str:
        if name in _VIEWS:
            desc = _VIEWS[name][2]
            head = f"-- {desc}"
        else:
            head = f"-- All {'pull requests' if name == 'pull_requests' else 'issues'}"
        lines = [f"-- GitHub {self.owner}/{self.repo}: {name}", head, "", "-- search query:", self._search_string(name), "", "-- column : type"]
        lines += [f"{c.name} : {c.type_name}" for c in self.get_schema(name, item_type)]
        return "\n".join(lines)

    def get_diagram_model(self) -> DiagramModel:
        return DiagramModel()

    def get_row_url(self, name, item_type, row_key: RowKey) -> str:
        # The row key *is* the PR/issue's html url.
        return row_key.value

    def delete_item(self, name, item_type) -> None:
        raise NotImplementedError("The GitHub provider is read-only")
