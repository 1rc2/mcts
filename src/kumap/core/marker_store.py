"""本地进度仓库：完成标记、私人点位、收藏路线、待回传队列。

全部落在用户目录的 JSON 文件里；无账号模式下这些就是唯一记录，
不会上传到任何地方。写入一律原子替换，避免断电/强杀损坏数据。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from . import paths
from .models import MapPoint, MarkerState, _clamp01, _now


def _atomic_write(path: Path, payload) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            path.rename(path.with_suffix(".broken"))
        except OSError:
            pass
        return default


class MarkerStore:
    def __init__(self, markers: Path | None = None, private: Path | None = None,
                 favorites: Path | None = None, on_change: Callable[[], None] | None = None):
        self.markers_path = markers or paths.markers_path()
        self.private_path = private or paths.private_points_path()
        self.favorites_path = favorites or paths.favorites_path()
        self.on_change = on_change
        self._states: dict[str, MarkerState] = {}
        self._pending: dict[str, bool] = {}
        self._private: dict[str, MapPoint] = {}
        self._favorites: list[str] = []
        self.load()

    # ------------------------------------------------------------------ 持久化
    def load(self) -> None:
        raw = _read_json(self.markers_path, {})
        for pid, st in (raw.get("states") or {}).items():
            self._states[str(pid)] = MarkerState.from_dict(st if isinstance(st, dict) else {"done": bool(st)})
        self._pending = {str(k): bool(v) for k, v in (raw.get("pending") or {}).items()}
        self.markers_path.parent.mkdir(parents=True, exist_ok=True)

        praw = _read_json(self.private_path, {})
        for item in praw.get("points") or []:
            try:
                p = MapPoint.from_dict(item)
                p.source = "private"
                self._private[p.id] = p
            except Exception:
                continue

        fr = _read_json(self.favorites_path, {})
        self._favorites = [str(x) for x in (fr.get("routes") or [])]

    def _save_markers(self) -> None:
        _atomic_write(self.markers_path, {
            "version": 1,
            "updated_at": _now(),
            "states": {pid: st.to_dict() for pid, st in self._states.items()},
            "pending": self._pending,
        })

    def _save_private(self) -> None:
        _atomic_write(self.private_path, {
            "version": 1,
            "updated_at": _now(),
            "points": [p.to_dict() for p in self._private.values()],
        })

    def _save_favorites(self) -> None:
        _atomic_write(self.favorites_path, {"version": 1, "routes": self._favorites})

    def _touch(self) -> None:
        if self.on_change:
            self.on_change()

    # ------------------------------------------------------------------ 完成状态
    def state(self, point_id: str) -> MarkerState:
        return self._states.get(point_id) or MarkerState()

    def is_done(self, point_id: str) -> bool:
        st = self._states.get(point_id)
        return bool(st and st.done)

    def all_states(self) -> dict[str, MarkerState]:
        return dict(self._states)

    def set_done(self, point_id: str, done: bool, origin: str = "local",
                 record_pending: bool = True) -> None:
        st = self._states.get(point_id) or MarkerState()
        st.done = bool(done)
        st.updated_at = _now()
        st.origin = origin
        self._states[point_id] = st
        if record_pending and origin != "remote":
            self._pending[point_id] = bool(done)
        else:
            self._pending.pop(point_id, None)
        self._save_markers()
        self._touch()

    def toggle(self, point_id: str) -> bool:
        done = not self.is_done(point_id)
        self.set_done(point_id, done)
        return done

    def set_note(self, point_id: str, note: str) -> None:
        st = self._states.get(point_id) or MarkerState()
        st.note = note
        st.updated_at = _now()
        self._states[point_id] = st
        self._save_markers()
        self._touch()

    def get_note(self, point_id: str) -> str:
        return self._states.get(point_id).note if point_id in self._states else ""

    def counts(self, ids: Iterable[str]) -> tuple[int, int]:
        idset = list(ids)
        done = sum(1 for pid in idset if self.is_done(pid))
        return done, len(idset)

    def reset_all(self) -> None:
        self._states.clear()
        self._pending.clear()
        self._save_markers()
        self._touch()

    # ------------------------------------------------------------------ 待回传队列
    def pending(self) -> dict[str, bool]:
        return dict(self._pending)

    def pending_count(self) -> int:
        return len(self._pending)

    def mark_pushed(self, point_ids: Iterable[str]) -> None:
        for pid in point_ids:
            self._pending.pop(pid, None)
        self._save_markers()
        self._touch()

    # ------------------------------------------------------------------ 云端合并（纯函数式接口）
    def apply_remote(self, remote: dict[str, bool], conflict: str = "newer") -> tuple[int, int]:
        """把云端标记写进本地。返回 (应用条数, 冲突条数)。

        冲突 = 云端与本地不一致、且本地这条改动还没回传出去。
        conflict="newer" 时保留本地（不覆盖 pending 队列里的值），冲突计入并等 push 覆盖云端；
        conflict="remote" 时直接采纳云端、丢弃本地未发送的改动，不计入冲突（否则界面会误报
        「N 条以本地较新为准」）。
        """
        applied = conflicts = 0
        for pid, done in remote.items():
            remote_done = bool(done)
            local = self._states.get(pid)
            if local and local.done == remote_done and pid not in self._pending:
                continue  # 两端一致
            if pid in self._pending and local is not None and conflict == "newer":
                conflicts += 1
                continue  # 本地有未回传的改动，以本地为准
            self._states[pid] = MarkerState(done=remote_done, updated_at=_now(), origin="remote")
            self._pending.pop(pid, None)
            applied += 1
        if applied or conflicts:
            self._save_markers()
            self._touch()
        return applied, conflicts

    # ------------------------------------------------------------------ 私人点位
    def private_points(self) -> list[MapPoint]:
        return list(self._private.values())

    def add_private(self, name: str, x: float, y: float, layer: str = "quest",
                    note: str = "") -> MapPoint:
        pid = f"private_{uuid.uuid4().hex[:10]}"
        p = MapPoint(id=pid, layer=layer, name=name or "私人点位", x=_clamp01(x), y=_clamp01(y),
                     note=note, author="本机", source="private")
        self._private[pid] = p
        self._save_private()
        self._touch()
        return p

    def update_private(self, point_id: str, **fields) -> None:
        p = self._private.get(point_id)
        if not p:
            return
        for k, v in fields.items():
            if hasattr(p, k) and k in {"name", "x", "y", "layer", "note"}:
                setattr(p, k, v)
        self._save_private()
        self._touch()

    def remove_private(self, point_id: str) -> None:
        if self._private.pop(point_id, None):
            self._states.pop(point_id, None)
            self._pending.pop(point_id, None)
            self._save_private()
            self._save_markers()
            self._touch()

    def get_point(self, point_id: str) -> MapPoint | None:
        return self._private.get(point_id)

    # ------------------------------------------------------------------ 收藏路线
    def favorites(self) -> list[str]:
        return list(self._favorites)

    def is_favorite(self, route_id: str) -> bool:
        return route_id in self._favorites

    def toggle_favorite(self, route_id: str) -> bool:
        if route_id in self._favorites:
            self._favorites.remove(route_id)
        else:
            self._favorites.append(route_id)
        self._save_favorites()
        self._touch()
        return route_id in self._favorites
