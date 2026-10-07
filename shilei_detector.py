"""
事类检测器核心逻辑
输入：起课引擎全量数据 + 事类名称
输出：加权分数报告
"""
import re
import sys
from itertools import combinations
from neo4j import GraphDatabase

# Neo4j 连接
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

# ─── 权重配置缓存 ─────────────────────────────────────────────
# 维度权重（从 Neo4j 加载）
WEIGHT_CONFIG = {}

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


def load_weight_config():
    """从 Neo4j 加载权重配置到内存"""
    global WEIGHT_CONFIG, COMBINATION_RATIO, COMBINATION_RATIO_BY_SHILEI, JUJI_CONFIG
    
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
    
    driver.close()
    print(f"[权重配置] 已加载: 维度权重={WEIGHT_CONFIG}, 2维组合={len(COMBINATION_RATIO)}个, 聚集系数={JUJI_CONFIG}")


def get_combination_ratio(dims: set, shilei_name: str = None) -> float:
    """获取多维组合的比例
    
    2维：直接查表（优先事类专属，fallback到全局）
    3维及以上：涉及的所有2维组合比例的求和（优先事类专属，fallback到全局）
    """
    dims_list = list(dims)
    if len(dims_list) < 2:
        return 0.0
    
    # 获取事类专属或全局的组合比例表
    shilei_ratios = COMBINATION_RATIO_BY_SHILEI.get(shilei_name, {}) if shilei_name else {}
    
    def _get_ratio(combo_key):
        """优先查事类专属，fallback到全局"""
        return shilei_ratios.get(combo_key, COMBINATION_RATIO.get(combo_key, 0.0))
    
    if len(dims_list) == 2:
        return _get_ratio(frozenset(dims_list))
    
    # 3维及以上：计算所有2维子组合的比例求和
    total_ratio = 0.0
    for combo in combinations(dims_list, 2):
        total_ratio += _get_ratio(frozenset(combo))
    
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
        'score': round(total_score, 2),
        'base_score': round(base_score, 2),
        'combo_score': round(combo_score, 2),
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

# 13处位置名（与8008输出对齐）
POSITION_NAMES = [
    '第1课上神', '第2课上神', '第3课上神', '第4课上神',
    '初传', '中传', '末传',
    '占时', '行年', '本命', '行年_2', '本命_2',
    '行年上神', '本命上神', '行年上神_2', '本命上神_2',
    '月将所乘天将'
]


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
    
    维度判断（基于combination_key段数）：
    - 4段 → 一维 → 计数1
    - 5段 → 二维 → 计数2
    - 6段 → 三维 → 计数3
    - 7段 → 四维 → 计数4
    
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
                # 判断维度：根据段数判断
                num_parts = len(parts)
                if num_parts >= 4:
                    weight = num_parts - 3  # 4段→1, 5段→2, 6段→3, 7段→4
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
                # 同一位置内，同一类象只计数1次（不累加）
                if leixiang_name not in pos_data[item_id]:
                    pos_data[item_id][leixiang_name] = weight
    
    return result


def get_pos_leixiang(leixiang_by_symbol: dict, pos_name: str) -> dict:
    """取某位置的类象，合并全局桶
    
    天将/地支/天干类象无位置语义，存在 __global__ 桶中；
    六亲/长生类象按位置存放。本函数把两者合并，供维度计算使用。
    """
    merged = dict(leixiang_by_symbol.get('__global__', {}))
    merged.update(leixiang_by_symbol.get(pos_name, {}))
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
        # 查询事类关联的类象（含权重）
        leixiang_result = session.run("""
            MATCH (s:事类 {name: $name})-[r:关联类象]->(l:Leixiang)
            RETURN l.name AS leixiang_name, r.权重 AS weight
        """, name=shilei_name)
        leixiang_dict = {record['leixiang_name']: (record['weight'] if record['weight'] is not None else 1.0) for record in leixiang_result}
        
        # 查询事类关联的神煞（含权重）
        shensha_result = session.run("""
            MATCH (s:事类 {name: $name})-[r:关联神煞]->(ss:Shensha)
            RETURN ss.name AS shensha_name, r.权重 AS weight
        """, name=shilei_name)
        shensha_dict = {record['shensha_name']: (record['weight'] if record['weight'] is not None else 1.0) for record in shensha_result}
    
    driver.close()
    
    return {
        'leixiang': leixiang_dict,
        'shensha': shensha_dict
    }


