import os

from .base import (
    Provider, Column, RowKey, RowPage, Capabilities,
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

    if not db_url.startswith(("sqlite://", "postgresql://", "mysql://")):
        db_url = f"sqlite:///{os.path.abspath(db_url)}"
    from .sqlalchemy_provider import SqlAlchemyProvider
    return SqlAlchemyProvider(db_url)
