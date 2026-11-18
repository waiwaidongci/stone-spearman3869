import os

# 同批所有版本共用的不可重置隐私预算总额（ε）。部署后不可通过接口修改。
TOTAL_EPSILON = float(os.environ.get('DP_TOTAL_EPSILON', '1.0'))
# 单次申请允许的 ε 范围。
MIN_EPSILON = 0.01
MAX_EPSILON = 1.0
# 加噪主种子（固定，配合发布编号派生确定性随机数流；不构成可重置的隐私预算）。
MASTER_SEED = int(os.environ.get('DP_MASTER_SEED', '20261118'))
# 公开产物文件名前缀。
ARTIFACT_PREFIX = 'privacy-release-'
QUEUE_KEY = 'privacy-queue'
