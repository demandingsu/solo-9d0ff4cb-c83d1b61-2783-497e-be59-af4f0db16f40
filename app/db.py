"""数据库连接与会话管理(SQLite)。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./specs.db")

# 确保 SQLite 文件所在目录存在(如 /data 卷)
if DATABASE_URL.startswith("sqlite:///") and ":memory:" not in DATABASE_URL:
    _path = DATABASE_URL.replace("sqlite:///", "", 1)
    _dir = os.path.dirname(_path)
    if _dir:
        os.makedirs(_dir, exist_ok=True)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _):  # 开启外键约束
    try:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
