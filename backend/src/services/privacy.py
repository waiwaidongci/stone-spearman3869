"""差分隐私发布与隐私预算台账服务。

预算纪律：
- 全局单行预算，同批所有版本共用；接口层面不可重置，数据更正不恢复额度；
- 公开动作之前先扣费（申请即扣费，扣费与台账写入同一事务、行锁串行化）；
- 并发申请在行锁上排队，余额不足整笔拒绝，绝不超支；
- 计算中断/失败后的重试沿用已持久化噪声，不重新扣费；
- 公开后撤销只改状态，台账与已花额度保持不变（不退费）；
- 结果文件与扣费记录（指纹）不一致时禁止公开。
"""
import os
import json
import csv
import io
from pathlib import Path

import numpy as np
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config import privacy as cfg
from src.models.privacy import PrivacyBudget, PrivacyRelease
from src.services import studies
from src.services import dp_algorithm as dp
from src.services.audits import log
from src.utils.serializers import record, now, checksum

ARTIFACT_DIR = Path(os.environ.get('DP_ARTIFACT_DIR', '/app/artifacts'))


def ensure_budget(db):
    """取全局预算行；不存在则按环境变量初始化（仅一次，之后不可重置）。"""
    row = db.scalar(select(PrivacyBudget).where(PrivacyBudget.id == 1))
    if row is None:
        try:
            row = PrivacyBudget(id=1, total_epsilon=cfg.TOTAL_EPSILON, spent_epsilon=0.0,
                                updated_at=now())
            db.add(row)
            db.flush()
        except IntegrityError:
            # 并发首刷时另一个事务已建账：回滚后重读，绝不重复建行。
            db.rollback()
            row = db.scalar(select(PrivacyBudget).where(PrivacyBudget.id == 1))
    return row


def budget_view(db):
    row = ensure_budget(db)
    return {'total_epsilon': row.total_epsilon,
            'spent_epsilon': row.spent_epsilon,
            'remaining_epsilon': round(row.total_epsilon - row.spent_epsilon, 12),
            'resettable': False}


def _validate_epsilon(value, remaining):
    try:
        eps = float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, '隐私预算 ε 必须是数值')
    if not (cfg.MIN_EPSILON <= eps <= cfg.MAX_EPSILON):
        raise HTTPException(422, f'单次 ε 必须在 {cfg.MIN_EPSILON} 到 {cfg.MAX_EPSILON} 之间')
    if eps > remaining + 1e-12:
        raise HTTPException(422, '剩余隐私预算不足，申请被拒绝；预算不可重置或恢复')
    return eps


def _next_ledger_seq(db):
    last = db.scalar(select(PrivacyRelease.ledger_seq).order_by(PrivacyRelease.ledger_seq.desc()).limit(1))
    return 1 if last is None else last + 1


def create_release(db, data, user, redis):
    studies.require(db, data.study_id)
    rows = studies.observations(db, data.study_id)
    if not rows:
        raise HTTPException(422, '先录入至少一条业务观测')
    if db.scalar(select(PrivacyRelease).where(
            PrivacyRelease.study_id == data.study_id, PrivacyRelease.name == data.name)):
        raise HTTPException(409, '同批次下发布名称已存在')
    snapshot = [{'source_key': x['source_key'], 'values': x['values']} for x in rows]
    # 先做网格口径校验，避免扣了费才发现数据无法发布。
    dp.joint_counts(snapshot)

    # 行锁串行化所有扣费，并发申请不得超支。
    budget = db.scalar(select(PrivacyBudget).where(PrivacyBudget.id == 1).with_for_update())
    if budget is None:
        budget = ensure_budget(db)
        db.flush()
        db.refresh(budget, with_for_update=True)
    remaining = budget.total_epsilon - budget.spent_epsilon
    eps = _validate_epsilon(data.epsilon, remaining)

    seq = _next_ledger_seq(db)
    seed = int(cfg.MASTER_SEED) + seq
    release = PrivacyRelease(
        study_id=data.study_id, name=data.name, status='reserved', epsilon=eps, seed=seed,
        snapshot=snapshot, checksum=checksum(snapshot), noise_draws=None, result=None,
        error='', ledger_seq=seq, charged_at=now(), created_at=now())
    db.add(release)
    # 公开前扣费：扣费与台账在同一事务提交。
    budget.spent_epsilon = round(budget.spent_epsilon + eps, 12)
    budget.updated_at = now()
    db.flush()
    log(db, user, 'privacy.charge', f'ledger#{seq} release={release.id} eps={eps}')
    try:
        db.commit()
    except IntegrityError:
        # 兜底：CHECK 约束或唯一约束在并发竞争下触发，整笔回滚、绝不超支。
        db.rollback()
        raise HTTPException(422, '并发申请竞争导致本笔未扣费成功，请按剩余预算重试')
    redis.rpush(cfg.QUEUE_KEY, release.id)
    return record(release)


def get_release(db, release_id):
    obj = db.get(PrivacyRelease, release_id)
    if not obj:
        raise HTTPException(404, '发布记录不存在')
    return record(obj)


def list_releases(db):
    return [record(x) for x in
            db.scalars(select(PrivacyRelease).order_by(PrivacyRelease.id.desc()))]