def fetch_all_shilei_rules() -> dict:
    """一次查询获取所有事类的规则（含权重）
    
    Returns:
        {
            '财运': {'leixiang': {'类象名': 权重, ...}, 'shensha': {'神煞名': 权重, ...}},
            ...
        }
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    result = {}
    with driver.session() as session:
        # 一次查询所有事类及其关联的类象（含权重，含天将专属权重）
        leixiang_result = session.run("""
            MATCH (s:事类)-[r:关联类象]->(l:Leixiang)
            RETURN s.name AS shilei_name, l.name AS leixiang_name, r.权重 AS weight, r.天将 AS tianjiang
        """)
        for record in leixiang_result:
            shilei_name = record['shilei_name']
            leixiang_name = record['leixiang_name']
            weight = record['weight'] if record['weight'] is not None else 1.0
            tianjiang = record['tianjiang']  # 可能为 None
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': {}, 'leixiang_tj': {}, 'shensha': {}}
            if tianjiang:
                # 天将专属权重：{类象名: {天将: weight}}
                if leixiang_name not in result[shilei_name]['leixiang_tj']:
                    result[shilei_name]['leixiang_tj'][leixiang_name] = {}
                result[shilei_name]['leixiang_tj'][leixiang_name][tianjiang] = weight
            else:
                result[shilei_name]['leixiang'][leixiang_name] = weight
        
        # 一次查询所有事类及其关联的神煞（含权重）
        shensha_result = session.run("""
            MATCH (s:事类)-[r:关联神煞]->(ss:Shensha)
            RETURN s.name AS shilei_name, ss.name AS shensha_name, r.权重 AS weight
        """)
        for record in shensha_result:
            shilei_name = record['shilei_name']
            shensha_name = record['shensha_name']
            weight = record['weight'] if record['weight'] is not None else 1.0
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': {}, 'leixiang_tj': {}, 'shensha': {}}
            result[shilei_name]['shensha'][shensha_name] = weight
    
    driver.close()
    return result


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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]  # 本命干支的地支部分
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
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 月将所乘天将
    yuejiang = ke_data.get('yuejiang', '')
    if yuejiang and isinstance(tianjiang_position, dict):
        yuejiang_tianjiang = tianjiang_position.get(yuejiang, '')
        if yuejiang_tianjiang:
            positions['月将所乘天将'] = {
                '地支': yuejiang,
                '天将': yuejiang_tianjiang,
            }
    
    # 3. 从8008输出提取类象数据（按符号分类）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞（按地支位置）
    shensha_data = ke_data.get('shensha', {})
    
    # 5. 查询事类规则
    rules = fetch_shilei_rules(shilei_name)
    shilei_leixiang = rules['leixiang']
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
        
        # 地支维度（月将所乘天将不计算地支）
        if dizhi and pos_name != '月将所乘天将':
            dz_key = f'DZ_{dizhi}'
            dz_leixiang = pos_leixiang.get(dz_key, {})
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
            tj_leixiang = pos_leixiang.get(tj_key, {})
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
            lq_leixiang = pos_leixiang.get(lq_key, {})
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
        
        # 长生维度（从8008读取，月将所乘天将不计算长生）
        if pos_name != '月将所乘天将':
            for cs_key in changsheng_keys:
                cs_leixiang = pos_leixiang.get(cs_key, {})
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
        
        # 神煞维度（月将所乘天将不计算神煞）
        pos_shensha = set()
        if pos_name != '月将所乘天将' and dizhi and dizhi in shensha_data:
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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]  # 本命干支的地支部分
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
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 月将所乘天将
    yuejiang = ke_data.get('yuejiang', '')
    if yuejiang and isinstance(tianjiang_position, dict):
        yuejiang_tianjiang = tianjiang_position.get(yuejiang, '')
        if yuejiang_tianjiang:
            positions['月将所乘天将'] = {
                '地支': yuejiang,
                '天将': yuejiang_tianjiang,
            }
    
    # 3. 从8008输出提取类象数据（只做1次）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞（只做1次）
    shensha_data = ke_data.get('shensha', {})
    
    # 5. 一次查询所有事类规则（避免N次数据库查询）
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类计算证数
    all_positions = POSITION_NAMES
    results = []
    
    for shilei_name, rules in all_rules.items():
        shilei_leixiang = rules['leixiang']
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
                dz_leixiang = pos_leixiang.get(dz_key, {})
                matched_items = {k: v * shilei_leixiang[k] for k, v in dz_leixiang.items() if k in shilei_leixiang}
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
                tj_leixiang = pos_leixiang.get(tj_key, {})
                matched_items = {k: v * shilei_leixiang[k] for k, v in tj_leixiang.items() if k in shilei_leixiang}
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
                lq_leixiang = pos_leixiang.get(lq_key, {})
                matched_items = {k: v * shilei_leixiang[k] for k, v in lq_leixiang.items() if k in shilei_leixiang}
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
                cs_leixiang = pos_leixiang.get(cs_key, {})
                matched_items = {k: v * shilei_leixiang[k] for k, v in cs_leixiang.items() if k in shilei_leixiang}
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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]  # 本命干支的地支部分
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
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
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
    if benming_1 and len(benming_1) >= 2:
        benming_dizhi = benming_1[1]
        benming_tianjiang = tianjiang_position.get(benming_dizhi, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命'] = {
            '地支': benming_dizhi,
            '天将': benming_tianjiang,
        }
    
    # 本命_2（女方）
    benming_2 = ke_data.get('benming_2')
    if benming_2 and len(benming_2) >= 2:
        benming_dizhi_2 = benming_2[1]
        benming_tianjiang_2 = tianjiang_position.get(benming_dizhi_2, '') if isinstance(tianjiang_position, dict) else ''
        positions['本命_2'] = {
            '地支': benming_dizhi_2,
            '天将': benming_tianjiang_2,
        }
    
    # 月将所乘天将
    yuejiang = ke_data.get('yuejiang', '')
    if yuejiang and isinstance(tianjiang_position, dict):
        yuejiang_tianjiang = tianjiang_position.get(yuejiang, '')
        if yuejiang_tianjiang:
            positions['月将所乘天将'] = {
                '地支': yuejiang,
                '天将': yuejiang_tianjiang,
            }
    
    # 3. 从8008输出提取类象数据（按符号分类）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞
    shensha_data = ke_data.get('shensha', {})
    
    # 5. 一次查询所有事类规则
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类，计算加权分数（含明细）
    all_positions = POSITION_NAMES
    shilei_results = {}
    
    for shilei_name, rules in all_rules.items():
        shilei_leixiang = rules['leixiang']
        shilei_leixiang_tj = rules.get('leixiang_tj', {})  # 天将专属权重
        shilei_shensha = rules['shensha']
        total_score = 0.0
        pos_scores = {}
        
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
            
            # 地支维度
            if dizhi:
                dz_key = f'DZ_{dizhi}'
                dz_leixiang = pos_leixiang.get(dz_key, {})
                # 找出匹配的类象及其权重（含事类关联权重）
                matched_items = []
                for lx_name, weight in dz_leixiang.items():
                    if lx_name in shilei_leixiang:
                        shilei_weight = shilei_leixiang[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                # 分离一维和多维
                one_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w == 1]
                multi_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w > 1]
                # 一维只取事类权重最大的1个，多维正常累加
                total_weight = 0.0
                if one_dim_items:
                    max_sw = max(sw for _, _, sw in one_dim_items)
                    total_weight += max_sw
                for _, w, sw in multi_dim_items:
                    total_weight += w * sw
                # 格式化输出
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight}")
                    elif weight > 1:
                        matched_str.append(f"{lx_name}×{weight}")
                    else:
                        matched_str.append(lx_name)
                dim_details['地支'] = {
                    'symbol': dizhi,
                    'matched': matched_str
                }
                dim_matches['地支'] = total_weight
            
            # 天将维度
            if tianjiang:
                tj_key = f'TJ_{tianjiang}'
                tj_leixiang = pos_leixiang.get(tj_key, {})
                matched_items = []
                for lx_name, weight in tj_leixiang.items():
                    # 优先查天将专属权重
                    if lx_name in shilei_leixiang_tj and tianjiang in shilei_leixiang_tj[lx_name]:
                        shilei_weight = shilei_leixiang_tj[lx_name][tianjiang]
                    elif lx_name in shilei_leixiang:
                        shilei_weight = shilei_leixiang[lx_name]
                    else:
                        continue  # 两个都没有，不匹配
                    matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w == 1]
                multi_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w > 1]
                total_weight = 0.0
                if one_dim_items:
                    max_sw = max(sw for _, _, sw in one_dim_items)
                    total_weight += max_sw
                for _, w, sw in multi_dim_items:
                    total_weight += w * sw
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight}")
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
                lq_leixiang = pos_leixiang.get(lq_key, {})
                matched_items = []
                for lx_name, weight in lq_leixiang.items():
                    if lx_name in shilei_leixiang:
                        shilei_weight = shilei_leixiang[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w == 1]
                multi_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w > 1]
                total_weight = 0.0
                if one_dim_items:
                    max_sw = max(sw for _, _, sw in one_dim_items)
                    total_weight += max_sw
                for _, w, sw in multi_dim_items:
                    total_weight += w * sw
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight}")
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
                lq_leixiang = pos_leixiang.get(lq_key, {})
                matched_items = []
                for lx_name, weight in lq_leixiang.items():
                    if lx_name in shilei_leixiang:
                        shilei_weight = shilei_leixiang[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w == 1]
                multi_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w > 1]
                total_weight = 0.0
                if one_dim_items:
                    max_sw = max(sw for _, _, sw in one_dim_items)
                    total_weight += max_sw
                for _, w, sw in multi_dim_items:
                    total_weight += w * sw
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight}")
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
                cs_leixiang = pos_leixiang.get(cs_key, {})
                matched_items = []
                for lx_name, weight in cs_leixiang.items():
                    if lx_name in shilei_leixiang:
                        shilei_weight = shilei_leixiang[lx_name]
                        matched_items.append((lx_name, weight, shilei_weight))
                one_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w == 1]
                multi_dim_items = [(lx, w, sw) for lx, w, sw in matched_items if w > 1]
                total_weight = 0.0
                if one_dim_items:
                    max_sw = max(sw for _, _, sw in one_dim_items)
                    total_weight += max_sw
                for _, w, sw in multi_dim_items:
                    total_weight += w * sw
                matched_str = []
                for lx_name, weight, shilei_weight in matched_items:
                    if shilei_weight != 1:
                        matched_str.append(f"{lx_name}×{shilei_weight}")
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
            shensha_weighted_score = sum(shilei_shensha[name] for name in shensha_intersection)
            
            # 应用神煞聚集效应：同一位置命中多个神煞时额外加分
            # 公式：基础分 × (1 + (命中数-1) × 聚集系数)
            juji_ratio = JUJI_CONFIG.get(shilei_name, 0.0)
            if len(shensha_intersection) > 1 and juji_ratio > 0:
                juji_multiplier = 1 + (len(shensha_intersection) - 1) * juji_ratio
                shensha_weighted_score *= juji_multiplier
            
            dim_details['神煞'] = {
                'matched': [f"{name}×{shilei_shensha[name]}" if shilei_shensha[name] != 1 else name for name in shensha_intersection]
            }
            dim_matches['神煞'] = shensha_weighted_score
            
            # 计算加权分数
            score_info = calculate_weighted_score(dim_matches, shilei_name=shilei_name)
            
            # 为每个维度添加得分
            for dim_name in dim_details:
                weight = WEIGHT_CONFIG.get(dim_name, 1.0)
                match_count = dim_matches.get(dim_name, 0)
                dim_details[dim_name]['score'] = round(match_count * weight, 2)
            
            if score_info['score'] > 0:
                pos_scores[pos_name] = {
                    'score': score_info['score'],
                    'base': score_info['base_score'],
                    'combo': score_info['combo_score'],
                    'dims': dim_details
                }
                total_score += score_info['score']
        
        if total_score > 0:
            shilei_results[shilei_name] = {
                'total_score': round(total_score, 2),
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

