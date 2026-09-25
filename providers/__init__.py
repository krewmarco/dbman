import os

from .base import (
    Provider, Column, RowKey, RowPage, RowTarget, Capabilities,
    DiagramModel, DiagramNode, DiagramEdge,
)


def create_provider(db_url: str) -> Provider:
    """Scheme dispatch: pick a Provider implementation for a connection string.

    To add a new provider: implement Provider (providers/base.py), set its
    capabilities honestly, and add one elif branch here.
    """
    if db_url.startswith(("couchdb://", "couchdbs://")):
        from .couchdb_provider import CouchDBProvider
        return CouchDBProvider(db_url)

    if db_url.startswith("notion://"):
        from .notion_provider import NotionProvider
        return NotionProvider(db_url)

    if db_url.startswith("github://"):
        from .github_provider import GitHubProvider
        return GitHubProvider(db_url)

    if not db_url.startswith(("sqlite://", "postgresql://", "mysql://")):
        db_url = f"sqlite:///{os.path.abspath(db_url)}"

    # dbman's own config db: SQLite browsing with a guard against taking the
    # config apart. Matched by file contents, not only the name - see
    # meta_db.is_config_db.
    if db_url.startswith("sqlite:///"):
        import meta_db
        if meta_db.is_config_db(meta_db.sqlite_url_path(db_url)):
            from .dbman_meta_provider import DbmanMetaProvider
            return DbmanMetaProvider(db_url)

    from .sqlalchemy_provider import SqlAlchemyProvider
    return SqlAlchemyProvider(db_url)
