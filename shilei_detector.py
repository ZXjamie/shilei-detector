"""
事类检测器核心逻辑
输入：起课引擎全量数据 + 事类名称
输出：加权分数报告
"""
import re
import sys
import json
import math
from itertools import combinations
from neo4j import GraphDatabase

# Neo4j 连接
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

# ─── 权重配置缓存 ─────────────────────────────────────────────
# 维度权重（从 Neo4j 加载）
WEIGHT_CONFIG = {}

# 位置权重（从 Neo4j 加载，键为位置类型名；男/女位置共用同名权重）
POSITION_WEIGHT = {}

# 2维组合比例（从 Neo4j 加载）
# key: frozenset(['维度1', '维度2']), value: 比例
COMBINATION_RATIO = {}

# 事类专属2维组合比例（从 Neo4j 加载）
# key: 事类名, value: {frozenset(['维度1', '维度2']): 比例}
# 查询时优先用事类专属，没有则 fallback 到全局 COMBINATION_RATIO
COMBINATION_RATIO_BY_SHILEI = {}

# 神煞聚集系数（从 Neo4j 加载）
# key: 事类名, value: 系数（0表示无聚集效应）
JUJI_CONFIG = {}

# 一维类象（士兵）累加折扣：士兵多寡计入分数，但单价低于多维类象（千夫长）
ONE_DIM_COEF = 0.2

# 千夫长（多维类象）单价系数
MULTI_COEF = 1.0

# 将军分：类神命中即得的固定分（整课只算一次，不随命中个数累加）
LS_FIXED_SCORE = 0.0

# 负信号事类：其多维类象（千夫长）撤职，按一维（士兵）计分
# 依据：这些事类的多维类象在别家课命中更多（区分度<1），累加反而放大错误
# 空集 = 所有事类的千夫长全部在位（不撤职）
DEMOTED_SHILEI = set()

# 类象级撤职：指定事类下的这些类象不再当千夫长（按士兵计，只计一次）
# 依据：判别力（本类命中率 / 他类命中率）低于阈值
DEMOTED_LEIXIANG = {}

# 类神共享度：{(维度key, 符号名): 被几个事类设为类神}
# 专属类神（=1）是真正的高判别特征；共享类神按 1/n 折价
LS_SHARE = {}
LS_ORDER = {}   # {(事类, 维度key, 符号): 顺序}  顺序越小越核心，99=核心标记
LS_ORDER_MODE = 'flat'
LS_SHILEI_MULT = {}
LS_DIM_MULT = {}   # {维度key: 倍数}  按类神所在维度加权   # {事类: 类神权重倍数}  按事类单独放大类神分  # flat / inv / sqrt / core2 / core3
LS_SHARE_POW = 1.0   # 共享度指数：0=不加权，1=1/共享度，>1=更陡（独有类神权重更高）

# 配位边：[{ma: 主符号, dz: 地支, cc: conditions_code, rate: 准验率}]
# 解析器读取 conditions_code 判断条件，满足则两项贡献各乘 (1+准验率)
# 准验率 = 命中该组合且来意=该事类的课数 ÷ 命中该组合的课数，由 recalc_zhuanshu_rate.py 动态重算
PEIWEI_EDGES = []

# 双神煞配位边（同位口径）
PEIWEI_PAIRS = []

# 配课边：[{cond: 条件地支, target: 被增强源符号, cc: conditions_code, rate: 准验率}]
# 课式级条件（非同位）：如日支=亥 时，未系符号单价乘 (1+准验率)
PEIKE_EDGES = []

# 维度关系权重：{(事类, 维度): 权重}，来自 关联类象.r.权重
DIM_REL_WEIGHT = {}
# 神煞覆盖：{事类: 关联神煞条数}，神煞维度关系权重 = SHENSHA_MAX ÷ 覆盖
SHENSHA_COVER = {}
SHENSHA_MAX = 25

# 合法维度（关联类象.维度 只接受这四个值）
_VALID_DIMS = {'dizhi', 'tianjiang', 'liuqin', 'changsheng'}

# 维度 → 关联边 源符号前缀
_DIM_SRC_PREFIX = {'dizhi': 'dz_', 'tianjiang': 'tj_', 'liuqin': 'lq_', 'changsheng': 'cs_'}


def _route_filter(pos_lx: dict, dim: str, symbol: str, routes: dict, pos_ck=None) -> dict:
    """按路由（源符号 + combination_key）过滤某位置的类象集。

    routes 结构：{维度: {(源符号, 类象名): {combination_key, ...}}}
      - 集合里含 None 表示该组关联边没有 combination_key → 通配整条类象
    pos_ck 结构：{类象名: {该位置实际触发的 combination_key, ...}}

    匹配规则：
    - 该维度没有任何路由信息时不过滤（兼容旧数据）
    - 裸边（无源符号）写入 ('', 类象)，永远匹配不上 → 相当于不生效
    - 组内有精确键时，必须键命中；否则只看 (源符号, 类象名)
    """
    if not pos_lx:
        return pos_lx
    allow = routes.get(dim)
    if allow is None:
        return pos_lx
    pref = _DIM_SRC_PREFIX.get(dim, '') + symbol
    pos_ck = pos_ck or {}
    out = {}
    for lx, w in pos_lx.items():
        spec = allow.get((pref, lx))
        if spec is None:
            continue
        if None in spec:          # 该 (源符号, 类象) 没有精确路由键 → 通配
            out[lx] = w
            continue
        if set(pos_ck.get(lx) or ()) & spec:
            out[lx] = w
    return out


def _lq_symbol(key: str) -> str:
    """LQ_妻财 / LQ_DUN_妻财 → 妻财（两个通道共用 lq_ 前缀的路由白名单）"""
    return key[7:] if key.startswith('LQ_DUN_') else key[3:]


# 同一乘关系的两侧类象映射（打分去重：同一事实不重复计分）
COMBO_MAP = {}
try:
    import os as _os
    _combo_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'combo_map.json')
    if _os.path.exists(_combo_file):
        with open(_combo_file, encoding='utf-8') as _f:
            COMBO_MAP = json.load(_f)
except Exception:
    COMBO_MAP = {}

# 高维组合比例（3/4/5/6维，从 Neo4j 加载）
# key: frozenset(['维度1', '维度2', ...]), value: 比例
COMBINATION_RATIO_3D = {}
COMBINATION_RATIO_4D = {}
COMBINATION_RATIO_5D = {}
COMBINATION_RATIO_6D = {}

# 事类专属高维组合比例
COMBINATION_RATIO_3D_BY_SHILEI = {}
COMBINATION_RATIO_4D_BY_SHILEI = {}
COMBINATION_RATIO_5D_BY_SHILEI = {}
COMBINATION_RATIO_6D_BY_SHILEI = {}

# 维度名映射（代码内部用拼音，显示用中文）
DIM_NAME_MAP = {
    'dizhi': '地支',
    'tianjiang': '天将',
    'shensha': '神煞',
}


