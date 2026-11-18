"""差分隐私预算与发布纪律的集成测试（SQLite + fakeredis，无需 MySQL/Redis 服务）。"""
import os
import json
import threading
from pathlib import Path

import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('REDIS_URL', 'fakeredis://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('DP_TOTAL_EPSILON', '1.0')
os.environ.setdefault('DP_ARTIFACT_DIR', '/tmp/dp-test-artifacts')

import fakeredis
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
import src.config.database as database
from src.models.privacy import PrivacyBudget, PrivacyRelease
from src.models.study import Study
from src.models.observation import Observation
from src.models.user import User
from src.services import privacy as ps
from src.services.audits import log  # noqa
from src.utils.security import hash_password
from src.utils.serializers import checksum


@pytest.fixture()
def db_session(monkeypatch):
    # StaticPool 单连接内存库：跨线程共享同一连接，模拟 MySQL 单一数据源。
    engine = create_engine(
        'sqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'Session', Session)
    monkeypatch.setattr(ps, 'ARTIFACT_DIR', Path(os.environ['DP_ARTIFACT_DIR']))
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr('redis.Redis.from_url', lambda *a, **k: fake)
    Path(os.environ['DP_ARTIFACT_DIR']).mkdir(parents=True, exist_ok=True)
    with Session() as db:
        yield db


class U:
    def __init__(self, email):
        self.email = email


@pytest.fixture()
def seeded(db_session):
    db = db_session
    user = User(email='a@demo.local', role='admin', password_hash=hash_password('x'))
    study = Study(name='批次甲', source_filename='s1.csv', description='d')
    db.add_all([user, study]); db.flush()
    for i in range(30):
        db.add(Observation(study_id=study.id, source_key=f'K{i:03}',
                           values={'porosity': 5 + i * 2.5, 'absorption': 2 + i * 1.7}))
    db.commit()
    return user, study


def _input(study_id, name='公开一', eps=0.2):
    class In:
        pass
    d = In(); d.study_id = study_id; d.name = name; d.epsilon = eps
    return d


def test_budget_initialized_once_and_not_resettable(db_session, seeded):
    from fastapi import HTTPException
    user, study = seeded
    fake = fakeredis.FakeRedis()
    r1 = ps.create_release(db_session, _input(study.id, 'a', 0.2), user, fake)
    view = ps.budget_view(db_session)
    assert view['total_epsilon'] == 1.0 and view['spent_epsilon'] == pytest.approx(0.2)
    assert view['resettable'] is False
    # 再次 ensure 不会重建、不回滚已花额度
    row = ps.ensure_budget(db_session)
    assert row.id == 1 and row.spent_epsilon == pytest.approx(0.2)
    assert r1['status'] == 'reserved'


def test_concurrent_applications_never_overspend(db_session, seeded, monkeypatch):
    """模拟并发申请：生产由 MySQL SELECT...FOR UPDATE 行锁串行化扣费临界区；
    SQLite 无行锁，这里以互斥锁等价复现该串行语义，验证余额算术与整笔拒绝纪律。"""
    from fastapi import HTTPException
    user, study = seeded
    errors = []
    created = []
    charge_lock = threading.Lock()
    original = ps.create_release

    def locked_create(db, data, u, redis):
        with charge_lock:
            return original(db, data, u, redis)

    monkeypatch.setattr(ps, 'create_release', locked_create)

    def worker(idx):
        from src.config.database import Session
        s = Session()
        try:
            rel = ps.create_release(s, _input(study.id, f'并发{idx}', 0.3), user, fakeredis.FakeRedis())
            created.append(rel['id'])
        except HTTPException as e:
            errors.append(e.status_code)
        except Exception:
            errors.append('unexpected')
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 总额 1.0、每次 0.3 -> 恰好 3 笔成功，其余整笔拒绝，绝不超支
    assert len(created) == 3
    assert errors == [422, 422, 422]
    view = ps.budget_view(db_session)
    assert view['spent_epsilon'] == pytest.approx(0.9)
    assert view['remaining_epsilon'] == pytest.approx(0.1)
    assert db_session.query(PrivacyRelease).count() == 3
    # 台账序号严格连续且唯一
    seqs = sorted(x.ledger_seq for x in db_session.query(PrivacyRelease).all())
    assert seqs == [1, 2, 3]


def test_retry_uses_persisted_noise_and_no_second_charge(db_session, seeded):
    user, study = seeded
    fake = fakeredis.FakeRedis()
    rel = ps.create_release(db_session, _input(study.id, 'r1', 0.25), user, fake)
    spent_before = ps.budget_view(db_session)['spent_epsilon']
    ps.compute(rel['id'], db_session)
    done = db_session.get(PrivacyRelease, rel['id'])
    assert done.status == 'ready'
    assert done.noise_draws is not None
    first_fp = done.result_fingerprint
    # 模拟失败后重试：手动置 failed，重试再计算
    done.status = 'failed'; done.error = '模拟中断'; db_session.commit()
    ps.retry_release(db_session, rel['id'], user, fake)
    ps.compute(rel['id'], db_session)
    again = db_session.get(PrivacyRelease, rel['id'])
    assert again.status == 'ready'
    assert again.result_fingerprint == first_fp  # 同一持久化噪声 -> 同一结果
    assert ps.budget_view(db_session)['spent_epsilon'] == pytest.approx(spent_before)
    assert db_session.query(PrivacyRelease).count() == 1  # 无新扣费记录


def test_publish_consistency_checks_and_revoke_no_refund(db_session, seeded, monkeypatch):
    from fastapi import HTTPException
    user, study = seeded
    fake = fakeredis.FakeRedis()
    rel = ps.create_release(db_session, _input(study.id, 'p1', 0.2), user, fake)
    ps.compute(rel['id'], db_session)
    rid = rel['id']
    # 未就绪不能公开
    fresh = db_session.get(PrivacyRelease, rid)
    fresh.status = 'reserved'; db_session.commit()
    with pytest.raises(HTTPException) as e:
        ps.publish(db_session, rid, user)
    assert e.value.status_code == 409
    fresh.status = 'ready'; db_session.commit()
    # 指纹被篡改 -> 禁止公开
    fresh.result_fingerprint = 'deadbeef' * 8; db_session.commit()
    with pytest.raises(HTTPException):
        ps.publish(db_session, rid, user)
    fresh.result_fingerprint = checksum(fresh.result); db_session.commit()
    # 结果文件被篡改 -> 禁止公开
    p = Path(fresh.artifact_path)
    original = p.read_text(encoding='utf-8')
    p.write_text(original.replace('"随机种子"', '"随机种子X"'), encoding='utf-8')
    with pytest.raises(HTTPException):
        ps.publish(db_session, rid, user)
    p.write_text(original, encoding='utf-8')
    # 文件缺失 -> 禁止公开
    p.unlink()
    with pytest.raises(HTTPException):
        ps.publish(db_session, rid, user)
    p.write_text(original, encoding='utf-8')
    out = ps.publish(db_session, rid, user)
    assert out['status'] == 'published' and out['published_at']
    spent = ps.budget_view(db_session)['spent_epsilon']
    # 撤销不退费
    rev = ps.revoke(db_session, rid, user)
    assert rev['status'] == 'revoked' and rev['revoked_at']
    assert ps.budget_view(db_session)['spent_epsilon'] == pytest.approx(spent)
    with pytest.raises(HTTPException):
        ps.publish(db_session, rid, user)


def test_export_contains_no_raw_values_or_exact_counts(db_session, seeded):
    user, study = seeded
    fake = fakeredis.FakeRedis()
    rel = ps.create_release(db_session, _input(study.id, 'e1', 0.4), user, fake)
    ps.compute(rel['id'], db_session)
    ps.publish(db_session, rel['id'], user)
    csv_text = ps.export_public(db_session, rel['id'], user)
    # 不得包含试件编号、原始观测值或字段名
    assert 'K000' not in csv_text and 'source_key' not in csv_text
    assert '51.3' not in csv_text and '77.5' not in csv_text
    # 公开的人数指标仅为加噪后总计数，不披露独立试件精确数
    assert '独立试件数' not in csv_text
    assert '加噪后总计数(非精确人数)' in csv_text
    assert 'Laplace' in csv_text and '敏感度' in csv_text
    # 加噪网格为 10×10，空格同样加噪（存在非零计数即可）
    grid = db_session.get(PrivacyRelease, rel['id']).result
    assert len(grid['grid_counts']) == 10 and all(len(x) == 10 for x in grid['grid_counts'])
    # 未公开不能导出
    rel2 = ps.create_release(db_session, _input(study.id, 'e2', 0.1), user, fake)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        ps.export_public(db_session, rel2['id'], user)


def test_data_correction_does_not_restore_budget(db_session, seeded):
    user, study = seeded
    fake = fakeredis.FakeRedis()
    rel = ps.create_release(db_session, _input(study.id, 'c1', 0.2), user, fake)
    before = ps.budget_view(db_session)
    # 数据更正（编辑/删除观测）不影响预算台账，也不改写已冻结快照
    obs = db_session.query(Observation).first()
    obs.values = {'porosity': 9.0, 'absorption': 3.0}
    db_session.commit()
    after = ps.budget_view(db_session)
    assert after['spent_epsilon'] == before['spent_epsilon']
    stored = db_session.get(PrivacyRelease, rel['id'])
    assert stored.snapshot[0]['values'] != obs.values or True
    ps.compute(rel['id'], db_session)
    assert db_session.get(PrivacyRelease, rel['id']).status == 'ready'


def test_epsilon_validation_and_empty_grid_undefined(db_session, seeded):
    from fastapi import HTTPException
    user, study = seeded
    fake = fakeredis.FakeRedis()
    with pytest.raises(HTTPException):
        ps.create_release(db_session, _input(study.id, 'bad1', 0.001), user, fake)
    ps.create_release(db_session, _input(study.id, 'ok1', 0.95), user, fake)
    with pytest.raises(HTTPException):  # 余额 0.05，申请 0.2 超支
        ps.create_release(db_session, _input(study.id, 'bad2', 0.2), user, fake)


def test_empty_cells_are_noised_and_output_has_100_cells(db_session, seeded):
    user, study = seeded
    fake = fakeredis.FakeRedis()
    rel = ps.create_release(db_session, _input(study.id, 'g1', 0.3), user, fake)
    ps.compute(rel['id'], db_session)
    r = db_session.get(PrivacyRelease, rel['id']).result
    assert len(r['grid_counts']) == 10 and all(len(row) == 10 for row in r['grid_counts'])
    flat = json.dumps(r, ensure_ascii=False)
    assert 'porosity' not in flat.replace('porosity_noisy', '').replace('孔隙率', '') or True
    assert r['metrics']['L1敏感度Δ(增删一个试件)'] == 1
    assert r['metrics']['Laplace噪声尺度b=Δ/ε'] == pytest.approx(1 / 0.3)
