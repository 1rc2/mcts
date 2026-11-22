"""数据来源层：本地公开数据 / 库街区官方开放接口。

设计要点（合规）：
1. 点位、路线等地图内容来自「公开共享数据」：内置示例数据、用户放入 import/ 的
   库街区导出文件或社区公开数据包。工具不抓取、不破解任何非公开接口。
2. 云端标记读写只走 kuro_api.json 中由用户/官方文档明确配置的 HTTPS 端点，
   并使用官方授权回调拿到的 token；未配置时云端功能整体降级为「本地模式」。
3. 所有 HTTP 请求都限制体积与超时，不上传游戏内任何数据，只上传点位完成状态。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import paths
from .models import MapDataset, Route

MAX_RESPONSE_BYTES = 12 * 1024 * 1024
DEFAULT_TIMEOUT = 12.0
USER_AGENT = "KuroMapHelper/0.1 (local map explorer helper; read-only public data)"

DEFAULT_RESOURCE_MAP = "map/world_map.png"
ROOT_PACK_NAME = "地面世界（瑝珑 / 黎那汐塔 / 黑海岸 / 罗伊冰原）"
IMAGE_PATTERNS = ("world_map.png", "world_map.jpg", "world_map.jpeg", "world_map.webp")


@dataclass(frozen=True)
class MapPack:
    """import/ 下可切换的一张地图（根目录=地面世界，maps/state_* = 官方子地图）。"""

    key: str
    name: str
    directory: Path
    points_file: Path | None = None
    image_file: Path | None = None
    is_root: bool = False
    state: str = ""
    country: str = ""
    bbox: tuple | None = None          # (x0, y0, x1, y1)，相对 state 8 总图


def _first_file(directory: Path, patterns: Iterable[str]) -> Path | None:
    for pattern in patterns:
        found = sorted(directory.glob(pattern), reverse=True)
        if found:
            return found[0]
    return None


def discover_map_packs(imp_dir: Path | None = None) -> list[MapPack]:
    """扫描 import 根目录与 import/maps/state_*，返回可切换的地图包。

    根目录是地面世界（state 8）；子目录里必须同时有点位与 pack.json/底图才列出，
    避免把半成品目录显示给用户。
    """
    root = Path(imp_dir) if imp_dir else paths.import_dir()
    packs: list[MapPack] = []
    root_points = _first_file(root, ("points*.json",))
    root_image = _first_file(root, IMAGE_PATTERNS)
    if root_points is not None:
        packs.append(MapPack("root", ROOT_PACK_NAME, root, root_points, root_image, True))

    maps_dir = root / "maps"
    if maps_dir.is_dir():
        entries: list[tuple[int, MapPack]] = []
        for d in sorted(maps_dir.iterdir()):
            if not d.is_dir():
                continue
            meta: dict = {}
            try:
                meta = json.loads((d / "pack.json").read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            points = None
            name = str(meta.get("name") or d.name)
            if meta.get("points"):
                p = d / str(meta["points"])
                points = p if p.exists() else None
            if points is None:
                points = _first_file(d, ("points*.json",))
            image = None
            if meta.get("image"):
                p = d / str(meta["image"])
                image = p if p.exists() else None
            if image is None:
                image = _first_file(d, IMAGE_PATTERNS)
            if points is None or image is None:
                continue
            key = str(meta.get("key") or d.name)
            order = int(meta.get("order") or 999)
            bbox = meta.get("bbox")
            if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
                bbox = None
            else:
                bbox = tuple(float(v) for v in bbox)
            entries.append((order, MapPack(
                key, name, d, points, image, False,
                str(meta.get("state") or ""), str(meta.get("country") or ""), bbox)))
        entries.sort(key=lambda item: (item[0], item[1].name))
        packs.extend(pack for _order, pack in entries)
    return packs


# ---------------------------------------------------------------- HTTP 工具


def http_json(url: str, payload: dict | None = None, headers: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET（payload=None）或 POST JSON。仅允许 http/https。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"拒绝非 HTTP(S) 请求：{url}")
    data = None
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers,
                                method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("响应体积超过上限，已丢弃")
        return json.loads(body.decode("utf-8"))


def http_form(url: str, form: dict[str, str], headers: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """OAuth token 端点用的 form POST。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"拒绝非 HTTP(S) 请求：{url}")
    body = urllib.parse.urlencode(form).encode("ascii")
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(MAX_RESPONSE_BYTES).decode("utf-8"))


