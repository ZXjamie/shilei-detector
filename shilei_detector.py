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
WEIGHT_CONFIG = {
    '天将': 2.0,
    '地支': 2.0,
    '六亲': 3.0,
    '长生': 1.5,
    '神煞': 1.0
}

# 2维组合比例（从 Neo4j 加载）
# key: frozenset(['维度1', '维度2']), value: 比例
COMBINATION_RATIO = {}


def load_weight_config():
    """从 Neo4j 加载权重配置到内存"""
    global WEIGHT_CONFIG, COMBINATION_RATIO
    
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with driver.session() as session:
        # 加载维度权重
        result = session.run("""
            MATCH (w:权重配置 {类型: '维度'})
            RETURN w.name AS name, w.权重 AS weight
        """)
        for record in result:
            name = record['name']
            weight = record['weight']
            if name and weight is not None:
                WEIGHT_CONFIG[name] = float(weight)
        
        # 加载2维组合比例
        result = session.run("""
            MATCH (c:组合比例)
            RETURN c.组合 AS combo, c.比例 AS ratio
        """)
        for record in result:
            combo = record['combo']
            ratio = record['ratio']
            if combo and ratio is not None:
                # combo 是列表，如 ['天将', '地支']
                if isinstance(combo, list) and len(combo) == 2:
                    COMBINATION_RATIO[frozenset(combo)] = float(ratio)
    
    driver.close()
    print(f"[权重配置] 已加载: 维度权重={WEIGHT_CONFIG}, 2维组合={len(COMBINATION_RATIO)}个")


def get_combination_ratio(dims: set) -> float:
    """获取多维组合的比例
    
    2维：直接查表
    3维及以上：涉及的所有2维组合比例的平均值
    """
    dims_list = list(dims)
    if len(dims_list) < 2:
        return 0.0
    
    if len(dims_list) == 2:
        return COMBINATION_RATIO.get(frozenset(dims_list), 0.0)
    
    # 3维及以上：计算所有2维子组合的比例平均值
    ratios = []
    for combo in combinations(dims_list, 2):
        ratio = COMBINATION_RATIO.get(frozenset(combo), 0.0)
        ratios.append(ratio)
    
    return sum(ratios) / len(ratios) if ratios else 0.0