def load_weight_config():
    """从 Neo4j 加载权重配置到内存"""
    global WEIGHT_CONFIG, COMBINATION_RATIO, COMBINATION_RATIO_BY_SHILEI, JUJI_CONFIG, POSITION_WEIGHT
    global COMBINATION_RATIO_3D, COMBINATION_RATIO_4D, COMBINATION_RATIO_5D, COMBINATION_RATIO_6D
    global COMBINATION_RATIO_3D_BY_SHILEI, COMBINATION_RATIO_4D_BY_SHILEI, COMBINATION_RATIO_5D_BY_SHILEI, COMBINATION_RATIO_6D_BY_SHILEI
    
    # 重置所有缓存（防止 Neo4j 中已删除的配置在内存中残留）
    WEIGHT_CONFIG = {}
    COMBINATION_RATIO = {}
    COMBINATION_RATIO_BY_SHILEI = {}
    JUJI_CONFIG = {}
    POSITION_WEIGHT = {}
    COMBINATION_RATIO_3D = {}
    COMBINATION_RATIO_4D = {}
    COMBINATION_RATIO_5D = {}
    COMBINATION_RATIO_6D = {}
    COMBINATION_RATIO_3D_BY_SHILEI = {}
    COMBINATION_RATIO_4D_BY_SHILEI = {}
    COMBINATION_RATIO_5D_BY_SHILEI = {}
    COMBINATION_RATIO_6D_BY_SHILEI = {}
    
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with driver.session() as session:
        # 加载维度权重
        result = session.run("""
            MATCH (w:权重配置 {类型: '维度'})
            RETURN w.名称 AS name, w.权重 AS weight
        """)
        for record in result:
            name = record['name']
            weight = record['weight']
            if name and weight is not None:
                WEIGHT_CONFIG[name] = float(weight)
        
        # 加载2维组合比例（区分全局和事类专属）
        result = session.run("""
            MATCH (c:组合比例)
            RETURN c.事类 AS shilei, c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            shilei = record.get('shilei')  # None表示全局
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                if isinstance(combo, list) and len(combo) == 2:
                    if shilei:
                        # 事类专属组合比例
                        if shilei not in COMBINATION_RATIO_BY_SHILEI:
                            COMBINATION_RATIO_BY_SHILEI[shilei] = {}
                        COMBINATION_RATIO_BY_SHILEI[shilei][frozenset(combo)] = float(ratio)
                    else:
                        # 全局组合比例
                        COMBINATION_RATIO[frozenset(combo)] = float(ratio)
        
        # 加载神煞聚集系数
        result = session.run("""
            MATCH (j:聚集系数)
            RETURN j.事类 AS shilei, j.系数 AS ratio
        """)
        for record in result:
            shilei = record['shilei']
            ratio = record['ratio']
            if shilei and ratio is not None:
                JUJI_CONFIG[shilei] = float(ratio)
        
        # 加载3维组合比例
        result = session.run("""
            MATCH (c:三维组合比例)
            RETURN c.事类 AS shilei, c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            shilei = record.get('shilei')
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                if isinstance(combo, list) and len(combo) == 3:
                    if shilei:
                        if shilei not in COMBINATION_RATIO_3D_BY_SHILEI:
                            COMBINATION_RATIO_3D_BY_SHILEI[shilei] = {}
                        COMBINATION_RATIO_3D_BY_SHILEI[shilei][frozenset(combo)] = float(ratio)
                    else:
                        COMBINATION_RATIO_3D[frozenset(combo)] = float(ratio)
        
        # 加载4维组合比例
        result = session.run("""
            MATCH (c:四维组合比例)
            RETURN c.事类 AS shilei, c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            shilei = record.get('shilei')
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                if isinstance(combo, list) and len(combo) == 4:
                    if shilei:
                        if shilei not in COMBINATION_RATIO_4D_BY_SHILEI:
                            COMBINATION_RATIO_4D_BY_SHILEI[shilei] = {}
                        COMBINATION_RATIO_4D_BY_SHILEI[shilei][frozenset(combo)] = float(ratio)
                    else:
                        COMBINATION_RATIO_4D[frozenset(combo)] = float(ratio)
        
        # 加载5维组合比例
        result = session.run("""
            MATCH (c:五维组合比例)
            RETURN c.事类 AS shilei, c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            shilei = record.get('shilei')
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                if isinstance(combo, list) and len(combo) == 5:
                    if shilei:
                        if shilei not in COMBINATION_RATIO_5D_BY_SHILEI:
                            COMBINATION_RATIO_5D_BY_SHILEI[shilei] = {}
                        COMBINATION_RATIO_5D_BY_SHILEI[shilei][frozenset(combo)] = float(ratio)
                    else:
                        COMBINATION_RATIO_5D[frozenset(combo)] = float(ratio)
        
        # 加载6维组合比例
        result = session.run("""
            MATCH (c:六维组合比例)
            RETURN c.事类 AS shilei, c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            shilei = record.get('shilei')
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                if isinstance(combo, list) and len(combo) == 6:
                    if shilei:
                        if shilei not in COMBINATION_RATIO_6D_BY_SHILEI:
                            COMBINATION_RATIO_6D_BY_SHILEI[shilei] = {}
                        COMBINATION_RATIO_6D_BY_SHILEI[shilei][frozenset(combo)] = float(ratio)
                    else:
                        COMBINATION_RATIO_6D[frozenset(combo)] = float(ratio)
    
        # 加载位置权重（键为位置类型名；男/女位置共用）
        result = session.run("""
            MATCH (w:权重配置 {类型: '位置'})
            RETURN w.名称 AS name, w.权重 AS weight
        """)
        for record in result:
            name = record['name']
            weight = record['weight']
            if name and weight is not None:
                POSITION_WEIGHT[name] = float(weight)
    
    driver.close()
    print(f"[权重配置] 已加载: 维度权重={WEIGHT_CONFIG}, 位置权重={POSITION_WEIGHT}, 2维组合={len(COMBINATION_RATIO)}个, 3维={len(COMBINATION_RATIO_3D)}个, 4维={len(COMBINATION_RATIO_4D)}个, 5维={len(COMBINATION_RATIO_5D)}个, 6维={len(COMBINATION_RATIO_6D)}个, 聚集系数={JUJI_CONFIG}")


def get_combination_ratio(dims: set, shilei_name: str = None) -> float:
    """获取多维组合的比例
    
    2维：直接查表（优先事类专属，fallback到全局）
    3维及以上：优先查对应维度的独立节点，没有则fallback到2维子对求和
    """
    dims_list = list(dims)
    if len(dims_list) < 2:
        return 0.0
    
    # 获取事类专属或全局的组合比例表
    shilei_ratios = COMBINATION_RATIO_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    
    def _get_ratio_2d(combo_key):
        """优先查事类专属，fallback到全局（2维用）"""
        return shilei_ratios.get(combo_key, COMBINATION_RATIO.get(combo_key, 0.0))
    
    if len(dims_list) == 2:
        return _get_ratio_2d(frozenset(dims_list))
    
    # 3维及以上：优先查对应维度的独立节点
    dim_count = len(dims_list)
    dim_key = frozenset(dims_list)
    
    # 选择对应维度的全局表和事类专属表
    if dim_count == 3:
        global_table = COMBINATION_RATIO_3D
        shilei_table = COMBINATION_RATIO_3D_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    elif dim_count == 4:
        global_table = COMBINATION_RATIO_4D
        shilei_table = COMBINATION_RATIO_4D_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    elif dim_count == 5:
        global_table = COMBINATION_RATIO_5D
        shilei_table = COMBINATION_RATIO_5D_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    elif dim_count == 6:
        global_table = COMBINATION_RATIO_6D
        shilei_table = COMBINATION_RATIO_6D_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    else:
        global_table = {}
        shilei_table = {}
    
    # 优先查事类专属高维节点，再查全局高维节点；节点存在即采用（含0值），都不存在则fallback到2维子对求和
    if dim_key in shilei_table:
        return shilei_table[dim_key]
    elif dim_key in global_table:
        return global_table[dim_key]
    else:
        # fallback: 2维子对求和
        total_ratio = 0.0
        for combo in combinations(dims_list, 2):
            total_ratio += _get_ratio_2d(frozenset(combo))
        return total_ratio


def calculate_weighted_score(dim_matches: dict, shilei_name: str = None) -> dict:
    """计算单个位置的加权分数
    
    Args:
        dim_matches: 各维度匹配数（六维度：天将/地支/地支六亲/遁干六亲/长生/神煞）
            {'天将': 2, '地支': 0, '地支六亲': 1, '遁干六亲': 0, '长生': 0, '神煞': 3}
        shilei_name: 事类名称（可选），用于查询事类专属组合比例
    
    Returns:
        {
            'score': 15.5,  # 总分
            'base_score': 8.0,  # 维度基础分
            'combo_score': 7.5,  # 组合加分
            'matched_dims': ['天将', '地支六亲', '神煞']  # 命中的维度
        }
    """
    # 1. 计算维度基础分（维度内累加，不封顶）
    base_scores = {}
    for dim_name, match_count in dim_matches.items():
        if match_count > 0:
            weight = WEIGHT_CONFIG.get(dim_name, 1.0)
            base_scores[dim_name] = match_count * weight
    
    base_score = sum(base_scores.values())
    matched_dims = list(base_scores.keys())
    
    # 2. 计算多维组合加分
    combo_score = 0.0
    if len(matched_dims) >= 2:
        # 获取组合比例
        ratio = get_combination_ratio(set(matched_dims), shilei_name)
        if ratio > 0:
            # 组合加分 = 涉及的维度基础分之和 × 组合比例
            combo_base_sum = sum(base_scores.values())
            combo_score = combo_base_sum * ratio
    
    total_score = base_score + combo_score
    
    return {
        'score': round(total_score, 1),
        'base_score': round(base_score, 1),
        'combo_score': round(combo_score, 1),
        'matched_dims': matched_dims
    }


# 启动时加载权重配置
load_weight_config()

# 天干五行（用于六亲计算）
TIANGAN_WUXING = {
    '甲': '木', '乙': '木', '丙': '火', '丁': '火', '戊': '土',
    '己': '土', '庚': '金', '辛': '金', '壬': '水', '癸': '水'
}

# 地支五行
DIZHI_WUXING = {
    '子': '水', '丑': '土', '寅': '木', '卯': '木', '辰': '土', '巳': '火',
    '午': '火', '未': '土', '申': '金', '酉': '金', '戌': '土', '亥': '水'
}

# 五行生克
WUXING_SHENG = {'木': '火', '火': '土', '土': '金', '金': '水', '水': '木'}
WUXING_KE = {'木': '土', '土': '水', '水': '火', '火': '金', '金': '木'}

# 天将简称映射
TIANJIANG_FULL = {
    '贵': '贵人', '蛇': '螣蛇', '雀': '朱雀', '合': '六合',
    '勾': '勾陈', '龙': '青龙', '空': '天空', '虎': '白虎',
    '常': '太常', '玄': '玄武', '阴': '太阴', '后': '天后'
}

# 十二长生顺序
CHANGSHENG_ORDER = ['长生', '沐浴', '冠带', '临官', '帝旺', '衰', '病', '死', '墓', '绝', '胎', '养']

# 位置总数（仅用于统计展示；覆盖率缩放已废除，不再参与得分）
POSITION_TOTAL = 12

# 13处位置名（与8008输出对齐）
POSITION_NAMES = [
    '第1课上神', '第2课上神', '第3课上神', '第4课上神',
    '初传', '中传', '末传',
    '占时', '行年', '本命', '行年_2', '本命_2',
    '行年上神', '本命上神', '行年上神_2', '本命上神_2'
]


def _bm_dz(bm):
    """本命字段取地支，兼容两种存量格式

    '癸巳'（完整干支）→ '巳'
    '巳'（单字地支）  → '巳'
    """
    if not bm:
        return ''
    return bm[1] if len(bm) >= 2 else bm


# [已禁用] 六亲和长生由类象检测器(8008)计算，8014只消费不重算
# def calc_liuqin(ri_gan: str, dizhi: str) -> str:
#     """计算地支相对于日干的六亲"""
#     if not ri_gan or not dizhi:
#         return ''
#     wo = TIANGAN_WUXING.get(ri_gan, '')
#     ta = DIZHI_WUXING.get(dizhi, '')
#     if not wo or not ta:
#         return ''
#     if wo == ta:
#         return '兄弟'
#     elif WUXING_SHENG.get(wo) == ta:
#         return '子孙'
#     elif WUXING_KE.get(wo) == ta:
#         return '妻财'
#     elif WUXING_SHENG.get(ta) == wo:
#         return '父母'
#     elif WUXING_KE.get(ta) == wo:
#         return '官鬼'
#     return ''


# [已禁用] 六亲和长生由类象检测器(8008)计算，8014只消费不重算
# def get_changsheng_position(ri_gan: str, dizhi: str) -> str:
#     """计算地支相对于日干的十二长生状态
#     
#     用天干五行计算，不用寄宫
#     甲乙木长生在亥，丙丁火长生在寅，戊己土长生在寅，庚辛金长生在巳，壬癸水长生在申
#     """
#     if not ri_gan or not dizhi:
#         return ''
#     
#     # 天干五行
#     wuxing = TIANGAN_WUXING.get(ri_gan, '')
#     if not wuxing:
#         return ''
#     
#     # 五行长生起始地支
#     changsheng_start = {'木': '亥', '火': '寅', '土': '寅', '金': '巳', '水': '申'}
#     start = changsheng_start.get(wuxing, '')
#     if not start:
#         return ''
#     
#     # 计算位置差
#     dizhi_order = ['子', '丑', '寅', '卯', '辰', '巳', '午', '未', '申', '酉', '戌', '亥']
#     start_idx = dizhi_order.index(start)
#     dizhi_idx = dizhi_order.index(dizhi)
#     diff = (dizhi_idx - start_idx) % 12
#     
#     return CHANGSHENG_ORDER[diff] if diff < len(CHANGSHENG_ORDER) else ''


def parse_tianjiang_position(tiandi_pan_with_jiang: str) -> dict:
    """解析天将位置：{地支: 天将}"""
    result = {}
    if not tiandi_pan_with_jiang:
        return result
    for cell in tiandi_pan_with_jiang.split('|'):
        cell = cell.strip()
        m = re.match(r'^([^\[]*?)([子丑寅卯辰巳午未申酉戌亥])\[:上\]([子丑寅卯辰巳午未申酉戌亥])$', cell)
        if m and m.group(1):
            tianjiang_short = m.group(1)[-1]
            tianpan = m.group(2)
            result[tianpan] = TIANJIANG_FULL.get(tianjiang_short, tianjiang_short)
    return result


def get_tiandipan(ke_data: dict) -> dict:
    """获取天地盘字典，兼容 tiandipan 和 tiandipan_struct 两种格式"""
    tiandipan = ke_data.get('tiandipan', {})
    # 兼容 tiandipan_struct 格式（列表格式）
    if not tiandipan:
        tiandipan_struct = ke_data.get('tiandipan_struct', [])
        if tiandipan_struct and isinstance(tiandipan_struct, list):
            tiandipan = {item[1]: item[0] for item in tiandipan_struct}
    return tiandipan


# 天干寄宫
TIANGAN_JIGONG = {
    '甲': '寅', '乙': '辰', '丙': '巳', '丁': '未', '戊': '巳',
    '己': '未', '庚': '申', '辛': '戌', '壬': '亥', '癸': '丑'
}


def parse_sike_positions(sike_str: str, ri_gan: str, ri_zhi: str) -> dict:
    """解析四课，返回每个位置的天将、地支、六亲、长生
    
    六亲和长生自己算，不依赖字符串解析
    """
    result = {}
    if not sike_str:
        return result
    
    parts = sike_str.strip().split()
    keti_names = [
        ("日支", "日支上神"),   # i=0: 第4课
        ("日支", "日支上神"),   # i=1: 第3课（下神=日支）
        ("日干", "日干上神"),   # i=2: 第2课
        ("日干", "日干上神")    # i=3: 第1课（下神=日干）
    ]
    
    for i, part in enumerate(parts):
        if i >= 4:
            break
        if part.startswith('○') or part.startswith('⊙'):
            part = part[1:]
        
        match = re.match(r'^([甲乙丙丁戊己庚辛壬癸]?)([龙虎雀蛇贵阴后常玄勾合空])([子丑寅卯辰巳午未申酉戌亥])\[:上\](.+)$', part)
        if match:
            dungan, tianjiang_short, shangshen, rest = match.groups()
            tianjiang = TIANJIANG_FULL.get(tianjiang_short, tianjiang_short)
            
            xia_match = re.match(r'^([子丑寅卯辰巳午未申酉戌亥甲乙丙丁戊己庚辛壬癸])', rest)
            if xia_match:
                xia_shen_raw = xia_match.group(1)
                ke_num = 4 - i
                xia_name, shang_name = keti_names[i]
                
                # 下神可能是天干（第1课=日干），需要转成地支
                if xia_shen_raw in TIANGAN_JIGONG:
                    xia_shen_dizhi = TIANGAN_JIGONG[xia_shen_raw]
                else:
                    xia_shen_dizhi = xia_shen_raw
                
                # 上神位置
                # [已禁用] 六亲和长生由8008计算，从leixiang_by_symbol中读取LQ_和CS_前缀
                result[f'第{ke_num}课上神'] = {
                    '地支': shangshen,
                    '天将': tianjiang,
                }
    return result


def parse_sanchuan_positions(sanchuan_str: str, ri_gan: str, ri_zhi: str) -> dict:
    """解析三传，返回每个位置的天将、地支、六亲、长生
    
    六亲和长生自己算，不依赖字符串解析
    """
    result = {}
    if not sanchuan_str:
        return result
    
    for line in sanchuan_str.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('○') or line.startswith('⊙'):
            line = line[1:]
        m = re.match(r'^(.+?)([子丑寅卯辰巳午未申酉戌亥])(.+?)(初|中|末)$', line)
        if m:
            ld = m.group(1)
            dizhi = m.group(2)
            tianjiang_short = m.group(3)
            chuan_type = m.group(4)
            chuan_name = {'初': '初传', '中': '中传', '末': '末传'}[chuan_type]
            
            # 天将简称转全称
            tianjiang = TIANJIANG_FULL.get(tianjiang_short, tianjiang_short)
            
            # [已禁用] 六亲和长生由8008计算，这里不再自己算
            # liuqin = calc_liuqin(ri_gan, dizhi)
            # changsheng = get_changsheng_position(ri_gan, dizhi)
            
            result[chuan_name] = {
                '地支': dizhi,
                '天将': tianjiang,
                # '六亲': liuqin,
                # '长生': changsheng
            }
    return result


def extract_leixiang_from_8008(leixiang_data: dict) -> dict:
    """从8008输出提取类象，按位置和符号分类，支持多维倍增
    
    输入格式: {'total_matched': N, 'by_category': {...}}
    
    Returns:
        {
            '初传': {
                'DZ_子': {'鬼神': 2, '桃花': 1, ...},
                'TJ_贵人': {'领导': 1, ...},
                'LQ_兄弟': {'债务': 1, ...},      # 地支六亲
                'LQ_DUN_兄弟': {'债务': 1, ...},  # 遁干六亲
                ...
            },
            '中传': {...},
            ...
        }
    
    提取逻辑：
    - 优先从 combination_key 提取 lx_xxx
    - 没有则用 meaning
    
    维度判断（以 fujia_tiaojian 附加条件为准，与管理员系统一致）：
    - 附加条件为空 → 一维 → 计数1
    - 附加条件按 ';' 拆，1 个条件 → 二维 → 计数2
    - 2 个条件 → 三维 → 计数3（依此类推）
    注意：不要按 combination_key 的 '|' 段数判断，全库键都是 4 段。
    
    六亲区分：
    - trigger包含"遁干" → LQ_DUN_xxx（遁干六亲）
    - 否则 → LQ_xxx（地支六亲）
    
    去重规则：
    - 同一位置内，同一符号+类象组合只计数1次
    """
    result = {}
    if not leixiang_data or not leixiang_data.get('by_category'):
        return result
    
    by_category = leixiang_data['by_category']
    
    for cat_name, cat_data in by_category.items():
        items = cat_data.get('items', []) if isinstance(cat_data, dict) else cat_data
        for item in items:
            item_id = item.get('id', '')  # 如 DZ_子、TJ_贵人、LQ_兄弟
            if not item_id:
                continue
            
            # 提取位置信息（从trigger字段，如"中传(寅)为兄弟"或"中传(遁干甲)为兄弟"）
            trigger = item.get('trigger', '')
            pos_name = ''
            is_dungan = False  # 是否是遁干六亲
            if trigger:
                # 提取位置名：中传(寅)为兄弟 → 中传
                match = re.match(r'^([^(\\s]+)', trigger)
                if match:
                    pos_name = match.group(1)
                # 检查是否是遁干六亲
                if '遁干' in trigger:
                    is_dungan = True
            
            if not pos_name:
                continue
            
            # 天将/地支/天干类象：原始类别（地支类象/天将类象）不含位置语义，
            # trigger为"X在课传年命"等，归入全局桶。
            # 但新增位置类别（占时/行年/本命等）的trigger带位置名，保留位置。
            if item_id.startswith(('TJ_', 'DZ_', 'TG_')) and pos_name not in POSITION_NAMES:
                pos_name = '__global__'
            
            # 初始化位置
            if pos_name not in result:
                result[pos_name] = {}
            
            # 优先从 combination_key 提取 lx_xxx
            combo_key = item.get('combination_key', '')
            leixiang_name = None
            weight = 1  # 默认一维计数1
            
            if combo_key:
                parts = combo_key.split('|')
                # 提取类象名
                for part in parts:
                    if part.startswith('lx_'):
                        leixiang_name = part[3:]  # 去掉 'lx_' 前缀
                        break
                # 判断维度：以 fujia_tiaojian（附加条件）为准，与管理员系统一致
                # 无附加条件 = 一维类象（士兵）；有附加条件 = 多维类象（千夫长）
                # 每多一个附加条件加一维
                num_parts = 1
                _fujia = (item.get('fujia_tiaojian') or '').strip()
                if _fujia:
                    _conds = [x for x in _fujia.split(';') if x.strip()]
                    num_parts = 1 + len(_conds)
                if num_parts >= 2:
                    weight = num_parts
            else:
                # 没有 combination_key，用 meaning
                leixiang_name = item.get('meaning', '')
            
            if leixiang_name:
                pos_data = result[pos_name]
                
                # 六亲需要区分地支六亲和遁干六亲
                if item_id.startswith('LQ_'):
                    if is_dungan:
                        # 遁干六亲：LQ_兄弟 → LQ_DUN_兄弟
                        item_id = 'LQ_DUN_' + item_id[3:]
                
                if item_id not in pos_data:
                    pos_data[item_id] = {}
                # 路由键：记录该位置该符号该线索实际触发的 combination_key，供路由级匹配
                if combo_key:
                    ck_slot = pos_data.setdefault('__ck__', {}).setdefault(item_id, {})
                    ck_slot.setdefault(leixiang_name, set()).add(combo_key)
                # 同一位置内，同一类象取【最大维度】（多维优先，避免被一维条目覆盖）
                if leixiang_name not in pos_data[item_id] or weight > pos_data[item_id][leixiang_name]:
                    pos_data[item_id][leixiang_name] = weight
    
    return result


def get_pos_leixiang(leixiang_by_symbol: dict, pos_name: str) -> dict:
    """取某位置的类象，合并全局桶
    
    天将/地支/天干类象无位置语义，存在 __global__ 桶中；
    六亲/长生类象按位置存放。本函数把两者合并，供维度计算使用。
    """
    # 深合并：同一 item_id 下逐类象取【最大权重】
    # 不能用 dict.update —— 浅合并会让位置桶（一维）整桶覆盖全局桶（多维），
    # 导致「子加亥」这类多维条目在同时出现在某位置时丢失维度倍数
    merged = {}
    ck_merged = {}
    for src in (leixiang_by_symbol.get('__global__', {}), leixiang_by_symbol.get(pos_name, {})):
        for item_id, lx_map in src.items():
            if item_id == '__ck__':
                # 路由键桶：{item_id: {类象名: set(combination_key)}}，并集合并
                for iid, lx_cks in lx_map.items():
                    slot = ck_merged.setdefault(iid, {})
                    for lx_name, cks in lx_cks.items():
                        slot.setdefault(lx_name, set()).update(cks)
                continue
            slot = merged.setdefault(item_id, {})
            for lx_name, w in lx_map.items():
                if lx_name not in slot or w > slot[lx_name]:
                    slot[lx_name] = w
    merged['__ck__'] = ck_merged
    return merged


def extract_shensha_by_position(shensha_data: dict, positions: dict) -> dict:
    """从神煞数据中，按位置归组"""
    result = {}
    if not shensha_data:
        return result
    
    for pos_name, pos_info in positions.items():
        dizhi = pos_info.get('地支', '')
        if dizhi and dizhi in shensha_data:
            shensha_list = shensha_data[dizhi]
            result[pos_name] = [s.get('name', '') for s in shensha_list if isinstance(s, dict)]
        else:
            result[pos_name] = []
    
    return result


def fetch_shilei_rules(shilei_name: str) -> dict:
    """从Neo4j查询事类关联的类象和神煞（含权重）
    
    Returns:
        {
            'leixiang': {'类象名1': 权重1, '类象名2': 权重2, ...},
            'shensha': {'神煞名1': 权重1, '神煞名2': 权重2, ...}
        }
    权重默认为1.0（关系无权重属性时）
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with driver.session() as session:
        # 查询事类关联的类象（含权重、维度、源符号）
        leixiang_result = session.run("""
            MATCH (s:事类 {name: $name})-[r:关联类象]->(l:Leixiang)
            RETURN l.name AS leixiang_name, coalesce(r.来意权重, r.权重) AS weight,
                   r.维度 AS dim, coalesce(r.源符号, '') AS source_symbol, r.状态 AS status,
                   coalesce(r.combination_key, '') AS combination_key
        """, name=shilei_name)
        leixiang_dict = {}
        routes = {}
        mismatches = []
        for record in leixiang_result:
            nm = record['leixiang_name']
            w = record['weight'] if record['weight'] is not None else 1.0
            dim = (record['dim'] or '').strip()
            src = (record['source_symbol'] or '').strip()
            if dim in _VALID_DIMS:
                if src and not src.startswith(_DIM_SRC_PREFIX.get(dim, '??')):
                    mismatches.append((shilei_name, nm, dim, src))
            status = (record['status'] or '').strip()
            if status not in ('', '正常', '待定'):
                continue
            leixiang_dict[nm] = w
            if dim in _VALID_DIMS:
                ck = (record['combination_key'] or '').strip()
                routes.setdefault(dim, {}).setdefault((src, nm), set()).add(ck or None)
        for _pm in mismatches[:20]:
            print(f"  ❌ 源符号前缀与维度不符（该路由永不命中）: {_pm[0]} → {_pm[1]}  维度={_pm[2]}  源符号={_pm[3]}")
        
        # 查询事类关联的神煞（含权重）
        shensha_result = session.run("""
            MATCH (s:事类 {name: $name})-[r:关联神煞]->(ss:Shensha)
            RETURN ss.name AS shensha_name, coalesce(r.来意权重, r.权重) AS weight
        """, name=shilei_name)
        shensha_dict = {record['shensha_name']: (record['weight'] if record['weight'] is not None else 1.0) for record in shensha_result}
    
    driver.close()
    
    return {
        'leixiang': leixiang_dict,
        'routes': routes,
        'shensha': shensha_dict
    }


def fetch_all_shilei_rules() -> dict:
    """一次查询获取所有事类的规则（含权重）
    
    Returns:
        {
            '财运': {
                'leixiang': {
                    'dizhi': {'类象名': 权重, ...},
                    'tianjiang': {'类象名': 权重, ...},
                    'liuqin': {'类象名': 权重, ...},
                    'changsheng': {'类象名': 权重, ...}
                },
                'shensha': {'神煞名': 权重, ...}
            },
            ...
        }
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    # 每次加载先复位，避免多次调用时覆盖数累加
    global DIM_REL_WEIGHT, SHENSHA_COVER, SHENSHA_MAX
    DIM_REL_WEIGHT = {}
    SHENSHA_COVER = {}
    SHENSHA_MAX = 25
    
    result = {}
    with driver.session() as session:
        # 一次查询所有事类及其关联的类象（含权重和维度）
        # 三态标记：只读取 状态 != "废弃" 的关联（正常、待定、NULL都算有效）
        leixiang_result = session.run("""
            MATCH (s:事类)-[r:关联类象]->(l:Leixiang)
            RETURN s.name AS shilei_name, l.name AS leixiang_name, coalesce(r.来意权重, r.权重) AS weight,
                   r.维度 AS dim, r.源符号 AS source_symbol, r.状态 AS status,
                   coalesce(r.combination_key, '') AS combination_key
            ORDER BY shilei_name, dim, weight, leixiang_name
        """)
        # 维度只接受四个规范值；非法维度记录跳过并告警，不参与任何计分
        _VALID_DIMS = {'dizhi', 'tianjiang', 'liuqin', 'changsheng'}
        _bad_dim = []       # [(事类, 类象, 原始维度)]
        _dim_conflict = []  # [((事类,维度), 首次值, 冲突值)]
        _prefix_mismatch = []  # [(事类, 类象, 维度, 源符号)] 源符号前缀与维度不符 → 该路由永远匹配不上
        for record in leixiang_result:
            shilei_name = record['shilei_name']
            leixiang_name = record['leixiang_name']
            weight = record['weight'] if record['weight'] is not None else 1.0
            dim = (record['dim'] or '').strip()
            if dim not in _VALID_DIMS:
                _bad_dim.append((shilei_name, leixiang_name, record['dim']))
                continue
            src = (record['source_symbol'] or '').strip()
            # 数据体检：源符号前缀必须与维度一致（dz_/tj_/lq_/cs_），否则该路由永不命中
            if src and not src.startswith(_DIM_SRC_PREFIX.get(dim, '??')):
                _prefix_mismatch.append((shilei_name, leixiang_name, dim, src))
            # 三态标记：废弃的边不参与计分（NULL/正常/待定 有效）
            status = (record['status'] or '').strip()
            if status not in ('', '正常', '待定'):
                continue
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': {}, 'shensha': {}, 'routes': {}}
            result[shilei_name]['leixiang'].setdefault(dim, {})[leixiang_name] = weight
            # 路由级匹配：白名单 {(源符号, 类象名): {combination_key 或 None}}
            ck = (record['combination_key'] or '').strip()
            result[shilei_name].setdefault('routes', {}).setdefault(dim, {}).setdefault(
                (src, leixiang_name), set()).add(ck or None)
            # 维度关系权重：同一 (事类, 维度) 内所有关联类象共用一份，首次写入为准
            _key = (shilei_name, dim)
            if _key in DIM_REL_WEIGHT and abs(DIM_REL_WEIGHT[_key] - weight) > 1e-9:
                _dim_conflict.append((_key, DIM_REL_WEIGHT[_key], weight))
            else:
                DIM_REL_WEIGHT.setdefault(_key, weight)
        
        # 一次查询所有事类及其关联的神煞（含权重）
        # 排除已是类神的神煞：类神走将军通道，避免与士兵通道重复计分
        shensha_result = session.run("""
            MATCH (s:事类)-[r:关联神煞]->(ss:Shensha)
            WHERE NOT (s)-[:类神]->(ss)
            RETURN s.name AS shilei_name, ss.name AS shensha_name, coalesce(r.来意权重, r.权重) AS weight
        """)
        for record in shensha_result:
            shilei_name = record['shilei_name']
            shensha_name = record['shensha_name']
            weight = record['weight'] if record['weight'] is not None else 1.0
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': {}, 'shensha': {}}
            result[shilei_name]['shensha'][shensha_name] = weight

        # 神煞覆盖数 = 该事类在该层的关联神煞条数（包括类神）
        for record in session.run("""
            MATCH (sh:事类)-[:关联神煞]->(ss:Shensha)
            RETURN sh.name AS n, count(DISTINCT ss) AS cov
        """):
            SHENSHA_COVER[record['n']] = int(record['cov'])

        # 类神（将军）通道：事类直接关联的符号，不依赖类象条件
        # 顺序字段即等级：顺序1=将军（权重最高），顺序越大越接近士兵
        LEISHEN_LABEL_MAP = {
            'DiZhi': 'dizhi',
            'Tianjiang': 'tianjiang',
            '六亲': 'liuqin',
            'Changsheng': 'changsheng',
            'Shensha': 'shensha',
        }
        ls_result = session.run("""
            MATCH (s:事类)-[r:类神]->(x)
            RETURN s.name AS shilei_name, x.name AS sym, labels(x)[0] AS lab,
                   r.顺序 AS ord, r.权重 AS w
        """)
        # 统计每个类神符号被几个事类共享（用于专属度加权）
        global LS_SHARE, LS_ORDER
        LS_SHARE = {}
        LS_ORDER = {}
        for record in session.run("""
            MATCH (s:事类)-[:类神]->(x)
            RETURN labels(x)[0] AS lab, x.name AS sym, count(DISTINCT s) AS n
        """):
            _k = LEISHEN_LABEL_MAP.get(record['lab'])
            if _k and record['sym']:
                LS_SHARE[(_k, record['sym'])] = max(int(record['n']), 1)
        for record in session.run("""
            MATCH (s:事类)-[r:类神]->(x)
            RETURN s.name AS sh, labels(x)[0] AS lab, x.name AS sym, r.顺序 AS ord, r.权重 AS w
        """):
            _k = LEISHEN_LABEL_MAP.get(record['lab'])
            if _k and record['sym'] and record['ord'] is not None:
                LS_ORDER[(record['sh'], _k, record['sym'])] = int(record['ord'])
        for record in ls_result:
            shilei_name = record['shilei_name']
            sym = record['sym']
            key = LEISHEN_LABEL_MAP.get(record['lab'])
            if not key or not sym:
                continue
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': {}, 'shensha': {}}
            ord_ = record['ord'] if record['ord'] is not None else 99
            # 类神单符号权重：读关系属性 r.权重（缺省 1.0）
            # 全事类倍数由 WEIGHT_CONFIG['类神'] 与 LS_SHILEI_MULT 控制
            _rw = record.get('w')
            ls_w = float(_rw) if _rw is not None else 1.0
            result[shilei_name].setdefault('leishen', {}).setdefault(key, {})[sym] = ls_w

        # 配位边：(神煞)-[:配位]->(地支/位置)，带 conditions_code 和动态准验率
        global PEIWEI_EDGES
        PEIWEI_EDGES = []
        peiwei_result = session.run("""
            MATCH (a:Shensha)-[r:配位]->(b)
            WHERE b:DiZhi OR b:位置
            RETURN a.name AS ma, b.name AS dz, 
                   r.conditions_code AS cc,
                   r.准验率 AS rate
        """)
        for record in peiwei_result:
            if record['ma'] and record['dz']:
                cc_str = record['cc'] or '{"type": "tongwei"}'
                try:
                    cc = json.loads(cc_str) if isinstance(cc_str, str) else cc_str
                except:
                    cc = {"type": "tongwei"}
                PEIWEI_EDGES.append({
                    'ma': record['ma'],
                    'dz': record['dz'],
                    'cc': cc,
                    'rate': float(record['rate']) if record['rate'] is not None else 0.0,
                })
        # 双神煞配位边（同位口径）
        global PEIWEI_PAIRS
        PEIWEI_PAIRS = []
        for record in session.run("""
            MATCH (a:Shensha)-[r:配位]->(b:Shensha)
            RETURN a.name AS ma, b.name AS mb, r.conditions_code AS cc, r.准验率 AS rate
        """):
            if record['ma'] and record['mb']:
                cc_str = record['cc'] or '{"type": "dual_tongwei"}'
                try:
                    cc = json.loads(cc_str) if isinstance(cc_str, str) else cc_str
                except:
                    cc = {"type": "dual_tongwei"}
                PEIWEI_PAIRS.append({
                    'ma': record['ma'], 'mb': record['mb'], 'cc': cc,
                    'rate': float(record['rate']) if record['rate'] is not None else 0.0,
                })

        SHENSHA_MAX = max(SHENSHA_COVER.values()) if SHENSHA_COVER else 25
        print(f"[维度关系权重] 已加载 {len(DIM_REL_WEIGHT)} 组 · 神煞覆盖上限 {SHENSHA_MAX} · 跳过非法维度 {len(_bad_dim)} 条 · 系数冲突 {len(_dim_conflict)} 个 · 源符号前缀不符 {len(_prefix_mismatch)} 条")
        for _b in _bad_dim[:10]:
            print(f"  ⚠️  非法维度已跳过: {_b[0]} → {_b[1]}  维度={_b[2]!r}")
        if len(_bad_dim) > 10:
            print(f"  ⚠️  其余 {len(_bad_dim)-10} 条省略")
        for _pm in _prefix_mismatch[:20]:
            print(f"  ❌ 源符号前缀与维度不符（该路由永不命中）: {_pm[0]} → {_pm[1]}  维度={_pm[2]}  源符号={_pm[3]}，应为 {_DIM_SRC_PREFIX.get(_pm[2],'?')}*")
        if len(_prefix_mismatch) > 20:
            print(f"  ❌ 其余 {len(_prefix_mismatch)-20} 条省略")
        for _c in _dim_conflict[:10]:
            print(f"  ⚠️  系数冲突（保留首次值）: {_c[0]}  首次={_c[1]}  冲突值={_c[2]}")
        if len(_dim_conflict) > 10:
            print(f"  ⚠️  其余 {len(_dim_conflict)-10} 个省略")
        print(f"[配位边] 已加载 {len(PEIWEI_EDGES)} 条单神煞 + {len(PEIWEI_PAIRS)} 条双神煞")

        # 配课边：课式级条件（非同位），如「日支=亥 → 未系增强」
        global PEIKE_EDGES
        PEIKE_EDGES = []
        for record in session.run("""
            MATCH (a:DiZhi)-[r:配课]->(b:DiZhi)
            RETURN a.name AS cond, b.name AS target,
                   r.conditions_code AS cc, r.准验率 AS rate
        """):
            if record['cond'] and record['target']:
                cc_str = record['cc'] or '{}'
                try:
                    cc = json.loads(cc_str) if isinstance(cc_str, str) else cc_str
                except:
                    cc = {}
                PEIKE_EDGES.append({
                    'cond': record['cond'],
                    'target': record['target'],
                    'cc': cc,
                    'rate': float(record['rate']) if record['rate'] is not None else 0.0,
                })
        print(f"[配课边] 已加载 {len(PEIKE_EDGES)} 条")

    driver.close()
    return result


def check_peiwei_conditions(cc: dict, dizhi: str, pos_shensha: set, ma: str, pos_name: str = '') -> bool:
    """配位条件解析器
    
    读取 conditions_code，判断当前位置是否满足配位条件。
    
    Args:
        cc: conditions_code 字典
        dizhi: 当前位置的地支
        pos_shensha: 当前位置的神煞名称集合
        ma: 主符号名称
        pos_name: 当前位置名称（如'占时'、'初传'等）
    
    Returns:
        bool: 是否满足条件
    """
    cc_type = cc.get('type', 'tongwei')
    
    if cc_type == 'tongwei':
        # 同位条件：地支 == 专属地支 且 该位置带主符号
        # 地支匹配由调用方保证（传入的 dizhi 就是边的 dz）
        # 只需检查主符号是否在该位置的神煞中
        return ma in pos_shensha
    
    elif cc_type == 'position_shensha':
        # 位置+神煞条件：指定位置带指定神煞
        # cc 格式: {"type": "position_shensha", "position": "zhanshi"}
        target_pos = cc.get('position', '')
        # 位置名称映射
        pos_map = {
            'zhanshi': '占时',
            'chuchuan': '初传',
            'zhongchuan': '中传',
            'mochuan': '末传',
            'xingnian': '行年',
            'benming': '本命',
        }
        target_pos_name = pos_map.get(target_pos, target_pos)
        # 检查当前位置是否匹配目标位置，且带主符号
        return pos_name == target_pos_name and ma in pos_shensha
    
    elif cc_type == 'dual_tongwei':
        partner = cc.get('partner', '')
        return bool(partner) and ma in pos_shensha and partner in pos_shensha

    # 未来扩展：其他条件类型
    # elif cc_type == 'tongwei_fujia':
    #     # 同位 + 附加符号
    #     fujia = cc.get('fujia', [])
    #     return ma in pos_shensha and any(f in pos_shensha for f in fujia)
    # elif cc_type == 'tongwei_wangxiang':
    #     # 同位 + 旺相条件
    #     ...
    
    return False


def detect_shilei(ke_data: dict, shilei_name: str, detail: bool = False) -> dict:
    """
    事类检测主函数
    
    证数计算规则：
    - 五个维度独立统计：天将、地支、六亲、长生、神煞
    - 每个位置每个维度最多1个证数
    - 每个位置最多5个证数
    - 总证数 = 所有位置的证数之和（最多35 = 7位置 × 5维度）
    
    Args:
        ke_data: 起课引擎全量数据
        shilei_name: 事类名称
        detail: 是否输出明细
    
    Returns:
        证数报告
    """
    # 1. 提取日干日支
    day_str = ke_data.get('day', '')
    ri_gan = day_str[0] if day_str and day_str[0] in '甲乙丙丁戊己庚辛壬癸' else None
    ri_zhi = day_str[1] if day_str and len(day_str) >= 2 and day_str[1] in '子丑寅卯辰巳午未申酉戌亥' else None
    
    # 2. 解析位置（四课、三传、行年、本命）
    positions = {}
    
    # 四课
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    
    # 三传
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
    # 行年上神（男方）
    xingnian_branch_1 = ke_data.get('xingnian_branch_1')
    tiandipan = get_tiandipan(ke_data)
    tianjiang_position = ke_data.get('tianjiang_position', {})
    if xingnian_branch_1 and tiandipan:
        xingnian_shangshen = tiandipan.get(xingnian_branch_1)
        if xingnian_shangshen:
            xingnian_tianjiang = tianjiang_position.get(xingnian_shangshen, '')
            positions['行年上神'] = {
                '地支': xingnian_shangshen,
                '天将': xingnian_tianjiang,
            }
    
    # 本命上神（男方）
    benming_1 = ke_data.get('benming_1')
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        if tiandipan:
            benming_shangshen = tiandipan.get(benming_dizhi)
            if benming_shangshen:
                benming_tianjiang = tianjiang_position.get(benming_shangshen, '')
                positions['本命上神'] = {
                    '地支': benming_shangshen,
                    '天将': benming_tianjiang,
                }
    
    # 行年上神_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2 and tiandipan:
        xingnian_shangshen_2 = tiandipan.get(xingnian_branch_2)
        if xingnian_shangshen_2:
            xingnian_tianjiang_2 = tianjiang_position.get(xingnian_shangshen_2, '')
            positions['行年上神_2'] = {
                '地支': xingnian_shangshen_2,
                '天将': xingnian_tianjiang_2,
            }
    
    # 本命上神_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        if tiandipan:
            benming_shangshen_2 = tiandipan.get(benming_dizhi_2)
            if benming_shangshen_2:
                benming_tianjiang_2 = tianjiang_position.get(benming_shangshen_2, '')
                positions['本命上神_2'] = {
                    '地支': benming_shangshen_2,
                    '天将': benming_tianjiang_2,
                }
    
    # 占时
    zhanshi = ke_data.get('zhanshi', '')
    if zhanshi:
        zhanshi_tianjiang = tianjiang_position.get(zhanshi, '') if isinstance(tianjiang_position, dict) else ''
        positions['占时'] = {
            '地支': zhanshi,
            '天将': zhanshi_tianjiang,
        }
    
    # 行年（男方）
    if xingnian_branch_1:
        xingnian_tianjiang = tianjiang_position.get(xingnian_branch_1, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年'] = {
            '地支': xingnian_branch_1,
            '天将': xingnian_tianjiang,
        }
    
    # 行年_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2:
        xingnian_tianjiang_2 = tianjiang_position.get(xingnian_branch_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年_2'] = {
            '地支': xingnian_branch_2,
            '天将': xingnian_tianjiang_2,
        }
    
    # 本命（男方）
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 3. 从8008输出提取类象数据（按符号分类）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞（按地支位置）
    shensha_data = ke_data.get('shensha', {})
    if isinstance(shensha_data, str):
        try:
            shensha_data = json.loads(shensha_data)
        except (ValueError, TypeError):
            shensha_data = {}
    
    # 5. 查询事类规则
    rules = fetch_shilei_rules(shilei_name)
    shilei_leixiang = rules['leixiang']
    routes = rules.get('routes', {})
    shilei_shensha = rules['shensha']
    
    # 6. 计算证数（每个位置每个维度独立判断）
    zhengshu = 0
    position_details = {}
    
    all_positions = POSITION_NAMES
    
    for pos_name in all_positions:
        pos_info = positions.get(pos_name, {})
        if not pos_info:
            continue
        
        pos_zhengshu = 0
        dim_details = {}
        
        dizhi = pos_info.get('地支', '')
        tianjiang = pos_info.get('天将', '')
        
        # [从8008读取] 六亲和长生由8008计算，从leixiang_by_symbol中提取
        # 查找该位置下以LQ_开头的六亲key
        pos_leixiang = get_pos_leixiang(leixiang_by_symbol, pos_name)
        liuqin_keys = [k for k in pos_leixiang.keys() if k.startswith('LQ_')]
        changsheng_keys = [k for k in pos_leixiang.keys() if k.startswith('CS_')]
        
        # 地支维度
        if dizhi:
            dz_key = f'DZ_{dizhi}'
            dz_leixiang = _route_filter(pos_leixiang.get(dz_key, {}), 'dizhi', dizhi, routes, pos_leixiang.get('__ck__', {}).get(dz_key, {}))
            matched_items = {k: v * shilei_leixiang[k] for k, v in dz_leixiang.items() if k in shilei_leixiang}
            if matched_items:
                pos_zhengshu += 1
                if detail:
                    dim_details['地支'] = {
                        'symbol': dizhi,
                        'matched': True,
                        'intersection_count': sum(matched_items.values()),
                        'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                    }
            else:
                if detail:
                    dim_details['地支'] = {'symbol': dizhi, 'matched': False}
        
        # 天将维度
        if tianjiang:
            tj_key = f'TJ_{tianjiang}'
            tj_leixiang = _route_filter(pos_leixiang.get(tj_key, {}), 'tianjiang', tianjiang, routes, pos_leixiang.get('__ck__', {}).get(tj_key, {}))
            matched_items = {k: v * shilei_leixiang[k] for k, v in tj_leixiang.items() if k in shilei_leixiang}
            if matched_items:
                pos_zhengshu += 1
                if detail:
                    dim_details['天将'] = {
                        'symbol': tianjiang,
                        'matched': True,
                        'intersection_count': sum(matched_items.values()),
                        'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                    }
            else:
                if detail:
                    dim_details['天将'] = {'symbol': tianjiang, 'matched': False}
        
        # 六亲维度（从8008读取）
        for lq_key in liuqin_keys:
            lq_leixiang = _route_filter(pos_leixiang.get(lq_key, {}), 'liuqin', _lq_symbol(lq_key), routes, pos_leixiang.get('__ck__', {}).get(lq_key, {}))
            matched_items = {k: v * shilei_leixiang[k] for k, v in lq_leixiang.items() if k in shilei_leixiang}
            if matched_items:
                pos_zhengshu += 1
                if detail:
                    liuqin_name = lq_key[3:]  # 去掉LQ_前缀
                    dim_details['六亲'] = {
                        'symbol': liuqin_name,
                        'matched': True,
                        'intersection_count': sum(matched_items.values()),
                        'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                    }
            else:
                if detail and '六亲' not in dim_details:
                    liuqin_name = lq_key[3:]
                    dim_details['六亲'] = {'symbol': liuqin_name, 'matched': False}
        
        # 长生维度（从8008读取）
        for cs_key in changsheng_keys:
                cs_leixiang = _route_filter(pos_leixiang.get(cs_key, {}), 'changsheng', cs_key[3:], routes, pos_leixiang.get('__ck__', {}).get(cs_key, {}))
                matched_items = {k: v * shilei_leixiang[k] for k, v in cs_leixiang.items() if k in shilei_leixiang}
                if matched_items:
                    pos_zhengshu += 1
                    if detail:
                        changsheng_name = cs_key[3:]  # 去掉CS_前缀
                        dim_details['长生'] = {
                            'symbol': changsheng_name,
                            'matched': True,
                            'intersection_count': sum(matched_items.values()),
                            'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                        }
                else:
                    if detail and '长生' not in dim_details:
                        changsheng_name = cs_key[3:]
                        dim_details['长生'] = {'symbol': changsheng_name, 'matched': False}
        
        # 神煞维度
        pos_shensha = set()
        if dizhi and dizhi in shensha_data:
            shensha_list = shensha_data[dizhi]
            pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
        
        shensha_intersection = {name for name in pos_shensha if name in shilei_shensha}
        if shensha_intersection:
            pos_zhengshu += 1
            if detail:
                dim_details['神煞'] = {
                    'matched': True,
                    'intersection_count': sum(shilei_shensha[name] for name in shensha_intersection),
                    'intersection_sample': list(shensha_intersection)[:3]
                }
        else:
            if detail:
                dim_details['神煞'] = {'matched': False}
        
        zhengshu += pos_zhengshu
        
        if detail:
            position_details[pos_name] = {
                'zhengshu': pos_zhengshu,
                'dimensions': dim_details
            }
    
    result = {
        'success': True,
        'shilei': shilei_name,
        'zhengshu': zhengshu,
        'max_zhengshu': len(all_positions) * 5,  # 7位置 × 5维度
        'shilei_leixiang_count': len(shilei_leixiang),
        'shilei_shensha_count': len(shilei_shensha)
    }
    
    if detail:
        result['position_details'] = position_details
    
    return result


def detect_all_shilei(ke_data: dict, detail: bool = False) -> dict:
    """一次计算所有17个事类的证数
    
    优化：只查1次Neo4j（获取所有事类规则），只解析1次六处位置
    
    Returns:
        {
            'success': True,
            'results': [
                {'shilei': '财运', 'zhengshu': 9, 'max_zhengshu': 35, 'position_details': {...}},
                ...
            ],
            'sorted': ['财运', '感情', ...]  # 按证数降序
        }
    """
    # 1. 提取日干日支
    day_str = ke_data.get('day', '')
    ri_gan = day_str[0] if day_str and day_str[0] in '甲乙丙丁戊己庚辛壬癸' else None
    ri_zhi = day_str[1] if day_str and len(day_str) >= 2 and day_str[1] in '子丑寅卯辰巳午未申酉戌亥' else None
    
    # 2. 解析位置（四课、三传、行年、本命）
    positions = {}
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
    # 行年上神（男方）
    xingnian_branch_1 = ke_data.get('xingnian_branch_1')
    tiandipan = get_tiandipan(ke_data)
    tianjiang_position = ke_data.get('tianjiang_position', {})
    if xingnian_branch_1 and tiandipan:
        xingnian_shangshen = tiandipan.get(xingnian_branch_1)
        if xingnian_shangshen:
            xingnian_tianjiang = tianjiang_position.get(xingnian_shangshen, '')
            positions['行年上神'] = {
                '地支': xingnian_shangshen,
                '天将': xingnian_tianjiang,
            }
    
    # 本命上神（男方）
    benming_1 = ke_data.get('benming_1')
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        if tiandipan:
            benming_shangshen = tiandipan.get(benming_dizhi)
            if benming_shangshen:
                benming_tianjiang = tianjiang_position.get(benming_shangshen, '')
                positions['本命上神'] = {
                    '地支': benming_shangshen,
                    '天将': benming_tianjiang,
                }
    
    # 行年上神_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2 and tiandipan:
        xingnian_shangshen_2 = tiandipan.get(xingnian_branch_2)
        if xingnian_shangshen_2:
            xingnian_tianjiang_2 = tianjiang_position.get(xingnian_shangshen_2, '')
            positions['行年上神_2'] = {
                '地支': xingnian_shangshen_2,
                '天将': xingnian_tianjiang_2,
            }
    
    # 本命上神_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        if tiandipan:
            benming_shangshen_2 = tiandipan.get(benming_dizhi_2)
            if benming_shangshen_2:
                benming_tianjiang_2 = tianjiang_position.get(benming_shangshen_2, '')
                positions['本命上神_2'] = {
                    '地支': benming_shangshen_2,
                    '天将': benming_tianjiang_2,
                }
    
    # 占时
    zhanshi = ke_data.get('zhanshi', '')
    if zhanshi:
        zhanshi_tianjiang = tianjiang_position.get(zhanshi, '') if isinstance(tianjiang_position, dict) else ''
        positions['占时'] = {
            '地支': zhanshi,
            '天将': zhanshi_tianjiang,
        }
    
    # 行年（男方）
    if xingnian_branch_1:
        xingnian_tianjiang = tianjiang_position.get(xingnian_branch_1, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年'] = {
            '地支': xingnian_branch_1,
            '天将': xingnian_tianjiang,
        }
    
    # 行年_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2:
        xingnian_tianjiang_2 = tianjiang_position.get(xingnian_branch_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年_2'] = {
            '地支': xingnian_branch_2,
            '天将': xingnian_tianjiang_2,
        }
    
    # 本命（男方）
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 3. 从8008输出提取类象数据（只做1次）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞（只做1次）
    shensha_data = ke_data.get('shensha', {})
    if isinstance(shensha_data, str):
        try:
            shensha_data = json.loads(shensha_data)
        except (ValueError, TypeError):
            shensha_data = {}
    
    # 5. 一次查询所有事类规则（避免N次数据库查询）
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类计算证数
    all_positions = POSITION_NAMES
    results = []
    
    for shilei_name, rules in all_rules.items():
        shilei_leixiang = rules['leixiang']
        # 规则表按维度分组：{维度: {类象名: 权重}}；取权重必须按维度，不能拿类象名去 in 整张表
        w_dz = shilei_leixiang.get('dizhi', {})
        w_tj = shilei_leixiang.get('tianjiang', {})
        w_lq = shilei_leixiang.get('liuqin', {})
        w_cs = shilei_leixiang.get('changsheng', {})
        routes = rules.get('routes', {})
        shilei_shensha = rules['shensha']
        zhengshu = 0
        position_details = {} if detail else None
        
        for pos_name in all_positions:
            pos_info = positions.get(pos_name, {})
            if not pos_info:
                continue
            
            dizhi = pos_info.get('地支', '')
            tianjiang = pos_info.get('天将', '')
            
            # [从8008读取] 六亲和长生由8008计算，从leixiang_by_symbol中提取
            pos_leixiang = get_pos_leixiang(leixiang_by_symbol, pos_name)
            liuqin_keys = [k for k in pos_leixiang.keys() if k.startswith('LQ_')]
            changsheng_keys = [k for k in pos_leixiang.keys() if k.startswith('CS_')]
            
            pos_zhengshu = 0
            dim_details = {} if detail else None
            
            # 地支维度
            if dizhi:
                dz_key = f'DZ_{dizhi}'
                dz_leixiang = _route_filter(pos_leixiang.get(dz_key, {}), 'dizhi', dizhi, routes, pos_leixiang.get('__ck__', {}).get(dz_key, {}))
                matched_items = {k: v * w_dz[k] for k, v in dz_leixiang.items() if k in w_dz}
                if matched_items:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['地支'] = {
                            'symbol': dizhi, 'matched': True,
                            'intersection_count': sum(matched_items.values()),
                            'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                        }
                elif detail:
                    dim_details['地支'] = {'symbol': dizhi, 'matched': False}
            
            # 天将维度
            if tianjiang:
                tj_key = f'TJ_{tianjiang}'
                tj_leixiang = _route_filter(pos_leixiang.get(tj_key, {}), 'tianjiang', tianjiang, routes, pos_leixiang.get('__ck__', {}).get(tj_key, {}))
                matched_items = {k: v * w_tj[k] for k, v in tj_leixiang.items() if k in w_tj}
                if matched_items:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['天将'] = {
                            'symbol': tianjiang, 'matched': True,
                            'intersection_count': sum(matched_items.values()),
                            'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                        }
                elif detail:
                    dim_details['天将'] = {'symbol': tianjiang, 'matched': False}
            
            # 六亲维度（从8008读取）
            for lq_key in liuqin_keys:
                lq_leixiang = _route_filter(pos_leixiang.get(lq_key, {}), 'liuqin', _lq_symbol(lq_key), routes, pos_leixiang.get('__ck__', {}).get(lq_key, {}))
                matched_items = {k: v * w_lq[k] for k, v in lq_leixiang.items() if k in w_lq}
                if matched_items:
                    pos_zhengshu += 1
                    if detail:
                        liuqin_name = lq_key[3:]
                        dim_details['六亲'] = {
                            'symbol': liuqin_name, 'matched': True,
                            'intersection_count': sum(matched_items.values()),
                            'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                        }
                elif detail and dim_details is not None and '六亲' not in dim_details:
                    liuqin_name = lq_key[3:]
                    dim_details['六亲'] = {'symbol': liuqin_name, 'matched': False}
            
            # 长生维度（从8008读取）
            for cs_key in changsheng_keys:
                cs_leixiang = _route_filter(pos_leixiang.get(cs_key, {}), 'changsheng', cs_key[3:], routes, pos_leixiang.get('__ck__', {}).get(cs_key, {}))
                matched_items = {k: v * w_cs[k] for k, v in cs_leixiang.items() if k in w_cs}
                if matched_items:
                    pos_zhengshu += 1
                    if detail:
                        changsheng_name = cs_key[3:]
                        dim_details['长生'] = {
                            'symbol': changsheng_name, 'matched': True,
                            'intersection_count': sum(matched_items.values()),
                            'intersection_sample': [f"{k}×{v:.1f}" if v != 1 else k for k, v in list(matched_items.items())[:3]]
                        }
                elif detail and dim_details is not None and '长生' not in dim_details:
                    changsheng_name = cs_key[3:]
                    dim_details['长生'] = {'symbol': changsheng_name, 'matched': False}
            
            # 神煞维度
            pos_shensha = set()
            if dizhi and dizhi in shensha_data:
                shensha_list = shensha_data[dizhi]
                pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
            shensha_intersection = {name for name in pos_shensha if name in shilei_shensha}
            if shensha_intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['神煞'] = {
                        'matched': True,
                        'intersection_count': sum(shilei_shensha[name] for name in shensha_intersection),
                        'intersection_sample': list(shensha_intersection)[:3]
                    }
            elif detail:
                dim_details['神煞'] = {'matched': False}
            
            zhengshu += pos_zhengshu
            
            if detail and pos_zhengshu > 0:
                position_details[pos_name] = {
                    'zhengshu': pos_zhengshu,
                    'dimensions': dim_details
                }
        
        entry = {
            'shilei': shilei_name,
            'zhengshu': zhengshu,
        }
        if detail:
            entry['position_details'] = position_details
        
        results.append(entry)
    
    # 按证数降序排序
    results.sort(key=lambda x: x['zhengshu'], reverse=True)
    sorted_names = [r['shilei'] for r in results]
    
    return {
        'success': True,
        'results': results,
        'sorted': sorted_names
    }


def detect_all_for_weight(ke_data: dict) -> dict:
    """直接输出加权分数（含调试明细）
    
    算法：
    1. 每个位置每个维度计算匹配类象数量
    2. 维度基础分 = 匹配数 × 维度权重（维度内累加，不封顶）
    3. 多维组合加分 = 维度基础分之和 × 组合比例
    4. 位置分数 = 基础分 + 组合加分
    5. 事类总分 = 所有位置分数之和
    
    Returns:
        {
            'success': True,
            'shilei_results': {
                '财运': {
                    'total_score': 25.5,
                    'positions': {
                        '第1课上神': {
                            'score': 8.0, 'base': 4.0, 'combo': 4.0,
                            'dims': {
                                '地支': {'symbol': '寅', 'matched': ['钱财', '金银'], 'score': 4.0},
                                '天将': {'symbol': '青龙', 'matched': ['财帛'], 'score': 2.0},
                                '六亲': {'symbol': '妻财', 'matched': [], 'score': 0},
                                '长生': {'symbol': '帝旺', 'matched': [], 'score': 0},
                                '神煞': {'matched': ['天财', '地财'], 'score': 1.0}
                            }
                        },
                        ...
                    }
                },
                ...
            },
            'sorted': ['财运', '感情', ...]  # 按总分降序
        }
    """
    # 1. 提取日干日支
    day_str = ke_data.get('day', '')
    ri_gan = day_str[0] if day_str and day_str[0] in '甲乙丙丁戊己庚辛壬癸' else None
    ri_zhi = day_str[1] if day_str and len(day_str) >= 2 and day_str[1] in '子丑寅卯辰巳午未申酉戌亥' else None
    
    # 2. 解析位置（四课、三传、行年、本命）
    positions = {}
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
    # 行年上神（男方）
    xingnian_branch_1 = ke_data.get('xingnian_branch_1')
    tiandipan = get_tiandipan(ke_data)
    tianjiang_position = ke_data.get('tianjiang_position', {})
    if xingnian_branch_1 and tiandipan:
        xingnian_shangshen = tiandipan.get(xingnian_branch_1)
        if xingnian_shangshen:
            xingnian_tianjiang = tianjiang_position.get(xingnian_shangshen, '')
            positions['行年上神'] = {
                '地支': xingnian_shangshen,
                '天将': xingnian_tianjiang,
            }
    
    # 本命上神（男方）
    benming_1 = ke_data.get('benming_1')
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        if tiandipan:
            benming_shangshen = tiandipan.get(benming_dizhi)
            if benming_shangshen:
                benming_tianjiang = tianjiang_position.get(benming_shangshen, '')
                positions['本命上神'] = {
                    '地支': benming_shangshen,
                    '天将': benming_tianjiang,
                }
    
    # 行年上神_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2 and tiandipan:
        xingnian_shangshen_2 = tiandipan.get(xingnian_branch_2)
        if xingnian_shangshen_2:
            xingnian_tianjiang_2 = tianjiang_position.get(xingnian_shangshen_2, '')
            positions['行年上神_2'] = {
                '地支': xingnian_shangshen_2,
                '天将': xingnian_tianjiang_2,
            }
    
    # 本命上神_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        if tiandipan:
            benming_shangshen_2 = tiandipan.get(benming_dizhi_2)
            if benming_shangshen_2:
                benming_tianjiang_2 = tianjiang_position.get(benming_shangshen_2, '')
                positions['本命上神_2'] = {
                    '地支': benming_shangshen_2,
                    '天将': benming_tianjiang_2,
                }
    
    # 占时
    zhanshi = ke_data.get('zhanshi', '')
    if zhanshi:
        zhanshi_tianjiang = tianjiang_position.get(zhanshi, '') if isinstance(tianjiang_position, dict) else ''
        positions['占时'] = {
            '地支': zhanshi,
            '天将': zhanshi_tianjiang,
        }
    
    # 行年（男方）
    if xingnian_branch_1:
        xingnian_tianjiang = tianjiang_position.get(xingnian_branch_1, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年'] = {
            '地支': xingnian_branch_1,
            '天将': xingnian_tianjiang,
        }
    
    # 行年_2（女方）
    xingnian_branch_2 = ke_data.get('xingnian_branch_2')
    if xingnian_branch_2:
        xingnian_tianjiang_2 = tianjiang_position.get(xingnian_branch_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['行年_2'] = {
            '地支': xingnian_branch_2,
            '天将': xingnian_tianjiang_2,
        }
    
    # 本命（男方）
    if _bm_dz(benming_1):
        benming_dizhi = _bm_dz(benming_1)
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if _bm_dz(benming_2):
        benming_dizhi_2 = _bm_dz(benming_2)
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 3. 从8008输出提取类象数据（按符号分类）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞
    shensha_data = ke_data.get('shensha', {})
    if isinstance(shensha_data, str):
        try:
            shensha_data = json.loads(shensha_data)
        except (ValueError, TypeError):
            shensha_data = {}
    
    # 5. 一次查询所有事类规则
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类，计算加权分数（含明细）
    all_positions = POSITION_NAMES
    shilei_results = {}
    
    for shilei_name, rules in all_rules.items():
        shilei_leixiang = rules['leixiang']  # 现在是按维度分组的
        routes = rules.get('routes', {})
        shilei_shensha = rules['shensha']
        total_score = 0.0
        lx_ch_total = 0.0   # 类象通道（千夫长+士兵）
        ls_ch_total = 0.0   # 类神通道（将军）
        ls_all_hits = set()
        pos_scores = {}
        # 撤职事类：多维类象按一维计（只看有没有，不累加维度）
        _demote = shilei_name in DEMOTED_SHILEI
        _demoted_lx = DEMOTED_LEIXIANG.get(shilei_name, set())
        def _ew(lx, w):
            if _demote or lx in _demoted_lx:
                return 1
            return w
        
        for pos_name in all_positions:
            pos_info = positions.get(pos_name, {})
            if not pos_info:
                continue
            
            dizhi = pos_info.get('地支', '')
            tianjiang = pos_info.get('天将', '')
            
            # [从8008读取] 六亲和长生由8008计算，从leixiang_by_symbol中提取
            pos_leixiang = get_pos_leixiang(leixiang_by_symbol, pos_name)
            liuqin_dz_keys = [k for k in pos_leixiang.keys() if k.startswith('LQ_') and not k.startswith('LQ_DUN_')]
            liuqin_dun_keys = [k for k in pos_leixiang.keys() if k.startswith('LQ_DUN_')]
            changsheng_keys = [k for k in pos_leixiang.keys() if k.startswith('CS_')]
            
            # 计算每个维度的匹配明细
            dim_details = {}
            dim_matches = {}  # 用于计算分数

            # 同一乘关系去重：该位置的（地支,天将）若构成已知乘关系，
            # 地支维度计过 D 侧类象后，天将维度不再计 T 侧类象（同一事实只算一次）
            # 乘关系去重已关闭：实测会误杀千夫长（地支侧命中后，天将侧的高维类象被跳过）
            _pair_key = ''
            _combo = None
            _dj_combo_used = False
            
            # 地支维度
            if dizhi:
                dz_key = f'DZ_{dizhi}'
                dz_leixiang = _route_filter(pos_leixiang.get(dz_key, {}), 'dizhi', dizhi, routes, pos_leixiang.get('__ck__', {}).get(dz_key, {}))
                # 从地支维度读取权重
                dizhi_weights = {**shilei_leixiang.get('leixiang_all', {}), **shilei_leixiang.get('dizhi', {})}
                # 找出匹配的类象及其权重（含事类关联权重）
                matched_items = []
                for lx_name, weight in dz_leixiang.items():
                    if lx_name in dizhi_weights:
                        shilei_weight = dizhi_weights[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                # 分离一维和多维
                one_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) == 1]
                multi_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) > 1]
                # 一维只取事类权重最大的1个，多维正常累加
                _parts = []
                if one_dim_items:
                    _parts.append(max(sw for _, _, sw in one_dim_items))  # 士兵（一维类象）只计一次，不累加
                _parts.extend(w * sw * MULTI_COEF for _, w, sw in multi_dim_items)
                total_weight = math.fsum(_parts)  # 多维类象：维度倍数 × 事类权重 × 千夫长系数
                # 格式化输出
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight:.1f}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                dim_details['地支'] = {
                    'symbol': dizhi,
                    'matched': matched_str
                }
                if _combo and any(lx in _combo['D'] for lx, _, _ in matched_items):
                    _dj_combo_used = True
                dim_matches['地支'] = total_weight
            
            # 天将维度
            if tianjiang:
                tj_key = f'TJ_{tianjiang}'
                tj_leixiang = _route_filter(pos_leixiang.get(tj_key, {}), 'tianjiang', tianjiang, routes, pos_leixiang.get('__ck__', {}).get(tj_key, {}))
                # 从天将维度读取权重
                tianjiang_weights = {**shilei_leixiang.get('leixiang_all', {}), **shilei_leixiang.get('tianjiang', {})}
                matched_items = []
                for lx_name, weight in tj_leixiang.items():
                    if lx_name in tianjiang_weights:
                        # 同一乘关系已在地支维度计过 → 天将侧不再重复计分
                        if _dj_combo_used and _combo and lx_name in _combo['T']:
                            continue
                        shilei_weight = tianjiang_weights[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) == 1]
                multi_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) > 1]
                _parts = []
                if one_dim_items:
                    _parts.append(max(sw for _, _, sw in one_dim_items))  # 士兵（一维类象）只计一次，不累加
                _parts.extend(w * sw * MULTI_COEF for _, w, sw in multi_dim_items)
                total_weight = math.fsum(_parts)
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight:.1f}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                dim_details['天将'] = {
                    'symbol': tianjiang,
                    'matched': matched_str
                }
                dim_matches['天将'] = total_weight
            
            # 地支六亲维度（从8008读取）
            for lq_key in liuqin_dz_keys:
                lq_leixiang = _route_filter(pos_leixiang.get(lq_key, {}), 'liuqin', _lq_symbol(lq_key), routes, pos_leixiang.get('__ck__', {}).get(lq_key, {}))
                # 从六亲维度读取权重
                liuqin_weights = {**shilei_leixiang.get('leixiang_all', {}), **shilei_leixiang.get('liuqin', {})}
                matched_items = []
                for lx_name, weight in lq_leixiang.items():
                    if lx_name in liuqin_weights:
                        shilei_weight = liuqin_weights[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) == 1]
                multi_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) > 1]
                _parts = []
                if one_dim_items:
                    _parts.append(max(sw for _, _, sw in one_dim_items))  # 士兵（一维类象）只计一次，不累加
                _parts.extend(w * sw * MULTI_COEF for _, w, sw in multi_dim_items)
                total_weight = math.fsum(_parts)
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight:.1f}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                liuqin_name = lq_key[3:]  # 去掉LQ_前缀
                dim_details['地支六亲'] = {
                    'symbol': liuqin_name,
                    'matched': matched_str
                }
                dim_matches['地支六亲'] = total_weight
            
            # 遁干六亲维度（从8008读取）
            for lq_key in liuqin_dun_keys:
                lq_leixiang = _route_filter(pos_leixiang.get(lq_key, {}), 'liuqin', _lq_symbol(lq_key), routes, pos_leixiang.get('__ck__', {}).get(lq_key, {}))
                # 从六亲维度读取权重（遁干六亲和地支六亲共用同一个权重表）
                liuqin_weights = {**shilei_leixiang.get('leixiang_all', {}), **shilei_leixiang.get('liuqin', {})}
                matched_items = []
                for lx_name, weight in lq_leixiang.items():
                    if lx_name in liuqin_weights:
                        shilei_weight = liuqin_weights[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) == 1]
                multi_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) > 1]
                _parts = []
                if one_dim_items:
                    _parts.append(max(sw for _, _, sw in one_dim_items))  # 士兵（一维类象）只计一次，不累加
                _parts.extend(w * sw * MULTI_COEF for _, w, sw in multi_dim_items)
                total_weight = math.fsum(_parts)
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight:.1f}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                liuqin_name = lq_key[7:]  # 去掉LQ_DUN_前缀
                dim_details['遁干六亲'] = {
                    'symbol': liuqin_name,
                    'matched': matched_str
                }
                dim_matches['遁干六亲'] = total_weight
            
            # 长生维度（从8008读取）
            for cs_key in changsheng_keys:
                cs_leixiang = _route_filter(pos_leixiang.get(cs_key, {}), 'changsheng', cs_key[3:], routes, pos_leixiang.get('__ck__', {}).get(cs_key, {}))
                # 从长生维度读取权重
                changsheng_weights = {**shilei_leixiang.get('leixiang_all', {}), **shilei_leixiang.get('changsheng', {})}
                matched_items = []
                for lx_name, weight in cs_leixiang.items():
                    if lx_name in changsheng_weights:
                        shilei_weight = changsheng_weights[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) == 1]
                multi_dim_items = [(lx, _ew(lx, w), sw) for lx, w, sw in matched_items if _ew(lx, w) > 1]
                _parts = []
                if one_dim_items:
                    _parts.append(max(sw for _, _, sw in one_dim_items))  # 士兵（一维类象）只计一次，不累加
                _parts.extend(w * sw * MULTI_COEF for _, w, sw in multi_dim_items)
                total_weight = math.fsum(_parts)
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight:.1f}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                changsheng_name = cs_key[3:]  # 去掉CS_前缀
                dim_details['长生'] = {
                    'symbol': changsheng_name,
                    'matched': matched_str
                }
                dim_matches['长生'] = total_weight
            
            # 神煞维度（含事类关联权重）
            pos_shensha = set()
            if dizhi and dizhi in shensha_data:
                shensha_list = shensha_data[dizhi]
                pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
            shensha_intersection = {name for name in pos_shensha if name in shilei_shensha}
            # 集合迭代顺序随哈希种子变化 → fsum + sorted 保证与顺序无关
            shensha_weighted_score = math.fsum(sorted(shilei_shensha[name] for name in shensha_intersection))
            
            # 应用神煞聚集效应：同一位置命中多个神煞时额外加分
            # 公式：基础分 × (1 + (命中数-1) × 聚集系数)
            juji_ratio = JUJI_CONFIG.get(shilei_name, 0.0)
            if len(shensha_intersection) > 1 and juji_ratio > 0:
                juji_multiplier = 1 + (len(shensha_intersection) - 1) * juji_ratio
                shensha_weighted_score *= juji_multiplier
            
            dim_details['神煞'] = {
                'matched': [f"{name}×{shilei_shensha[name]:.1f}" if shilei_shensha[name] != 1 else name for name in shensha_intersection]
            }
            dim_matches['神煞'] = shensha_weighted_score
            
            # 计算加权分数
            score_info = calculate_weighted_score(dim_matches, shilei_name=shilei_name)
            
            # 为每个维度添加得分（应用事类关联权重）
            for dim_name in dim_details:
                dim_weight = WEIGHT_CONFIG.get(dim_name, 1.0)
                weighted_match = dim_matches.get(dim_name, 0)
                # weighted_match 已经包含事类关联权重，再乘以维度权重
                dim_details[dim_name]['score'] = round(weighted_match * dim_weight, 1)
            
            # 类神（将军）通道：符号命中即得高分，按顺序分级（顺序1=将军）
            ls = all_rules.get(shilei_name, {}).get('leishen', {})
            ls_hits = []
            ls_raw = 0.0
            if ls:
                def _share(k, sym):
                    _m = LS_ORDER_MODE
                    if _m == 'flat':
                        return 1
                    _o = LS_ORDER.get((shilei_name, k, sym), 5)
                    if _m == 'inv':
                        return max(_o, 1) if _o != 99 else 0.05
                    if _m == 'sqrt':
                        return (max(_o, 1) ** 0.5) if _o != 99 else 1.0
                    if _m == 'core2':
                        return 0.5 if (_o <= 3 or _o == 99) else 1.0
                    if _m == 'core3':
                        return 0.33 if (_o <= 3 or _o == 99) else 1.0
                    return 1
                def _dm(k):
                    return LS_DIM_MULT.get(k, 1.0)
                # ── 类神通道三阶口径 ──
                # ① 符号基础分 = 维度权重（地支3.0/天将1.0/六亲1.0或0.5/长生0.5/神煞1.0）
                # ② 维度单价   = 维度权重 × 维度关系权重(事类,维度)
                # ③ 类神分     = 维度单价 × Σ(1 + 准验率_i)，未命中配位则 ×1
                # 类神边上的 r.权重 不参与计分（历史残留）。
                def _dim_rel_wei(kind):
                    if kind == 'shensha':
                        _cov = SHENSHA_COVER.get(shilei_name, 0)
                        return (SHENSHA_MAX / _cov) if _cov else 0.0
                    if kind == 'changsheng':
                        return 1.0
                    return DIM_REL_WEIGHT.get((shilei_name, kind), 1.0)

                def _unit_price(kind, dim_key):
                    if kind == 'shensha' and not SHENSHA_COVER.get(shilei_name):
                        return 0.0
                    return WEIGHT_CONFIG.get(dim_key, 1.0) * _dim_rel_wei(kind)

                ls_items = []   # [(kind, name, 单价, 显示标签)]
                if ls.get('dizhi', {}).get(dizhi):
                    ls_items.append(('dizhi', dizhi, _unit_price('dizhi', '地支'), f"{dizhi}(地支)"))
                if ls.get('tianjiang', {}).get(tianjiang):
                    ls_items.append(('tianjiang', tianjiang, _unit_price('tianjiang', '天将'), f"{tianjiang}(天将)"))
                for _k in liuqin_dz_keys:
                    _n = _k[3:]
                    if ls.get('liuqin', {}).get(_n):
                        ls_items.append(('liuqin', _n, _unit_price('liuqin', '地支六亲'), f"{_n}(六亲)"))
                for _k in liuqin_dun_keys:
                    _n = _k[7:]
                    if ls.get('liuqin', {}).get(_n):
                        ls_items.append(('liuqin', _n, _unit_price('liuqin', '遁干六亲'), f"{_n}(遁干六亲)"))
                for _k in changsheng_keys:
                    _n = _k[3:]
                    if ls.get('changsheng', {}).get(_n):
                        ls_items.append(('changsheng', _n, _unit_price('changsheng', '长生'), f"{_n}(长生)"))
                for _s in sorted(pos_shensha):
                    if ls.get('shensha', {}).get(_s):
                        ls_items.append(('shensha', _s, _unit_price('shensha', '神煞'), f"{_s}(神煞)"))
                matched_rates = {}   # {(kind, name): [准验率, ...]}
                matched_hits = []

                for edge in PEIWEI_EDGES:
                    _ma, _dz, _cc, _rate = edge['ma'], edge['dz'], edge['cc'], edge['rate']
                    if _rate <= 0:
                        continue
                    _cc_type = _cc.get('type', 'tongwei')
                    if _cc_type == 'tongwei' and _dz != dizhi:
                        continue
                    if not check_peiwei_conditions(_cc, _dz, pos_shensha, _ma, pos_name):
                        continue
                    matched_rates.setdefault(('shensha', _ma), []).append(_rate)
                    if _cc_type == 'tongwei':
                        matched_rates.setdefault(('dizhi', _dz), []).append(_rate)
                    matched_hits.append(f"配位:{_ma}见{_dz}(×{1+_rate:.3f})")

                for pair in PEIWEI_PAIRS:
                    _pa, _pb, _prate = pair['ma'], pair['mb'], pair['rate']
                    if _prate <= 0:
                        continue
                    if not check_peiwei_conditions(pair['cc'], dizhi, pos_shensha, _pa, pos_name):
                        continue
                    for _ps in (_pa, _pb):
                        matched_rates.setdefault(('shensha', _ps), []).append(_prate)
                    matched_hits.append(f"配位:{_pa}同{_pb}(×{1+_prate:.3f})")

                # 配课边：课式级条件（非同位），与配位一样是**双向加成**——
                # 起点与终点各自在自己出现的位置上生效。
                # 如「日支=亥 ⇄ 未」：课里每个亥位、每个未位都乘 (1+准验率)。
                for pe in PEIKE_EDGES:
                    _krate = pe['rate']
                    if _krate <= 0:
                        continue
                    _cc_type = pe['cc'].get('type', 'rizhi')
                    
                    # 类型1：日支条件（现有）
                    if _cc_type == 'rizhi' and ri_zhi and pe['cond'] == ri_zhi:
                        if dizhi == pe['cond'] or dizhi == pe['target']:
                            matched_rates.setdefault(('dizhi', dizhi), []).append(_krate)
                            matched_hits.append(
                                f"配课:{pe['cond']}⇄{pe['target']}@{dizhi}(×{1+_krate:.3f})")
                    
                    # 类型2：乘关系（新增）- 位置层面判断
                    elif _cc_type == 'cheng':
                        _tj = pe['cc'].get('tianjiang', '')
                        _dz = pe['cc'].get('dizhi', '')
                        if dizhi == _dz and tianjiang == _tj:
                            # 地支和天将都获得加成（双向）
                            matched_rates.setdefault(('dizhi', dizhi), []).append(_krate)
                            matched_rates.setdefault(('tianjiang', tianjiang), []).append(_krate)
                            matched_hits.append(
                                f"配课:{_tj}乘{_dz}@{pos_name}(×{1+_krate:.3f})")
                    
                    # 类型3：加关系（新增）- 位置层面判断
                    elif _cc_type == 'jia':
                        _tianpan = pe['cc'].get('tianpan', '')
                        _dipan = pe['cc'].get('dipan', '')
                        # 判断该位置是否满足"天盘加地盘"
                        if dizhi == _dipan:
                            _actual_tianpan = tiandipan.get(_dipan, '')
                            if _actual_tianpan == _tianpan:
                                matched_rates.setdefault(('dizhi', dizhi), []).append(_krate)
                                matched_hits.append(
                                    f"配课:{_tianpan}加{_dipan}@{pos_name}(×{1+_krate:.3f})")
                    
                    # 类型4：临关系（新增）- 位置层面判断
                    elif _cc_type == 'lin':
                        _tj = pe['cc'].get('tianjiang', '')
                        _dipan = pe['cc'].get('dipan', '')
                        # 临 = 天将乘天盘，天盘加地盘
                        # 即：该位置地支=地盘，该位置天将=天将
                        if dizhi == _dipan and tianjiang == _tj:
                            matched_rates.setdefault(('dizhi', dizhi), []).append(_krate)
                            matched_rates.setdefault(('tianjiang', tianjiang), []).append(_krate)
                            matched_hits.append(
                                f"配课:{_tj}临{_dipan}@{pos_name}(×{1+_krate:.3f})")

                # 入账：单价 × Σ(1+准验率)；未命中配位则 ×1
                # 注意：ls_items / matched_rates 的迭代顺序可能随进程哈希种子变化，
                # 累加必须用 fsum（精确求和、与顺序无关），否则末位差会经 round(x,1) 放大成 0.1
                ls_parts = []
                for _kind, _name, _price, _label in ls_items:
                    if _price <= 0:
                        continue
                    _rs = matched_rates.get((_kind, _name), [])
                    if _rs:
                        _factor = math.fsum(1 + r for r in _rs)
                        ls_hits.append(f"{_label}×{_factor:.3f}")
                    else:
                        _factor = 1.0
                        ls_hits.append(_label)
                    ls_parts.append(_price * _factor)
                ls_raw = math.fsum(ls_parts)
                ls_hits.extend(matched_hits)

            ls_weight = WEIGHT_CONFIG.get('类神', 1.0) * LS_SHILEI_MULT.get(shilei_name, 1.0)
            ls_score = round(ls_raw * ls_weight, 1)
            if ls_hits:
                dim_details['类神'] = {
                    'matched': ls_hits,
                    'score': ls_score
                }

            # 位置权重（男/女位置共用同名权重，去掉 _2 后缀查表）
            pos_weight = POSITION_WEIGHT.get(pos_name.replace('_2', ''), 1.0)
            pos_score = round(score_info['score'] * pos_weight + ls_score, 1)
            if ls_hits:
                ls_all_hits.update(ls_hits)

            if pos_score > 0:
                pos_scores[pos_name] = {
                    'score': pos_score,
                    'pos_weight': pos_weight,
                    'base': score_info['base_score'],
                    'combo': score_info['combo_score'],
                    'lx_base': round(score_info['score'] * pos_weight, 3),
                    'ls_raw': round(ls_raw, 4),
                    'dims': dim_details
                }
                total_score += pos_score
                lx_ch_total += score_info['score'] * pos_weight
                ls_ch_total += ls_score
        
        hit = len({p.replace('_2', '') for p in pos_scores}) if pos_scores else 0
        # 覆盖率已废除：各位置权重并不相同，用「命中位置类型数 ÷ 12」做统一缩放会偏颇粗糙。
        # 总分只由位置得分求和得出，位置差异由位置权重表达。
        norm_total = total_score if total_score > 0 else 0.0
        # 将军分：命中即得固定分，整课只算一次（"往那一站就是高分"，不随命中个数累加）
        if ls_all_hits and LS_FIXED_SCORE:
            norm_total += LS_FIXED_SCORE
        if norm_total > 0:
            shilei_results[shilei_name] = {
                'total_score': round(norm_total, 1),
                'hit_positions': hit,
                'lx_ch': round(lx_ch_total, 1),
                'ls_ch': round(ls_ch_total, 1),
                'positions': pos_scores
            }
    
    # 按总分降序排序
    sorted_items = sorted(shilei_results.items(), key=lambda x: x[1]['total_score'], reverse=True)
    sorted_names = [name for name, _ in sorted_items]
    
    return {
        'success': True,
        'shilei_results': shilei_results,
        'sorted': sorted_names
    }

