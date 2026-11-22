#!/usr/bin/env python3
"""生成 kuro-map-helper 的「示例点位数据集 + 探索路线数据」（可复现）。

用法：
    cd /data/kuro-map-helper && python3 tools/gen_sample_data.py

输出：
    src/kumap/resources/data/points.json
    src/kumap/resources/data/routes.json

口径说明（重要）：
    * source 固定为 "sample"。坐标是示例底图（1536x1024）上的归一化坐标 0~1，
      不是库街区大地图的真实坐标，仅用于演示界面渲染 / 标记同步 / 顺序导航。
    * 素材、声骸、道具等名词取自游戏公开资料用语，版权归权利方所有；
      点位备注是玩家口吻的示例短句，不代表真实刷新位置。
    * 固定随机种子 SEED，重复运行输出一致。
    * 运行时用 assert 卡住数据契约（坐标、区域包围盒、点间距、路线引用与步距），
      末尾打印统计。
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

SEED = 20261121

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "src" / "kumap" / "resources" / "data"
POINTS_PATH = DATA_DIR / "points.json"
ROUTES_PATH = DATA_DIR / "routes.json"

MAP_W, MAP_H = 1536, 1024

MIN_SEP = 0.004          # 契约：任意两点欧氏距离必须 > 0.004
SEP_TARGET = 0.0075      # 生成时的安全余量
MAX_STEP = 0.16          # 契约：路线相邻步归一化距离 < 0.16
STEP_TARGET = 0.125      # 普通候选的抓取半径（留安全余量）
PRIMARY_TARGET = 0.145   # 主题层（路线主打层）允许更大的抓取半径
MAX_BYTES = 300 * 1024   # 契约：单文件体积控制在 300KB 以内

LAYERS = [
    {"key": "chest", "label": "宝箱", "color": "#f2b134"},
    {"key": "anchor", "label": "锚点", "color": "#3ec6c0"},
    {"key": "material", "label": "材料", "color": "#66c46a"},
    {"key": "echo", "label": "声骸", "color": "#b07de0"},
    {"key": "puzzle", "label": "解谜", "color": "#5b9df9"},
    {"key": "quest", "label": "任务", "color": "#f2705b"},
]
LAYER_KEYS = [l["key"] for l in LAYERS]
LAYER_LABEL = {l["key"]: l["label"] for l in LAYERS}

# 各层目标数量（合计 340，落在 320~380 区间）
LAYER_COUNTS = {
    "chest": 118,
    "anchor": 27,
    "material": 95,
    "echo": 47,
    "puzzle": 26,
    "quest": 23,
}

# 8 个区域，包围盒无缝铺满整张示例底图（归一化坐标）
REGIONS = [
    {
        "id": "r1", "name": "荒石坡", "bbox": {"x0": 0.00, "y0": 0.00, "x1": 0.24, "y1": 0.52},
        "spots": ["北崖", "碎石河湾", "东口台地", "风蚀柱群", "旧矿洞口", "南坡裂谷", "哨塔残基",
                  "白桦洼地", "断桥下", "深砂坑", "岩拱阴影", "溪石滩", "废弃营寨", "西岭风口",
                  "乱石阵", "高台边缘"],
        "mats": ["植珀", "红针晶簇", "紫晶簇", "辉萤石", "珍珠草", "清芬草", "香苏", "菱果",
                 "花蕈", "黑纹蛙", "金环蜓", "鸟蛋"],
    },
    {
        "id": "r2", "name": "中曲台地", "bbox": {"x0": 0.24, "y0": 0.00, "x1": 0.47, "y1": 0.52},
        "spots": ["中枢广场", "回旋坡道", "东缘断崖", "苔痕阶地", "水渠旧址", "南阶台", "石圈遗迹",
                  "长风口", "曲水湾", "梯田埂", "瞭望柱", "砾石滩", "旧井台", "废祠后院",
                  "草垛坡", "双石柱"],
        "mats": ["清芬草", "香柠草", "夜息香", "地丁堇", "崖仙子", "龙衔珠", "云露", "蓝棘蜥",
                 "银环蜥", "叶翅蛉", "红针晶簇", "龙萤石"],
    },
    {
        "id": "r3", "name": "虎口山脉", "bbox": {"x0": 0.47, "y0": 0.00, "x1": 0.73, "y1": 0.52},
        "spots": ["北隘口", "断层栈道", "悬棺岩", "西岭矿脉", "雪线台地", "鹰嘴崖", "深涧索道",
                  "碎石长坡", "旧炮台", "阳面草坝", "阴面冰穴", "石阶入口", "云杉坳", "鹰巢柱",
                  "山口风门", "废弃工棚"],
        "mats": ["紫晶簇", "植珀", "辉萤石", "银雪莲", "龙萤石", "云母釉岩", "蚀夜幽兰",
                 "暂星", "兽肉", "鸟蛋", "隐火蜕", "晶螯蝎"],
    },
    {
        "id": "r4", "name": "残息海岸", "bbox": {"x0": 0.73, "y0": 0.00, "x1": 1.00, "y1": 0.52},
        "spots": ["退潮礁盘", "沉船骨架", "南沙嘴", "灯塔断基", "海蚀拱门", "盐渍洼地", "潮汐池群",
                  "黑礁石", "东栈桥", "搁浅渔舟", "贝丘", "海雾崖脚", "卵石滩", "旧码头",
                  "碎珊瑚滩", "咸风洞"],
        "mats": ["紫珊瑚", "锦色贝", "月藻", "水灯花", "盐海珀", "雨声蜗", "扁竹花",
                 "铛铛蟹", "鸢尾花", "垂青橄榄", "莲实", "盾金龟"],
    },
    {
        "id": "r5", "name": "青栎庭院", "bbox": {"x0": 0.00, "y0": 0.52, "x1": 0.24, "y1": 1.00},
        "spots": ["西门庭柱", "南圃花畦", "旧喷泉水池", "栎树林道", "碑园长阶", "东厢废墟",
                  "地窖入口", "藤蔓井台", "苔石小径", "落叶坑", "石栏桥", "果园北埂", "塔基阴影",
                  "鹿苑栅栏", "水井广场", "断墙画壁"],
        "mats": ["木莲", "地涌莲", "灯笼果", "金阳凤", "妙弋花", "云芝", "金铃子", "鸳鸯花",
                 "伞下客", "雀翎果", "蓝羽蝶", "禽肉"],
    },
    {
        "id": "r6", "name": "乘霄山", "bbox": {"x0": 0.24, "y0": 0.52, "x1": 0.47, "y1": 1.00},
        "spots": ["山门石阶", "云海平台", "北峰风口", "冰舌末端", "古松盘道", "药圃遗址",
                  "雪崩堆积", "南崖石窟", "祭祀台基", "雪线路标", "山顶寒潭", "登山绳架",
                  "银莲坡", "观星台", "石灯甬道", "雾中回廊"],
        "mats": ["银雪莲", "蚀夜幽兰", "傲寒钟", "龙吐珠", "伪贝母", "珍珠草", "香柠草",
                 "紫晶簇", "伏霜虫", "见月花", "石蔷薇", "鸟蛋"],
    },
    {
        "id": "r7", "name": "回音峡", "bbox": {"x0": 0.47, "y0": 0.52, "x1": 0.73, "y1": 1.00},
        "spots": ["峡口回声壁", "吊桥墩", "东岸石阶", "共鸣石柱", "深潭栈桥", "风铃崖",
                  "谷底暗河", "层叠岩台", "悬巢梯", "断琴壁", "钟乳洞", "回音观测台",
                  "峡腰小径", "乱石滩", "影下石屋", "旧渡口"],
        "mats": ["辉萤石", "龙萤石", "夜息香", "香苏", "地丁堇", "花蕈", "青竹蜥", "金环蜓",
                 "云露", "日冕菊", "愚惑金", "植珀"],
    },
    {
        "id": "r8", "name": "今州城郊", "bbox": {"x0": 0.73, "y0": 0.52, "x1": 1.00, "y1": 1.00},
        "spots": ["城东官道", "桥头集市", "南关闸口", "旧运河湾", "田字坊墙", "芦苇渡",
                  "城隍小庙", "砖窑旧址", "西陵墓道", "水车广场", "酒肆后巷", "驿站长亭",
                  "城南菜圃", "陶片坡", "城根洞门", "望楼基座"],
        "mats": ["珍珠草", "清芬草", "香柠草", "金铃子", "菱果", "栎木果", "桂叶",
                 "云芝", "青枝果", "盾金龟", "叶翅蛉", "红针晶簇"],
    },
]

# 每层在各区域的相对密度（自然聚集，不做规则网格）
LAYER_REGION_WEIGHTS = {
    "chest":    [1.35, 1.10, 0.85, 1.20, 1.00, 0.80, 1.05, 0.95],
    "anchor":   [1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00],
    "material": [0.80, 1.05, 1.15, 1.05, 1.30, 1.25, 0.95, 0.85],
    "echo":     [1.05, 1.05, 1.25, 0.90, 0.85, 1.10, 1.10, 0.90],
    "puzzle":   [0.90, 1.25, 1.00, 1.00, 1.10, 1.05, 1.10, 0.95],
    "quest":    [1.00, 1.00, 0.85, 1.10, 0.95, 1.05, 1.20, 1.30],
}

# ── 公开名词库（示例用途，版权归权利方） ──────────────────────────────────
COMMON_CURRENCY = ["贝币", "浮金波纹", "联能纹章", "凝漆纹章", "高频声波讯号", "中频声波讯号",
                   "低频声波讯号", "营养块", "声匣"]

ECHOS_BY_COST = {
    4: ["飞廉之猩", "无冠者", "哀声鸷", "云闪之鳞", "朔雷之鳞", "鸣钟之龟", "辉萤军势",
        "聚械机偶", "无常凶鹭", "无妄者", "罗蕾莱", "叹息古龙", "赫卡忒", "荣耀狮像",
        "车刃镰", "无归的谬误", "游鳞机枢"],
    3: ["紫羽鹭", "青羽鹭", "振铎乐师", "奏谕乐师", "暗鬃狼", "坚岩斗士", "冥渊守卫",
        "磐石守卫", "绿熔蜥", "刺玫菇", "箭簇熊", "戏猿", "巡哨机傀", "踏光兽",
        "琉璃刀伶", "巨布偶", "飓力熊", "雪鬃狼", "霜鬃狼", "雷鬃狼"],
    1: ["呜咔咔", "咔嚓嚓", "阿嗞嗞", "呼咻咻", "咕咕河豚", "啾啾河豚", "遁地鼠", "碎獠猪",
        "火鬃狼", "晶螯蝎", "游弋蝶", "寒霜陆龟", "通行灯偶", "冷凝棱镜", "热熔棱镜",
        "衍射棱镜", "湮灭棱镜", "惊蛰猎手", "破霜猎手", "巡徊猎手", "鸣泣战士", "审判战士",
        "先锋幼岩", "裂变幼岩", "侏侏鸵", "叮咚咚", "融火虫"],
}

PUZZLE_KINDS = ["共鸣方碑", "四色灯偶", "石序谜题", "三重冠塔", "溢彩画", "音乐飞萤",
                "观景点", "藏宝图残页", "隙声蝶群", "飞猎手挑战", "无音区封印", "机关转盘",
                "风铃共鸣阵", "光影拼图", "石柱校准", "潮汐刻度盘"]

QUEST_KINDS = ["纪闻任务", "伴星任务", "道引任务", "危行任务", "委托告示", "残卷收集",
               "报告递交", "巡礼之梦", "飞檐同好会", "日志文献", "全息战略", "声匣寻访"]

ANCHOR_KINDS = ["中枢信标", "小型信标", "临时锚点", "废弃信标", "山口信标", "海岸信标",
                "谷地信标", "城根信标"]

LAYER_KINDS = {"anchor": ANCHOR_KINDS, "puzzle": PUZZLE_KINDS, "quest": QUEST_KINDS}

# ── 玩家口吻备注：主题内成对拼接，保证唯一且长度 15~35 字 ──────────────────
NOTE_GROUPS = {
    "terrain": {
        "A": ["沿河岸向北徒步可达", "跳下断崖落地即是", "踩着倒塌的石柱能爬上去",
              "洞口被藤蔓遮住一半", "要顺崖壁小径慢慢挪", "从水底游上去可以抄近路",
              "高处有个风门跳下去就到", "地面塌陷处藏着个暗坑", "得先绕过一片乱石堆",
              "藏在倒塌雕像的背后", "从背面绕过去没人守", "要爬到最高那棵树梢才看得见"],
        "B": ["落点有伤害别硬跳", "注意脚下打滑", "附近有高点可以开视野", "小心崖边的风蚀结构",
              "旁边就是可交互的谜题", "地形复杂建议慢走", "回程记得留一点耐力",
              "顺路能把侧洞也补掉", "附近没有锚点悠着点", "雨天崖壁更滑",
              "从这里可以俯瞰半个区域", "跳下去之前先看看体力"],
    },
    "combat": {
        "A": ["先清掉旁边的残象再动手", "夜里会被残象包围", "要等巡逻的机傀走开",
              "附近有个残象巢点", "精英怪守在路口", "刷新一波后才有反应",
              "建议组队来处理更稳", "打完会有短暂共鸣反应", "声骸就压在怪堆中间",
              "靠近时会被锁定", "附近有飞猎手在活动", "清完立刻撤离别恋战"],
        "B": ["掉落比较肥值得绕", "记得留爆发收尾", "别硬拼绕开也行", "带够复苏剂再来",
              "适合顺手刷一下声骸", "附近没有补给点", "打完可以直接传下一段",
              "小心它召唤的小怪", "这条线连着三个点位", "对单共鸣者不太友好",
              "建议用闪避反击磨", "旁边有掩体可以回耐力"],
    },
    "gather": {
        "A": ["成片长在向阳的坡面", "潮水退去后才能靠近", "跟着采集指引走不会漏",
              "就在浅水里泡着", "一丛挨着一丛很密集", "刷新点其实在另一侧山坡",
              "和相邻的采集点连成片", "被碎石埋掉了一角", "岩缝里还藏着几株",
              "顺路就能捡到", "盐渍地上只长了这一点", "夜里发光更容易找"],
        "B": ["附近还散落着珍珠草", "可以顺路补一下采集", "清完这条线还剩不少体力",
              "建议标记后一次性清空", "别错过崖壁上的采集物", "顺手刷一下采集图鉴",
              "旁边有清水可以补药材", "每天刷新值得绕一趟", "和突破材料伴生",
              "采完记得回信标交料", "这一片密度还不错", "对强迫症很友好"],
    },
    "time": {
        "A": ["夜里刷新白天只留空箱", "整点后才会重新出现", "要等潮位变化",
              "跟着任务时间线推进", "限时窗口很短", "错过要等下一轮",
              "先做过桥动画再找", "版本活动期才在", "需要前置解谜才会浮出来",
              "开关只在黄昏触发", "雨天才有回声", "这个点位分两段刷新"],
        "B": ["不急的话可以放着", "建议白天来更好找", "记得到附近交任务",
              "错开时段再回来", "顺路看一眼就好", "别把它排进收尾段",
              "示例注记不代表真实刷新", "配合锚点传送更省事", "适合卡在两步之间",
              "错过也不影响清空", "最好在第二轮路过", "先记状态回头补"],
    },
    "misc": {
        "A": ["从信标直线冲刺约二十步", "骑乘声骸冲刺一段更好到达", "顺着风声走就不会迷路",
              "入口在瀑布后面的缝隙里", "标记在旧地图上略有偏差", "需要二段跳才够高度",
              "从屋顶一路滑过去最快", "先开视野再决定路线", "藏在一个很不起眼的凹口",
              "沿着暗河走到底", "石阶数到第七级右转", "从小地图边缘绕进去"],
        "B": ["适合当作环线的一个节点", "可以搭着任务一起做", "建议连着相邻点位一起清",
              "有点偏但值得过去", "可当作收尾的一站", "顺路即可不必专门跑",
              "记得在本地标记一下", "清完直接接下一段", "位置比较直观",
              "示例数据仅作演示", "附近可以补一下口粮", "和任务线正好同向"],
    },
}

LAYER_NOTE_THEME = {
    "chest": ["terrain", "misc", "gather"],
    "anchor": ["misc", "terrain", "time"],
    "material": ["gather", "terrain", "misc"],
    "echo": ["combat", "gather", "misc"],
    "puzzle": ["misc", "time", "terrain"],
    "quest": ["time", "misc", "combat"],
}

ROUTE_SPECS = [
    {"id": "route_r1_chest_sweep", "name": "荒石坡全清·宝箱一轮", "author": "漂泊者示例",
     "description": "从中枢信标出发，清完北崖与碎石河湾一带宝箱，末段顺手采集植珀与珍珠草。",
     "tags": ["开荒", "宝箱"], "regions": ["r1", "r2"], "prefer": ["chest", "material"],
     "steps": 24, "minutes": 52},
    {"id": "route_pearl_grass_loop", "name": "珍珠草·清芬草采集环线", "author": "采药的秧秧",
     "description": "以采集为主的两区环线，单日一轮可囤够凉茶与合成材料，末段回锚点交料。",
     "tags": ["采集", "日常"], "regions": ["r2", "r7"], "prefer": ["material", "chest"],
     "steps": 26, "minutes": 40},
    {"id": "route_echo_hunting", "name": "声骸巡回·残象巢点串", "author": "声骸收藏癖",
     "description": "沿虎口山脉与回音峡串联野外声骸，先打高 COST，漏掉的箱子随手补。",
     "tags": ["声骸", "战斗"], "regions": ["r3", "r7"], "prefer": ["echo", "chest"],
     "steps": 22, "minutes": 65},
    {"id": "route_puzzle_chain", "name": "解谜串联·方碑与灯偶", "author": "强迫症清图办",
     "description": "专治遗留解谜：共鸣方碑、四色灯偶一轮走完，个别点位需要等刷新。",
     "tags": ["解谜", "清空"], "regions": ["r5", "r6", "r7"], "prefer": ["puzzle", "chest"],
     "steps": 16, "minutes": 48},
    {"id": "route_quest_line", "name": "纪闻任务推进线", "author": "跟着剧情走",
     "description": "以任务点为节点串起今州城郊与残息海岸，任务点位稀疏，沿途顺手补解谜与宝箱。",
     "tags": ["任务", "剧情"], "regions": ["r8", "r4"], "prefer": ["quest", "puzzle"],
     "steps": 12, "minutes": 70},
    {"id": "route_anchor_express", "name": "信标快速点亮线", "author": "开图快递小哥",
     "description": "把青栎庭院与乘霄山的信标串成一条点亮线，中间用宝箱点当跳板省体力。",
     "tags": ["开荒", "锚点"], "regions": ["r5", "r6"], "prefer": ["anchor", "chest"],
     "steps": 15, "minutes": 35},
    {"id": "route_coast_chest", "name": "残息海岸·礁盘宝箱清空", "author": "海雾里的漂泊者",
     "description": "退潮时段走完礁盘与沉船骨架，采集段集中在紫珊瑚与锦色贝的富集带。",
     "tags": ["宝箱", "采集"], "regions": ["r4"], "prefer": ["chest", "material"],
     "steps": 20, "minutes": 44},
    {"id": "route_mountain_mixed", "name": "乘霄山混合全清·银雪莲与声骸", "author": "雪山常驻",
     "description": "银雪莲采集、声骸巢点与宝箱混排的一条长线，建议分两轮跟走并留返程体力。",
     "tags": ["综合", "雪山"], "regions": ["r6", "r7", "r3"],
     "prefer": ["material", "echo", "chest"], "steps": 28, "minutes": 96},
]

LAYER_STEP_LABELS = {
    "anchor": ["点亮信标", "确认传送点", "解锁锚点"],
    "chest": ["落地清点", "开奇藏箱", "补漏箱体", "清点物资箱"],
    "material": ["顺路采集", "补一波药材", "采集矿石", "收一遍采集点"],
    "echo": ["清理声骸巢", "捕捉声骸", "打完残象取声骸"],
    "puzzle": ["解谜取箱", "校准机关", "触发谜题"],
    "quest": ["推进任务", "交接告示", "跟完剧情"],
}
STEP_NOTES = ["向东北贴崖走", "用冲刺拉近距离", "可跳过但别漏", "等刷新再来", "必拿",
              "注意落石", "顺路即可", "留够体力", "走下层的石阶", "先开视野", "沿水边",
              "绕到背面更近", "别拖到夜里", "从这里跳水更快", "接锚点收尾", "带够复苏剂"]


# ── 基础工具 ─────────────────────────────────────────────────────────────
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def dedup_keep_order(items):
    out = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def largest_remainder(total, weights):
    """把 total 按 weights 分配成整数（最大余数法）。"""
    s = float(sum(weights))
    raw = [total * w / s for w in weights]
    base = [int(math.floor(v)) for v in raw]
    order = sorted(range(len(weights)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in order[: total - sum(base)]:
        base[i] += 1
    return base


class Occupancy:
    """栅格索引：判定候选点是否与已有点保持足够间距。"""

    CELL = 0.02

    def __init__(self):
        self.cells = {}

    def free(self, p, min_d):
        gx, gy = int(p[0] / self.CELL), int(p[1] / self.CELL)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in self.cells.get((gx + dx, gy + dy), ()):
                    if dist(p, q) < min_d:
                        return False
        return True

    def add(self, p):
        self.cells.setdefault((int(p[0] / self.CELL), int(p[1] / self.CELL)), []).append(p)


def build_structures(rng, region):
    """每个区域：若干聚集热点 + 沿边界蜿蜒的探索串（模拟玩家推进路径）。"""
    b = region["bbox"]
    x0, y0, x1, y1 = b["x0"], b["y0"], b["x1"], b["y1"]
    w, h = x1 - x0, y1 - y0
    inset = 0.010

    clusters = []
    for _ in range(rng.randint(4, 6)):
        cx = rng.uniform(x0 + inset + 0.010, x1 - inset - 0.010)
        cy = rng.uniform(y0 + inset + 0.010, y1 - inset - 0.010)
        clusters.append((cx, cy, rng.uniform(0.012, 0.030)))

    def edge_point(edge, t, pad):
        if edge == 0:
            return (x0 + pad + t * (w - 2 * pad), y0 + pad)
        if edge == 1:
            return (x0 + pad + t * (w - 2 * pad), y1 - pad)
        if edge == 2:
            return (x0 + pad, y0 + pad + t * (h - 2 * pad))
        return (x1 - pad, y0 + pad + t * (h - 2 * pad))

    chains = []
    for _ in range(rng.randint(2, 3)):
        e1, e2 = rng.sample(range(4), 2)
        start = edge_point(e1, rng.uniform(0.0, 0.4), inset + rng.uniform(0.0, 0.030))
        end = edge_point(e2, rng.uniform(0.6, 1.0), inset + rng.uniform(0.0, 0.030))
        poly = [start]
        for k in (1 / 3.0, 2 / 3.0):
            px = start[0] + (end[0] - start[0]) * k
            py = start[1] + (end[1] - start[1]) * k
            dx, dy = end[0] - start[0], end[1] - start[1]
            norm = math.hypot(dx, dy) or 1.0
            bow = rng.uniform(-0.055, 0.055)
            poly.append((clamp(px - dy / norm * bow, x0 + inset, x1 - inset),
                         clamp(py + dx / norm * bow, y0 + inset, y1 - inset)))
        poly.append(end)
        segs = [dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1)]
        if min(segs) > 0.02:
            chains.append(poly)

    return {"clusters": clusters, "chains": chains}


def sample_pos(rng, struct, bbox):
    """从热点或探索串采样一个候选坐标（尚未做间距检查）。"""
    if struct["chains"] and rng.random() < 0.30:
        poly = rng.choice(struct["chains"])
        segs = [dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1)]
        u = rng.random() * sum(segs)
        acc, idx = 0.0, 0
        for i, s in enumerate(segs):
            if acc + s >= u:
                idx = i
                break
            acc += s
        t = (u - acc) / segs[idx] if segs[idx] else 0.0
        px = poly[idx][0] + (poly[idx + 1][0] - poly[idx][0]) * t + rng.gauss(0.0, 0.008)
        py = poly[idx][1] + (poly[idx + 1][1] - poly[idx][1]) * t + rng.gauss(0.0, 0.008)
    else:
        cx, cy, sigma = rng.choice(struct["clusters"])
        px = cx + rng.gauss(0.0, sigma)
        py = cy + rng.gauss(0.0, sigma)
    return (round(clamp(px, bbox["x0"] + 0.005, bbox["x1"] - 0.005), 4),
            round(clamp(py, bbox["y0"] + 0.005, bbox["y1"] - 0.005), 4))


def make_note_picker(rng):
    """返回 take(themes)：按主题给出全局唯一、长度合规的玩家口吻备注。"""
    pools = {t: [] for t in NOTE_GROUPS}
    for theme, pair in NOTE_GROUPS.items():
        for a in pair["A"]:
            for b in pair["B"]:
                note = "{}，{}。".format(a, b)
                if 15 <= len(note) <= 35:
                    pools[theme].append(note)
    for lst in pools.values():
        rng.shuffle(lst)
    cursor = {t: 0 for t in pools}
    used = set()

    def drain(order):
        for theme in order:
            lst = pools[theme]
            while cursor[theme] < len(lst):
                cand = lst[cursor[theme]]
                cursor[theme] += 1
                if cand not in used:
                    used.add(cand)
                    return cand
        return None

    def take(themes):
        order = list(themes)
        rng.shuffle(order)
        return drain(order) or drain(list(pools))

    return take


def build_name(rng, region, layer, used_names):
    """形如「宝箱 · 荒石坡北崖」「解谜 · 荒石坡共鸣方碑（北崖）」的唯一名称。"""
    kinds = LAYER_KINDS.get(layer)
    for cycle in range(12):
        if kinds:
            token = rng.choice(kinds)
            if rng.random() < 0.70:
                token = "{}（{}）".format(token, rng.choice(region["spots"]))
        else:
            token = rng.choice(region["spots"])
        suffix = "" if cycle == 0 else "·{}".format("贰叁肆伍陆柒捌玖"[cycle - 1])
        name = "{} · {}{}{}".format(LAYER_LABEL[layer], region["name"], token, suffix)
        if name not in used_names:
            used_names.add(name)
            return name
    raise AssertionError("名称组合空间耗尽: {}".format(region["id"]))


def make_drops(rng, layer, region):
    """2~4 个公开素材 / 声骸 / 道具名；声骸层顺带返回 COST。"""
    cost = None
    if layer == "material":
        picks = rng.sample(region["mats"], rng.choice([2, 3, 3]))
        if rng.random() < 0.6:
            picks.append("贝币")
    elif layer == "echo":
        cost = rng.choice([1, 3, 4])
        picks = [rng.choice(ECHOS_BY_COST[cost])]
        if rng.random() < 0.45:
            picks.append(rng.choice(ECHOS_BY_COST[cost]))
        picks.append(rng.choice(["贝币", "高频声波讯号", "联能纹章", "浮金波纹"]))
    elif layer == "chest":
        picks = rng.sample(COMMON_CURRENCY, rng.choice([2, 3]))
        if rng.random() < 0.55:
            picks.append(rng.choice(region["mats"]))
    elif layer == "anchor":
        picks = ["贝币", "营养块", "低频声波讯号"]
    elif layer == "puzzle":
        picks = rng.sample(["贝币", "声匣", "高频声波讯号", "浮金波纹", "营养块"], 3)
        if rng.random() < 0.5:
            picks.append(rng.choice(region["mats"]))
    else:  # quest
        picks = ["声匣", rng.choice(["贝币", "联能纹章", "凝漆纹章"])]
        if rng.random() < 0.5:
            picks.append("中频声波讯号")
        if rng.random() < 0.4:
            picks.append(rng.choice(region["mats"]))
    picks = dedup_keep_order(picks)
    while len(picks) < 2:
        picks.append(rng.choice(COMMON_CURRENCY))
    return dedup_keep_order(picks)[:4], cost


def generate_points(rng):
    structs = {r["id"]: build_structures(rng, r) for r in REGIONS}
    occ = Occupancy()
    take_note = make_note_picker(rng)
    used_names = set()
    raw = []

    for layer in LAYER_KEYS:
        counts = largest_remainder(LAYER_COUNTS[layer], LAYER_REGION_WEIGHTS[layer])
        for region, cnt in zip(REGIONS, counts):
            bbox = region["bbox"]
            for _ in range(cnt):
                pos = None
                for _ in range(60):
                    cand = sample_pos(rng, structs[region["id"]], bbox)
                    if occ.free(cand, SEP_TARGET):
                        pos = cand
                        break
                if pos is None:  # 兜底：区域内纯随机
                    for _ in range(300):
                        cand = (round(rng.uniform(bbox["x0"] + 0.006, bbox["x1"] - 0.006), 4),
                                round(rng.uniform(bbox["y0"] + 0.006, bbox["y1"] - 0.006), 4))
                        if occ.free(cand, SEP_TARGET):
                            pos = cand
                            break
                if pos is None:
                    continue
                occ.add(pos)
                drops, cost = make_drops(rng, layer, region)
                if layer == "echo":
                    level = cost
                elif layer == "material":
                    level = rng.choice([1, 1, 2, 2, 3])
                else:
                    level = None
                raw.append({
                    "layer": layer,
                    "name": build_name(rng, region, layer, used_names),
                    "x": pos[0],
                    "y": pos[1],
                    "region": region["id"],
                    "level": level,
                    "drops": drops,
                    "note": take_note(LAYER_NOTE_THEME[layer]),
                    "author": "库街区示例数据",
                    "source": "sample",
                })

    raw.sort(key=lambda p: (p["layer"], p["region"], p["x"], p["y"]))
    seq, points = {}, []
    for p in raw:
        seq[p["layer"]] = seq.get(p["layer"], 0) + 1
        p["id"] = "{}_{:04d}".format(p["layer"], seq[p["layer"]])
        points.append({"id": p["id"], "layer": p["layer"], "name": p["name"],
                       "x": p["x"], "y": p["y"], "region": p["region"], "level": p["level"],
                       "drops": p["drops"], "note": p["note"], "author": p["author"],
                       "source": p["source"]})
    return points


def walk_route(rng, spec, points):
    """贪心 nearest-neighbor 游走：优先声明层，其次次要层，并逐步拉向未覆盖区域。

    约束：相邻步归一化距离 <= STEP_TARGET（< 契约 0.16）。
    """
    region_ids = set(spec["regions"])
    pool = [p for p in points if p["region"] in region_ids]
    anchors = [p for p in pool if p["layer"] == "anchor"]
    assert anchors, "区域 {} 内没有锚点".format(sorted(region_ids))
    target = spec["steps"]
    best = None
    for _ in range(300):
        start = rng.choice(anchors)
        path = [start]
        used = {start["id"]}
        touched = {start["region"]}
        while len(path) < target:
            last = (path[-1]["x"], path[-1]["y"])
            near = [p for p in pool if p["id"] not in used
                    and dist(last, (p["x"], p["y"])) <= STEP_TARGET]
            tiers = []
            for layer in spec["prefer"]:  # 主题层允许更大的抓取半径
                tiers.append([p for p in pool if p["id"] not in used and p["layer"] == layer
                              and dist(last, (p["x"], p["y"])) <= PRIMARY_TARGET])
            tiers.append(near)
            tier = next((t for t in tiers if t), [])
            if not tier:
                break
            if rng.random() > 0.88:  # 少量混搭，避免整条线过于单调
                tier = near or tier
            missing = region_ids - touched
            pull = 1.6 + 2.8 * (len(path) / target)  # 越到后段越想去补齐未覆盖区域
            weights = []
            for p in tier:
                d = dist(last, (p["x"], p["y"]))
                bonus = 1.0 if p["layer"] in spec["prefer"] else 0.55
                if missing and p["region"] in missing:
                    bonus *= pull
                if len(path) >= 2:  # 倾向顺向前进，减少原地折返
                    prev = (path[-2]["x"], path[-2]["y"])
                    vx, vy = last[0] - prev[0], last[1] - prev[1]
                    nx, ny = p["x"] - last[0], p["y"] - last[1]
                    align = (nx * vx + ny * vy) / ((math.hypot(nx, ny) or 1.0)
                                                   * (math.hypot(vx, vy) or 1.0))
                    bonus *= 1.25 + 0.85 * align
                weights.append(bonus / (0.02 + d))
            nxt = rng.choices(tier, weights=weights, k=1)[0]
            path.append(nxt)
            used.add(nxt["id"])
            touched.add(nxt["region"])
        if len(path) >= 12:
            worst = max(dist((path[i]["x"], path[i]["y"]), (path[i + 1]["x"], path[i + 1]["y"]))
                        for i in range(len(path) - 1))
            covered = len({p["region"] for p in path})
            score = (covered, len(path), -worst)
            if best is None or score > best[0]:
                best = (score, path)
        if best and best[0][0] >= len(region_ids) and best[0][1] >= target:
            break
    assert best is not None, "路线 {} 无法生成连贯步序".format(spec["id"])
    return best[1]


def build_routes(rng, points):
    routes = []
    for spec in ROUTE_SPECS:
        path = walk_route(rng, spec, points)
        assert path[0]["layer"] == "anchor", "路线 {} 起点不是锚点".format(spec["id"])
        offset = rng.randrange(4)
        steps = [{"seq": i + 1,
                  "point_id": path[i]["id"],
                  "label": LAYER_STEP_LABELS[path[i]["layer"]][
                      (offset + i) % len(LAYER_STEP_LABELS[path[i]["layer"]])],
                  "note": rng.choice(STEP_NOTES)} for i in range(len(path))]
        touched = dedup_keep_order([p["region"] for p in path])
        routes.append({"id": spec["id"], "name": spec["name"], "author": spec["author"],
                       "description": spec["description"], "tags": spec["tags"],
                       "est_minutes": spec["minutes"], "region_ids": touched, "steps": steps})
    return routes


# ── 自校验（数据契约） ────────────────────────────────────────────────────
def validate(points, routes):
    assert 320 <= len(points) <= 380, "总点数 {} 不在 320~380".format(len(points))
    ids = [p["id"] for p in points]
    assert len(set(ids)) == len(ids), "point id 重复"
    assert len({p["name"] for p in points}) == len(points), "point name 重复"
    assert len({p["note"] for p in points}) == len(points), "point note 重复"

    bbox = {r["id"]: r["bbox"] for r in REGIONS}
    per_layer = {k: 0 for k in LAYER_KEYS}
    per_region = {r["id"]: 0 for r in REGIONS}

    for p in points:
        assert p["layer"] in per_layer, "非法 layer: {}".format(p["layer"])
        assert 0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0, "坐标越界 {}".format(p["id"])
        b = bbox.get(p["region"])
        assert b is not None, "未知 region {}".format(p["region"])
        assert b["x0"] <= p["x"] <= b["x1"] and b["y0"] <= p["y"] <= b["y1"], \
            "{} 不在区域 {} 包围盒内".format(p["id"], p["region"])
        assert 2 <= len(p["drops"]) <= 4, "{} drops 数量 {}".format(p["id"], len(p["drops"]))
        assert all(isinstance(d, str) and d for d in p["drops"]), "{} drops 非法".format(p["id"])
        assert 15 <= len(p["note"]) <= 35, "{} note 长度 {}".format(p["id"], len(p["note"]))
        assert p["source"] == "sample" and p["author"], "{} 缺少口径字段".format(p["id"])
        per_layer[p["layer"]] += 1
        per_region[p["region"]] += 1

    cell = 0.02
    grid = {}
    min_d, min_pair = 9.9, None
    for p in points:
        gx, gy = int(p["x"] / cell), int(p["y"] / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot(p["x"] - q["x"], p["y"] - q["y"])
                    if d < min_d:
                        min_d, min_pair = d, (q["id"], p["id"])
        grid.setdefault((gx, gy), []).append(p)
    assert min_d > MIN_SEP, "最小间距 {:.5f} 未超过 {}".format(min_d, MIN_SEP)
    assert len(per_region) == 8 and min(per_region.values()) >= 20, "区域覆盖不足"

    assert len(routes) == 8, "路线数量 {} != 8".format(len(routes))
    assert len({r["id"] for r in routes}) == 8, "route id 重复"
    by_id = {p["id"]: p for p in points}
    max_step, steps_total = 0.0, 0
    for r in routes:
        steps = r["steps"]
        steps_total += len(steps)
        assert 12 <= len(steps) <= 28, "{} 步数 {}".format(r["id"], len(steps))
        assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1)), \
            "{} seq 未从 1 连续递增".format(r["id"])
        assert by_id[steps[0]["point_id"]]["layer"] == "anchor", \
            "{} 首步不是锚点".format(r["id"])
        seen = set()
        for s in steps:
            assert s["point_id"] in by_id, "{} 引用不存在的 point_id {}".format(
                r["id"], s["point_id"])
            assert s["point_id"] not in seen, "{} 重复访问 {}".format(r["id"], s["point_id"])
            seen.add(s["point_id"])
        for a, b in zip(steps, steps[1:]):
            pa, pb = by_id[a["point_id"]], by_id[b["point_id"]]
            d = math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])
            max_step = max(max_step, d)
            assert d < MAX_STEP, "{} 第 {}→{} 步距离 {:.4f} >= {}".format(
                r["id"], a["seq"], b["seq"], d, MAX_STEP)
        for rid in r["region_ids"]:
            assert rid in bbox, "{} 非法 region_id {}".format(r["id"], rid)

    return {"per_layer": per_layer, "per_region": per_region, "min_point_dist": min_d,
            "min_point_pair": min_pair, "max_step_dist": max_step, "steps_total": steps_total,
            "route_steps": {r["id"]: len(r["steps"]) for r in routes}}


def dump(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    size = path.stat().st_size
    assert size < MAX_BYTES, "{} 体积 {:.1f}KB 超出 300KB".format(path.name, size / 1024)
    assert json.loads(path.read_text(encoding="utf-8")) == doc, "{} 回读不一致".format(path.name)
    return size


def main():
    rng = random.Random(SEED)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    points = generate_points(rng)
    routes = build_routes(rng, points)
    stats = validate(points, routes)

    points_doc = {
        "schema_version": 1,
        "source": "sample",
        "disclaimer": ("示例数据：坐标与名称仅用于演示界面与同步流程，非库街区官方点位数据。"
                       "可自行替换为公开导出文件。"),
        "map": {"id": "world_map_v1", "name": "示例·大地图底图", "width": MAP_W, "height": MAP_H},
        "layers": LAYERS,
        "regions": [{"id": r["id"], "name": r["name"], "bbox": r["bbox"]} for r in REGIONS],
        "points": points,
    }
    routes_doc = {
        "schema_version": 1,
        "source": "sample",
        "disclaimer": "示例路线：由示例点位串联而成，仅演示顺序导航与轨迹可视化。",
        "routes": routes,
    }
    size_p = dump(POINTS_PATH, points_doc)
    size_r = dump(ROUTES_PATH, routes_doc)

    print("== 示例数据生成完成 (seed={}) ==".format(SEED))
    print("points 总数      : {}   (契约 320~380)".format(len(points)))
    print("分层数量         : " + " / ".join("{} {}".format(LAYER_LABEL[k], stats["per_layer"][k])
                                             for k in LAYER_KEYS))
    print("分区域数量       : " + " / ".join("{} {}".format(r["name"], stats["per_region"][r["id"]])
                                             for r in REGIONS))
    print("routes 条数      : {}   步数合计 {}".format(len(routes), stats["steps_total"]))
    print("各路线步数       : " + ", ".join("{}={}".format(k, v)
                                            for k, v in stats["route_steps"].items()))
    print("最小点间距       : {:.5f}  (契约 > {:.3f})  {}".format(
        stats["min_point_dist"], MIN_SEP, " <-> ".join(stats["min_point_pair"])))
    print("最大相邻步距离   : {:.5f}  (契约 < {:.2f})".format(stats["max_step_dist"], MAX_STEP))
    print("points.json      : {:.1f} KB  {}".format(size_p / 1024, POINTS_PATH))
    print("routes.json      : {:.1f} KB  {}".format(size_r / 1024, ROUTES_PATH))
    print("自校验           : 全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
