# 示例数据说明（points.json / routes.json）

> 目录：`src/kumap/resources/data/` ｜ 生成脚本：仓库根 `tools/gen_sample_data.py` ｜ `schema_version = 1`

这两份 JSON 是**演示用示例数据**，让大地图渲染、图层开关、标记同步、路线顺序导航在首次启动时就有内容可看。
它们**不是**库街区（kurobbs）大地图的真实点位，坐标与备注全部由脚本按随机种子生成。

## 1. 数据口径（务必先读）

| 项 | 口径 |
| --- | --- |
| `source` | 顶层与每条记录都是 `"sample"`。程序可据此显示「示例数据」角标，并禁用「按真实坐标导航」这类承诺。导入别处数据时应改写为 `"imported"`（社区/站点导出）或 `"user"`（本地自建）。 |
| 坐标 | `x` / `y` 是**归一化坐标**（0~1，4 位小数），对应 `map` 声明的示例底图 `1536 x 1024`，左上角为原点、x 向右、y 向下；像素换算 `px = x * width`、`py = y * height`。**与游戏内真实坐标、库街区地图坐标不存在任何对应关系。** |
| 区域 | 8 个区域的 `bbox` 无缝铺满 `[0,1] x [0,1]`，每个点的 `region` 都落在自身包围盒内。区域名沿用公开世界观用语（荒石坡、中曲台地、虎口山脉、残息海岸、青栎庭院、乘霄山、回音峡、今州城郊）。 |
| 名词 | 素材 / 声骸 / 道具名取自游戏公开资料用语（珍珠草、清芬草、植珀、紫珊瑚、银雪莲、贝币、浮金波纹、联能纹章、凝漆纹章、高频声波讯号、呜咔咔、无常凶鹭、飞廉之猩、哀声鸷、云闪之鳞…）。相关名词与 IP 版权归广州库洛科技有限公司所有，本项目仅作知识性引用，不含任何官方美术资源或未公开数据。 |
| 备注 / 作者 | `note` 是玩家口吻的示例短句，`author` 固定 `库街区示例数据`。两份文件顶层均有 `disclaimer`，界面应在数据源面板与「关于」页原样展示。 |

## 2. points.json

顶层键：`schema_version` `source` `disclaimer` `map{id,name,width,height}` `layers[]` `regions[]` `points[]`。

图层（`points[].layer` 只能取这 6 个 key，颜色由 `layers[].color` 提供）：

| key | label | color | 内容 |
| --- | --- | --- | --- |
| `chest` | 宝箱 | `#f2b134` | 奇藏箱 / 物资箱 / 藏宝地 |
| `anchor` | 锚点 | `#3ec6c0` | 信标 / 传送点，每区 3~4 个 |
| `material` | 材料 | `#66c46a` | 采集物、矿石、突破材料 |
| `echo` | 声骸 | `#b07de0` | 野外声骸巢点 |
| `puzzle` | 解谜 | `#5b9df9` | 共鸣方碑、四色灯偶、观景点等 |
| `quest` | 任务 | `#f2705b` | 纪闻 / 伴星 / 道引 / 危行任务 |

点位记录（与当前文件中的真实一条一致）：

```json
{
  "id": "chest_0001",                     // {layer}_{4位序号}，按 图层→区域→x→y 稳定编号
  "layer": "chest",
  "name": "宝箱 · 荒石坡岩拱阴影",          // 「层标签 · 区域名 + 地标」，全表唯一
  "x": 0.0054, "y": 0.2059,               // 归一化坐标，落在所属 region 的 bbox 内
  "region": "r1",
  "level": null,                          // material: 1~3 稀有度；echo: 声骸 COST 1/3/4；其余 null
  "drops": ["联能纹章", "中频声波讯号", "植珀"], // 2~4 个公开名词
  "note": "潮水退去后才能靠近，对强迫症很友好。",  // 15~35 字，全表唯一
  "author": "库街区示例数据",
  "source": "sample"
}
```

当前规模：`336` 点 = 宝箱 118 / 材料 95 / 声骸 47 / 锚点 27 / 解谜 26 / 任务 23；
8 个区域各 37~45 点；其中约 65 个点贴着区域边界成串，用于模拟跨区推进路径。文件约 `128 KB`。

## 3. routes.json

顶层键：`schema_version` `source` `disclaimer` `routes[]`。单条路线：

```json
{
  "id": "route_r1_chest_sweep",
  "name": "荒石坡全清·宝箱一轮",
  "author": "漂泊者示例",
  "description": "从中枢信标出发，清完北崖与碎石河湾一带宝箱，末段顺手采集植珀与珍珠草。",
  "tags": ["开荒", "宝箱"],
  "est_minutes": 52,
  "region_ids": ["r2", "r1"],             // 按实际经过顺序去重，始终非空
  "steps": [{"seq": 1, "point_id": "anchor_0007", "label": "解锁锚点", "note": "走下层的石阶"}]
}
```

- 8 条路线，步数 12~28（合计 163 步）；`seq` 从 1 连续递增。
- `point_id` 必须存在于 `points.json`，可跨图层；同一条路线内不重复访问同一点。
- **首步固定为 `anchor`**（信标起步），`label` 与该步点位图层匹配。
- 主题分布：宝箱线以 `chest` 为主（17~21/步）、材料线以 `material` 为主（14~17 步）、
  声骸线 `echo` 10 步、解谜线 `puzzle` 6 步、信标线含 3 个 `anchor`。
- 相邻步归一化距离 `< 0.16`（实测最大 `0.1441`，约底图 220 px），轨迹连续不瞬移。
- `est_minutes` 是示例估算，不代表真实游戏耗时。

