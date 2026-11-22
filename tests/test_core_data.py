"""核心数据层测试：配置、标记存储、同步合并、本地数据源。

不依赖 GUI，也不依赖网络；用 KUMAPH_DATA_DIR 把写入隔离到临时目录。
"""

from __future__ import annotations

import json

import pytest

from kumap.core import paths
from kumap.core.config import DEFAULTS, Config
from kumap.core.marker_store import MarkerStore
from kumap.core.models import MapDataset, MapPoint
from kumap.core.provider import LocalDataProvider, ProviderError, discover_map_packs
from kumap.core.sync import SyncEngine


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMAPH_DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- 配置


def test_config_defaults_and_clamp(data_dir):
    cfg = Config(paths.config_path())
    assert cfg.ui["theme"] == DEFAULTS["ui"]["theme"]
    assert cfg.hotkeys["mark_done"] == "ctrl+alt+m"

    cfg.set("vision", "interval_ms", 1)          # 低于下限
    assert cfg.vision["interval_ms"] == 500
    cfg.set("vision", "interval_ms", 99999)      # 高于上限
    assert cfg.vision["interval_ms"] == 10000
    cfg.set("ui", "opacity", 5.0)
    assert cfg.ui["opacity"] == 1.0
    cfg.set("ui", "theme", "nonsense")
    assert cfg.ui["theme"] == "dark"


def test_config_roundtrip_and_layer_flags(data_dir):
    cfg = Config(paths.config_path())
    cfg.set("ui", "theme", "light")
    cfg.set("sync", "conflict", "remote")
    cfg.set_layer_visible("chest", False)
    assert cfg.layer_visible("chest") is False
    assert cfg.layer_visible("anchor") is True   # 未在文件里时默认显示

    again = Config(paths.config_path())
    assert again.ui["theme"] == "light"
    assert again.sync["conflict"] == "remote"
    assert again.layer_visible("chest") is False


def test_config_recovers_from_broken_file(data_dir):
    p = paths.config_path()
    p.write_text("{ this is not json", encoding="utf-8")
    cfg = Config(p)                              # 不应抛异常
    assert cfg.ui["theme"] == DEFAULTS["ui"]["theme"]
    assert p.with_suffix(".broken").exists()


def test_config_keeps_unknown_sections(data_dir):
    p = paths.config_path()
    p.write_text(json.dumps({"ui": {"theme": "light"}, "future": {"x": 1}}), encoding="utf-8")
    cfg = Config(p)
    assert cfg.data["future"]["x"] == 1
    assert cfg.ui["theme"] == "light"


# ---------------------------------------------------------------- 标记存储


def _store() -> MarkerStore:
    return MarkerStore(markers=paths.markers_path(), private=paths.private_points_path(),
                       favorites=paths.favorites_path())


def test_marker_toggle_builds_pending_queue(data_dir):
    st = _store()
    assert st.pending_count() == 0
    assert st.toggle("chest_0001") is True
    assert st.is_done("chest_0001") is True
    assert st.pending() == {"chest_0001": True}

    st.toggle("chest_0001")                      # 取消完成也要能回传
    assert st.is_done("chest_0001") is False
    assert st.pending() == {"chest_0001": False}


def test_marker_persistence(data_dir):
    st = _store()
    st.set_done("echo_0007", True)
    st.set_note("echo_0007", "在树洞里")
    st.toggle_favorite("route_x")

    reloaded = _store()
    assert reloaded.is_done("echo_0007") is True
    assert reloaded.get_note("echo_0007") == "在树洞里"
    assert reloaded.is_favorite("route_x") is True
    assert reloaded.pending_count() == 1         # 待回传队列同样落盘


def test_mark_pushed_clears_only_those_ids(data_dir):
    st = _store()
    st.set_done("a", True)
    st.set_done("b", True)
    st.mark_pushed(["a"])
    assert st.pending() == {"b": True}


def test_apply_remote_newer_policy_keeps_local_pending(data_dir):
    st = _store()
    st.set_done("p1", True)                      # 本地改了还没回传 -> pending
    st.set_done("p2", False)
    st.mark_pushed(["p2"])                       # p2 已确认，不再是 pending

    applied, conflicts = st.apply_remote({"p1": False, "p2": True, "p3": True}, conflict="newer")
    assert conflicts == 1                        # p1 与云端冲突，以本地为准
    assert st.is_done("p1") is True
    assert st.is_done("p2") is True              # 已推送过的按云端为准
    assert st.is_done("p3") is True
    assert applied == 2


