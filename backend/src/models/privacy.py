from sqlalchemy import Column,Integer,String,Float,Text,JSON,ForeignKey,UniqueConstraint,CheckConstraint
from src.config.database import Base


class PrivacyBudget(Base):
    """全局单行预算总账：同批所有版本共用，额度不可重置、不恢复。"""
    __tablename__ = 'privacy_budgets'
    __table_args__ = (
        # 硬约束兜底：即使底层锁语义不同，已花额度也绝不可能静默超过总额。
        CheckConstraint('spent_epsilon <= total_epsilon', name='ck_privacy_budget_not_overspent'),
    )
    id = Column(Integer, primary_key=True)
    total_epsilon = Column(Float, nullable=False)
    spent_epsilon = Column(Float, nullable=False, default=0.0)
    updated_at = Column(String(40), nullable=False)


class PrivacyRelease(Base):
    """差分隐私发布申请 + 不可撤销扣费台账（一条记录即一次扣费）。"""
    __tablename__ = 'privacy_releases'
    __table_args__ = (UniqueConstraint('study_id', 'name'),)
    id = Column(Integer, primary_key=True)
    study_id = Column(Integer, ForeignKey('studies.id'), nullable=False)
    name = Column(String(120), nullable=False)
    # reserved：已扣费待计算；computing：worker 处理中；ready：可公开；
    # published：已公开；failed：计算失败（可重试，不重复扣费）；revoked：公开后撤销（不退费）。
    status = Column(String(20), nullable=False, default='reserved')
    epsilon = Column(Float, nullable=False)
    seed = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)
    checksum = Column(String(64), nullable=False)
    # 持久化的噪声抽样：中断恢复/重试沿用，绝不重新抽样、绝不重复扣费。
    noise_draws = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    result_fingerprint = Column(String(64), nullable=True)
    artifact_path = Column(String(200), nullable=True)
    error = Column(Text, default='')
    ledger_seq = Column(Integer, nullable=False)
    charged_at = Column(String(40), nullable=False)
    published_at = Column(String(40), nullable=True)
    revoked_at = Column(String(40), nullable=True)
    created_at = Column(String(40), nullable=False)