## 4. 重新生成与自检

```bash
cd /data/kuro-map-helper && python3 tools/gen_sample_data.py
```

种子在脚本顶部：`SEED = 20261121`。重复运行输出**字节级一致**（已用 md5 验证），
调整分布只需改 `LAYER_COUNTS`（各层数量）、`LAYER_REGION_WEIGHTS`（层 x 区域密度）、
`REGIONS`（区域与地标名）或 `ROUTE_SPECS`（路线定义），改完重跑并确认自校验通过。

`validate()` 在写盘前对每条契约做 `assert`，任一失败即不产出文件：

1. 总点数 ∈ [320, 380]；`id / name / note` 全表唯一；`layer` 属于 6 个 key。
2. `0 ≤ x, y ≤ 1`，且点落在所属 `region` 的 `bbox` 内。
3. `2 ≤ len(drops) ≤ 4`；`15 ≤ len(note) ≤ 35`。
4. 任意两点欧氏距离 > `0.004`（生成时按 `0.0075` 留余量；实测最小 `0.00751`）。
5. 8 条路线；每条 12~28 步、`seq` 连续、`point_id` 全部存在且不重复；首步图层为 `anchor`。
6. 路线相邻步距离 < `0.16`（生成时按 `0.125 / 0.145` 两级抓取半径；实测最大 `0.1441`）。
7. 单文件 < `300 KB`；`ensure_ascii=False` + `indent=1` + UTF-8；写盘后回读解析并与内存对象比对。

快速验收：

```bash
python3 -c "import json;print(len(json.load(open('src/kumap/resources/data/points.json'))['points']))"  # 336
python3 -c "import json;print(len(json.load(open('src/kumap/resources/data/routes.json'))['routes']))"  # 8
```

## 5. 替换成库街区导出 / 社区公开数据

`resources/data/` 属于**程序资源**（由 `core/paths.py` 的 `resource_root()` 解析），打包与升级会被覆盖，
正式数据推荐走导入而不是改这里：

1. **放进导入目录**（推荐）：把导出文件放到 `data_root()/import/`（即 `paths.import_dir()`；
   Windows `%APPDATA%/KuroMapHelper/import`，Linux/macOS `~/.config/KuroMapHelper/import`，
   便携模式为程序旁 `data/import`）。导入器解析后写入本地库，记录 `source = "imported"`、
   `author` 写实际出处，示例文件原样保留以便回退。
2. **整体替换资源**（便携演示包）：产出同 schema 的两份 JSON 覆盖 `resources/data/`。
   因为 `routes[].steps[].point_id` 依赖 `points[].id`，**两份必须同时替换**；
   真实数据建议改用可稳定复现的 id（如 `{layer}_{来源点位ID}`），
   否则一旦按坐标重排编号，收藏 / 完成状态 / 同步队列都会错位。

外部数据 → 本 schema 的最小字段映射：

| 目标字段 | 来源与处理 |
| --- | --- |
| `x` / `y` | 站点导出通常是其自身底图像素或 0~100 百分比：像素按 `x = px / 源底图宽`、百分比除以 100。**换算参数必须随数据一起记录，不要套用示例底图的 1536x1024。** |
| `layer` | 类型归类：奇藏箱 / 物资箱 / 藏宝地 → `chest`；小型 / 中枢信标 → `anchor`；采集物 / 矿石 / 突破材料 / 动物 → `material`；声骸 / 野外 BOSS → `echo`；解谜 / 观景点 / 隙声蝶 / 飞猎手 / 无音区 → `puzzle`；纪闻 / 伴星 / 危行 / 道引任务 → `quest`。 |
| `name` | 优先用来源点位名；缺失时按 `层标签 · 区域名 + 地标` 兜底，保持全表唯一。 |
| `region` | 来源带真实区域名时建立「源区域名 → `regions[].id`」对照；同时**必须一并替换 `map` 与 `regions[].bbox`**，示例包围盒与真实地图并不对应。只有坐标没有区域名时，用 `bbox` 反查；查不到就归入「未分类」。 |
| `level` | `material` 记稀有度 1~3，`echo` 记 COST 1/3/4，其余留 `null`。 |
| `drops` | 来源的产出 / 掉落字段，裁剪到 2~4 项，用公开中文名。 |
| `note` | 来源说明或留空；导入数据不要复用示例备注。 |
| `author` / `source` / `disclaimer` | 写清出处与整理日期（例如 `库街区大地图导出 2026-11`、游戏版本号），顶层 `disclaimer` 换成实际来源声明。 |

参考出处（公开页面，抓取前先阅读站点条款与 robots 约定，仅供个人离线使用，**不要二次分发原始点位库**）：

- 库街区鸣潮大地图（点位分类与筛选口径参考）：<https://www.kurobbs.com/mc/map/>
- 鸣潮 BWIKI 素材 / 声骸目录：<https://wiki.biligame.com/wutheringwaves/>
- 社区互动地图（中文名对照，适合做字段映射）：<https://wuthering.gg/zh-Hans/map>

合规提示：游戏名词、图标与地图美术版权归广州库洛科技有限公司。本仓库自带的
`tools/fetch_official_map.py` 只做**匿名公开静态资源**的本地化（不涉及登录、验证码与任何用户数据接口，
口径见 `docs/合规说明.md` 第 4 节），属于低频一次性操作，抓到的内容仅限个人学习研究、不得二次分发或打包再发；
程序运行时不会联网抓取任何官方数据。示例坐标一律为程序生成，请勿对外宣称为官方或库街区真实点位。
