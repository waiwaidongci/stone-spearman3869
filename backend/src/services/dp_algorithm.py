"""差分隐私发布算法。

口径（与公开结果文件中的 mechanism 字段保持一致）：
1. 孔隙率、吸水率按预设固定网格联合计数，网格边界不依赖任何数据；
2. 对网格的每个单元（含空格）独立加入 Laplace 噪声，尺度 b=1/epsilon；
   增删一个试件只改变恰好一个单元计数 1，因此 L1 敏感度 Delta=1，单次发布满足 epsilon-DP；
3. 对加噪计数做非负后处理（截断到 0，后处理不额外消耗预算）；
4. 由加噪网格的行/列边际求平均秩，再计算 Spearman 秩相关；
   全部推导只使用加噪计数，不接触原值与精确人数。
"""
import math
import numpy as np

# 预设固定网格：两个百分比指标均为 [0,100)，步长 10，共 10×10 个单元。
# 边界为常量，绝不随观测调整，否则网格本身会泄露数据信息。
GRID_MIN = 0.0
GRID_MAX = 100.0
GRID_BINS = 10
GRID_EDGES = [GRID_MIN + (GRID_MAX - GRID_MIN) * i / GRID_BINS for i in range(GRID_BINS + 1)]
GRID_LABELS = [f'[{GRID_EDGES[i]:g},{GRID_EDGES[i + 1]:g})' for i in range(GRID_BINS)]
# 100% 本身归入最后一个单元。
SENSITIVITY = 1
KEYS = ('porosity', 'absorption')