# ---------------------------------------------------------------- 接口配置


@dataclass
class ApiConfig:
    """库街区官方开放接口配置（由用户在 kuro_api.json 中填写官方文档给出的端点）。

    库街区并未公开面向第三方应用的注册式 OAuth 文档端点；本工具不自带任何
    私有接口地址，也不做逆向抓取。缺配置时账号云端功能提示「未配置」，
    全部本地功能照常可用。
    """

    authorize_url: str = ""
    token_url: str = ""
    userinfo_url: str = ""
    markers_url: str = ""          # GET 读取用户大地图标记
    push_markers_url: str = ""     # POST 回写用户大地图标记
    points_url: str = ""           # GET 公开点位数据（可选，替代内置示例）
    routes_url: str = ""           # GET 玩家分享路线（可选）
    client_id: str = ""
    scopes: list[str] = field(default_factory=lambda: ["map.markers.read", "map.markers.write"])
    redirect_host: str = "127.0.0.1"
    redirect_port: int = 8756
    redirect_path: str = "/callback"
    verify_tls: bool = True

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.redirect_host}:{self.redirect_port}{self.redirect_path}"

    def is_configured(self) -> bool:
        return bool(self.authorize_url and self.token_url and self.client_id)

    def cloud_configured(self) -> bool:
        return self.is_configured() and bool(self.markers_url)

    @staticmethod
    def load(path: Path | None = None) -> "ApiConfig":
        path = path or paths.api_config_path()
        if not path.exists():
            return ApiConfig()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ApiConfig()
        known = {f for f in ApiConfig.__dataclass_fields__}
        return ApiConfig(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        path = path or paths.api_config_path()
        payload = {k: getattr(self, k) for k in ApiConfig.__dataclass_fields__}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def example_payload(self) -> dict:
        return {
            "authorize_url": "https://<库街区官方授权域名>/oauth2/authorize",
            "token_url": "https://<库街区官方授权域名>/oauth2/token",
            "userinfo_url": "https://<库街区官方授权域名>/api/v1/userinfo",
            "markers_url": "https://<库街区官方授权域名>/api/v1/map/markers",
            "push_markers_url": "https://<库街区官方授权域名>/api/v1/map/markers",
            "points_url": "",
            "routes_url": "",
            "client_id": "<官方发放的 client_id>",
            "scopes": ["map.markers.read", "map.markers.write"],
            "redirect_port": 8756,
        }


# ---------------------------------------------------------------- Provider


class ProviderError(Exception):
    pass


class DataProvider:
    name = "base"
    display = "未知数据源"
    supports_cloud = False

    def load_dataset(self) -> MapDataset:  # pragma: no cover - 抽象
        raise NotImplementedError

    def load_routes(self, valid_point_ids: set[str] | None = None) -> list[Route]:
        """valid_point_ids 不为 None 时，丢弃引用了不存在点位的路线。"""
        raise NotImplementedError

    def map_image_path(self, dataset: MapDataset) -> Path | None:
        return None


class LocalDataProvider(DataProvider):
    """内置示例数据 + import/ 目录下用户放入的公开导出数据。"""

    name = "local"
    display = "本地公开数据 / 示例数据"
    supports_cloud = False

    def __init__(self, res_root: Path | None = None, imp_dir: Path | None = None,
                 override_points: str | None = None, override_map: str | None = None):
        self.res = res_root or paths.resource_root()
        self.imp = imp_dir or paths.import_dir()
        self.override_points = override_points
        self.override_map = override_map

    # --- 点位 ---
    def _base_payload(self) -> tuple[dict, Path]:
        cand: list[Path] = []
        if self.override_points:
            cand.append(Path(self.override_points))
        if self.imp.is_dir():
            cand += sorted(self.imp.glob("points*.json"), reverse=True)
        cand.append(self.res / "data" / "points.json")
        for p in cand:
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8")), p
                except Exception as exc:
                    raise ProviderError(f"数据文件解析失败：{p}（{exc}）") from exc
        raise ProviderError("未找到任何点位数据文件")

    def _merge_payload(self, base: dict, exclude: Path | None = None) -> dict:
        """把 import/ 目录里的 JSON 分片合并进来，同一 id 只保留优先级最高的一份。

        优先级：设置里显式指定的数据文件 > import/points*.json（文件名倒序，最新优先）>
        import/ 其它分片 > 内置示例数据。也就是说内置示例永远排在最后，用户导入的公开
        数据总能覆盖同名示例点；而多个导入文件之间，以选定的主数据文件为准。
        """
        if not self.imp.is_dir():
            return base
        shards: list[dict] = []
        for f in sorted(self.imp.glob("*.json"), reverse=True):
            if f.name.startswith("routes") or f == exclude:
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict):
                shards.append(d)
        if not shards:
            return base

        order = shards + [base] if exclude == self.res / "data" / "points.json" else [base] + shards
        # 主数据是真实导入包时，别再把内置示例点合并进来：示例坐标不对应真实底图，
        # 会在官方地图上散落一堆无意义的点。主数据是示例时仍按原逻辑合并分片。
        if str(base.get("source") or "") != "sample":
            order = [d for d in order
                     if d is base or str(d.get("source") or "") != "sample"]
        index: dict[str, dict] = {}
        for d in order:
            for p in d.get("points", []) or []:
                if isinstance(p, dict) and p.get("id"):
                    index.setdefault(str(p["id"]), p)

        out = dict(order[0])                     # 元信息（map/source/disclaimer）取优先级最高的那份
        out["points"] = list(index.values())
        layers = [l for l in (out.get("layers") or []) if isinstance(l, dict) and l.get("key")]
        regions = [r for r in (out.get("regions") or []) if isinstance(r, dict) and r.get("id")]
        for d in order[1:]:
            for lay in d.get("layers", []) or []:
                if isinstance(lay, dict) and lay.get("key") and \
                        not any(l.get("key") == lay["key"] for l in layers):
                    layers.append(lay)
            for reg in d.get("regions", []) or []:
                if isinstance(reg, dict) and reg.get("id") and \
                        not any(r.get("id") == reg["id"] for r in regions):
                    regions.append(reg)
        out["layers"] = layers
        out["regions"] = regions
        return out

    def load_dataset(self) -> MapDataset:
        base, src = self._base_payload()
        base = self._merge_payload(base, exclude=src)
        ds = MapDataset.from_payload(base)
        if not ds.map_image:
            cand = src.parent / "world_map.png"
            ds.map_image = str(cand) if cand.exists() else None
        return ds

    def _routes_candidates(self) -> list[Path]:
        out: list[Path] = []
        if self.imp.is_dir():
            out += sorted(self.imp.glob("routes*.json"), reverse=True)
        out.append(self.res / "data" / "routes.json")
        return out

    def load_routes(self, valid_point_ids: set[str] | None = None) -> list[Route]:
        """与点位同口径：先读到的胜出（import/ 分片按文件名倒序在前，内置示例最后）。
        这样社区路线包只要 id 撞上内置示例，也是导入的那条生效，不会被示例悄悄盖掉。

        valid_point_ids 不为 None 时，丢弃引用了不存在点位的路线：换成官方数据包后，
        内置示例路线引用的示例点已被移除，不过滤就会留下一条条空路线。
        """
        routes: dict[str, Route] = {}
        for p in self._routes_candidates():
            if not p.exists():
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for r in d.get("routes", []):
                if not (isinstance(r, dict) and r.get("id")):
                    continue
                route = Route.from_dict(r)
                if valid_point_ids is not None and (
                        not route.steps or any(s.point_id not in valid_point_ids for s in route.steps)):
                    continue
                routes.setdefault(str(r["id"]), route)
        return list(routes.values())

    def map_image_path(self, dataset: MapDataset) -> Path | None:
        """底图优先级：用户显式覆盖 → 数据文件声明 → import/world_map.png → 内置资源。"""
        for cand in (self.override_map, dataset.map_image):
            if not cand:
                continue
            p = Path(cand)
            if not p.is_absolute():
                # 相对路径先按数据包目录找（数据作者写 "world_map.png" 的直觉做法），
                # 再退回内置资源目录。否则同目录里明明有底图，却静默用了内置占位图，
                # 点位与底图不同源，看起来就是「位置全错」。
                for base in (self.imp, self.res):
                    cand_path = base / p
                    if cand_path.exists():
                        return cand_path
                continue
            if p.exists():
                return p
        # 同源底图：PNG 优先，其次 JPEG/WEBP（官方导出多为 JPG，避免强制转码）
        for name in ("world_map.png", "world_map.jpg", "world_map.jpeg", "world_map.webp"):
            imp = self.imp / name
            if imp.exists():
                return imp
        dflt = self.res / DEFAULT_RESOURCE_MAP
        return dflt if dflt.exists() else None