def test_apply_remote_prefers_remote_policy(data_dir):
    st = _store()
    st.set_done("p1", True)
    applied, conflicts = st.apply_remote({"p1": False}, conflict="remote")
    assert conflicts == 0 and applied == 1
    assert st.is_done("p1") is False
    assert st.pending_count() == 0               # 采纳云端后不必再回传


def test_apply_remote_no_change_when_equal(data_dir):
    st = _store()
    st.set_done("p1", True)
    st.mark_pushed(["p1"])
    applied, conflicts = st.apply_remote({"p1": True}, conflict="newer")
    assert (applied, conflicts) == (0, 0)


def test_counts_and_reset(data_dir):
    st = _store()
    ids = ["a", "b", "c"]
    st.set_done("a", True)
    assert st.counts(ids) == (1, 3)
    st.reset_all()
    assert st.counts(ids) == (0, 3)
    assert st.pending_count() == 0


def test_private_points_crud(data_dir):
    st = _store()
    p = st.add_private("我的采集点", 0.4, 0.6, layer="material", note="每次刷新都来")
    assert isinstance(p, MapPoint) and p.source == "private"
    st.update_private(p.id, name="改名了", x=0.1)
    got = st.get_point(p.id)
    assert got.name == "改名了" and got.x == 0.1

    again = _store()
    assert [q.id for q in again.private_points()] == [p.id]
    again.remove_private(p.id)
    assert _store().private_points() == []


def test_private_point_out_of_range_is_clamped(data_dir):
    st = _store()
    p = st.add_private("边界", 1.7, -0.3)
    assert 0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0


# ---------------------------------------------------------------- 同步（纯逻辑）


def test_sync_merge_pull_and_push_flow(data_dir):
    st = _store()
    eng = SyncEngine(st, conflict="newer")
    assert eng.cloud_enabled() is False          # 没有 provider 就不该联网

    st.set_done("a", True)
    st.set_done("b", False)
    assert eng.build_push_payload() == {"a": True, "b": False}

    rep = eng.merge_pull({"a": False, "c": True})
    assert rep.ok and rep.conflicts == 1 and rep.pulled == 1
    assert st.is_done("c") is True

    rep2 = eng.confirm_push(eng.build_push_payload())
    assert rep2.pushed == 2 and st.pending_count() == 0
    assert "拉取 1 条" in rep.summary()


def test_sync_conflict_policy_switch(data_dir):
    st = _store()
    eng = SyncEngine(st, conflict="newer")
    st.set_done("a", True)
    assert eng.merge_pull({"a": False}).conflicts == 1
    eng.set_conflict("remote")
    assert eng.merge_pull({"a": False}).conflicts == 0
    assert st.is_done("a") is False


def test_sync_report_summary_text(data_dir):
    st = _store()
    eng = SyncEngine(st)
    assert eng.merge_pull({}).summary() == "两端进度已一致"


# ---------------------------------------------------------------- 本地数据源


def _payload(points, layers=None, regions=None):
    return {
        "schema_version": 1,
        "source": "unit",
        "disclaimer": "测试数据",
        "map": {"id": "m", "name": "m", "width": 100, "height": 100},
        "layers": layers or [{"key": "chest", "label": "宝箱", "color": "#f2b134"}],
        "regions": regions or [],
        "points": points,
    }


def test_local_provider_falls_back_to_bundled_sample(data_dir):
    prov = LocalDataProvider(imp_dir=data_dir / "empty_import")
    ds = prov.load_dataset()
    assert len(ds.points) == 336
    assert {l.key for l in ds.ordered_layers()} == {"chest", "anchor", "material",
                                                    "echo", "puzzle", "quest"}
    assert len(prov.load_routes()) == 8


def test_local_provider_import_dir_priority(data_dir):
    imp = data_dir / "import"
    imp.mkdir()
    newer = _payload([{"id": "chest_9001", "layer": "chest", "name": "导入点",
                       "x": 0.5, "y": 0.5}])
    (imp / "points_2026.json").write_text(json.dumps(newer, ensure_ascii=False), encoding="utf-8")
    # 另一分片：同 id 以导入为准，新 id 合并进来
    extra = _payload([{"id": "chest_9001", "layer": "chest", "name": "被覆盖", "x": 0.9, "y": 0.9},
                      {"id": "chest_9002", "layer": "chest", "name": "分片点", "x": 0.2, "y": 0.2}])
    (imp / "extra.json").write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")

    ds = LocalDataProvider(imp_dir=imp).load_dataset()
    names = {p.id: p.name for p in ds.points}
    assert names == {"chest_9001": "导入点", "chest_9002": "分片点"}
    assert ds.source == "unit"