def compute(release_id, db, write_artifact=True):
    """worker 调用：使用持久化噪声计算；重试不重新抽样、不重复扣费。"""
    obj = db.get(PrivacyRelease, release_id)
    if not obj or obj.status in ('published', 'revoked', 'ready'):
        return
    if checksum(obj.snapshot) != obj.checksum:
        raise ValueError('发布快照校验失败')
    obj.status = 'computing'
    db.commit()

    # 先按固定种子生成噪声候选；若已持久化则必须沿用，保证中断恢复结果一致。
    grid = dp.joint_counts(obj.snapshot)
    candidate, _ = dp.noisy_grid(grid, obj.epsilon, obj.seed)
    if obj.noise_draws is not None:
        draws = np.asarray(obj.noise_draws, dtype=float)
        if draws.shape != candidate.shape:
            raise ValueError('持久化噪声结构不一致，已中止以保护预算口径')
    else:
        draws = candidate
        obj.noise_draws = draws.tolist()
        db.commit()

    # 复用统一的结果组装口径，以持久化噪声替换随机抽样；后处理全部在 assemble_result 内完成。
    result = dp.assemble_result(grid, draws, obj.epsilon, obj.seed)
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True)
    obj.result = result
    obj.result_fingerprint = checksum(result)
    obj.error = ''
    obj.status = 'ready'
    if write_artifact:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / f'{cfg.ARTIFACT_PREFIX}{obj.id}.json'
        path.write_text(payload, encoding='utf-8')
        obj.artifact_path = str(path)
    db.commit()
    return obj


def retry_release(db, release_id, user, redis):
    """失败任务重试：沿用同一扣费记录与持久化噪声，不重复扣费。"""
    obj = db.get(PrivacyRelease, release_id)
    if not obj:
        raise HTTPException(404, '发布记录不存在')
    if obj.status != 'failed':
        raise HTTPException(409, '仅失败状态的发布可以重试')
    obj.status = 'reserved'
    obj.error = ''
    log(db, user, 'privacy.retry', f'release={release_id} ledger#{obj.ledger_seq} 不重复扣费')
    db.commit()
    redis.rpush(cfg.QUEUE_KEY, release_id)
    return record(obj)


def publish(db, release_id, user):
    """公开：校验扣费台账与结果文件一致后才允许；撤销不退费。"""
    obj = db.get(PrivacyRelease, release_id)
    if not obj:
        raise HTTPException(404, '发布记录不存在')
    if obj.status == 'published':
        raise HTTPException(409, '该发布已公开')
    if obj.status == 'revoked':
        raise HTTPException(409, '已撤销的发布不能重新公开（预算不退回，请另申请）')
    if obj.status != 'ready' or not obj.result or not obj.result_fingerprint:
        raise HTTPException(409, '发布结果尚未就绪，不能公开')
    # 扣费记录必须存在且与发布金额一致。
    if obj.charged_at is None or not (obj.epsilon and obj.ledger_seq):
        raise HTTPException(409, '扣费记录缺失，禁止公开')
    if checksum(obj.result) != obj.result_fingerprint:
        raise HTTPException(409, '结果与扣费记录指纹不一致，禁止公开')
    if obj.artifact_path:
        path = Path(obj.artifact_path)
        if not path.exists():
            raise HTTPException(409, '公开结果文件缺失，禁止公开')
        on_disk = json.loads(path.read_text(encoding='utf-8'))
        if checksum(on_disk) != obj.result_fingerprint:
            raise HTTPException(409, '结果文件与扣费记录不一致，禁止公开')
    obj.status = 'published'
    obj.published_at = now()
    log(db, user, 'privacy.publish', f'release={release_id} ledger#{obj.ledger_seq} eps={obj.epsilon}')
    db.commit()
    return record(obj)


def revoke(db, release_id, user):
    obj = db.get(PrivacyRelease, release_id)
    if not obj:
        raise HTTPException(404, '发布记录不存在')
    if obj.status != 'published':
        raise HTTPException(409, '仅已公开的发布可以撤销')
    obj.status = 'revoked'
    obj.revoked_at = now()
    # 显式不退费：台账与 spent_epsilon 保持不动。
    log(db, user, 'privacy.revoke', f'release={release_id} 预算不退回 eps={obj.epsilon}')
    db.commit()
    return record(obj)


def export_public(db, release_id, user):
    """导出公开 CSV：只含加噪计数与机制说明，不含原值或精确人数。"""
    obj = db.get(PrivacyRelease, release_id)
    if not obj:
        raise HTTPException(404, '发布记录不存在')
    if obj.status != 'published':
        raise HTTPException(409, '仅已公开的发布可以导出公开文件')
    r = obj.result
    stream = io.StringIO()
    w = csv.writer(stream)
    w.writerow(['发布编号', '隐私机制指标', '值'])
    for k, v in r['metrics'].items():
        w.writerow([release_id, k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v])
    w.writerow([release_id, '机制说明', r['mechanism']])
    w.writerow([release_id, '边界说明', r['caveat']])
    w.writerow([release_id, '解释', r['interpretation']])
    w.writerow([])
    w.writerow(['加噪联合计数（行=孔隙率网格组，列=吸水率网格组；空格同样加噪）'])
    w.writerow(['网格组'] + r['grid_labels']['cols'])
    for label, row_counts in zip(r['grid_labels']['rows'], r['grid_counts']):
        w.writerow([label] + row_counts)
    log(db, user, 'privacy.export', f'release={release_id}')
    db.commit()
    return '\ufeff' + stream.getvalue()
