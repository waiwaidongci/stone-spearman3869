"""差分隐私模块的 HTTP 端到端测试（FastAPI TestClient + SQLite + fakeredis）。"""
import os
import os.path
from pathlib import Path

import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('DP_TOTAL_EPSILON', '1.0')
os.environ.setdefault('DP_ARTIFACT_DIR', '/tmp/dp-e2e-artifacts')

import fakeredis
# 任何按 URL 建 Redis 的代码（限流、隐私服务）都落到 fakeredis。
import redis as redis_pkg
redis_pkg.Redis.from_url = staticmethod(lambda *a, **k: fakeredis.FakeRedis())
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
import src.config.database as database
from src.services import privacy as ps
import src.middlewares.rate_limit as rl
from src.models.user import User
from src.models.study import Study
from src.models.observation import Observation
from src.models.privacy import PrivacyRelease
from src.utils.security import hash_password


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine('sqlite:///:memory:',
                           connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'Session', Session)
    monkeypatch.setattr(ps, 'ARTIFACT_DIR', Path(os.environ['DP_ARTIFACT_DIR']))
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr('redis.Redis.from_url', lambda *a, **k: fake)
    rl.redis = fake
    Path(os.environ['DP_ARTIFACT_DIR']).mkdir(parents=True, exist_ok=True)

    with Session() as db:
        db.add(User(email='admin@demo.local', role='admin',
                    password_hash=hash_password('Research2026!')))
        db.add(User(email='viewer@demo.local', role='viewer',
                    password_hash=hash_password('Research2026!')))
        study = Study(name='批次端到端', source_filename='s.csv', description='d')
        db.add(study); db.flush()
        for i in range(40):
            db.add(Observation(study_id=study.id, source_key=f'V{i:03}',
                               values={'porosity': 4 + i * 2.2, 'absorption': 1 + i * 1.9}))
        db.commit()

    from src.main import app
    with TestClient(app) as c:
        yield c, fake, Session


def _token(c, email):
    r = c.post('/auth/login', json={'email': email, 'password': 'Research2026!'})
    assert r.status_code == 200
    return r.json()['token']


def _h(t):
    return {'Authorization': f'Bearer {t}'}


def test_full_privacy_flow(client):
    c, fake, Session = client
    admin = _token(c, 'admin@demo.local')
    viewer = _token(c, 'viewer@demo.local')

    # 预算视图
    b = c.get('/privacy/budget', headers=_h(admin)).json()
    assert b['total_epsilon'] == 1.0 and b['spent_epsilon'] == 0.0 and b['resettable'] is False

    # 申请即扣费并入队
    r = c.post('/privacy/releases', headers=_h(admin),
               json={'study_id': 1, 'name': '端到端公开', 'epsilon': 0.3})
    assert r.status_code == 200, r.text
    rid = r.json()['id']
    assert r.json()['status'] == 'reserved'
    assert fake.llen('privacy-queue') == 1
    b2 = c.get('/privacy/budget', headers=_h(admin)).json()
    assert b2['spent_epsilon'] == pytest.approx(0.3)

    # 模拟 worker 出队计算（直接调用服务，复用同一套 compute 逻辑）
    with Session() as db:
        ps.compute(rid, db)
    detail = c.get(f'/privacy/releases/{rid}', headers=_h(admin)).json()
    assert detail['status'] == 'ready'
    assert detail['result_fingerprint']
    assert detail['noise_draws'] is not None
    assert len(detail['result']['grid_counts']) == 10

    # 未公开前 viewer 不能导出
    assert c.get(f'/privacy/releases/{rid}/export', headers=_h(viewer)).status_code == 409

    # viewer 不能执行公开（需要 admin）
    assert c.post(f'/privacy/releases/{rid}/publish', headers=_h(viewer)).status_code == 403

    # admin 公开，随后可导出
    pub = c.post(f'/privacy/releases/{rid}/publish', headers=_h(admin))
    assert pub.status_code == 200 and pub.json()['status'] == 'published'
    export = c.get(f'/privacy/releases/{rid}/export', headers=_h(admin))
    assert export.status_code == 200 and 'text/csv' in export.headers['content-type']
    body = export.content.decode('utf-8')
    assert 'V000' not in body and '独立试件数' not in body
    assert '加噪后总计数(非精确人数)' in body and 'Laplace' in body

    # 撤销不退费
    rev = c.post(f'/privacy/releases/{rid}/revoke', headers=_h(admin)).json()
    assert rev['status'] == 'revoked'
    b3 = c.get('/privacy/budget', headers=_h(admin)).json()
    assert b3['spent_epsilon'] == pytest.approx(0.3)
    # 撤销后不能再公开、不能导出
    assert c.post(f'/privacy/releases/{rid}/publish', headers=_h(admin)).status_code == 409
    assert c.get(f'/privacy/releases/{rid}/export', headers=_h(admin)).status_code == 409

    # 继续申请直到余额耗尽：剩 0.7，再两笔 0.3 + 一笔 0.2 后超支被拒
    for name, eps in [('第二次', 0.3), ('第三次', 0.3)]:
        rr = c.post('/privacy/releases', headers=_h(admin),
                    json={'study_id': 1, 'name': name, 'epsilon': eps})
        assert rr.status_code == 200
    over = c.post('/privacy/releases', headers=_h(admin),
                  json={'study_id': 1, 'name': '超支笔', 'epsilon': 0.2})
    assert over.status_code == 422
    final = c.get('/privacy/budget', headers=_h(admin)).json()
    assert final['spent_epsilon'] == pytest.approx(0.9)
    assert final['remaining_epsilon'] == pytest.approx(0.1)


def test_invalid_epsilon_and_auth(client):
    c, _, _ = client
    admin = _token(c, 'admin@demo.local')
    # 未认证
    assert c.get('/privacy/budget').status_code == 401
    # ε 越界
    bad = c.post('/privacy/releases', headers=_h(admin),
                 json={'study_id': 1, 'name': '越界', 'epsilon': 5.0})
    assert bad.status_code == 422
    # 不存在的批次
    bad2 = c.post('/privacy/releases', headers=_h(admin),
                  json={'study_id': 999, 'name': '无批次', 'epsilon': 0.1})
    assert bad2.status_code == 404