class KuroApiProvider(DataProvider):
    """库街区官方开放接口（需用户配置官方端点 + 完成官方授权）。"""

    name = "kuro_api"
    display = "库街区官方接口"
    supports_cloud = True

    def __init__(self, api: ApiConfig, token_provider: Callable[[], str | None],
                 fallback: LocalDataProvider | None = None):
        self.api = api
        self.token_provider = token_provider
        self.fallback = fallback or LocalDataProvider()

    def _headers(self) -> dict:
        token = self.token_provider()
        if not token:
            raise ProviderError("尚未完成库街区官方授权，无法使用云端数据")
        return {"Authorization": f"Bearer {token}"}

    def load_dataset(self) -> MapDataset:
        if not self.api.points_url:
            return self.fallback.load_dataset()
        try:
            payload = http_json(self.api.points_url, headers=self._headers() if self.api.is_configured() else None)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            raise ProviderError(f"读取库街区点位数据失败，已回退本地数据：{exc}") from exc
        return MapDataset.from_payload(payload)

    def load_routes(self, valid_point_ids: set[str] | None = None) -> list[Route]:
        """与 LocalDataProvider 保持同一签名：上层统一按 valid_point_ids 过滤。"""
        if not self.api.routes_url:
            return self.fallback.load_routes(valid_point_ids=valid_point_ids)
        try:
            payload = http_json(self.api.routes_url, headers=self._headers() if self.api.is_configured() else None)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            raise ProviderError(f"读取库街区路线数据失败，已回退本地数据：{exc}") from exc
        routes: list[Route] = []
        for r in payload.get("routes", []):
            route = Route.from_dict(r)
            if valid_point_ids is not None and (
                    not route.steps
                    or any(s.point_id not in valid_point_ids for s in route.steps)):
                continue
            routes.append(route)
        return routes

    def map_image_path(self, dataset: MapDataset) -> Path | None:
        return self.fallback.map_image_path(dataset)

    # --- 云端标记 ---
    def fetch_markers(self) -> dict[str, bool]:
        if not self.api.markers_url:
            raise ProviderError("未配置库街区标记读取端点")
        payload = http_json(self.api.markers_url, headers=self._headers())
        items = payload.get("markers") or payload.get("data") or payload
        out: dict[str, bool] = {}
        if isinstance(items, dict):
            for k, v in items.items():
                out[str(k)] = bool(v.get("done") if isinstance(v, dict) else v)
        elif isinstance(items, Iterable):
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    out[str(it["id"])] = bool(it.get("done", it.get("completed", False)))
        return out

    def push_markers(self, delta: dict[str, bool]) -> dict:
        if not self.api.push_markers_url:
            raise ProviderError("未配置库街区标记回写端点")
        markers = [{"id": pid, "done": done} for pid, done in delta.items()]
        return http_json(self.api.push_markers_url, payload={"markers": markers},
                         headers=self._headers())
