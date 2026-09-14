import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Index,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    text,
    update,
)


class Store:
    def __init__(self, url: str | None = None):
        self.engine = create_engine(
            url or os.getenv("DATABASE_URL", "sqlite:///./sidequest.db"),
            connect_args={"check_same_thread": False},
        )
        metadata = MetaData()
        self.documents = Table(
            "documents",
            metadata,
            Column("id", String, primary_key=True),
            Column("owner", String, nullable=False, index=True),
            Column("kind", String, nullable=False),
            Column("data", JSON, nullable=False),
            Column("status", String, default="ready"),
            Column("created_at", String, nullable=False),
        )
        Index(
            "idx_documents_owner_kind_created",
            self.documents.c.owner,
            self.documents.c.kind,
            self.documents.c.created_at,
        )
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA optimize"))

    def put(self, owner, kind, data, id=None, status="ready"):
        id = id or uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                self.documents.insert().values(
                    id=id,
                    owner=owner,
                    kind=kind,
                    data=data,
                    status=status,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        return id

    def get(self, owner, id):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.documents).where(
                        self.documents.c.id == id, self.documents.c.owner == owner
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def list(self, owner, kind, limit=30):
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.documents)
                .where(self.documents.c.owner == owner, self.documents.c.kind == kind)
                .order_by(self.documents.c.created_at.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def change(self, owner, id, data=None, status=None, expected=None):
        query = update(self.documents).where(
            self.documents.c.id == id, self.documents.c.owner == owner
        )
        if expected:
            query = query.where(self.documents.c.status.in_(expected))
        values = {}
        if data is not None:
            values["data"] = data
        if status is not None:
            values["status"] = status
        with self.engine.begin() as conn:
            return conn.execute(query.values(**values)).rowcount > 0

    def delete(self, owner, id):
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    self.documents.delete().where(
                        self.documents.c.owner == owner, self.documents.c.id == id
                    )
                ).rowcount
                > 0
            )

    def recover(self):
        # Replay is cheap and deterministic; interrupted runs are explicit failures, never false successes.
        with self.engine.begin() as conn:
            conn.execute(
                update(self.documents)
                .where(
                    self.documents.c.kind == "run",
                    self.documents.c.status.in_(["queued", "running"]),
                )
                .values(status="interrupted")
            )