def calculate_weighted_score(dim_matches: dict) -> dict:
    """计算单个位置的加权分数
    
    Args:
        dim_matches: 各维度匹配数
            {'天将': 2, '地支': 0, '六亲': 1, '长生': 0, '神煞': 3}
    
    Returns:
        {
            'score': 15.5,  # 总分
            'base_score': 8.0,  # 维度基础分
            'combo_score': 7.5,  # 组合加分
            'matched_dims': ['天将', '六亲', '神煞']  # 命中的维度
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
        ratio = get_combination_ratio(set(matched_dims))
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


def calc_liuqin(ri_gan: str, dizhi: str) -> str:
    """计算地支相对于日干的六亲"""
    if not ri_gan or not dizhi:
        return ''
    wo = TIANGAN_WUXING.get(ri_gan, '')
    ta = DIZHI_WUXING.get(dizhi, '')
    if not wo or not ta:
        return ''
    if wo == ta:
        return '兄弟'
    elif WUXING_SHENG.get(wo) == ta:
        return '子孙'
    elif WUXING_KE.get(wo) == ta:
        return '妻财'
    elif WUXING_SHENG.get(ta) == wo:
        return '父母'
    elif WUXING_KE.get(ta) == wo:
        return '官鬼'
    return ''


def get_changsheng_position(ri_gan: str, dizhi: str) -> str:
    """计算地支相对于日干的十二长生状态
    
    用天干五行计算，不用寄宫
    甲乙木长生在亥，丙丁火长生在寅，戊己土长生在寅，庚辛金长生在巳，壬癸水长生在申
    """
    if not ri_gan or not dizhi:
        return ''
    
    # 天干五行
    wuxing = TIANGAN_WUXING.get(ri_gan, '')
    if not wuxing:
        return ''
    
    # 五行长生起始地支
    changsheng_start = {'木': '亥', '火': '寅', '土': '寅', '金': '巳', '水': '申'}
    start = changsheng_start.get(wuxing, '')
    if not start:
        return ''
    
    # 计算位置差
    dizhi_order = ['子', '丑', '寅', '卯', '辰', '巳', '午', '未', '申', '酉', '戌', '亥']
    start_idx = dizhi_order.index(start)
    dizhi_idx = dizhi_order.index(dizhi)
    diff = (dizhi_idx - start_idx) % 12
    
    return CHANGSHENG_ORDER[diff] if diff < len(CHANGSHENG_ORDER) else ''


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
                # 长生：上神地支在日干五行下的长生状态
                result[f'第{ke_num}课上神'] = {
                    '地支': shangshen,
                    '天将': tianjiang,
                    '六亲': calc_liuqin(ri_gan, shangshen),
                    '长生': get_changsheng_position(ri_gan, shangshen)
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
            
            # 六亲和长生自己算
            liuqin = calc_liuqin(ri_gan, dizhi)
            changsheng = get_changsheng_position(ri_gan, dizhi)
            
            result[chuan_name] = {
                '地支': dizhi,
                '天将': tianjiang,
                '六亲': liuqin,
                '长生': changsheng
            }
    return result


def extract_leixiang_from_8008(leixiang_data: dict) -> dict:
    """从8008输出提取类象，按符号分类
    
    输入格式: {'total_matched': N, 'by_category': {...}}
    
    Returns:
        {
            'DZ_子': ['荡妇', '桃花', ...],  # 地支子触发的类象name
            'TJ_贵人': ['领导', '东北', ...],  # 天将贵人触发的类象name
            'LQ_妻财': ['钱财', '妻子', ...],  # 六亲妻财触发的类象name
            'CS_长生': ['父亲', '学校', ...],  # 长生状态触发的类象name
        }
    
    提取逻辑：
    - 优先从 combination_key 提取 lx_xxx（天将/地支有值）
    - 没有则用 meaning（六亲/长生的 combination_key 为空）
    """
    result = {}
    if not leixiang_data or not leixiang_data.get('by_category'):
        return result
    
    by_category = leixiang_data['by_category']
    
    for cat_name, cat_data in by_category.items():
        items = cat_data.get('items', []) if isinstance(cat_data, dict) else cat_data
        for item in items:
            item_id = item.get('id', '')  # 如 DZ_子、TJ_贵人
            if not item_id:
                continue
            
            # 优先从 combination_key 提取 lx_xxx
            combo_key = item.get('combination_key', '')
            leixiang_name = None
            
            if combo_key:
                parts = combo_key.split('|')
                for part in parts:
                    if part.startswith('lx_'):
                        leixiang_name = part[3:]  # 去掉 'lx_' 前缀
                        break
            
            # 没有 combination_key 或没有 lx_ 前缀，则用 meaning
            if not leixiang_name:
                leixiang_name = item.get('meaning', '')
            
            if leixiang_name:
                if item_id not in result:
                    result[item_id] = set()
                result[item_id].add(leixiang_name)
    
    return result


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
    """从Neo4j查询事类关联的类象和神煞"""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with driver.session() as session:
        # 查询事类关联的类象
        leixiang_result = session.run("""
            MATCH (s:事类 {name: $name})-[:关联类象]->(l:Leixiang)
            RETURN l.name AS leixiang_name
        """, name=shilei_name)
        leixiang_set = {record['leixiang_name'] for record in leixiang_result}
        
        # 查询事类关联的神煞
        shensha_result = session.run("""
            MATCH (s:事类 {name: $name})-[:关联神煞]->(ss:Shensha)
            RETURN ss.name AS shensha_name
        """, name=shilei_name)
        shensha_set = {record['shensha_name'] for record in shensha_result}
    
    driver.close()
    
    return {
        'leixiang': leixiang_set,
        'shensha': shensha_set
    }


def fetch_all_shilei_rules() -> dict:
    """一次查询获取所有事类的规则
    
    Returns:
        {
            '财运': {'leixiang': set(...), 'shensha': set(...)},
            '感情': {'leixiang': set(...), 'shensha': set(...)},
            ...
        }
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    result = {}
    with driver.session() as session:
        # 一次查询所有事类及其关联的类象
        leixiang_result = session.run("""
            MATCH (s:事类)-[:关联类象]->(l:Leixiang)
            RETURN s.name AS shilei_name, l.name AS leixiang_name
        """)
        for record in leixiang_result:
            shilei_name = record['shilei_name']
            leixiang_name = record['leixiang_name']
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': set(), 'shensha': set()}
            result[shilei_name]['leixiang'].add(leixiang_name)
        
        # 一次查询所有事类及其关联的神煞
        shensha_result = session.run("""
            MATCH (s:事类)-[:关联神煞]->(ss:Shensha)
            RETURN s.name AS shilei_name, ss.name AS shensha_name
        """)
        for record in shensha_result:
            shilei_name = record['shilei_name']
            shensha_name = record['shensha_name']
            if shilei_name not in result:
                result[shilei_name] = {'leixiang': set(), 'shensha': set()}
            result[shilei_name]['shensha'].add(shensha_name)
    
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
    
    # 2. 解析六处位置
    positions = {}
    
    # 四课
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    
    # 三传
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
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
    
    all_positions = ['第1课上神', '第2课上神', '第3课上神', '第4课上神', '初传', '中传', '末传']
    
    for pos_name in all_positions:
        pos_info = positions.get(pos_name, {})
        if not pos_info:
            continue
        
        pos_zhengshu = 0
        dim_details = {}
        
        dizhi = pos_info.get('地支', '')
        tianjiang = pos_info.get('天将', '')
        liuqin = pos_info.get('六亲', '')
        changsheng = pos_info.get('长生', '')
        
        # 地支维度
        if dizhi:
            dz_key = f'DZ_{dizhi}'
            dz_leixiang = leixiang_by_symbol.get(dz_key, set())
            intersection = dz_leixiang & shilei_leixiang
            if intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['地支'] = {
                        'symbol': dizhi,
                        'matched': True,
                        'intersection_count': len(intersection),
                        'intersection_sample': list(intersection)[:3]
                    }
            else:
                if detail:
                    dim_details['地支'] = {'symbol': dizhi, 'matched': False}
        
        # 天将维度
        if tianjiang:
            tj_key = f'TJ_{tianjiang}'
            tj_leixiang = leixiang_by_symbol.get(tj_key, set())
            intersection = tj_leixiang & shilei_leixiang
            if intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['天将'] = {
                        'symbol': tianjiang,
                        'matched': True,
                        'intersection_count': len(intersection),
                        'intersection_sample': list(intersection)[:3]
                    }
            else:
                if detail:
                    dim_details['天将'] = {'symbol': tianjiang, 'matched': False}
        
        # 六亲维度
        if liuqin:
            lq_key = f'LQ_{liuqin}'
            lq_leixiang = leixiang_by_symbol.get(lq_key, set())
            intersection = lq_leixiang & shilei_leixiang
            if intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['六亲'] = {
                        'symbol': liuqin,
                        'matched': True,
                        'intersection_count': len(intersection),
                        'intersection_sample': list(intersection)[:3]
                    }
            else:
                if detail:
                    dim_details['六亲'] = {'symbol': liuqin, 'matched': False}
        
        # 长生维度
        if changsheng:
            cs_key = f'CS_{changsheng}'
            cs_leixiang = leixiang_by_symbol.get(cs_key, set())
            intersection = cs_leixiang & shilei_leixiang
            if intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['长生'] = {
                        'symbol': changsheng,
                        'matched': True,
                        'intersection_count': len(intersection),
                        'intersection_sample': list(intersection)[:3]
                    }
            else:
                if detail:
                    dim_details['长生'] = {'symbol': changsheng, 'matched': False}
        
        # 神煞维度
        pos_shensha = set()
        if dizhi and dizhi in shensha_data:
            shensha_list = shensha_data[dizhi]
            pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
        
        shensha_intersection = pos_shensha & shilei_shensha
        if shensha_intersection:
            pos_zhengshu += 1
            if detail:
                dim_details['神煞'] = {
                    'matched': True,
                    'intersection_count': len(shensha_intersection),
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
    
    # 2. 解析六处位置（只做1次）
    positions = {}
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
    # 3. 从8008输出提取类象数据（只做1次）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞（只做1次）
    shensha_data = ke_data.get('shensha', {})
    
    # 5. 一次查询所有事类规则（只查1次Neo4j）
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类计算证数
    all_positions = ['第1课上神', '第2课上神', '第3课上神', '第4课上神', '初传', '中传', '末传']
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
            liuqin = pos_info.get('六亲', '')
            changsheng = pos_info.get('长生', '')
            
            pos_zhengshu = 0
            dim_details = {} if detail else None
            
            # 地支维度
            if dizhi:
                dz_key = f'DZ_{dizhi}'
                dz_leixiang = leixiang_by_symbol.get(dz_key, set())
                intersection = dz_leixiang & shilei_leixiang
                if intersection:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['地支'] = {
                            'symbol': dizhi, 'matched': True,
                            'intersection_count': len(intersection),
                            'intersection_sample': list(intersection)[:3]
                        }
                elif detail:
                    dim_details['地支'] = {'symbol': dizhi, 'matched': False}
            
            # 天将维度
            if tianjiang:
                tj_key = f'TJ_{tianjiang}'
                tj_leixiang = leixiang_by_symbol.get(tj_key, set())
                intersection = tj_leixiang & shilei_leixiang
                if intersection:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['天将'] = {
                            'symbol': tianjiang, 'matched': True,
                            'intersection_count': len(intersection),
                            'intersection_sample': list(intersection)[:3]
                        }
                elif detail:
                    dim_details['天将'] = {'symbol': tianjiang, 'matched': False}
            
            # 六亲维度
            if liuqin:
                lq_key = f'LQ_{liuqin}'
                lq_leixiang = leixiang_by_symbol.get(lq_key, set())
                intersection = lq_leixiang & shilei_leixiang
                if intersection:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['六亲'] = {
                            'symbol': liuqin, 'matched': True,
                            'intersection_count': len(intersection),
                            'intersection_sample': list(intersection)[:3]
                        }
                elif detail:
                    dim_details['六亲'] = {'symbol': liuqin, 'matched': False}
            
            # 长生维度
            if changsheng:
                cs_key = f'CS_{changsheng}'
                cs_leixiang = leixiang_by_symbol.get(cs_key, set())
                intersection = cs_leixiang & shilei_leixiang
                if intersection:
                    pos_zhengshu += 1
                    if detail:
                        dim_details['长生'] = {
                            'symbol': changsheng, 'matched': True,
                            'intersection_count': len(intersection),
                            'intersection_sample': list(intersection)[:3]
                        }
                elif detail:
                    dim_details['长生'] = {'symbol': changsheng, 'matched': False}
            
            # 神煞维度
            pos_shensha = set()
            if dizhi and dizhi in shensha_data:
                shensha_list = shensha_data[dizhi]
                pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
            shensha_intersection = pos_shensha & shilei_shensha
            if shensha_intersection:
                pos_zhengshu += 1
                if detail:
                    dim_details['神煞'] = {
                        'matched': True,
                        'intersection_count': len(shensha_intersection),
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
    
    # 2. 解析七处位置
    positions = {}
    sike_positions = parse_sike_positions(ke_data.get('sike', ''), ri_gan, ri_zhi)
    positions.update(sike_positions)
    sanchuan_positions = parse_sanchuan_positions(ke_data.get('sanchuan', ''), ri_gan, ri_zhi)
    positions.update(sanchuan_positions)
    
    # 3. 从8008输出提取类象数据（按符号分类）
    leixiang_by_symbol = extract_leixiang_from_8008(ke_data.get('leixiang', {}))
    
    # 4. 提取神煞
    shensha_data = ke_data.get('shensha', {})
    
    # 5. 一次查询所有事类规则
    all_rules = fetch_all_shilei_rules()
    
    # 6. 对每个事类，计算加权分数（含明细）
    all_positions = ['第1课上神', '第2课上神', '第3课上神', '第4课上神', '初传', '中传', '末传']
    shilei_results = {}
    
    for shilei_name, rules in all_rules.items():
        shilei_leixiang = rules['leixiang']
        shilei_shensha = rules['shensha']
        total_score = 0.0
        pos_scores = {}
        
        for pos_name in all_positions:
            pos_info = positions.get(pos_name, {})
            if not pos_info:
                continue
            
            dizhi = pos_info.get('地支', '')
            tianjiang = pos_info.get('天将', '')
            liuqin = pos_info.get('六亲', '')
            changsheng = pos_info.get('长生', '')
            
            # 计算每个维度的匹配明细
            dim_details = {}
            dim_matches = {}  # 用于计算分数
            
            # 地支维度
            if dizhi:
                dz_key = f'DZ_{dizhi}'
                dz_leixiang = leixiang_by_symbol.get(dz_key, set())
                intersection = dz_leixiang & shilei_leixiang
                dim_details['地支'] = {
                    'symbol': dizhi,
                    'matched': list(intersection)
                }
                dim_matches['地支'] = len(intersection)
            
            # 天将维度
            if tianjiang:
                tj_key = f'TJ_{tianjiang}'
                tj_leixiang = leixiang_by_symbol.get(tj_key, set())
                intersection = tj_leixiang & shilei_leixiang
                dim_details['天将'] = {
                    'symbol': tianjiang,
                    'matched': list(intersection)
                }
                dim_matches['天将'] = len(intersection)
            
            # 六亲维度
            if liuqin:
                lq_key = f'LQ_{liuqin}'
                lq_leixiang = leixiang_by_symbol.get(lq_key, set())
                intersection = lq_leixiang & shilei_leixiang
                dim_details['六亲'] = {
                    'symbol': liuqin,
                    'matched': list(intersection)
                }
                dim_matches['六亲'] = len(intersection)
            
            # 长生维度
            if changsheng:
                cs_key = f'CS_{changsheng}'
                cs_leixiang = leixiang_by_symbol.get(cs_key, set())
                intersection = cs_leixiang & shilei_leixiang
                dim_details['长生'] = {
                    'symbol': changsheng,
                    'matched': list(intersection)
                }
                dim_matches['长生'] = len(intersection)
            
            # 神煞维度
            pos_shensha = set()
            if dizhi and dizhi in shensha_data:
                shensha_list = shensha_data[dizhi]
                pos_shensha = {s.get('name', '') for s in shensha_list if isinstance(s, dict)}
            shensha_intersection = pos_shensha & shilei_shensha
            dim_details['神煞'] = {
                'matched': list(shensha_intersection)
            }
            dim_matches['神煞'] = len(shensha_intersection)
            
            # 计算加权分数
            score_info = calculate_weighted_score(dim_matches)
            
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