def test_local_provider_override_points_beats_import(data_dir):
    """显式指定的数据文件优先级最高：同 id 用它，import 分片只做补充。"""
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_a.json").write_text(json.dumps(_payload([
        {"id": "shared", "layer": "chest", "name": "导入版", "x": 0.1, "y": 0.1},
        {"id": "only_import", "layer": "chest", "name": "补充分片", "x": 0.3, "y": 0.3},
    ])), encoding="utf-8")
    own = data_dir / "mine.json"
    own.write_text(json.dumps(_payload([
        {"id": "shared", "layer": "chest", "name": "自选版", "x": 0.9, "y": 0.9},
    ])), encoding="utf-8")

    ds = LocalDataProvider(imp_dir=imp, override_points=str(own)).load_dataset()
    got = {p.id: p for p in ds.points}
    assert set(got) == {"shared", "only_import"}
    assert got["shared"].name == "自选版" and got["shared"].x == 0.9


def test_local_provider_real_import_drops_bundled_sample(data_dir):
    """导入包作为主数据时，内置示例点不应再混入（坐标口径不同，会散落在真实地图上）。"""
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_official.json").write_text(json.dumps(_payload([
        {"id": "off-1", "layer": "chest", "name": "官方点", "x": 0.2, "y": 0.3},
        {"id": "off-2", "layer": "chest", "name": "官方点2", "x": 0.4, "y": 0.5},
    ]), ensure_ascii=False), encoding="utf-8")

    ds = LocalDataProvider(imp_dir=imp).load_dataset()
    assert {p.id for p in ds.points} == {"off-1", "off-2"}     # 示例的 336 点不再合并


def test_local_provider_import_shards_override_bundled_sample(data_dir):
    """只往 import/ 丢一个非 points*.json 的分片时，它必须能覆盖内置示例点。"""
    imp = data_dir / "import"
    imp.mkdir()
    shard = _payload([{"id": "chest_0001", "layer": "chest", "name": "真实导出点",
                       "x": 0.11, "y": 0.22}],
                     layers=[{"key": "chest", "label": "宝箱", "color": "#000000"}])
    (imp / "export_part1.json").write_text(json.dumps(shard, ensure_ascii=False), encoding="utf-8")

    ds = LocalDataProvider(imp_dir=imp).load_dataset()
    by_id = {p.id: p for p in ds.points}
    assert by_id["chest_0001"].name == "真实导出点"
    assert len(ds.points) == 336                      # 示例点仍在，只是被同名点替换
    assert ds.layer_map()["chest"].label == "宝箱"


def test_local_provider_import_routes_override_bundled(data_dir):
    """导入路线与内置示例同 id 时以导入为准 —— 和点位同一套优先级口径。"""
    imp = data_dir / "import"
    imp.mkdir()
    sample_id = LocalDataProvider(imp_dir=imp).load_routes()[0].id
    (imp / "routes_real.json").write_text(json.dumps({
        "schema_version": 1, "source": "unit",
        "routes": [{"id": sample_id, "name": "导入路线",
                    "steps": [{"seq": 1, "point_id": "chest_0001", "label": "起点"}]}],
    }, ensure_ascii=False), encoding="utf-8")

    got = {r.id: r for r in LocalDataProvider(imp_dir=imp).load_routes()}
    assert got[sample_id].name == "导入路线"
    assert got[sample_id].steps[0].label == "起点"
    assert len(got) == 8                                   # 其余示例路线照常保留


