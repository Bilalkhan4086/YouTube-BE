import os
import time

from sqlalchemy import JSON, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Capacity(Base):
    __tablename__ = 'media_capacity'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Job(Base):
    __tablename__ = 'media_jobs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    stage: Mapped[str] = mapped_column(String(30), default='queued')
    created: Mapped[float] = mapped_column(Float, default=time.time)
    started: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    publish_after: Mapped[float] = mapped_column(Float, default=0, index=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    run_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cleanup_after: Mapped[float] = mapped_column(Float, default=0, server_default='0', index=True)
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')


class Admission(Base):
    __tablename__ = 'media_admissions'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float] = mapped_column(Float, index=True)


class Database:
    def __init__(self, url: str):
        from sqlalchemy.engine import make_url

        url = make_url(url)
        if url.drivername in {'postgres', 'postgresql'}:
            url = url.set(drivername='postgresql+psycopg')
        if url.get_backend_name() == 'postgresql' and os.getenv('DYNO'):
            if 'sslmode' not in url.query:
                url = url.update_query_dict({'sslmode': 'require'})
        connect_args = {}
        if make_url(url).get_backend_name() == 'postgresql':
            connect_args = {'connect_timeout': 5,
                            'options': '-c statement_timeout=15000 -c lock_timeout=5000'}
        self.engine = create_engine(url, pool_pre_ping=True, pool_timeout=10, pool_size=2, max_overflow=1,
                                    connect_args=connect_args)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        from app.distributed.migrate import upgrade

        upgrade(self.engine)
