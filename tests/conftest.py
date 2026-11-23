import os
import tempfile

# 必须在导入 app 之前设置数据库路径(每个测试会话一个独立文件)
_tmpdir = tempfile.mkdtemp(prefix="gauge-spec-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def client():
    return TestClient(app)