def test_local_provider_drops_routes_with_missing_points(data_dir):
    """换成官方点位包后，引用示例点的内置路线要自动丢弃，不留空路线。"""
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_official.json").write_text(json.dumps(_payload([
        {"id": "off-1", "layer": "chest", "name": "官方点", "x": 0.2, "y": 0.3},
    ])), encoding="utf-8")
    (imp / "routes_official.json").write_text(json.dumps({
        "source": "unit", "routes": [
            {"id": "route-real", "name": "官方路线",
             "steps": [{"seq": 1, "point_id": "off-1", "label": "起点"}]},
            {"id": "route-broken", "name": "悬空路线",
             "steps": [{"seq": 1, "point_id": "missing", "label": "没有这个点"}]},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    got = LocalDataProvider(imp_dir=imp).load_routes(valid_point_ids={"off-1"})
    assert [r.id for r in got] == ["route-real"]


def test_local_provider_rejects_broken_json(data_dir):
    own = data_dir / "bad.json"
    own.write_text("nope", encoding="utf-8")
    with pytest.raises(ProviderError):
        LocalDataProvider(imp_dir=data_dir, override_points=str(own)).load_dataset()


def test_local_provider_missing_file_is_error_not_crash(data_dir):
    with pytest.raises(ProviderError):
        LocalDataProvider(res_root=data_dir / "nothing", imp_dir=data_dir).load_dataset()


def test_map_image_path_prefers_override(data_dir):
    png = data_dir / "custom.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    prov = LocalDataProvider(imp_dir=data_dir, override_map=str(png))
    ds = MapDataset.from_payload(_payload([]))
    assert prov.map_image_path(ds) == png


def test_map_image_path_uses_sidecar_in_import(data_dir):
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_a.json").write_text(json.dumps(_payload([])), encoding="utf-8")
    (imp / "world_map.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ds = LocalDataProvider(imp_dir=imp).load_dataset()
    got = LocalDataProvider(imp_dir=imp).map_image_path(ds)
    assert got is not None and got.name == "world_map.png"


def test_map_image_path_accepts_jpg_sidecar(data_dir):
    """官方导出通常是 JPG：与点位同目录的 world_map.jpg 也要能自动识别。"""
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_a.json").write_text(json.dumps(_payload([])), encoding="utf-8")
    (imp / "world_map.jpg").write_bytes(b"\xff\xd8\xff\xe0JFIF")
    ds = LocalDataProvider(imp_dir=imp).load_dataset()
    got = LocalDataProvider(imp_dir=imp).map_image_path(ds)
    assert got is not None and got.name == "world_map.jpg"


def test_discover_map_packs_root_and_submaps(data_dir):
    imp = data_dir / "import"
    imp.mkdir()
    (imp / "points_root.json").write_text(json.dumps(_payload([])), encoding="utf-8")
    (imp / "world_map.jpg").write_bytes(b"\xff\xd8\xff\xe0JFIF")
    sub = imp / "maps" / "state_910"
    sub.mkdir(parents=True)
    (sub / "points_kurobbs_910.json").write_text(json.dumps(_payload([])), encoding="utf-8")
    (sub / "world_map.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sub / "pack.json").write_text(json.dumps(
        {"state": 910, "name": "时隙废都", "points": "points_kurobbs_910.json",
         "image": "world_map.png"}, ensure_ascii=False), encoding="utf-8")

    packs = discover_map_packs(imp)
    assert [p.key for p in packs] == ["root", "state_910"]
    assert packs[0].is_root and "地面世界" in packs[0].name
    assert packs[1].name == "时隙废都"
    assert packs[1].points_file.name == "points_kurobbs_910.json"
    assert packs[1].image_file.name == "world_map.png"


def test_dataset_from_payload_is_forgiving():
    ds = MapDataset.from_payload({
        "points": [
            {"id": "a", "layer": "chest", "name": "A", "x": 2, "y": -1},   # 越界钳制
            {"id": "b", "layer": "unknown_layer", "name": "B", "x": 0.1, "y": 0.1},
            {"layer": "chest", "name": "没有 id"},                          # 被丢弃
        ]
    })
    assert [p.id for p in ds.points] == ["a", "b"]
    assert ds.points[0].x == 1.0 and ds.points[0].y == 0.0
    keys = {l.key for l in ds.ordered_layers()}
    assert "chest" in keys and "unknown_layer" in keys
    assert ds.layer_map()["unknown_layer"].label == "unknown_layer"


def test_point_to_dict_roundtrip():
    p = MapPoint.from_dict({"id": "x", "layer": "echo", "name": "N", "x": 0.25, "y": 0.5,
                            "drops": ["a", "b"], "note": "备注"})
    back = MapPoint.from_dict(p.to_dict())
    assert back.id == "x" and back.drops == ["a", "b"] and back.note == "备注"
