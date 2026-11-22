#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把库街区大地图的「公开静态资源」离线打包进本工具。

数据来源（均为免登录的公开 CDN 资源，不含任何账号 Cookie / Token）：
  * 地图资源配置   POST https://api.kurobbs.com/map/core/config/getMapResource
  * 地图瓦片索引   POST https://api.kurobbs.com/map/core/config/getMapIdList
  * 点位 / 类型 / 地区  https://web-static.kurobbs.com/mcmap/...

产物：
  assets/maps/huanglong.jpg   拼接后的离线底图（默认宽 6144）
  data/map.json               地图元数据
  data/markers.json           归一化点位
  data/routes.json            由公开点位生成的示例路线

依赖：Python 3.9+、Pillow（pip install pillow）
用法：python scripts/import-official-map.py
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = 'https://api.kurobbs.com'
CDN = 'https://web-static.kurobbs.com'
MAP_ID = '8'                                        # 8 = 煌珑
NATIVE_TILE = 1024
COORD_RATE = 100
COORD_BASE = 850
COORD_SCALE = NATIVE_TILE / COORD_BASE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, '.cache', 'official-map')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Referer': 'https://www.kurobbs.com/mc/map/',
    'Content-Type': 'application/json',
}


def post_json(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    with urllib.request.urlopen(req, timeout=30) as res:
        body = json.loads(res.read().decode('utf-8'))
    if body.get('code') != 200:
        raise RuntimeError(f'{url} -> {body}')
    return body['data']


def get_bytes(url):
    req = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent'], 'Referer': HEADERS['Referer']})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def fetch_json(url):
    return json.loads(get_bytes(url).decode('utf-8'))


def cached_tile(name, url):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    for attempt in range(3):
        try:
            blob = get_bytes(url)
            with open(path, 'wb') as fh:
                fh.write(blob)
            return path
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def build_catalog(catalog):
    """typeId -> {name, table}"""
    meta = {}

    def walk(node, table=None):
        if isinstance(node, dict):
            table = node.get('tableName') or table
            tid, name = node.get('id'), node.get('name')
            if tid and name and tid not in meta:
                meta[tid] = {'name': name, 'table': table or ''}
            for child in node.get('children') or []:
                walk(child, table)
        elif isinstance(node, list):
            for child in node:
                walk(child, table)

    walk(catalog)
    return meta


def build_regions(country, map_id):
    """地区节点（stateId 匹配）列表：name + 世界坐标"""
    out = []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        if str(node.get('stateId')) == str(map_id) and node.get('name') is not None:
            x, y = node.get('xPosition'), node.get('yPosition')
            if x is not None and y is not None:
                out.append({'name': node['name'], 'x': float(x), 'y': float(y)})
        for key in ('countrys', 'children'):
            for child in node.get(key) or []:
                walk(child)

    walk(country)
    return out


def world_to_px(x, y):
    """游戏世界坐标 -> 地图像素坐标（Leaflet CRS.Simple，x 向右、y 向下）"""
    return x / COORD_RATE * COORD_SCALE + NATIVE_TILE, y / COORD_RATE * COORD_SCALE


def tile_row(ty):
    """瓦片文件名使用 TMS 的 y（自下而上），换算为自上而下的行号"""
    return -ty - 1


def map_type(table, name):
    if table == '大地图-采集物':
        return 'material'
    if table in ('大地图-敌人', '大地图-强敌', '大地图-BOSS'):
        return 'enemy'
    if table == '大地图-收集道具':
        return 'chest'
    if table == '大地图-地图探索':
        return 'anchor' if ('信标' in name or '锚' in name) else 'puzzle'
    if table == '大地图-副本挑战':
        return 'quest'
    if table == '大地图-NPC':
        return 'npc'
    return 'puzzle'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tile-size', type=int, default=384, help='拼接用瓦片边长（下载时缩放）')
    parser.add_argument('--width', type=int, default=6144, help='输出底图宽度')
    parser.add_argument('--skip-image', action='store_true', help='只生成数据，不重拼底图')
    args = parser.parse_args()

    from PIL import Image

    print('[1/6] 获取地图资源版本…')
    resource = post_json(f'{API}/map/core/config/getMapResource', {})
    print('    resource =', resource)

    print('[2/6] 获取瓦片索引…')
    tile_map = post_json(f'{API}/map/core/config/getMapIdList', {})
    tiles = sorted(tile_map[MAP_ID], key=lambda s: tuple(int(v) for v in s.split('_')[1:]))
    coords = []
    for key in tiles:
        _, tx, ty = key.split('_')
        coords.append((int(tx), int(ty)))
    min_x = min(c[0] for c in coords)
    max_x = max(c[0] for c in coords)
    rows_all = [tile_row(c[1]) for c in coords]
    min_row, max_row = min(rows_all), max(rows_all)
    cols, rows = max_x - min_x + 1, max_row - min_row + 1
    print(f'    {len(coords)} 张瓦片，网格 {cols}x{rows}（x {min_x}~{max_x}, 行 {min_row}~{max_row}）')

    print('[3/6] 拉取点位 / 类型 / 地区 JSON…')
    positions = fetch_json(f'{CDN}/mcmap/position/{MAP_ID}/position.json')
    catalog = fetch_json(f'{CDN}/mcmap/catalog/{resource}/{MAP_ID}/catalog.json')
    country = fetch_json(f'{CDN}/mcmap/country/{resource}/country.json')
    type_meta = build_catalog(catalog)
    regions = build_regions(country, MAP_ID)
    print(f'    {len(positions)} 组点位，{len(type_meta)} 个类型，{len(regions)} 个地区节点')

    total_px = (cols * NATIVE_TILE, rows * NATIVE_TILE)
    origin_x = min_x * NATIVE_TILE
    origin_y = min_row * NATIVE_TILE

    def to_uv(x, y):
        px, py = world_to_px(x, y)
        return (px - origin_x) / total_px[0], (py - origin_y) / total_px[1]

    print('[4/6] 生成点位数据…')
    markers = []
    skipped = 0
    counters = {}
    for group in positions:
        tid = group.get('id')
        info = type_meta.get(tid, {'name': group.get('name') or tid, 'table': ''})
        for loc in group.get('location') or []:
            x, y = loc.get('x'), loc.get('y')
            if x is None or y is None:
                skipped += 1
                continue
            u, v = to_uv(x, y)
            if not (0 <= u <= 1 and 0 <= v <= 1):
                skipped += 1
                continue
            n = counters.get(tid, 0) + 1
            counters[tid] = n
            # 选择最近的地区名
            region = ''
            best = None
            for r in regions:
                d = (r['x'] - x) ** 2 + (r['y'] - y) ** 2
                if best is None or d < best:
                    best, region = d, r['name']
            desc = (loc.get('description') or '').strip()
            if len(desc) > 160:
                desc = desc[:157] + '…'
            markers.append({
                'id': f'off-{tid}-{n}',
                'type': map_type(info['table'], info['name']),
                'u': round(u, 6),
                'v': round(v, 6),
                'region': region,
                'name': info['name'],
                'drops': re.sub(r'\s+', ' ', desc),
                'note': '',
            })

    print(f'    写入 {len(markers)} 个点位（跳过 {skipped} 个越界/无坐标）')

    # 示例路线：在点位最多的地区里取同类点位做最近邻串联
    def build_route(mtype, title, desc):
        pool = [m for m in markers if m['type'] == mtype]
        if len(pool) < 4:
            return None
        region_count = {}
        for m in pool:
            region_count[m['region']] = region_count.get(m['region'], 0) + 1
        region = max(region_count, key=region_count.get)
        local = [m for m in pool if m['region'] == region]
        local.sort(key=lambda m: (m['u'], m['v']))
        chain = [local[0]]
        rest = local[1:]
        while rest and len(chain) < 12:
            last = chain[-1]
            rest.sort(key=lambda m: (m['u'] - last['u']) ** 2 + (m['v'] - last['v']) ** 2)
            chain.append(rest.pop(0))
        return {
            'id': f'route-{mtype}-{len(chain)}',
            'name': title,
            'author': '官方公开数据自动生成',
            'desc': desc,
            'points': [
                {'u': m['u'], 'v': m['v'], 'markerId': m['id'], 'label': m['name']}
                for m in chain
            ],
        }

    routes = [
        build_route('chest', '煌珑 · 奇藏箱示例路线', '依据官方公开点位自动串联，仅作路线功能演示，非最优路径。'),
        build_route('material', '煌珑 · 采集示例路线', '依据官方公开点位自动串联，仅作路线功能演示，非最优路径。'),
    ]
    routes = [r for r in routes if r]

    print('[5/6] 拼接底图…')
    map_image = 'assets/maps/huanglong.jpg'
    out_w = args.width
    out_h = round(out_w * rows / cols)
    if not args.skip_image:
        tile_size = args.tile_size
        canvas = Image.new('RGB', (cols * tile_size, rows * tile_size), (10, 13, 16))
        jobs = []
        for tx, ty in coords:
            key = f'{MAP_ID}_{tx}_{ty}'
            url = (f'{CDN}/mcmap/tiles/{resource}/{MAP_ID}/{key}.png'
                   f'?x-oss-process=image/format,png/resize,w_{tile_size},h_{tile_size}')
            jobs.append((key, url))
        total = len(jobs)
        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(cached_tile, f'{key}_{tile_size}.png', url): key for key, url in jobs}
            for future in as_completed(futures):
                future.result()
                done += 1
                if done % 40 == 0 or done == total:
                    print(f'    {done}/{total}')
        for tx, ty in coords:
            key = f'{MAP_ID}_{tx}_{ty}'
            path = os.path.join(CACHE, f'{key}_{tile_size}.png')
            tile = Image.open(path).convert('RGBA')
            if tile.size != (tile_size, tile_size):
                tile = tile.resize((tile_size, tile_size), Image.LANCZOS)
            canvas.paste(tile, ((tx - min_x) * tile_size, (tile_row(ty) - min_row) * tile_size), tile)
        out_path = os.path.join(ROOT, map_image)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        final = canvas.resize((out_w, out_h), Image.LANCZOS)
        final.save(out_path, 'JPEG', quality=86, optimize=True, progressive=True)
        print(f'    已写入 {map_image} ({out_w}x{out_h}, {os.path.getsize(out_path) / 1048576:.1f} MB)')

    print('[6/6] 写入 data/*.json…')
    width_m = total_px[0] / COORD_SCALE
    region_points = []
    seen = set()
    for r in regions:
        u, v = to_uv(r['x'], r['y'])
        if not (0 <= u <= 1 and 0 <= v <= 1) or r['name'] in seen:
            continue
        seen.add(r['name'])
        region_points.append({'name': r['name'], 'u': round(u, 5), 'v': round(v, 5)})

    map_json = {
        'id': 'kuro-huanglong',
        'name': '鸣潮 · 煌珑（官方公开数据）',
        'image': map_image,
        'width': out_w,
        'height': out_h,
        'worldWidthMeters': round(width_m),
        'attribution': ('底图与点位来自库街区大地图公开静态资源（web-static.kurobbs.com），'
                        '由本工具离线打包，仅作探索参考；不包含账号数据，不代表官方立场。'),
        'regions': region_points[:60],
    }

    with open(os.path.join(ROOT, 'data', 'map.json'), 'w', encoding='utf-8') as fh:
        json.dump(map_json, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, 'data', 'markers.json'), 'w', encoding='utf-8') as fh:
        json.dump({
            'version': 2,
            'source': '库街区大地图公开静态资源（离线打包）',
            'map': map_json['id'],
            'markers': markers,
        }, fh, ensure_ascii=False, separators=(',', ':'))
    with open(os.path.join(ROOT, 'data', 'routes.json'), 'w', encoding='utf-8') as fh:
        json.dump({'version': 2, 'routes': routes}, fh, ensure_ascii=False, indent=1)

    stats = {}
    for m in markers:
        stats[m['type']] = stats.get(m['type'], 0) + 1
    print('    类型分布：', stats)
    print('完成。')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('导入失败：', exc, file=sys.stderr)
        sys.exit(1)