def _bin_index(v):
    if not (GRID_MIN <= v <= GRID_MAX) or not math.isfinite(v):
        raise ValueError('孔隙率与吸水率必须落在固定网格 [0,100] 内才能发布')
    if v == GRID_MAX:
        return GRID_BINS - 1
    return int((v - GRID_MIN) // ((GRID_MAX - GRID_MIN) / GRID_BINS))


def joint_counts(snapshot):
    """固定网格联合计数；空网格单元自然为 0（之后同样加噪）。"""
    if not snapshot:
        raise ValueError('空批次无法发布')
    grid = np.zeros((GRID_BINS, GRID_BINS), dtype=np.int64)
    for row in snapshot:
        values = row['values']
        if any(k not in values for k in KEYS):
            raise ValueError('观测字段不完整')
        x = float(values[KEYS[0]])
        y = float(values[KEYS[1]])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError('观测必须为有限数值')
        grid[_bin_index(x), _bin_index(y)] += 1
    return grid


def noisy_grid(grid, epsilon, seed):
    """对全部单元（含空格）加 Laplace 噪声，再截断到非负整数。

    截断与取整属于差分隐私后处理，不额外消耗预算。
    """
    rng = np.random.default_rng(np.random.SeedSequence(int(seed)))
    scale = SENSITIVITY / float(epsilon)
    draws = rng.laplace(0.0, scale, size=(GRID_BINS, GRID_BINS))
    noisy = grid.astype(float) + draws
    clamped = np.maximum(0.0, noisy)
    counts = np.rint(clamped).astype(np.int64)
    return draws, counts


def _marginal_ranks(marginals):
    """由一维加噪边际（组内计数）求各组平均秩。

    组 g 占据的秩区间起点为之前各组计数之和，组内平均秩 = 起点 + (n_g+1)/2。
    """
    ranks = np.empty(len(marginals), dtype=float)
    start = 0
    for i, n in enumerate(marginals):
        n = int(n)
        ranks[i] = start + (n + 1) / 2.0
        start += n
    return ranks, start


def dp_spearman(counts):
    """仅用加噪网格计数推导 Spearman 秩相关。"""
    row_totals = counts.sum(axis=1)   # 各孔隙率组的加噪人数
    col_totals = counts.sum(axis=0)   # 各吸水率组的加噪人数
    rx, n_x = _marginal_ranks(row_totals)
    ry, n_y = _marginal_ranks(col_totals)
    total = int(counts.sum())
    if total == 0:
        return {'defined': False, 'rho': None,
                'reason': '加噪并截断后网格总计数为0，秩相关无法定义；可使用新预算重新申请。',
                'noisy_total': 0}
    # 以加噪单元计数为权重，计算两组平均秩的 Pearson 相关。
    mx = float(np.dot(row_totals, rx) / n_x)
    my = float(np.dot(col_totals, ry) / n_y)
    vx = float(np.dot(row_totals, (rx - mx) ** 2) / n_x)
    vy = float(np.dot(col_totals, (ry - my) ** 2) / n_y)
    if vx == 0.0 or vy == 0.0:
        return {'defined': False, 'rho': None,
                'reason': '加噪边际集中在单一网格组，秩相关未定义（常量列情形）。',
                'noisy_total': total}
    mean_xy = float(sum(counts[i, j] * rx[i] * ry[j]
                        for i in range(GRID_BINS) for j in range(GRID_BINS)) / total)
    cov = mean_xy - mx * my
    rho = cov / math.sqrt(vx * vy)
    # 浮点误差可能略微越过 [-1,1]，钳制仅为展示层后处理。
    rho = max(-1.0, min(1.0, float(rho)))
    return {'defined': True, 'rho': rho, 'reason': '', 'noisy_total': total}


def assemble_result(grid, draws, epsilon, seed):
    """给定固定网格计数与（已持久化的）噪声抽样，组装公开结果。

    这是结果结构的唯一来源：一次性发布与中断恢复都走这里，保证口径一致。
    输出中不含任何原值或精确人数。
    """
    counts = np.maximum(0, np.rint(grid.astype(float) + draws)).astype(np.int64)
    spear = dp_spearman(counts)
    scale = SENSITIVITY / float(epsilon)
    return {
        'metrics': {
            '差分隐私秩相关ρ(加噪网格推导)': spear['rho'],
            '加噪后总计数(非精确人数)': spear['noisy_total'],
            'L1敏感度Δ(增删一个试件)': SENSITIVITY,
            'Laplace噪声尺度b=Δ/ε': scale,
            '隐私预算ε': float(epsilon),
            '随机种子': int(seed),
            '网格单元数': GRID_BINS * GRID_BINS,
        },
        'mechanism': (
            f'孔隙率与吸水率按固定 10×10 网格（{GRID_EDGES[0]:g}–{GRID_EDGES[-1]:g}，步长'
            f' {(GRID_MAX - GRID_MIN) / GRID_BINS:g}）联合计数；增删一个试件只改变一个单元，'
            f'L1 敏感度 Δ={SENSITIVITY}；全部 {GRID_BINS * GRID_BINS} 个单元（含空格）独立加入'
            f' Laplace(0, b={scale:.6g}) 噪声，单次发布满足 ε={epsilon}-DP；'
            '加噪计数截断为非负整数后，由行/列加噪边际的平均秩推导 Spearman 秩相关，'
            '截断、取整与秩相关推导均为差分隐私后处理，不再消耗预算。'
        ),
        'caveat': (
            '本结果为网格近似且含噪声，仅用于公开交流与后续假设，不解释为因果；'
            '公开内容不含单件原值与精确人数。' + ('' if spear['defined'] else spear['reason'])
        ),
        'grid_title': '加噪联合计数（空格同样加噪；非精确人数）',
        'grid_labels': {'row': '孔隙率(%) 网格组', 'col': '吸水率(%) 网格组',
                        'rows': GRID_LABELS, 'cols': GRID_LABELS},
        'grid_counts': counts.tolist(),
        'marginals': {
            'porosity_noisy': counts.sum(axis=1).tolist(),
            'absorption_noisy': counts.sum(axis=0).tolist(),
        },
        'ranks': {'porosity_avg_rank': _marginal_ranks(counts.sum(axis=1))[0].tolist(),
                  'absorption_avg_rank': _marginal_ranks(counts.sum(axis=0))[0].tolist()},
        'interpretation': (
            f"基于加噪固定网格推导的 Spearman ρ={spear['rho']:.4f}（ε={epsilon}，"
            f"b={scale:.4g}）。该秩相关为含噪近似，只作为后续研究假设。"
            if spear['defined'] else
            f"本次加噪后秩相关无法定义：{spear['reason']}（ε={epsilon}）。预算已消耗，不退回。"
        ),
    }


def build_release(snapshot, epsilon, seed):
    """一次性完整发布计算（主要用于算法自测）。"""
    grid = joint_counts(snapshot)
    draws, _ = noisy_grid(grid, epsilon, seed)
    return assemble_result(grid, draws, epsilon, seed)
