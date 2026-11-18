import os,time,json,logging
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from src.config.database import Session
from src.models.run import Run
from src.models.privacy import PrivacyRelease
from src.models import user,study,observation,review,audit
from src.config import privacy as privacy_cfg
from src.services.algorithm import calculate
from src.services import privacy as privacy_service
from src.utils.serializers import checksum
redis=Redis.from_url(os.environ['REDIS_URL'])
Path('/app/artifacts').mkdir(exist_ok=True)
# Recover jobs interrupted after dequeue. A project runs exactly one worker.
# 隐私任务恢复后沿用已持久化噪声，重试不重新抽样、不重复扣费。
with Session() as db:
    pending=list(db.scalars(select(Run).where(Run.status.in_(['queued','running']))))
    for run in pending: redis.rpush('analysis-queue',run.id)
    privacy_pending=list(db.scalars(select(PrivacyRelease).where(
        PrivacyRelease.status.in_(['reserved','computing']))))
    for rel in privacy_pending: redis.rpush(privacy_cfg.QUEUE_KEY,rel.id)

def handle_analysis(id):
    with Session() as db:
        run=db.get(Run,id)
        if not run or run.status in ('completed','failed'): return
        run.status='running';db.commit()
        try:
            if checksum(run.snapshot)!=run.checksum: raise ValueError('输入快照校验失败')
            result=calculate(run.snapshot,run.parameters)
            result['input_sha256']=run.checksum
            run.result=result;run.status='completed'
            Path(f'/app/artifacts/run-{id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
        except Exception as e:
            logging.exception('analysis failed');run.status='failed';run.error=str(e)
        db.commit()

def handle_privacy(id):
    with Session() as db:
        rel=db.get(PrivacyRelease,id)
        if not rel or rel.status in ('ready','published','revoked'): return
        try:
            privacy_service.compute(id,db)
        except Exception as e:
            logging.exception('privacy release failed')
            fresh=db.get(PrivacyRelease,id)
            if fresh and fresh.status not in ('ready','published','revoked'):
                fresh.status='failed';fresh.error=str(e);db.commit()

while True:
    Path('/tmp/worker-heartbeat').touch()
    # 两队列独立，隐私计算与内部研究互不阻塞。
    job=redis.blpop(['analysis-queue',privacy_cfg.QUEUE_KEY],timeout=2)
    if not job: continue
    queue_name,raw=job
    id=int(raw)
    if queue_name==privacy_cfg.QUEUE_KEY.encode():
        handle_privacy(id)
    else:
        handle_analysis(id)
