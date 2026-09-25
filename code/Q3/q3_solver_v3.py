# -*- coding: utf-8 -*-
"""Q3 v3: continuous-interval communication and resource-constrained relay MILP.
Run: python q3_solver_v3.py --root /path/to/data --output ./output_v3
Python >=3.10; numpy scipy>=1.11 openpyxl matplotlib. See README.md.
The outer search is heuristic; MILP bounds apply only to its fixed column pool.
"""
from __future__ import annotations
import argparse
import json
import hashlib
import platform
import heapq
import itertools
from functools import lru_cache
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import coo_matrix
import copy
import csv
import math
import random
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Iterable, Set, Any

import numpy as np
from scipy.io import loadmat
from openpyxl import load_workbook
import matplotlib.pyplot as plt


# ============================================================
# 0. 可统一修改的路径与算法参数
# ============================================================

RANDOM_SEED = 20260924
EARTH_RADIUS_M = 6_371_008.8
G = 9.81
MAX_STOPS_PER_ROUTE = 4               # 控制多点架次长度，便于求解与解释
ALNS_REMOVE_MIN = 4
ALNS_REMOVE_MAX = 9
DEFAULT_ALNS_ITERS = 800

# 最终综合目标权重（均在初始解基准上归一化）
OBJ_WEIGHTS = {
    "lateness": 0.45,
    "makespan": 0.25,
    "energy": 0.20,
    "trips": 0.10,
}

# Repair 局部代理代价：不调用完整 Decoder 时使用
PROXY_ENERGY_W = 1.0
PROXY_DURATION_W = 0.40       # 小时尺度
PROXY_TRIP_FIXED = 0.35
PROXY_TIGHT_SLACK_W = 0.80


# ============================================================
# 1. 数据对象
# ============================================================

@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    lon: float
    lat: float
    ground_alt: float
    work_alt: float


@dataclass(frozen=True)
class Box:
    box_id: str
    service_id: str
    material_type: str
    mass: float
    volume: float
    first_batch: bool
    first_deadline: Optional[float]
    expected_time: float
    priority: float
    hard_deadline: Optional[float]


@dataclass(frozen=True)
class DroneType:
    type_id: str
    name: str
    empty_mass: float
    max_payload: float
    max_volume: float
    cruise_speed: float
    empty_range: float
    full_range: float
    battery_energy: float
    reserve_ratio: float
    prep_time: float
    load_time_per_box: float
    service_base_time: float
    service_time_per_box: float
    climb_speed: float
    descent_speed: float
    climb_efficiency: float
    descent_efficiency: float


@dataclass(frozen=True)
class DroneUnit:
    drone_id: str
    type_id: str


@dataclass(frozen=True)
class BatterySpec:
    type_id: str
    count: int
    full_charge_time: float


@dataclass(frozen=True)
class Arc:
    from_node: str
    to_node: str
    distance_m: float
    dem_max_m: float
    cruise_alt_m: float
    start_work_alt_m: float
    end_work_alt_m: float
    climb_m: float
    descent_m: float
    flight_time_by_type: Dict[str, float]


@dataclass
class Stop:
    service_id: str
    box_ids: List[str]


@dataclass
class Route:
    stops: List[Stop]
    internal_id: int = 0

    def clone(self) -> "Route":
        return Route([Stop(s.service_id, list(s.box_ids)) for s in self.stops], self.internal_id)

    def box_ids(self) -> List[str]:
        return [b for s in self.stops for b in s.box_ids]

    def service_ids(self) -> List[str]:
        return [s.service_id for s in self.stops]

    def signature(self) -> Tuple:
        return tuple((s.service_id, tuple(sorted(s.box_ids))) for s in self.stops)

    def normalize(self) -> None:
        """合并同服务区 Stop、删除空 Stop，保持首次出现顺序。"""
        order: List[str] = []
        acc: Dict[str, List[str]] = defaultdict(list)
        for s in self.stops:
            if s.service_id not in acc:
                order.append(s.service_id)
            acc[s.service_id].extend(s.box_ids)
        self.stops = [Stop(sid, sorted(set(acc[sid]))) for sid in order if acc[sid]]


@dataclass
class LegEval:
    from_node: str
    to_node: str
    payload_mass: float
    payload_volume: float
    distance_m: float
    climb_m: float
    descent_m: float
    flight_time: float
    energy_kwh: float


@dataclass
class StopEval:
    service_id: str
    box_ids: List[str]
    delivery_mass: float
    delivery_volume: float
    arrival_offset: float
    service_duration: float
    delivery_offset: float


@dataclass
class RouteEval:
    feasible: bool
    reason: str
    drone_type: str
    total_boxes: int = 0
    total_mass: float = 0.0
    total_volume: float = 0.0
    pre_operation_time: float = 0.0
    takeoff_offset: float = 0.0
    duration: float = 0.0
    total_energy: float = 0.0
    end_soc: float = 0.0
    latest_start: float = math.inf
    soft_due_start: float = math.inf
    stops: List[StopEval] = field(default_factory=list)
    legs: List[LegEval] = field(default_factory=list)
    box_delivery_offsets: Dict[str, float] = field(default_factory=dict)


@dataclass
class BatteryState:
    battery_id: str
    type_id: str
    available_time: float = 0.0


@dataclass
class DroneState:
    drone_id: str
    type_id: str
    available_time: float = 0.0


@dataclass
class ScheduledTrip:
    route_index: int
    drone_id: str
    drone_type: str
    battery_id: str
    start_time: float
    takeoff_time: float
    return_time: float
    energy_kwh: float
    end_soc: float
    charge_end_time: float
    service_sequence: List[str]
    box_delivery: Dict[str, float]
    route_eval: RouteEval
    trip_id: str = ""


@dataclass
class DecodeResult:
    feasible: bool
    reason: str
    trips: List[ScheduledTrip] = field(default_factory=list)
    box_delivery: Dict[str, float] = field(default_factory=dict)
    bottleneck_routes: List[int] = field(default_factory=list)


@dataclass
class Metrics:
    weighted_lateness: float
    makespan: float
    energy: float
    trips: int
    hard_violations: int


# ============================================================
# 2. 文件定位与 Excel 读取
# ============================================================

def resolve_input_file(root: Path, base_name: str, suffix: str) -> Path:
    """优先按正式文件名查找；找不到时允许附件名带 (3)/(4) 等后缀。"""
    exact = list(root.rglob(base_name + suffix))
    if exact:
        return exact[0]
    candidates = [p for p in root.rglob(f"{base_name}*{suffix}") if "output" not in {x.lower() for x in p.parts}]
    if not candidates:
        raise FileNotFoundError(f"未找到文件：{base_name}{suffix}，搜索根目录：{root}")
    candidates.sort(key=lambda p: (len(p.name), len(str(p))))
    return candidates[0]


def cell_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    return float(v)


def load_nodes(path: Path) -> Dict[str, Node]:
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    nodes: Dict[str, Node] = {}

    # O01 固定在第 3 行，服务区从第 7 行起；同时做字符串识别，避免模板轻微变化。
    for row in ws.iter_rows(values_only=True):
        nid = str(row[0]).strip() if row and row[0] else ""
        if nid == "O01" or (nid.startswith("S") and len(nid) == 4):
            name = str(row[1]) if row[1] is not None else nid
            lon, lat, alt = float(row[2]), float(row[3]), float(row[4])
            work_alt = alt if nid == "O01" else alt + 30.0
            nodes[nid] = Node(nid, name, lon, lat, alt, work_alt)
    wb.close()
    if "O01" not in nodes or len(nodes) != 16:
        raise ValueError(f"节点读取异常：共读取 {len(nodes)} 个节点，应为 16 个（O01+15 服务区）")
    return nodes


def load_boxes(path: Path) -> Dict[str, Box]:
    wb = load_workbook(path, data_only=True, read_only=True)
    if "逐箱货箱清单" not in wb.sheetnames:
        raise ValueError("物资需求文件中未找到 '逐箱货箱清单' Sheet")
    ws = wb["逐箱货箱清单"]
    boxes: Dict[str, Box] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        box_id = str(row[0]).strip()
        sid = str(row[1]).strip()
        material = str(row[2]).strip()
        mass = float(row[3])
        volume = float(row[4])
        first = str(row[5]).strip() == "是"
        first_deadline = cell_float(row[6])
        expected = float(row[7])
        priority = float(row[8])

        # 题意口径：医疗物资的期望送达时间作为硬要求；首批箱满足首批截止时间。
        hard_candidates: List[float] = []
        if first and first_deadline is not None:
            hard_candidates.append(first_deadline)
        if material == "医疗物资":
            hard_candidates.append(expected)
        hard_deadline = min(hard_candidates) if hard_candidates else None

        boxes[box_id] = Box(
            box_id, sid, material, mass, volume, first, first_deadline,
            expected, priority, hard_deadline
        )
    wb.close()
    if len(boxes) != 80:
        raise ValueError(f"逐箱货箱读取异常：读取 {len(boxes)} 箱，应为 80 箱")
    return boxes


def load_drone_data(path: Path) -> Tuple[Dict[str, DroneType], List[DroneUnit], Dict[str, BatterySpec]]:
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]

    types: Dict[str, DroneType] = {}
    for r in range(3, 6):
        vals = [ws.cell(r, c).value for c in range(1, 19)]
        tid = str(vals[0]).strip()
        types[tid] = DroneType(
            type_id=tid,
            name=str(vals[1]),
            empty_mass=float(vals[2]),
            max_payload=float(vals[3]),
            max_volume=float(vals[4]),
            cruise_speed=float(vals[5]),
            empty_range=float(vals[6]),
            full_range=float(vals[7]),
            battery_energy=float(vals[8]),
            reserve_ratio=float(vals[9]) / 100.0,
            prep_time=float(vals[10]),
            load_time_per_box=float(vals[11]),
            service_base_time=float(vals[12]),
            service_time_per_box=float(vals[13]),
            climb_speed=float(vals[14]),
            descent_speed=float(vals[15]),
            climb_efficiency=float(vals[16]),
            descent_efficiency=float(vals[17]),
        )

    drones: List[DroneUnit] = []
    for r in range(9, 17):
        did = ws.cell(r, 1).value
        if did:
            drones.append(DroneUnit(str(did).strip(), str(ws.cell(r, 2).value).strip()))

    batteries: Dict[str, BatterySpec] = {}
    for r in range(20, 23):
        tid = str(ws.cell(r, 1).value).strip()
        batteries[tid] = BatterySpec(tid, int(ws.cell(r, 2).value), float(ws.cell(r, 3).value))
    wb.close()
    return types, drones, batteries


# ============================================================
# 3. DEM 与 Arc Library
# ============================================================

def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def supercover_cells(r0: int, c0: int, r1: int, c1: int) -> List[Tuple[int, int]]:
    """整数栅格上的 supercover 直线，包含直线穿过/接触到的像元。"""
    x0, y0, x1, y1 = c0, r0, c1, r1
    dx, dy = x1 - x0, y1 - y0
    nx, ny = abs(dx), abs(dy)
    sx = 0 if dx == 0 else (1 if dx > 0 else -1)
    sy = 0 if dy == 0 else (1 if dy > 0 else -1)
    x, y = x0, y0
    cells = {(y, x)}
    ix = iy = 0
    while ix < nx or iy < ny:
        decision = (1 + 2 * ix) * ny - (1 + 2 * iy) * nx
        if decision == 0:
            # 穿过网格角点时把两侧像元也纳入 supercover
            if ix < nx:
                cells.add((y, x + sx))
            if iy < ny:
                cells.add((y + sy, x))
            if ix < nx:
                x += sx; ix += 1
            if iy < ny:
                y += sy; iy += 1
        elif decision < 0:
            x += sx; ix += 1
        else:
            y += sy; iy += 1
        cells.add((y, x))
    return list(cells)


class ArcLibrary:
    def __init__(self, nodes: Dict[str, Node], drone_types: Dict[str, DroneType], dem_path: Path):
        self.nodes = nodes
        self.drone_types = drone_types
        mat = loadmat(dem_path)
        self.dem = np.asarray(mat["dem"], dtype=float)
        self.lat = np.asarray(mat["latitude"], dtype=float).reshape(-1)
        self.lon = np.asarray(mat["longitude"], dtype=float).reshape(-1)
        nodata = mat.get("nodata", np.array([[-32767.0]]))
        self.nodata = float(np.asarray(nodata).reshape(-1)[0])
        self.arcs: Dict[Tuple[str, str], Arc] = {}
        self._node_rc: Dict[str, Tuple[int, int]] = {}
        self._prepare_indices()
        self._build()

    def _prepare_indices(self) -> None:
        lon_min, lon_max = float(self.lon.min()), float(self.lon.max())
        lat_min, lat_max = float(self.lat.min()), float(self.lat.max())
        for nid, n in self.nodes.items():
            if not (lon_min <= n.lon <= lon_max and lat_min <= n.lat <= lat_max):
                raise ValueError(f"节点 {nid} 超出 DEM 范围")
            r = int(np.argmin(np.abs(self.lat - n.lat)))
            c = int(np.argmin(np.abs(self.lon - n.lon)))
            self._node_rc[nid] = (r, c)

    def _build(self) -> None:
        ids = sorted(self.nodes.keys(), key=lambda x: (x != "O01", x))
        pair_geo: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                na, nb = self.nodes[a], self.nodes[b]
                dist = haversine_m(na.lon, na.lat, nb.lon, nb.lat)
                ra, ca = self._node_rc[a]
                rb, cb = self._node_rc[b]
                cells = supercover_cells(ra, ca, rb, cb)
                vals = []
                for r, c in cells:
                    if 0 <= r < self.dem.shape[0] and 0 <= c < self.dem.shape[1]:
                        v = self.dem[r, c]
                        if np.isfinite(v) and abs(v - self.nodata) > 1e-6:
                            vals.append(float(v))
                if not vals:
                    raise ValueError(f"航段 {a}-{b} 没有有效 DEM 像元")
                pair_geo[(a, b)] = (dist, max(vals))

        for (a, b), (dist, dem_max) in pair_geo.items():
            for u, v in [(a, b), (b, a)]:
                nu, nv = self.nodes[u], self.nodes[v]
                cruise_alt = dem_max + 50.0
                climb = max(0.0, cruise_alt - nu.work_alt)
                descent = max(0.0, cruise_alt - nv.work_alt)
                times = {}
                for tid, dt in self.drone_types.items():
                    times[tid] = climb / dt.climb_speed + dist / dt.cruise_speed + descent / dt.descent_speed
                self.arcs[(u, v)] = Arc(
                    u, v, dist, dem_max, cruise_alt,
                    nu.work_alt, nv.work_alt, climb, descent, times
                )

        # 基本一致性检查
        for (a, b), arc in list(self.arcs.items()):
            rev = self.arcs[(b, a)]
            if abs(arc.distance_m - rev.distance_m) > 1e-6 or abs(arc.dem_max_m - rev.dem_max_m) > 1e-6:
                raise AssertionError(f"Arc 对称性检查失败：{a}<->{b}")

    def __getitem__(self, key: Tuple[str, str]) -> Arc:
        return self.arcs[key]

    def export_csv(self, path: Path) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["from", "to", "distance_m", "dem_max_m", "cruise_alt_m", "climb_m", "descent_m", "time_A", "time_B", "time_C"])
            for (a, b), arc in sorted(self.arcs.items()):
                w.writerow([
                    a, b, arc.distance_m, arc.dem_max_m, arc.cruise_alt_m,
                    arc.climb_m, arc.descent_m,
                    arc.flight_time_by_type.get("A", ""),
                    arc.flight_time_by_type.get("B", ""),
                    arc.flight_time_by_type.get("C", ""),
                ])


# ============================================================
# 4. Route Evaluator
# ============================================================

class RouteEvaluator:
    def __init__(self, boxes: Dict[str, Box], drone_types: Dict[str, DroneType], arcs: ArcLibrary):
        self.boxes = boxes
        self.drone_types = drone_types
        self.arcs = arcs
        self.cache: Dict[Tuple[Tuple, str], RouteEval] = {}

    @staticmethod
    def equivalent_range(dt: DroneType, payload: float) -> float:
        ratio = min(max(payload / dt.max_payload, 0.0), 1.0)
        return dt.empty_range - (dt.empty_range - dt.full_range) * (ratio ** 1.5)

    def leg_energy(self, dt: DroneType, arc: Arc, payload: float) -> float:
        """
        计算约定：
        1) 水平能耗 = 电池可用能量 × 水平距离 / 当前载荷等效航程；
        2) 爬升附加能耗 = m g h / eta，换算为 kWh；
        3) 下降附加能耗效率为 0 时不额外计算下降能耗。
        """
        rng = self.equivalent_range(dt, payload)
        if rng <= 0:
            return math.inf
        e_hor = dt.battery_energy * arc.distance_m / rng
        total_mass = dt.empty_mass + payload
        e_up = total_mass * G * arc.climb_m / max(dt.climb_efficiency, 1e-9) / 3.6e6
        return e_hor + e_up

    def evaluate(self, route: Route, type_id: str) -> RouteEval:
        route.normalize()
        sig = (route.signature(), type_id)
        if sig in self.cache:
            return self.cache[sig]
        dt = self.drone_types[type_id]

        # ---------- 结构合法性 ----------
        if not route.stops:
            out = RouteEval(False, "EMPTY_ROUTE", type_id)
            self.cache[sig] = out; return out
        if len(route.stops) > MAX_STOPS_PER_ROUTE:
            out = RouteEval(False, "TOO_MANY_STOPS", type_id)
            self.cache[sig] = out; return out
        service_ids = [s.service_id for s in route.stops]
        if len(service_ids) != len(set(service_ids)):
            out = RouteEval(False, "DUPLICATE_SERVICE", type_id)
            self.cache[sig] = out; return out
        all_boxes = route.box_ids()
        if len(all_boxes) != len(set(all_boxes)):
            out = RouteEval(False, "DUPLICATE_BOX", type_id)
            self.cache[sig] = out; return out
        for s in route.stops:
            if not s.box_ids:
                out = RouteEval(False, "EMPTY_STOP", type_id)
                self.cache[sig] = out; return out
            for bid in s.box_ids:
                if bid not in self.boxes or self.boxes[bid].service_id != s.service_id:
                    out = RouteEval(False, "BOX_DESTINATION_MISMATCH", type_id)
                    self.cache[sig] = out; return out

        stop_mass = [sum(self.boxes[b].mass for b in s.box_ids) for s in route.stops]
        stop_vol = [sum(self.boxes[b].volume for b in s.box_ids) for s in route.stops]
        total_mass, total_vol = sum(stop_mass), sum(stop_vol)
        nbox = len(all_boxes)
        if total_mass > dt.max_payload + 1e-9:
            out = RouteEval(False, "MASS_CAPACITY_EXCEEDED", type_id, nbox, total_mass, total_vol)
            self.cache[sig] = out; return out
        if total_vol > dt.max_volume + 1e-12:
            out = RouteEval(False, "VOLUME_CAPACITY_EXCEEDED", type_id, nbox, total_mass, total_vol)
            self.cache[sig] = out; return out

        # 后缀载荷
        k = len(route.stops)
        suffix_mass = [0.0] * (k + 1)
        suffix_vol = [0.0] * (k + 1)
        for i in range(k - 1, -1, -1):
            suffix_mass[i] = suffix_mass[i + 1] + stop_mass[i]
            suffix_vol[i] = suffix_vol[i + 1] + stop_vol[i]

        pre = dt.prep_time + nbox * dt.load_time_per_box
        current_time = pre
        stop_evals: List[StopEval] = []
        leg_evals: List[LegEval] = []
        total_energy = 0.0
        box_offsets: Dict[str, float] = {}

        nodes = ["O01"] + service_ids + ["O01"]
        # 去程 + 服务
        for i, stop in enumerate(route.stops):
            a, b = nodes[i], nodes[i + 1]
            arc = self.arcs[(a, b)]
            payload = suffix_mass[i]
            pvol = suffix_vol[i]
            ft = arc.flight_time_by_type[type_id]
            en = self.leg_energy(dt, arc, payload)
            current_time += ft
            arrival = current_time
            service_time = dt.service_base_time + len(stop.box_ids) * dt.service_time_per_box
            current_time += service_time
            delivery = current_time
            for bid in stop.box_ids:
                box_offsets[bid] = delivery
            total_energy += en
            leg_evals.append(LegEval(a, b, payload, pvol, arc.distance_m, arc.climb_m, arc.descent_m, ft, en))
            stop_evals.append(StopEval(stop.service_id, list(stop.box_ids), stop_mass[i], stop_vol[i], arrival, service_time, delivery))

        # 返程：空载
        a, b = nodes[-2], "O01"
        arc = self.arcs[(a, b)]
        ft = arc.flight_time_by_type[type_id]
        en = self.leg_energy(dt, arc, 0.0)
        current_time += ft
        total_energy += en
        leg_evals.append(LegEval(a, b, 0.0, 0.0, arc.distance_m, arc.climb_m, arc.descent_m, ft, en))

        end_soc = 1.0 - total_energy / dt.battery_energy
        if end_soc + 1e-12 < dt.reserve_ratio:
            out = RouteEval(
                False, "ENERGY_RESERVE_VIOLATION", type_id, nbox, total_mass, total_vol,
                pre, pre, current_time, total_energy, end_soc,
                stops=stop_evals, legs=leg_evals, box_delivery_offsets=box_offsets
            )
            self.cache[sig] = out; return out

        hard_limits = []
        soft_limits = []
        for bid, off in box_offsets.items():
            bobj = self.boxes[bid]
            if bobj.hard_deadline is not None:
                hard_limits.append(bobj.hard_deadline - off)
            soft_limits.append(bobj.expected_time - off)
        latest = min(hard_limits) if hard_limits else math.inf
        soft_due = min(soft_limits) if soft_limits else math.inf
        if latest < -1e-9:
            out = RouteEval(
                False, "HARD_DEADLINE_IMPOSSIBLE", type_id, nbox, total_mass, total_vol,
                pre, pre, current_time, total_energy, end_soc,
                latest, soft_due, stop_evals, leg_evals, box_offsets
            )
            self.cache[sig] = out; return out

        out = RouteEval(
            True, "OK", type_id, nbox, total_mass, total_vol,
            pre, pre, current_time, total_energy, end_soc,
            latest, soft_due, stop_evals, leg_evals, box_offsets
        )
        self.cache[sig] = out
        return out

    def feasible_types(self, route: Route) -> Dict[str, RouteEval]:
        out = {}
        for tid in self.drone_types:
            ev = self.evaluate(route, tid)
            if ev.feasible:
                out[tid] = ev
        return out

    def proxy_cost(self, route: Route) -> float:
        fes = self.feasible_types(route)
        if not fes:
            return math.inf
        vals = []
        for ev in fes.values():
            tight = 0.0
            if math.isfinite(ev.latest_start):
                tight = max(0.0, 600.0 - ev.latest_start) / 600.0
            vals.append(
                PROXY_ENERGY_W * ev.total_energy
                + PROXY_DURATION_W * (ev.duration / 3600.0)
                + PROXY_TRIP_FIXED
                + PROXY_TIGHT_SLACK_W * tight
            )
        return min(vals)


# ============================================================
# 5. 路线构造与 Regret-2 Repair
# ============================================================

def ensure_route_integrity(routes: List[Route], boxes: Dict[str, Box], allow_missing: bool = False) -> None:
    seen: List[str] = []
    for r in routes:
        r.normalize()
        for s in r.stops:
            for b in s.box_ids:
                if boxes[b].service_id != s.service_id:
                    raise AssertionError(f"货箱 {b} 被挂到错误服务区 {s.service_id}")
                seen.append(b)
    if len(seen) != len(set(seen)):
        raise AssertionError("Solution 中存在重复货箱")
    if not allow_missing and set(seen) != set(boxes):
        miss = set(boxes) - set(seen)
        extra = set(seen) - set(boxes)
        raise AssertionError(f"货箱完整性失败：missing={sorted(miss)}, extra={sorted(extra)}")


def route_from_box(box: Box) -> Route:
    return Route([Stop(box.service_id, [box.box_id])])


def insert_box_into_route(route: Route, box: Box, position: Optional[int]) -> Route:
    nr = route.clone()
    # 同服务区优先直接挂载，不增加访问点
    for s in nr.stops:
        if s.service_id == box.service_id:
            s.box_ids.append(box.box_id)
            nr.normalize()
            return nr
    if position is None:
        position = len(nr.stops)
    nr.stops.insert(position, Stop(box.service_id, [box.box_id]))
    nr.normalize()
    return nr


def best_order_for_stops(route: Route, evaluator: RouteEvaluator, arc_lib: ArcLibrary) -> Route:
    """至多7站枚举顺序；较长路线用反转邻域，避免阶乘规模。"""
    import itertools
    if len(route.stops) <= 1:
        return route.clone()
    best = route.clone()
    best_c = evaluator.proxy_cost(best)
    if len(route.stops)<=7:perms=itertools.permutations(route.stops)
    else:
        perms=(route.stops[:a]+list(reversed(route.stops[a:b]))+route.stops[b:]
               for a in range(len(route.stops)) for b in range(a+2,len(route.stops)+1))
    for perm in perms:
        nr = Route([Stop(s.service_id, list(s.box_ids)) for s in perm])
        c = evaluator.proxy_cost(nr)
        if c < best_c - 1e-10:
            best, best_c = nr, c
    return best


def construct_initial_routes(boxes: Dict[str, Box], evaluator: RouteEvaluator) -> List[Route]:
    """
    初始解（以“先保证硬时限可调度”为第一原则）：
    1) 每个服务区的硬时限箱形成独立紧急种子，不往紧急种子中塞普通箱，
       避免原本 A/B 可执行的紧急任务因增载而被迫占用稀缺的 C 型机；
    2) 普通箱按服务区单独进行容量装箱；
    3) 只对“不含硬时限箱”的普通路线进行跨服务区贪心 merge。

    这样得到的初始解通常架次数略多，但资源 Decoder 更容易首先得到全局可行解；
    后续 ALNS 再负责把普通任务、甚至部分有裕量的任务重新合并。
    """
    by_service: Dict[str, List[Box]] = defaultdict(list)
    for b in boxes.values():
        by_service[b.service_id].append(b)

    routes: List[Route] = []
    assigned: Set[str] = set()

    # I. 硬时限种子：严格保持轻载，优先保证 A/B/C 机型选择灵活性
    services = sorted(by_service)
    for sid in services:
        hard = sorted(
            [b for b in by_service[sid] if b.hard_deadline is not None],
            key=lambda b: (b.hard_deadline, -b.priority, b.box_id)
        )
        if not hard:
            continue
        r = Route([Stop(sid, [b.box_id for b in hard])])
        if evaluator.feasible_types(r):
            routes.append(r)
            assigned.update(r.box_ids())
        else:
            # 极端情况下拆成单箱，确保不会因为组批导致物理不可行
            for b in hard:
                rb = route_from_box(b)
                if not evaluator.feasible_types(rb):
                    raise RuntimeError(f"单箱硬时限任务都不可行：{b.box_id}")
                routes.append(rb)
                assigned.add(b.box_id)

    # II. 普通箱：在同一服务区内做顺序装箱；不占用硬时限种子的容量
    for sid in services:
        soft = sorted(
            [b for b in by_service[sid] if b.box_id not in assigned],
            key=lambda b: (b.expected_time, -b.priority, -b.mass, b.box_id)
        )
        current: Optional[Route] = None
        for b in soft:
            if current is None:
                current = route_from_box(b)
                continue
            trial = insert_box_into_route(current, b, None)
            if evaluator.feasible_types(trial):
                current = trial
            else:
                routes.append(current)
                current = route_from_box(b)
        if current is not None:
            routes.append(current)

    ensure_route_integrity(routes, boxes)

    def contains_hard(r: Route) -> bool:
        return any(boxes[bid].hard_deadline is not None for bid in r.box_ids())

    # III. 仅合并普通路线，减少架次；紧急种子先不碰，确保初始 Decoder 稳定可行
    improved = True
    while improved:
        improved = False
        best_merge = None
        for i in range(len(routes)):
            if contains_hard(routes[i]):
                continue
            for j in range(i + 1, len(routes)):
                if contains_hard(routes[j]):
                    continue
                ra, rb = routes[i], routes[j]
                service_union = set(ra.service_ids()) | set(rb.service_ids())
                if len(service_union) > MAX_STOPS_PER_ROUTE:
                    continue
                merged_map: Dict[str, List[str]] = defaultdict(list)
                order = []
                for r in (ra, rb):
                    for s in r.stops:
                        if s.service_id not in merged_map:
                            order.append(s.service_id)
                        merged_map[s.service_id].extend(s.box_ids)
                merged = Route([Stop(sid, merged_map[sid]) for sid in order])
                merged = best_order_for_stops(merged, evaluator, evaluator.arcs)
                cm = evaluator.proxy_cost(merged)
                if not math.isfinite(cm):
                    continue
                old = evaluator.proxy_cost(ra) + evaluator.proxy_cost(rb)
                saving = old - cm
                if saving > 0.02 and (best_merge is None or saving > best_merge[0]):
                    best_merge = (saving, i, j, merged)
        if best_merge:
            _, i, j, merged = best_merge
            routes[i] = merged
            routes.pop(j)
            improved = True

    ensure_route_integrity(routes, boxes)
    return routes

def remove_boxes_from_routes(routes: List[Route], remove_ids: Set[str]) -> List[Route]:
    out = []
    for r in routes:
        nr = r.clone()
        for s in nr.stops:
            s.box_ids = [b for b in s.box_ids if b not in remove_ids]
        nr.normalize()
        if nr.stops:
            out.append(nr)
    return out


def enumerate_insert_options(routes: List[Route], box: Box, evaluator: RouteEvaluator) -> List[Tuple[float, int, Route]]:
    opts: List[Tuple[float, int, Route]] = []
    # 现有路线
    for i, r in enumerate(routes):
        old = evaluator.proxy_cost(r)
        if box.service_id in r.service_ids():
            nr = insert_box_into_route(r, box, None)
            c = evaluator.proxy_cost(nr)
            if math.isfinite(c):
                opts.append((c - old, i, nr))
        elif len(r.stops) < MAX_STOPS_PER_ROUTE:
            for pos in range(len(r.stops) + 1):
                nr = insert_box_into_route(r, box, pos)
                c = evaluator.proxy_cost(nr)
                if math.isfinite(c):
                    opts.append((c - old, i, nr))
    # 新路线，index == len(routes)
    nr = route_from_box(box)
    c = evaluator.proxy_cost(nr)
    if math.isfinite(c):
        opts.append((c, len(routes), nr))
    opts.sort(key=lambda x: x[0])
    return opts


def regret2_repair(routes: List[Route], unassigned_ids: List[str], boxes: Dict[str, Box], evaluator: RouteEvaluator) -> Optional[List[Route]]:
    routes = [r.clone() for r in routes]
    pool = set(unassigned_ids)
    while pool:
        best_choice = None
        for bid in list(pool):
            b = boxes[bid]
            opts = enumerate_insert_options(routes, b, evaluator)
            if not opts:
                return None
            best_cost = opts[0][0]
            second = opts[1][0] if len(opts) >= 2 else best_cost + 10.0
            regret = second - best_cost
            # 硬时限/早期望箱在 regret 相同情况下优先
            urgency = (b.hard_deadline if b.hard_deadline is not None else b.expected_time)
            key = (regret, -best_cost, -b.priority, -1.0 / max(urgency, 1.0))
            if best_choice is None or key > best_choice[0]:
                best_choice = (key, bid, opts[0])
        _, bid, (cost, idx, nr) = best_choice
        if idx == len(routes):
            routes.append(nr)
        else:
            routes[idx] = nr
        pool.remove(bid)
    return routes


# ============================================================
# 6. Resource Decoder
# ============================================================

def charge_time_seconds(soc: float, full_charge_time: float) -> float:
    soc = min(max(soc, 0.0), 1.0)
    if soc >= 1.0 - 1e-12:
        return 0.0
    if soc >= 0.9:
        return full_charge_time * 0.35 * (1.0 - soc) / 0.1
    return full_charge_time * (0.65 * (0.9 - soc) / 0.9 + 0.35)


class ResourceDecoder:
    def __init__(
        self,
        evaluator: RouteEvaluator,
        drone_units: List[DroneUnit],
        batteries: Dict[str, BatterySpec],
        boxes: Dict[str, Box],
    ):
        self.evaluator = evaluator
        self.drone_units = drone_units
        self.battery_specs = batteries
        self.boxes = boxes
        self.route_release: Dict[int, float] = {}

    def set_route_release(self, release: Optional[Dict[int, float]] = None) -> None:
        self.route_release = dict(release or {})

    def decode(self, routes: List[Route]) -> DecodeResult:
        drones: Dict[str, DroneState] = {
            d.drone_id: DroneState(d.drone_id, d.type_id, 0.0) for d in self.drone_units
        }
        battery_states: Dict[str, BatteryState] = {}
        for tid, spec in self.battery_specs.items():
            for i in range(1, spec.count + 1):
                bid = f"BAT-{tid}{i:02d}"
                battery_states[bid] = BatteryState(bid, tid, 0.0)

        unscheduled = set(range(len(routes)))
        trips: List[ScheduledTrip] = []

        # 每条路线的三机型评价一次性取好
        route_evals: Dict[int, Dict[str, RouteEval]] = {}
        for i, r in enumerate(routes):
            fes = self.evaluator.feasible_types(r)
            if not fes:
                return DecodeResult(False, f"ROUTE_{i}_NO_FEASIBLE_TYPE", bottleneck_routes=[i])
            route_evals[i] = fes

        while unscheduled:
            route_choices = []
            for ri in sorted(unscheduled):
                type_options = []
                r = routes[ri]
                for tid, ev in route_evals[ri].items():
                    ds = [d for d in drones.values() if d.type_id == tid]
                    bs = [b for b in battery_states.values() if b.type_id == tid]
                    if not ds or not bs:
                        continue
                    d = min(ds, key=lambda x: (x.available_time, x.drone_id))
                    b = min(bs, key=lambda x: (x.available_time, x.battery_id))
                    start = max(d.available_time, b.available_time, self.route_release.get(ri, 0.0))
                    finish = start + ev.duration
                    deadline_ok = (not math.isfinite(ev.latest_start)) or start <= ev.latest_start + 1e-9
                    hard_slack = (ev.latest_start - start) if math.isfinite(ev.latest_start) else math.inf
                    soft_slack = ev.soft_due_start - start
                    # 类型评分：先按时，再早完成，再低能耗；轻任务不强制占 C
                    capacity_ratio = ev.total_mass / max(self.evaluator.drone_types[tid].max_payload, 1e-9)
                    type_score = finish + 80.0 * ev.total_energy + 60.0 * max(0.0, 0.25 - capacity_ratio)
                    type_options.append((deadline_ok, type_score, tid, d, b, start, finish, ev, hard_slack, soft_slack))

                feasible_now = [x for x in type_options if x[0]]
                if feasible_now:
                    # 路线紧迫度：硬时限优先，其次软期望
                    best_opt = min(feasible_now, key=lambda x: x[1])
                    has_hard = math.isfinite(best_opt[7].latest_start)
                    priority_key = (
                        0 if has_hard else 1,
                        best_opt[8] if has_hard else best_opt[9],
                        best_opt[6],
                    )
                    route_choices.append((priority_key, ri, best_opt))
                else:
                    # 当前资源状态下没有机型能满足硬最晚开始，但可能是排序导致；记录为危急候选
                    if type_options:
                        best_bad = min(type_options, key=lambda x: (x[8], x[1]))
                        route_choices.append((( -1, best_bad[8], best_bad[6]), ri, best_bad))

            if not route_choices:
                return DecodeResult(False, "NO_SCHEDULABLE_ROUTE", trips=trips, bottleneck_routes=sorted(unscheduled))

            route_choices.sort(key=lambda x: x[0])
            _, ri, opt = route_choices[0]
            deadline_ok, _, tid, d, b, start, finish, ev, hard_slack, soft_slack = opt
            if not deadline_ok:
                # 当前 EDF/LST 列表调度失败。返回关键路线，交给 ALNS 改结构。
                critical = [x[1] for x in route_choices[:min(5, len(route_choices))]]
                return DecodeResult(False, "GLOBAL_SCHEDULE_INFEASIBLE_HARD_DEADLINE", trips=trips, bottleneck_routes=critical)

            delivery = {bid: start + off for bid, off in ev.box_delivery_offsets.items()}
            spec = self.battery_specs[tid]
            chg = charge_time_seconds(ev.end_soc, spec.full_charge_time)
            charge_end = finish + chg
            trip = ScheduledTrip(
                route_index=ri,
                drone_id=d.drone_id,
                drone_type=tid,
                battery_id=b.battery_id,
                start_time=start,
                takeoff_time=start + ev.takeoff_offset,
                return_time=finish,
                energy_kwh=ev.total_energy,
                end_soc=ev.end_soc,
                charge_end_time=charge_end,
                service_sequence=routes[ri].service_ids(),
                box_delivery=delivery,
                route_eval=ev,
            )
            trips.append(trip)
            d.available_time = finish
            b.available_time = charge_end
            unscheduled.remove(ri)

        # 最终按开始时间编号，避免优化过程内部 ID 混乱
        trips.sort(key=lambda t: (t.start_time, t.drone_id, t.return_time))
        box_delivery: Dict[str, float] = {}
        for idx, t in enumerate(trips, start=1):
            t.trip_id = f"Q3T{idx:03d}"
            for bid, tm in t.box_delivery.items():
                if bid in box_delivery:
                    return DecodeResult(False, "DUPLICATE_BOX_AFTER_DECODING", trips=trips)
                box_delivery[bid] = tm
        return DecodeResult(True, "OK", trips, box_delivery)


# ============================================================
# 7. 指标、校验与 ALNS
# ============================================================

def compute_metrics(decoded: DecodeResult, boxes: Dict[str, Box]) -> Metrics:
    if not decoded.feasible:
        return Metrics(math.inf, math.inf, math.inf, 10**9, 10**9)
    late = 0.0
    hard_v = 0
    for bid, b in boxes.items():
        t = decoded.box_delivery[bid]
        late += b.priority * max(0.0, t - b.expected_time) / max(b.expected_time, 1.0)
        if b.hard_deadline is not None and t > b.hard_deadline + 1e-6:
            hard_v += 1
    makespan = max((t.return_time for t in decoded.trips), default=0.0)
    energy = sum(t.energy_kwh for t in decoded.trips)
    return Metrics(late, makespan, energy, len(decoded.trips), hard_v)


def normalized_score(m: Metrics, base: Metrics) -> float:
    if m.hard_violations > 0 or not all(math.isfinite(x) for x in [m.weighted_lateness, m.makespan, m.energy]):
        return math.inf
    late_scale = max(base.weighted_lateness, 1.0)
    makespan_scale = max(base.makespan, 1.0)
    energy_scale = max(base.energy, 1e-6)
    trip_scale = max(base.trips, 1)
    return (
        OBJ_WEIGHTS["lateness"] * m.weighted_lateness / late_scale
        + OBJ_WEIGHTS["makespan"] * m.makespan / makespan_scale
        + OBJ_WEIGHTS["energy"] * m.energy / energy_scale
        + OBJ_WEIGHTS["trips"] * m.trips / trip_scale
    )


def validate_final_solution(routes: List[Route], decoded: DecodeResult, boxes: Dict[str, Box], drone_types: Dict[str, DroneType]) -> None:
    ensure_route_integrity(routes, boxes)
    if not decoded.feasible:
        raise AssertionError(f"Decoder 不可行：{decoded.reason}")
    if set(decoded.box_delivery) != set(boxes):
        raise AssertionError("最终逐箱交付不完整")
    # 硬时限
    for bid, b in boxes.items():
        if b.hard_deadline is not None and decoded.box_delivery[bid] > b.hard_deadline + 1e-6:
            raise AssertionError(f"硬时限违反：{bid}, t={decoded.box_delivery[bid]:.2f}, ddl={b.hard_deadline}")
    # 无人机占用不重叠
    by_drone = defaultdict(list)
    by_bat = defaultdict(list)
    for t in decoded.trips:
        by_drone[t.drone_id].append((t.start_time, t.return_time, t.trip_id))
        by_bat[t.battery_id].append((t.start_time, t.charge_end_time, t.trip_id))
        if t.end_soc + 1e-9 < drone_types[t.drone_type].reserve_ratio:
            raise AssertionError(f"返航 SOC 不足：{t.trip_id}")
    for did, ints in by_drone.items():
        ints.sort()
        for a, b in zip(ints, ints[1:]):
            if a[1] > b[0] + 1e-7:
                raise AssertionError(f"无人机冲突 {did}: {a} vs {b}")
    for bid, ints in by_bat.items():
        ints.sort()
        for a, b in zip(ints, ints[1:]):
            if a[1] > b[0] + 1e-7:
                raise AssertionError(f"电池占用/充电冲突 {bid}: {a} vs {b}")


def destroy_random(routes: List[Route], q: int, rng: random.Random) -> Tuple[List[Route], List[str]]:
    all_ids = [b for r in routes for b in r.box_ids()]
    q = min(q, len(all_ids))
    rem = set(rng.sample(all_ids, q))
    return remove_boxes_from_routes(routes, rem), list(rem)


def destroy_related(routes: List[Route], q: int, boxes: Dict[str, Box], nodes: Dict[str, Node], rng: random.Random) -> Tuple[List[Route], List[str]]:
    all_ids = [b for r in routes for b in r.box_ids()]
    seed = rng.choice(all_ids)
    sb = boxes[seed]
    ns = nodes[sb.service_id]
    ranked = sorted(
        all_ids,
        key=lambda bid: (
            haversine_m(ns.lon, ns.lat, nodes[boxes[bid].service_id].lon, nodes[boxes[bid].service_id].lat)
            + 0.15 * abs(sb.expected_time - boxes[bid].expected_time)
        )
    )
    rem = set(ranked[:min(q, len(ranked))])
    return remove_boxes_from_routes(routes, rem), list(rem)


def destroy_worst(routes: List[Route], q: int, boxes: Dict[str, Box], decoded: DecodeResult, rng: random.Random) -> Tuple[List[Route], List[str]]:
    # 对迟到贡献大的箱优先；若均不迟到，则优先晚送达/低裕量箱
    scored = []
    for bid, b in boxes.items():
        t = decoded.box_delivery.get(bid, 0.0)
        late = b.priority * max(0.0, t - b.expected_time) / max(b.expected_time, 1.0)
        ratio = t / max(b.expected_time, 1.0)
        scored.append((late * 1000 + ratio, rng.random(), bid))
    scored.sort(reverse=True)
    rem = {x[2] for x in scored[:min(q, len(scored))]}
    return remove_boxes_from_routes(routes, rem), list(rem)


def alns_optimize(
    initial_routes: List[Route],
    boxes: Dict[str, Box],
    nodes: Dict[str, Node],
    evaluator: RouteEvaluator,
    decoder: ResourceDecoder,
    iterations: int,
    seed: int,
) -> Tuple[List[Route], DecodeResult, Metrics, List[Tuple[int, float]]]:
    rng = random.Random(seed)
    current_routes = [r.clone() for r in initial_routes]
    current_dec = decoder.decode(current_routes)
    if not current_dec.feasible:
        # 初始 Decoder 失败时，使用最保守退化方案：每个 Route 只保留单服务区/必要时单箱。
        conservative = []
        for r in current_routes:
            for s in r.stops:
                # 同服务区按 C 型容量逐箱顺序装箱，直到不能再加
                cr: Optional[Route] = None
                for bid in s.box_ids:
                    b = boxes[bid]
                    if cr is None:
                        cr = route_from_box(b)
                    else:
                        trial = insert_box_into_route(cr, b, None)
                        if evaluator.feasible_types(trial):
                            cr = trial
                        else:
                            conservative.append(cr)
                            cr = route_from_box(b)
                if cr is not None:
                    conservative.append(cr)
        current_routes = conservative
        current_dec = decoder.decode(current_routes)
        if not current_dec.feasible:
            raise RuntimeError(f"即使保守单点拆分方案仍无法调度：{current_dec.reason}")

    base_metrics = compute_metrics(current_dec, boxes)
    current_metrics = base_metrics
    current_score = normalized_score(current_metrics, base_metrics)
    best_routes = [r.clone() for r in current_routes]
    best_dec = current_dec
    best_metrics = current_metrics
    best_score = current_score
    history = [(0, best_score)]

    destroy_names = ["random", "related", "worst"]
    weights = {k: 1.0 for k in destroy_names}
    temp0 = 0.08

    for it in range(1, iterations + 1):
        # 自适应加权抽样 destroy 算子
        total_w = sum(weights.values())
        x = rng.random() * total_w
        acc = 0.0
        op = destroy_names[-1]
        for name in destroy_names:
            acc += weights[name]
            if x <= acc:
                op = name; break
        q = rng.randint(ALNS_REMOVE_MIN, ALNS_REMOVE_MAX)
        if op == "random":
            partial, rem = destroy_random(current_routes, q, rng)
        elif op == "related":
            partial, rem = destroy_related(current_routes, q, boxes, nodes, rng)
        else:
            partial, rem = destroy_worst(current_routes, q, boxes, current_dec, rng)

        cand_routes = regret2_repair(partial, rem, boxes, evaluator)
        if cand_routes is None:
            weights[op] *= 0.995
            continue
        try:
            ensure_route_integrity(cand_routes, boxes)
        except Exception:
            weights[op] *= 0.995
            continue

        cand_dec = decoder.decode(cand_routes)
        if not cand_dec.feasible:
            weights[op] *= 0.997
            continue
        cand_metrics = compute_metrics(cand_dec, boxes)
        cand_score = normalized_score(cand_metrics, base_metrics)
        if not math.isfinite(cand_score):
            continue

        temp = max(0.002, temp0 * (1.0 - it / max(iterations, 1)))
        delta = cand_score - current_score
        accept = delta <= 0 or rng.random() < math.exp(-delta / temp)
        if accept:
            current_routes, current_dec = cand_routes, cand_dec
            current_metrics, current_score = cand_metrics, cand_score
            weights[op] *= 1.002
        else:
            weights[op] *= 0.999

        if cand_score < best_score - 1e-10:
            best_routes = [r.clone() for r in cand_routes]
            best_dec = cand_dec
            best_metrics = cand_metrics
            best_score = cand_score
            weights[op] += 0.15

        if it % 10 == 0 or it == iterations:
            history.append((it, best_score))

    return best_routes, best_dec, best_metrics, history


# ============================================================

@dataclass(frozen=True)
class CommParams:
    frequency_mhz: float
    system_loss_db: float
    obstacle_loss_db: float
    sensitivity_dbm: float
    fade_margin_db: float
    transport_pt_dbm: float
    transport_gain_dbi: float
    relay_access_pt_dbm: float
    relay_access_gain_dbi: float
    relay_backhaul_pt_dbm: float
    relay_backhaul_gain_dbi: float
    gateway_pt_dbm: float
    gateway_gain_dbi: float
    gateway_height_agl: float


@dataclass(frozen=True)
class RelaySpec:
    type_id: str
    name: str
    empty_mass: float
    comm_module_mass: float
    takeoff_mass: float
    cruise_speed: float
    cruise_power_kw: float
    energy_kwh: float
    reserve_ratio: float
    prep_time: float
    link_time: float
    turnaround_time: float
    climb_speed: float
    descent_speed: float
    climb_efficiency: float
    descent_efficiency: float
    hover_power_kw: float
    comm_power_kw: float
    max_hover_agl: float
    component_count: int
    full_charge_time: float


@dataclass(frozen=True)
class RelayUnit:
    relay_id: str
    type_id: str


@dataclass(frozen=True)
class Position3D:
    lon: float
    lat: float
    alt: float


@dataclass
class TrajectorySample:
    trip_id: str
    time: float
    phase: str
    pos: Position3D
    direct_ok: bool = False
    relay_sortie_id: str = ""


@dataclass(frozen=True)
class RelayCandidate:
    candidate_id: str
    row: int
    col: int
    lon: float
    lat: float
    ground_alt: float
    agl: float
    alt: float


@dataclass
class CommDemand:
    demand_id: str
    trip_id: str
    start_time: float
    end_time: float
    sample_indices: List[int]
    options: List[str] = field(default_factory=list)


@dataclass
class RelaySession:
    candidate_id: str
    service_start: float
    service_end: float
    demand_ids: List[str]


@dataclass
class RelaySortie:
    sortie_id: str
    relay_id: str
    component_id: str
    candidate_id: str
    start_time: float
    link_complete_time: float
    service_end_time: float
    return_time: float
    energy_kwh: float
    end_soc: float
    charge_end_time: float
    lon: float
    lat: float
    alt: float
    demand_ids: List[str]


def load_comm_params(path: Path) -> CommParams:
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    vals = {}
    for r in range(3, 17):
        cat = str(ws.cell(r, 1).value or "").strip()
        name = str(ws.cell(r, 2).value or "").strip()
        v = float(ws.cell(r, 5).value)
        vals[(cat, name)] = v
    wb.close()
    return CommParams(
        frequency_mhz=vals[("传播参数", "载波频率（MHz）")],
        system_loss_db=vals[("传播参数", "系统损耗（dB）")],
        obstacle_loss_db=vals[("传播参数", "地形遮挡附加损耗（dB）")],
        sensitivity_dbm=vals[("接收参数", "接收灵敏度（dBm）")],
        fade_margin_db=vals[("接收参数", "衰落裕量（dB）")],
        transport_pt_dbm=vals[("运输无人机", "发射功率（dBm）")],
        transport_gain_dbi=vals[("运输无人机", "天线增益（dBi）")],
        relay_access_pt_dbm=vals[("中继接入端", "发射功率（dBm）")],
        relay_access_gain_dbi=vals[("中继接入端", "天线增益（dBi）")],
        relay_backhaul_pt_dbm=vals[("中继回传端", "发射功率（dBm）")],
        relay_backhaul_gain_dbi=vals[("中继回传端", "天线增益（dBi）")],
        gateway_pt_dbm=vals[("固定网关 G01", "发射功率（dBm）")],
        gateway_gain_dbi=vals[("固定网关 G01", "天线增益（dBi）")],
        gateway_height_agl=vals[("固定网关 G01", "天线离地高度（m）")],
    )


def load_relay_data(path: Path) -> Tuple[RelaySpec, List[RelayUnit]]:
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    v = [ws.cell(3, c).value for c in range(1, 20)]
    component_count = int(ws.cell(12, 2).value)
    full_charge = float(ws.cell(12, 3).value)
    spec = RelaySpec(
        type_id=str(v[0]).strip(), name=str(v[1]), empty_mass=float(v[2]),
        comm_module_mass=float(v[3]), takeoff_mass=float(v[4]), cruise_speed=float(v[5]),
        cruise_power_kw=float(v[6]), energy_kwh=float(v[7]), reserve_ratio=float(v[8]) / 100.0,
        prep_time=float(v[9]), link_time=float(v[10]), turnaround_time=float(v[11]),
        climb_speed=float(v[12]), descent_speed=float(v[13]), climb_efficiency=float(v[14]),
        descent_efficiency=float(v[15]), hover_power_kw=float(v[16]), comm_power_kw=float(v[17]),
        max_hover_agl=float(v[18]), component_count=component_count, full_charge_time=full_charge,
    )
    units = []
    for r in range(7, 20):
        rid = ws.cell(r, 1).value
        if rid and str(rid).startswith("R") and str(rid) != spec.type_id:
            units.append(RelayUnit(str(rid).strip(), str(ws.cell(r, 2).value).strip()))
    wb.close()
    if len(units) != 2:
        raise ValueError(f"中继无人机读取异常：读取 {len(units)} 架，应为 2 架")
    return spec, units


class CommunicationModel:
    """统一实现 DEM 视线、FSPL、双向链路预算及中继候选点地形查询。"""
    def __init__(self, dem_path: Path, nodes: Dict[str, Node], params: CommParams):
        mat = loadmat(dem_path)
        self.dem = np.asarray(mat["dem"], dtype=float)
        self.lat = np.asarray(mat["latitude"], dtype=float).reshape(-1)
        self.lon = np.asarray(mat["longitude"], dtype=float).reshape(-1)
        self.nodata = float(np.asarray(mat.get("nodata", np.array([[-32767.0]]))).reshape(-1)[0])
        self.nodes = nodes
        self.p = params
        o = nodes["O01"]
        self.gateway = Position3D(o.lon, o.lat, o.ground_alt + params.gateway_height_agl)
        self._los_cache: Dict[Tuple[int, int, int, int, int, int], bool] = {}
        self._backhaul_cache: Dict[Tuple[int, int, int], bool] = {}

    def nearest_rc(self, lon0: float, lat0: float) -> Tuple[int, int]:
        return int(np.argmin(np.abs(self.lat - lat0))), int(np.argmin(np.abs(self.lon - lon0)))

    def valid_dem(self, r: int, c: int) -> bool:
        if not (0 <= r < self.dem.shape[0] and 0 <= c < self.dem.shape[1]):
            return False
        z = float(self.dem[r, c])
        return np.isfinite(z) and abs(z - self.nodata) > 1e-6

    @staticmethod
    def _quant(p: Position3D) -> Tuple[int, int, int]:
        return (int(round(p.lon * 1e6)), int(round(p.lat * 1e6)), int(round(p.alt)))

    def terrain_los(self, a: Position3D, b: Position3D) -> bool:
        qa, qb = self._quant(a), self._quant(b)
        key = qa + qb if qa <= qb else qb + qa
        if key in self._los_cache:
            return self._los_cache[key]
        r0, c0 = self.nearest_rc(a.lon, a.lat)
        r1, c1 = self.nearest_rc(b.lon, b.lat)
        cells = supercover_cells(r0, c0, r1, c1)
        dx = b.lon - a.lon; dy = b.lat - a.lat
        den = dx * dx + dy * dy
        ok = True
        for r, c in cells:
            if not self.valid_dem(r, c):
                continue
            # 端点像元不作为中间地形障碍；运输/中继端点可在该像元上空。
            if (r == r0 and c == c0) or (r == r1 and c == c1):
                continue
            x, y = float(self.lon[c]), float(self.lat[r])
            frac = 0.0 if den <= 1e-20 else ((x - a.lon) * dx + (y - a.lat) * dy) / den
            frac = min(1.0, max(0.0, frac))
            line_alt = a.alt + frac * (b.alt - a.alt)
            if float(self.dem[r, c]) >= line_alt - 1e-6:
                ok = False; break
        self._los_cache[key] = ok
        return ok

    @staticmethod
    def distance3d_m(a: Position3D, b: Position3D) -> float:
        dh = haversine_m(a.lon, a.lat, b.lon, b.lat)
        return math.sqrt(dh * dh + (a.alt - b.alt) ** 2)

    def fspl_db(self, a: Position3D, b: Position3D) -> float:
        d_km = max(self.distance3d_m(a, b) / 1000.0, 1e-6)
        return 32.44 + 20.0 * math.log10(self.p.frequency_mhz) + 20.0 * math.log10(d_km)

    def max_loss(self, kind: str) -> float:
        req = self.p.sensitivity_dbm + self.p.fade_margin_db
        if kind == "TG":
            fwd = self.p.transport_pt_dbm + self.p.transport_gain_dbi + self.p.gateway_gain_dbi - self.p.system_loss_db - req
            rev = self.p.gateway_pt_dbm + self.p.gateway_gain_dbi + self.p.transport_gain_dbi - self.p.system_loss_db - req
        elif kind == "TR":
            fwd = self.p.transport_pt_dbm + self.p.transport_gain_dbi + self.p.relay_access_gain_dbi - self.p.system_loss_db - req
            rev = self.p.relay_access_pt_dbm + self.p.relay_access_gain_dbi + self.p.transport_gain_dbi - self.p.system_loss_db - req
        elif kind == "RG":
            fwd = self.p.relay_backhaul_pt_dbm + self.p.relay_backhaul_gain_dbi + self.p.gateway_gain_dbi - self.p.system_loss_db - req
            rev = self.p.gateway_pt_dbm + self.p.gateway_gain_dbi + self.p.relay_backhaul_gain_dbi - self.p.system_loss_db - req
        else:
            raise KeyError(kind)
        return min(fwd, rev)

    def link_margin_db(self, a: Position3D, b: Position3D, kind: str) -> float:
        loss = self.fspl_db(a, b)
        if not self.terrain_los(a, b):
            loss += self.p.obstacle_loss_db
        return self.max_loss(kind) - loss

    def link_available(self, a: Position3D, b: Position3D, kind: str) -> bool:
        return self.link_margin_db(a, b, kind) >= -1e-9

    def candidate(self, r: int, c: int, agl: float) -> Optional[RelayCandidate]:
        if not self.valid_dem(r, c): return None
        ground = float(self.dem[r, c])
        return RelayCandidate(f"C{r:04d}_{c:04d}_{int(agl):03d}", r, c, float(self.lon[c]), float(self.lat[r]), ground, agl, ground + agl)

    def candidate_pos(self, cand: RelayCandidate) -> Position3D:
        return Position3D(cand.lon, cand.lat, cand.alt)

    def relay_backhaul_ok(self, cand: RelayCandidate) -> bool:
        key = (cand.row, cand.col, int(cand.agl))
        if key not in self._backhaul_cache:
            self._backhaul_cache[key] = self.link_available(self.candidate_pos(cand), self.gateway, "RG")
        return self._backhaul_cache[key]

    def line_dem_max(self, a: Position3D, b: Position3D) -> float:
        r0, c0 = self.nearest_rc(a.lon, a.lat); r1, c1 = self.nearest_rc(b.lon, b.lat)
        vals = [float(self.dem[r, c]) for r, c in supercover_cells(r0,c0,r1,c1) if self.valid_dem(r,c)]
        if not vals: raise ValueError("中继航线无有效 DEM 像元")
        return max(vals)



def relay_flight_profile(cand: RelayCandidate, comm: CommunicationModel, spec: RelaySpec) -> Tuple[float, float, float, float]:
    """返回 outbound_time, return_time, flight_energy, cruise_alt。"""
    o = comm.nodes["O01"]
    start = Position3D(o.lon, o.lat, o.ground_alt)
    cp = comm.candidate_pos(cand)
    dist = haversine_m(start.lon, start.lat, cp.lon, cp.lat)
    demmax = comm.line_dem_max(start, cp)
    cruise_alt = max(demmax + 50.0, start.alt, cp.alt)
    out_climb = max(0.0, cruise_alt - start.alt)
    out_desc = max(0.0, cruise_alt - cp.alt)
    ret_climb = max(0.0, cruise_alt - cp.alt)
    ret_desc = max(0.0, cruise_alt - start.alt)
    horiz_t = dist / spec.cruise_speed
    out_t = out_climb/spec.climb_speed + horiz_t + out_desc/spec.descent_speed
    ret_t = ret_climb/spec.climb_speed + horiz_t + ret_desc/spec.descent_speed
    e_cr = 2.0 * spec.cruise_power_kw * horiz_t / 3600.0
    e_up = spec.takeoff_mass * G * (out_climb + ret_climb) / max(spec.climb_efficiency,1e-9) / 3.6e6
    return out_t, ret_t, e_cr + e_up, cruise_alt


def relay_sortie_energy(cand: RelayCandidate, service_duration: float, comm: CommunicationModel, spec: RelaySpec) -> Tuple[float, float, float]:
    out_t, ret_t, flight_e, _ = relay_flight_profile(cand, comm, spec)
    hover_comm_e = (spec.hover_power_kw + spec.comm_power_kw) * (spec.link_time + max(0.0, service_duration)) / 3600.0
    return flight_e + hover_comm_e, out_t, ret_t


# ============================================================
# V3: exact cell traversal and sufficient continuous-time certificates
# ============================================================

class Terrain:
    """Piecewise-constant DEM cells, using the supplied true coordinates.

    A line touches every cell intersecting the actual geographic segment.
    A boundary/corner touch includes both adjacent cells. NoData is rejected.
    This is a raster-model convention, not sub-pixel terrain reconstruction.
    """
    def __init__(self, path):
        m=loadmat(path)
        self.dem=np.asarray(m['dem'],float)
        self.lat=np.asarray(m['latitude'],float).ravel()
        self.lon=np.asarray(m['longitude'],float).ravel()
        self.nodata=float(np.asarray(m.get('nodata',[[-32767.]])).ravel()[0])
        self.dx=float(self.lon[1]-self.lon[0]); self.dy=float(self.lat[1]-self.lat[0])
        if not np.allclose(np.diff(self.lon),self.dx) or not np.allclose(np.diff(self.lat),self.dy):
            raise ValueError('DEM coordinate arrays must form a regular grid.')
        self.shape=self.dem.shape

    def xy(self,p):
        return np.array([(p.lon-self.lon[0])/self.dx+.5,(p.lat-self.lat[0])/self.dy+.5])

    def valid(self,r,c):
        return (0<=r<self.shape[0] and 0<=c<self.shape[1]
                and np.isfinite(self.dem[r,c]) and abs(self.dem[r,c]-self.nodata)>1e-6)

    @lru_cache(maxsize=120000)
    def trace(self,lon1,lat1,lon2,lat2):
        a=self.xy(Position3D(lon1,lat1,0));b=self.xy(Position3D(lon2,lat2,0));v=b-a
        if np.any(np.minimum(a,b)<0) or np.any(np.maximum(a,b)>[self.shape[1],self.shape[0]]):
            raise ValueError('Segment is outside DEM bounds.')
        ts=[0.,1.]
        for k in range(2):
            if abs(v[k])>1e-12:
                grid=np.arange(math.ceil(min(a[k],b[k])),math.floor(max(a[k],b[k]))+1)
                t=(grid-a[k])/v[k]
                ts.extend(t[(t>1e-12)&(t<1-1e-12)].tolist())
        ts=np.unique(np.asarray(ts));lo=ts[:-1];hi=ts[1:]
        mid=a[None,:]+((lo+hi)/2)[:,None]*v
        cols=np.floor(mid[:,0]).astype(int);rows=np.floor(mid[:,1]).astype(int)
        records=[(int(r),int(c),float(l),float(h)) for r,c,l,h in zip(rows,cols,lo,hi)]
        # Whole edges lying on a grid line belong to both neighboring cells.
        for k in range(2):
            if abs(v[k])<1e-12 and abs(a[k]-round(a[k]))<1e-8:
                records += [(r-(k==1),c-(k==0),l,h) for r,c,l,h in list(records)]
        # Isolated corner/edge touches, including endpoints.
        pts=a[None,:]+ts[:,None]*v
        for t,pt in zip(ts,pts):
            cc=[math.floor(pt[0])];rr=[math.floor(pt[1])]
            if abs(pt[0]-round(pt[0]))<1e-8: cc=[round(pt[0])-1,round(pt[0])]
            if abs(pt[1]-round(pt[1]))<1e-8: rr=[round(pt[1])-1,round(pt[1])]
            for r,c in itertools.product(rr,cc): records.append((r,c,t,t))
        # Outside cells on the outermost DEM boundary have no physical extent inside.
        records=[x for x in records if 0<=x[0]<self.shape[0] and 0<=x[1]<self.shape[1]]
        rr=np.array([x[0] for x in records],int);cc=np.array([x[1] for x in records],int)
        zz=self.dem[rr,cc]
        if not np.all(np.isfinite(zz)&(abs(zz-self.nodata)>1e-6)):
            raise ValueError('NoData encountered on a required line segment.')
        return rr,cc,np.array([x[2] for x in records]),np.array([x[3] for x in records])

    def line_max(self,a,b):
        r,c,_,_=self.trace(a.lon,a.lat,b.lon,b.lat)
        return float(self.dem[r,c].max())

    def los(self,a,b):
        r,c,lo,hi=self.trace(a.lon,a.lat,b.lon,b.lat)
        z0=a.alt+(b.alt-a.alt)*lo;z1=a.alt+(b.alt-a.alt)*hi
        return bool(np.all(self.dem[r,c] < np.minimum(z0,z1)-1e-7))

    @staticmethod
    def clip(poly,k,bound,keep_greater):
        out=[]
        for a,b in zip(poly,poly[1:]+poly[:1]):
            ia=a[k]>=bound-1e-10 if keep_greater else a[k]<=bound+1e-10
            ib=b[k]>=bound-1e-10 if keep_greater else b[k]<=bound+1e-10
            if ia:out.append(a)
            if ia!=ib:
                u=(bound-a[k])/(b[k]-a[k]);out.append(a+u*(b-a))
        return out

    def triangle_clear(self,s,a,b):
        """Check the entire swept radio-ray triangle against DEM cell prisms."""
        xy=np.array([self.xy(p) for p in (s,a,b)])
        z=np.array([s.alt,a.alt,b.alt])
        v1=xy[1]-xy[0];v2=xy[2]-xy[0]
        det=v1[0]*v2[1]-v1[1]*v2[0]
        if abs(det)<1e-8:
            # In a vertical projected triangle, the lower envelope lies on edges.
            return self.los(s,a) and self.los(s,b) and self.los(a,b)
        coeff=np.linalg.solve(np.column_stack([xy,np.ones(3)]),z)
        c0=max(0,math.floor(xy[:,0].min()-1e-9));c1=min(self.shape[1]-1,math.floor(xy[:,0].max()))
        r0=max(0,math.floor(xy[:,1].min()-1e-9));r1=min(self.shape[0]-1,math.floor(xy[:,1].max()))
        rr,cc=np.mgrid[r0:r1+1,c0:c1+1];cx=cc+.5;cy=rr+.5
        mask=np.ones(rr.shape,bool);sign=1 if det>0 else -1
        for p,q in zip(xy,np.roll(xy,-1,axis=0)):
            d=q-p
            cross=sign*(d[0]*(cy-p[1])-d[1]*(cx-p[0]))
            mask &= cross>=-.5*(abs(d[0])+abs(d[1]))-1e-8
        rr=rr[mask];cc=cc[mask]
        vals=self.dem[rr,cc]
        valid=np.isfinite(vals)&(abs(vals-self.nodata)>1e-6)
        # The affine height minimum over the full square is a safe lower bound.
        lower=coeff[0]*(cc+.5)+coeff[1]*(rr+.5)+coeff[2]-.5*(abs(coeff[0])+abs(coeff[1]))
        risky=np.where((~valid)|(vals>=lower-1e-7))[0]
        for j in risky:
            r=int(rr[j]);c=int(cc[j]);poly=[p.copy() for p in xy]
            for k,v,ge in ((0,c,True),(0,c+1,False),(1,r,True),(1,r+1,False)):
                if not poly:break
                poly=self.clip(poly,k,v,ge)
            if not poly:continue
            if not valid[j]:raise ValueError('NoData in swept radio-ray triangle.')
            low=min(coeff[0]*p[0]+coeff[1]*p[1]+coeff[2] for p in poly)
            if vals[j]>=low-1e-7:return False
        return True


class ExactArcLibrary(ArcLibrary):
    def __init__(self,nodes,dtypes,terrain):
        self.nodes=nodes;self.drone_types=dtypes;self.terrain=terrain
        self.dem=terrain.dem;self.lat=terrain.lat;self.lon=terrain.lon;self.arcs={}
        for a,b in itertools.combinations(sorted(nodes),2):
            na=nodes[a];nb=nodes[b]
            pa=Position3D(na.lon,na.lat,na.work_alt);pb=Position3D(nb.lon,nb.lat,nb.work_alt)
            d=haversine_m(na.lon,na.lat,nb.lon,nb.lat);z=terrain.line_max(pa,pb);h=z+50.
            if h+1e-6<max(na.work_alt,nb.work_alt):
                raise ValueError(f'Flight-height convention inconsistent for {a}-{b}; inspect input.')
            for u,v in ((a,b),(b,a)):
                n1=nodes[u];n2=nodes[v];up=h-n1.work_alt;down=h-n2.work_alt
                times={g:up/t.climb_speed+d/t.cruise_speed+down/t.descent_speed for g,t in dtypes.items()}
                self.arcs[u,v]=Arc(u,v,d,z,h,n1.work_alt,n2.work_alt,up,down,times)


class CertifiedCommunication(CommunicationModel):
    def __init__(self,terrain,nodes,params,extra_margin=0.):
        self.terrain=terrain;self.dem=terrain.dem;self.lon=terrain.lon;self.lat=terrain.lat
        self.nodata=terrain.nodata;self.nodes=nodes;self.p=params;self.extra_margin=extra_margin
        o=nodes['O01'];self.gateway=Position3D(o.lon,o.lat,o.ground_alt+params.gateway_height_agl)
        self._backhaul_cache={};self.cert_cache={}

    def nearest_rc(self,lon0,lat0):
        x,y=self.terrain.xy(Position3D(lon0,lat0,0))
        return int(math.floor(y)),int(math.floor(x))

    def terrain_los(self,a,b):return self.terrain.los(a,b)

    def fspl_db(self,a,b):
        return 32.45+20*math.log10(self.p.frequency_mhz)+20*math.log10(max(self.distance3d_m(a,b)/1000.,1e-6))

    def line_dem_max(self,a,b):return self.terrain.line_max(a,b)

    def link_available(self,a,b,kind):
        loss=self.fspl_db(a,b);budget=self.max_loss(kind)-self.extra_margin
        if loss+self.p.obstacle_loss_db<=budget:return True
        if loss>budget:return False
        return self.terrain_los(a,b)

    @staticmethod
    def poskey(p):return (p.lon,p.lat,p.alt)

    def interval_certificate(self,fixed,a,b,kind):
        # No altitude/coordinate rounding: cache hits never change geometry.
        key=(kind,self.poskey(fixed),self.poskey(a),self.poskey(b))
        if key in self.cert_cache:return self.cert_cache[key]
        lats=np.array([fixed.lat,a.lat,b.lat])
        latmin=0. if lats.min()<=0<=lats.max() else min(abs(lats))
        cosmax=math.cos(math.radians(latmin))
        upper=0.
        for p in (a,b):
            dx=EARTH_RADIUS_M*math.radians(p.lon-fixed.lon)*cosmax
            dy=EARTH_RADIUS_M*math.radians(p.lat-fixed.lat)
            upper=max(upper,math.sqrt(dx*dx+dy*dy+(p.alt-fixed.alt)**2))
        # Rhumb-path bound above the great-circle distance, convex along a->b.
        loss=32.45+20*math.log10(self.p.frequency_mhz)+20*math.log10(max(upper/1000,1e-6))
        budget=self.max_loss(kind)-self.extra_margin
        if loss+self.p.obstacle_loss_db<=budget:
            ans=(True,'NLOS_BOUND',budget-loss-self.p.obstacle_loss_db)
        elif loss<=budget and self.link_available(a,fixed,kind) and self.link_available(b,fixed,kind) and self.terrain.triangle_clear(fixed,a,b):
            ans=(True,'LOS_TRIANGLE',budget-loss)
        else:ans=(False,'UNPROVEN',float('-inf'))
        self.cert_cache[key]=ans
        return ans


@dataclass
class CommPiece:
    trip_id:str
    route_index:int
    start:float
    end:float
    phase:str
    a:Position3D
    b:Position3D
    direct:bool=False
    proof:str=''
    margin:float=float('-inf')

def interpolate(a,b,f):
    return Position3D(a.lon+(b.lon-a.lon)*f,a.lat+(b.lat-a.lat)*f,a.alt+(b.alt-a.alt)*f)

def trajectory_pieces(decoded,nodes,dtypes,arcs,comm,step):
    pieces=[]
    for tr in decoded.trips:
        dt=dtypes[tr.drone_type];now=tr.takeoff_time
        def add(duration,phase,a,b):
            nonlocal now
            if duration<=1e-9:return
            n=1 if a==b else max(1,math.ceil(duration/step))
            for i in range(n):
                pa=interpolate(a,b,i/n);pb=interpolate(a,b,(i+1)/n)
                ok,why,margin=comm.interval_certificate(comm.gateway,pa,pb,'TG')
                pieces.append(CommPiece(tr.trip_id,tr.route_index,now+duration*i/n,now+duration*(i+1)/n,phase,pa,pb,ok,why,margin))
            now+=duration
        seq=['O01']+tr.service_sequence+['O01']
        for i,(u,v) in enumerate(zip(seq,seq[1:])):
            nu=nodes[u];nv=nodes[v];arc=arcs[u,v]
            a=Position3D(nu.lon,nu.lat,nu.work_alt);ah=Position3D(nu.lon,nu.lat,arc.cruise_alt_m)
            b=Position3D(nv.lon,nv.lat,nv.work_alt);bh=Position3D(nv.lon,nv.lat,arc.cruise_alt_m)
            add(arc.climb_m/dt.climb_speed,'爬升 '+u+'→'+v,a,ah)
            add(arc.distance_m/dt.cruise_speed,'巡航 '+u+'→'+v,ah,bh)
            add(arc.descent_m/dt.descent_speed,'下降 '+u+'→'+v,bh,b)
            if v!='O01':add(tr.route_eval.stops[i].service_duration,'投送 '+v,b,b)
        if abs(now-tr.return_time)>1e-6:raise AssertionError('Trajectory/route duration mismatch.')
    return pieces


def read_input(root,pattern,suffix):
    found=sorted(p for p in root.rglob(pattern+'*'+suffix) if not any(x.startswith('output') for x in p.parts))
    if len(found)==0:raise FileNotFoundError(f'{root}: missing {pattern}{suffix}')
    if len(found)>1:
        hashes={hashlib.sha256(p.read_bytes()).hexdigest() for p in found}
        if len(hashes)>1:raise ValueError(f'Multiple different input versions for {pattern}: '+str(found)+'; use a clean data folder.')
    return found[0]


@dataclass
class DemandInterval:
    index:int
    trip_id:str
    route_index:int
    start:float
    end:float
    pieces:List[int]
    sites:frozenset

@dataclass
class RelayColumn:
    site:str
    service_start:float
    service_end:float
    start:float
    returned:float
    unit_ready:float
    component_ready:float
    energy:float
    soc:float
    covers:Tuple[int,...]

@dataclass
class RelayPlan:
    feasible:bool
    status:str
    columns:List[RelayColumn]=field(default_factory=list)
    sorties:List[RelaySortie]=field(default_factory=list)
    demands:List[DemandInterval]=field(default_factory=list)
    pieces:List[CommPiece]=field(default_factory=list)
    uncovered:List[int]=field(default_factory=list)
    diagnostics:dict=field(default_factory=dict)

class RelayPlanner:
    def __init__(self,comm,spec,units,grid_cells=64,heights=(120.,240.,300.),time_limit=20.,gap=.01,max_sites=64,window_step=300.):
        self.comm=comm;self.spec=spec;self.units=units;self.time_limit=time_limit;self.gap=gap
        self.max_sites=max_sites;self.window_step=window_step
        self.sites={};self.profiles={};self.calls=0;self.logs=[]
        nodes=list(comm.nodes.values());t=comm.terrain
        # All feasible access links lie within their LOS distance limit.
        radius=1000*10**((comm.max_loss('TR')-32.45-20*math.log10(comm.p.frequency_mhz))/20)
        padlon=math.degrees(radius/(EARTH_RADIUS_M*math.cos(math.radians(max(n.lat for n in nodes)))))
        padlat=math.degrees(radius/EARTH_RADIUS_M)
        lo=min(n.lon for n in nodes)-padlon;hi=max(n.lon for n in nodes)+padlon
        la=min(n.lat for n in nodes)-padlat;lb=max(n.lat for n in nodes)+padlat
        rr=np.where((t.lat>=la)&(t.lat<=lb))[0];cc=np.where((t.lon>=lo)&(t.lon<=hi))[0]
        positions=set()
        for r in range(int(rr.min()),int(rr.max())+1,grid_cells):
            for c in range(int(cc.min()),int(cc.max())+1,grid_cells):
                positions.add((r,c))
                z=t.dem[r:min(r+grid_cells,t.shape[0]),c:min(c+grid_cells,t.shape[1])]
                good=np.isfinite(z)&(abs(z-t.nodata)>1e-6)
                if np.any(good):
                    dr,dc=np.unravel_index(np.argmax(np.where(good,z,-np.inf)),z.shape)
                    positions.add((r+int(dr),c+int(dc)))
        for n in nodes:
            r,c=comm.nearest_rc(n.lon,n.lat)
            for dr,dc in itertools.product((-18,0,18),repeat=2):positions.add((r+dr,c+dc))
        for r,c in sorted(positions):
            for h in heights:self.add_site(r,c,h)
        print(f'  Relay candidates with feasible backhaul: {len(self.sites)}',flush=True)

    def add_site(self,r,c,h):
        if not 0<h<=self.spec.max_hover_agl:return None
        ca=self.comm.candidate(int(r),int(c),float(h))
        if not ca or ca.candidate_id in self.sites:return ca
        if not self.comm.relay_backhaul_ok(ca):return None
        out,ret,e,_=relay_flight_profile(ca,self.comm,self.spec)
        if e+(self.spec.hover_power_kw+self.spec.comm_power_kw)*self.spec.link_time/3600 >= (1-self.spec.reserve_ratio)*self.spec.energy_kwh:return None
        self.sites[ca.candidate_id]=ca;self.profiles[ca.candidate_id]=(out,ret,e)
        return ca

    def covers(self,cid,p):
        ca=self.sites[cid]
        return self.comm.interval_certificate(self.comm.candidate_pos(ca),p.a,p.b,'TR')[0]

    def coverage_demands(self,pieces):
        bad=[i for i,p in enumerate(pieces) if not p.direct]
        opts={i:set() for i in bad}
        for cid in self.sites:
            for i in bad:
                if self.covers(cid,pieces[i]):opts[i].add(cid)
        missing=[i for i in bad if not opts[i]]
        if missing:
            old=set(self.sites)
            for i in missing:
                p=pieces[i];mid=interpolate(p.a,p.b,.5);r,c=self.comm.nearest_rc(mid.lon,mid.lat)
                for dr,dc in itertools.product((-32,-16,-8,0,8,16,32),repeat=2):
                    for h in (120.,210.,270.,self.spec.max_hover_agl):self.add_site(r+dr,c+dc,h)
            for cid in sorted(set(self.sites)-old):
                for i in bad:
                    if self.covers(cid,pieces[i]):opts[i].add(cid)
        # Merge only adjacent pieces with identical certified candidate sets.
        demands=[]
        for i in bad:
            p=pieces[i];cs=frozenset(opts[i])
            if demands and demands[-1].trip_id==p.trip_id and abs(demands[-1].end-p.start)<1e-6 and demands[-1].sites==cs:
                demands[-1].end=p.end;demands[-1].pieces.append(i)
            else:demands.append(DemandInterval(len(demands),p.trip_id,p.route_index,p.start,p.end,[i],cs))
        return demands

    def make_columns(self,demands):
        bysite=defaultdict(list)
        for d in demands:
            for cid in sorted(d.sites):bysite[cid].append(d)
        # Exact same coverage plus no worse transit/energy is safe dominance.
        equivalent=defaultdict(list)
        for cid,ds in bysite.items():equivalent[tuple(d.index for d in ds)].append(cid)
        kept=[]
        for ids in equivalent.values():
            front=[]
            for cid in sorted(ids,key=lambda x:self.profiles[x][2]):
                a=np.array(self.profiles[cid])
                if any(np.all(np.array(self.profiles[x])<=a+1e-10) for x in front):continue
                front=[x for x in front if not np.all(a<=np.array(self.profiles[x])+1e-10)]
                front.append(cid)
            kept.extend(front)
        # A bounded, expandable restricted site pool prevents quadratic memory growth.
        # Preserve coverage of every demand; do not interpret this restriction as
        # physical infeasibility or as a global optimality certificate.
        masks={cid:sum(1<<d.index for d in bysite[cid]) for cid in kept}
        lengths=np.array([d.end-d.start for d in demands])
        quality={cid:sum(lengths[d.index] for d in bysite[cid])/(.15+self.profiles[cid][2]) for cid in kept}
        selected=[];uncovered=(1<<len(demands))-1
        while uncovered:
            candidate=max(kept,key=lambda x:((masks[x]&uncovered).bit_count()/(.3+self.profiles[x][2]),quality[x]))
            if not masks[candidate]&uncovered:break
            selected.append(candidate);uncovered &= ~masks[candidate]
        for cid in sorted(kept,key=lambda x:quality[x],reverse=True):
            if len(selected)>=self.max_sites:break
            if cid not in selected:selected.append(cid)
        kept=selected
        self.last_site_count=len(kept)
        columns=[];seen=set();power=(self.spec.hover_power_kw+self.spec.comm_power_kw)/3600
        emax=(1-self.spec.reserve_ratio)*self.spec.energy_kwh
        for cid in kept:
            out,ret,flight=self.profiles[cid]
            maxservice=(emax-flight)/power-self.spec.link_time
            ds=sorted(bysite[cid],key=lambda d:(d.start,d.end))
            buckets=defaultdict(list)
            for d in ds:buckets[math.floor(d.start/self.window_step)].append(d.start)
            starts=sorted(min(values) for values in buckets.values())
            for a in starts:
                prepstart=a-self.spec.prep_time-out-self.spec.link_time
                if prepstart < -1e-7:continue
                eligible=[d for d in ds if d.start>=a-1e-7 and d.end<=a+maxservice+1e-7]
                if not eligible:continue
                ends=sorted({d.end for d in eligible})
                choices=set(ends[:1])
                for span in (self.window_step,900.,1800.,3600.,maxservice):
                    possible=[b for b in ends if b<=a+span+1e-8]
                    if possible:choices.add(possible[-1])
                # Deliberately finite windows; solver optimality is pool-restricted.
                for b in sorted(choices):
                    covers=tuple(sorted(d.index for d in eligible if d.end<=b+1e-7))
                    if not covers:continue
                    aa=min(demands[j].start for j in covers);bb=max(demands[j].end for j in covers)
                    key=(cid,aa,bb)
                    if key in seen:continue
                    seen.add(key);e=flight+power*(self.spec.link_time+bb-aa)
                    soc=1-e/self.spec.energy_kwh;returned=bb+ret
                    columns.append(RelayColumn(cid,aa,bb,aa-self.spec.prep_time-out-self.spec.link_time,returned,
                                  returned+self.spec.turnaround_time,returned+charge_time_seconds(soc,self.spec.full_charge_time),e,soc,covers))
        return columns

    @staticmethod
    def capacity_rows(columns,field,cap,addrow):
        # Interval-graph clique constraints, using half-open occupation intervals.
        events=defaultdict(lambda:[[],[]])
        for j,c in enumerate(columns):
            events[c.start][1].append(j);events[getattr(c,field)][0].append(j)
        active=set();last=None
        for tm,(ends,starts) in sorted(events.items()):
            active.difference_update(ends);active.update(starts)
            if not starts or len(active)<=cap:continue
            sig=frozenset(active)
            if sig==last:continue
            addrow([(j,1.) for j in active],-np.inf,cap);last=sig

    def solve_columns(self,columns,demands,decoded,weights,scales,relaxed=False):
        n=len(columns);nd=len(demands)
        # Occupancy-flow equalities have O(number of columns) nonzeros, unlike
        # explicitly expanding every interval clique into a dense incidence row.
        event_groups=[]
        for field,cap in (('unit_ready',len(self.units)),('component_ready',self.spec.component_count)):
            ev=defaultdict(lambda:[[],[]])
            for j,col in enumerate(columns):ev[col.start][1].append(j);ev[getattr(col,field)][0].append(j)
            event_groups.append((sorted(ev.items()),cap))
        occ_start=n+1+(nd if relaxed else 0);nv=occ_start+sum(len(ev) for ev,cap in event_groups)
        c=np.zeros(nv);lb=np.zeros(nv);ub=np.ones(nv);integrality=np.ones(nv)
        transport_end=max(t.return_time for t in decoded.trips)
        lb[n]=transport_end;ub[n]=max([transport_end]+[x.returned for x in columns]);integrality[n]=0
        offset=occ_start
        for events,cap in event_groups:
            ub[offset:offset+len(events)]=cap;integrality[offset:offset+len(events)]=0;offset+=len(events)
        c[n]=weights['makespan']/scales['makespan']
        for j,col in enumerate(columns):c[j]=weights['energy']*col.energy/scales['energy']+weights['trips']/scales['trips']
        ri=[];ci=[];data=[];lower=[];upper=[]
        def row(items,l,u):
            ix=len(lower);lower.append(l);upper.append(u)
            for j,v in items:ri.append(ix);ci.append(j);data.append(v)
        covering=[[] for _ in demands]
        for j,col in enumerate(columns):
            for k in col.covers:covering[k].append((j,1.))
        tripmap={t.trip_id:t for t in decoded.trips}
        for k,d in enumerate(demands):
            items=covering[k]
            if relaxed:
                items=items+[(n+1+k,1.)]
                tr=tripmap[d.trip_id];slack=tr.route_eval.latest_start-tr.start_time
                urgency=25. if math.isfinite(slack) and slack<1200 else 3. if math.isfinite(slack) else 1.
                c[n+1+k]=1000*urgency*(1+(d.end-d.start)/600)
            row(items,1.,np.inf)
        offset=occ_start
        for events,cap in event_groups:
            for k,(_, (ends,starts)) in enumerate(events):
                items=[(offset+k,1.)]+([(offset+k-1,-1.)] if k else [])
                items += [(j,-1.) for j in starts]+[(j,1.) for j in ends]
                row(items,0.,0.)
            offset+=len(events)
        for j,col in enumerate(columns):row([(j,col.returned),(n,-1.)],-np.inf,0.)
        A=coo_matrix((data,(ri,ci)),shape=(len(lower),nv)).tocsc()
        # scipy/HiGHS accepts continuous time coefficients; no integer-time rounding.
        result=milp(c,integrality=integrality,bounds=Bounds(lb,ub),
                    constraints=LinearConstraint(A,np.asarray(lower),np.asarray(upper)),
                    options={'time_limit':self.time_limit,'mip_rel_gap':self.gap,'presolve':True})
        valid=False
        if result.x is not None and np.all(np.isfinite(result.x)):
            residual=A@result.x
            valid=bool(np.all(residual>=np.asarray(lower)-1e-5) and np.all(residual<=np.asarray(upper)+1e-5)
                       and np.max(abs(result.x[:n]-np.round(result.x[:n])),initial=0)<1e-5)
        diag={'status':int(result.status),'message':str(result.message),'restricted_columns':n,
              'constraints':len(lower),'objective':float(result.fun) if valid else None,
              'dual_bound':float(result.mip_dual_bound) if getattr(result,'mip_dual_bound',None) is not None else None,
              'mip_gap':float(result.mip_gap) if getattr(result,'mip_gap',None) is not None else None,
              'scope':'fixed transport schedule; finite relay candidates and service-window columns',
              'restricted_sites':getattr(self,'last_site_count',0),'available_geometric_sites':len(self.sites),
              'phase_I':relaxed,'incumbent_validated':valid}
        if not valid:return [],list(range(nd)),diag
        chosen=[columns[j] for j in range(n) if result.x[j]>.5]
        uncovered=[k for k in range(nd) if relaxed and result.x[n+1+k]>.5]
        return chosen,uncovered,diag

    def assign(self,columns,demands):
        units={u.relay_id:0. for u in self.units};components={f'RE-{i:02d}':0. for i in range(1,self.spec.component_count+1)}
        sorties=[]
        for col in sorted(columns,key=lambda x:(x.start,x.returned,x.site)):
            uu=[u for u,t in units.items() if t<=col.start+1e-6]
            cc=[u for u,t in components.items() if t<=col.start+1e-6]
            if not uu or not cc:raise AssertionError('MILP interval-capacity solution cannot be colored.')
            uid=min(uu,key=lambda u:(units[u],u));bid=min(cc,key=lambda u:(components[u],u))
            units[uid]=col.unit_ready;components[bid]=col.component_ready;ca=self.sites[col.site]
            sorties.append(RelaySortie(f'Q3R{len(sorties)+1:03d}',uid,bid,col.site,col.start,col.service_start,
                           col.service_end,col.returned,col.energy,col.soc,col.component_ready,ca.lon,ca.lat,ca.alt,
                           sorted({demands[i].trip_id for i in col.covers})))
        return sorties

    def plan(self,pieces,decoded,weights,scales):
        self.calls+=1;t0=time.time()
        demands=self.coverage_demands(pieces)
        if not demands:return RelayPlan(True,'NO_RELAY_REQUIRED',pieces=pieces)
        cols=self.make_columns(demands)
        counts=np.zeros(len(demands),int)
        for col in cols:counts[list(col.covers)]+=1
        missing=np.where(counts==0)[0].tolist()
        if missing:
            return RelayPlan(False,'NO_COLUMN_FOR_DEMAND',demands=demands,pieces=pieces,uncovered=missing,
                             diagnostics={'columns':len(cols),'missing_geometry':sum(not demands[i].sites for i in missing)})
        chosen,unc,diag=self.solve_columns(cols,demands,decoded,weights,scales)
        if unc:
            chosen,unc,diag=self.solve_columns(cols,demands,decoded,weights,scales,True)
        diag['elapsed_s']=time.time()-t0;diag['call']=self.calls
        self.logs.append(diag)
        if unc:return RelayPlan(False,'RELAY_RESOURCE_CONFLICT',chosen,[],demands,pieces,unc,diag)
        sorties=self.assign(chosen,demands)
        return RelayPlan(True,'FEASIBLE',chosen,sorties,demands,pieces,[],diag)


@dataclass
class JointSolution:
    routes:List[Route]
    release:dict
    decoded:DecodeResult
    metrics:Metrics
    relay:RelayPlan
    score:float
    label:str=''

def joint_vector(sol):
    mm=sol.metrics;rr=sol.relay.sorties
    return (mm.weighted_lateness,max([mm.makespan]+[r.return_time for r in rr]),
            mm.energy+sum(r.energy_kwh for r in rr),mm.trips+len(rr))

def joint_objective(metrics,relay,weights,scales):
    vector=(metrics.weighted_lateness,max([metrics.makespan]+[r.return_time for r in relay.sorties]),
            metrics.energy+sum(r.energy_kwh for r in relay.sorties),metrics.trips+len(relay.sorties))
    return sum(weights[k]*v/scales[k] for k,v in zip(('lateness','makespan','energy','trips'),vector))

def evaluate_joint(routes,release,decoder,nodes,dtypes,arcs,comm,planner,weights,scales,step,repair_limit=0,verbose=False):
    routes=[r.clone() for r in routes];rel={sig:float(v) for sig,v in release.items() if any(r.signature()==sig for r in routes)}
    for attempt in range(repair_limit+1):
        decoder.set_route_release({i:rel.get(r.signature(),0.) for i,r in enumerate(routes)})
        decoded=decoder.decode(routes)
        if not decoded.feasible:return None,{'reason':decoded.reason}
        validate_final_solution(routes,decoded,decoder.boxes,dtypes)
        pieces=trajectory_pieces(decoded,nodes,dtypes,arcs,comm,step)
        relay=planner.plan(pieces,decoded,weights,scales)
        if relay.feasible:
            metrics=compute_metrics(decoded,decoder.boxes)
            return JointSolution(routes,rel,decoded,metrics,relay,joint_objective(metrics,relay,weights,scales)),relay.diagnostics
        if verbose:print(f'  Joint repair {attempt}: {relay.status}, unserved intervals={len(relay.uncovered)}',flush=True)
        if attempt==repair_limit:return None,relay.diagnostics|{'reason':relay.status}
        tmap={t.trip_id:t for t in decoded.trips};affected={relay.demands[i].trip_id for i in relay.uncovered}
        choices=[]
        for tid in sorted(affected):
            tr=tmap[tid];slack=tr.route_eval.latest_start-tr.start_time
            if slack<30.:continue
            ds=[relay.demands[i] for i in relay.uncovered if relay.demands[i].trip_id==tid]
            shift=240.
            if relay.status=='NO_COLUMN_FOR_DEMAND':
                if any(not d.sites for d in ds):return None,{'reason':'NO_CERTIFIED_GEOMETRIC_COVER','trip':tid}
                earliest=min(d.start for d in ds)
                firstsites=next(d.sites for d in ds if d.start==earliest)
                needed=min(planner.spec.prep_time+planner.profiles[c][0]+planner.spec.link_time for c in firstsites)
                shift=max(60.,needed-earliest+1.)
            elif relay.columns:
                # Repair the earliest missing service first. A late soft-deadline
                # trip must not repeatedly distract repair from an earlier conflict.
                # Jump to resource-release events, instead of many fixed 240 s steps.
                first=min(ds,key=lambda d:d.start);last=max(d.end for d in ds)
                possible=[]
                for cid in sorted(first.sites):
                    out,ret,_=planner.profiles[cid]
                    start=first.start-planner.spec.prep_time-out-planner.spec.link_time
                    finish=last+ret+planner.spec.turnaround_time
                    delta=max(0.,-start)
                    for _ in range(2*len(relay.columns)+2):
                        a=start+delta;b=finish+delta
                        events=sorted({a}|{max(a,c.start) for c in relay.columns if c.start<b and c.unit_ready>a})
                        conflict=None
                        for tm in events:
                            active=[c for c in relay.columns if c.start<=tm+1e-8 and c.unit_ready>tm+1e-8]
                            if len(active)>=len(planner.units):
                                conflict=min(c.unit_ready for c in active)-a;break
                        if conflict is None:break
                        delta+=max(1.,conflict)
                    possible.append(delta)
                if possible:shift=max(240.,min(possible)+1.)
            score=(-min(d.start for d in ds),min(slack,1e7),sum(d.end-d.start for d in ds))
            choices.append((score,tr,min(shift,slack)))
        if not choices:return None,{'reason':'HARD_DEADLINE_BLOCKS_RELEASE_REPAIR'}
        _,target,shift=max(choices,key=lambda x:x[0])
        sig=routes[target.route_index].signature();rel[sig]=target.start_time+shift
        if verbose:print(f'    release {target.trip_id} by {shift:.1f}s -> {rel[sig]:.1f}s',flush=True)
    raise AssertionError('Unreachable')

def update_archive(archive,sol):
    v=np.array(joint_vector(sol))
    if any(np.all(np.array(joint_vector(x))<=v+1e-8) for x in archive):return archive
    return [x for x in archive if not np.all(v<=np.array(joint_vector(x))+1e-8)]+[sol]

def joint_search(initial,decoder,nodes,dtypes,arcs,comm,planner,weights,scales,step,iterations,seed):
    rng=random.Random(seed);current=initial;best=initial;archive=[initial];history=[]
    # Deterministic timing candidates explicitly test the waiting introduced by
    # feasibility repair. These are candidate tests, not a monotonic bisection.
    timing=[('all',factor) for factor in (.75,.9)]
    timing += [(sig,.75) for sig,value in sorted(initial.release.items(),key=lambda kv:-kv[1]) if value>0]
    operators=['release_advance','route_repair','late_repair','stop_reverse','route_split']
    opweights={k:1. for k in operators};opweights['route_repair']=2.;opweights['late_repair']=2.
    for it in range(1,iterations+1):
        t0=time.time();op='timing_sweep' if it<=len(timing) else rng.choices(operators,weights=[opweights[k] for k in operators])[0]
        routes=[r.clone() for r in current.routes];release=dict(current.release)
        if op=='timing_sweep':
            key,factor=timing[it-1]
            if key=='all':release={k:factor*v for k,v in release.items()}
            elif key in release:release[key]*=factor
        elif op=='release_advance':
            keys=[k for k,v in release.items() if v>1e-7]
            if not keys:op='route_repair'
            else:
                k=rng.choice(keys);release[k]=max(0.,release[k]-rng.choice([120.,240.,480.,960.,1e9]))
        if op in ('route_repair','late_repair'):
            if op=='late_repair':partial,removed=destroy_worst(routes,rng.randint(2,5),decoder.boxes,current.decoded,rng)
            else:partial,removed=destroy_related(routes,rng.randint(2,5),decoder.boxes,nodes,rng)
            new=regret2_repair(partial,removed,decoder.boxes,decoder.evaluator)
            if new is None:continue
            routes=new
        elif op=='stop_reverse':
            choices=[i for i,r in enumerate(routes) if len(r.stops)>1]
            if not choices:continue
            ri=rng.choice(choices);a,b=sorted(rng.sample(range(len(routes[ri].stops)),2))
            routes[ri].stops[a:b+1]=reversed(routes[ri].stops[a:b+1])
        elif op=='route_split':
            choices=[i for i,r in enumerate(routes) if len(r.box_ids())>1]
            ri=rng.choice(choices);r=routes.pop(ri)
            # Separating emergency and ordinary boxes can remove hard/soft coupling.
            hard=[bid for bid in r.box_ids() if decoder.boxes[bid].hard_deadline is not None]
            soft=[bid for bid in r.box_ids() if bid not in hard]
            if not hard or not soft:
                ids=r.box_ids();rng.shuffle(ids);hard=ids[:len(ids)//2];soft=ids[len(ids)//2:]
            for ids in (hard,soft):
                new=Route([Stop(s.service_id,[b for b in s.box_ids if b in ids]) for s in r.stops]);new.normalize();routes.append(new)
        try:ensure_route_integrity(routes,decoder.boxes)
        except ValueError:continue
        cand,diag=evaluate_joint(routes,release,decoder,nodes,dtypes,arcs,comm,planner,weights,scales,step,0)
        accepted=False
        if cand is not None:
            cand.label=f'iteration_{it}_{op}';archive=update_archive(archive,cand)
            temp=max(.002,.04*(1-it/max(iterations,1)))
            accepted=cand.score<=current.score or rng.random()<math.exp(min(0.,(current.score-cand.score)/temp))
            if accepted:current=cand
            if cand.score<best.score-1e-9:
                best=cand
                if op in opweights:opweights[op]+=.5
        elif op in opweights:opweights[op]=max(.3,opweights[op]*.97)
        record={'iteration':it,'operator':op,'feasible':cand is not None,'accepted':accepted,
                'candidate_score':cand.score if cand else None,'best_score':best.score,'seconds':time.time()-t0,
                'reason':diag.get('reason',diag.get('message',''))}
        history.append(record)
        print(f'  Joint {it}/{iterations} {op}: feasible={cand is not None}; best={best.score:.5f}; {record["seconds"]:.1f}s',flush=True)
    return best,archive,history


def check_no_overlap(intervals,label):
    for resource,ints in intervals.items():
        seq=sorted(ints)
        for a,b in zip(seq,seq[1:]):
            if a[1]>b[0]+1e-6:raise AssertionError(f'{label} conflict: {resource}: {a} versus {b}')

def audit_solution(sol,nodes,dtypes,dunits,bats,rspec,runits,arcs,comm,planner,audit_step):
    decoded=sol.decoded;boxes=decoder_boxes=planner.boxes
    validate_final_solution(sol.routes,decoded,boxes,dtypes)
    byu=defaultdict(list);byc=defaultdict(list)
    rd={u.relay_id for u in runits};components={f'RE-{i:02d}' for i in range(1,rspec.component_count+1)}
    td={u.drone_id:u.type_id for u in dunits};deliveries=[]
    verifier=RouteEvaluator(boxes,dtypes,arcs)
    for tr in decoded.trips:
        if td.get(tr.drone_id)!=tr.drone_type:raise AssertionError('Transport unit/type mismatch.')
        if tr.battery_id not in {f'BAT-{tr.drone_type}{i:02d}' for i in range(1,bats[tr.drone_type].count+1)}:raise AssertionError('Unknown transport battery.')
        ev=verifier.evaluate(sol.routes[tr.route_index],tr.drone_type)
        if not ev.feasible or abs(ev.total_energy-tr.energy_kwh)>1e-7 or abs(tr.return_time-tr.start_time-ev.duration)>1e-6:raise AssertionError('Transport physics mismatch.')
        if tr.service_sequence!=sol.routes[tr.route_index].service_ids() or abs(tr.takeoff_time-tr.start_time-ev.takeoff_offset)>1e-6 or abs(tr.end_soc-ev.end_soc)>1e-8:raise AssertionError('Transport trajectory/SOC mismatch.')
        if tr.start_time< -1e-7:raise AssertionError('Negative transport start.')
        charge=charge_time_seconds(ev.end_soc,bats[tr.drone_type].full_charge_time)
        if abs(tr.charge_end_time-tr.return_time-charge)>1e-6:raise AssertionError('Transport recharge mismatch.')
        for bid,offset in ev.box_delivery_offsets.items():
            if abs(decoded.box_delivery[bid]-tr.start_time-offset)>1e-6:raise AssertionError('Box delivery time mismatch.')
            deliveries.append(bid)
    if sorted(deliveries)!=sorted(boxes):raise AssertionError('Cargo not assigned exactly once.')
    for s in sol.relay.sorties:
        ca=planner.sites[s.candidate_id]
        if max(abs(s.lon-ca.lon),abs(s.lat-ca.lat),abs(s.alt-ca.alt))>1e-8:raise AssertionError('Relay site/trajectory mismatch.')
        if s.relay_id not in rd or s.component_id not in components:raise AssertionError('Unknown relay resource.')
        if s.start_time< -1e-6 or not 0<ca.agl<=rspec.max_hover_agl+1e-6:raise AssertionError('Relay height/start limit.')
        if not comm.valid_dem(ca.row,ca.col) or not comm.relay_backhaul_ok(ca):raise AssertionError('Invalid relay location/backhaul.')
        en,out,ret=relay_sortie_energy(ca,s.service_end_time-s.link_complete_time,comm,rspec)
        if abs(en-s.energy_kwh)>1e-7 or s.end_soc<rspec.reserve_ratio-1e-8:raise AssertionError('Relay energy/SOC error.')
        if abs(s.link_complete_time-s.start_time-rspec.prep_time-out-rspec.link_time)>1e-6:raise AssertionError('Relay not in place before service.')
        if abs(s.return_time-s.service_end_time-ret)>1e-6:raise AssertionError('Relay return-time error.')
        if abs(s.charge_end_time-s.return_time-charge_time_seconds(s.end_soc,rspec.full_charge_time))>1e-6:raise AssertionError('Relay recharge error.')
        byu[s.relay_id].append((s.start_time,s.return_time+rspec.turnaround_time,s.sortie_id))
        byc[s.component_id].append((s.start_time,s.charge_end_time,s.sortie_id))
    check_no_overlap(byu,'Relay drone');check_no_overlap(byc,'Relay energy component')
    # Reconstruct time partition independently of the optimizer's demand merging.
    pieces=trajectory_pieces(decoded,nodes,dtypes,arcs,comm,30.)
    interval_rows=[];samples=[];failures=[];min_margin=float('inf');direct_t=relay_t=0.
    def verify_piece(p,depth=0):
        nonlocal min_margin
        ok,why,margin=comm.interval_certificate(comm.gateway,p.a,p.b,'TG')
        chosen=None
        if not ok:
            for s in sol.relay.sorties:
                if s.link_complete_time<=p.start+1e-6 and s.service_end_time>=p.end-1e-6:
                    ca=planner.sites[s.candidate_id];cp=comm.candidate_pos(ca)
                    good,proof,mg=comm.interval_certificate(cp,p.a,p.b,'TR')
                    if good and comm.relay_backhaul_ok(ca):
                        chosen=s;ok=True;why=proof;margin=min(mg,comm.link_margin_db(cp,comm.gateway,'RG')-comm.extra_margin);break
        if not ok:
            # The independent partition can straddle a planned handover boundary.
            cuts=[p.start,p.end]
            cuts += [v for s in sol.relay.sorties for v in (s.link_complete_time,s.service_end_time) if p.start+1e-7<v<p.end-1e-7]
            if len(cuts)==2 and depth<8:cuts.append((p.start+p.end)/2)
            if len(cuts)>2:
                cuts=sorted(set(cuts))
                for aa,bb in zip(cuts,cuts[1:]):
                    q=CommPiece(p.trip_id,p.route_index,aa,bb,p.phase,
                        interpolate(p.a,p.b,(aa-p.start)/(p.end-p.start)),interpolate(p.a,p.b,(bb-p.start)/(p.end-p.start)))
                    verify_piece(q,depth+1)
                return
            failures.append((p.trip_id,p.start,p.end));return
        min_margin=min(min_margin,margin)
        interval_rows.append([p.trip_id,p.phase,p.start,p.end,'DIRECT_CERTIFIED' if chosen is None else 'RELAY_BACKUP_DIRECT_FIRST',
                              chosen.sortie_id if chosen else '',why,margin])
    for p in pieces:verify_piece(p)
    if failures:raise AssertionError(f'Continuous interval certificate failed: {failures[:8]}')
    # Extra pointwise audit records actual direct-first states, not proof by sampling.
    for p in pieces:
        times=np.linspace(p.start,p.end,max(1,math.ceil((p.end-p.start)/audit_step))+1)
        for t in times:
            pos=interpolate(p.a,p.b,(t-p.start)/(p.end-p.start))
            if comm.link_available(pos,comm.gateway,'TG'):
                mode='DIRECT';rid='';margin=comm.link_margin_db(pos,comm.gateway,'TG')
            else:
                options=[]
                for s in sol.relay.sorties:
                    if s.link_complete_time-1e-7<=t<=s.service_end_time+1e-7:
                        cp=Position3D(s.lon,s.lat,s.alt)
                        if comm.link_available(pos,cp,'TR') and comm.link_available(cp,comm.gateway,'RG'):
                            mg=min(comm.link_margin_db(pos,cp,'TR'),comm.link_margin_db(cp,comm.gateway,'RG'));options.append((mg,s.sortie_id))
                if not options:raise AssertionError(f'Independent point audit outage at {p.trip_id}, {t:.6f}s')
                margin,rid=max(options);mode='RELAY'
            samples.append([p.trip_id,float(t),p.phase,pos.lon,pos.lat,pos.alt,mode,rid,margin])
    # Aggregate guaranteed interval coverage, not equal-weight sample counts.
    for row in interval_rows:
        if row[4]=='DIRECT_CERTIFIED':direct_t+=row[3]-row[2]
        else:relay_t+=row[3]-row[2]
    report={'boxes':len(boxes),'delivered_once':len(deliveries),'hard_deadline_violations':0,
            'resource_conflicts':0,'continuous_unproven_intervals':0,'point_audit_outages':0,
            'point_audit_step_s':audit_step,'point_audit_samples':len(samples),'certified_intervals':len(interval_rows),
            'minimum_certified_link_margin_db':min_margin,'direct_certified_seconds':direct_t,
            'relay_backup_seconds':relay_t,'total_transport_airborne_and_delivery_seconds':direct_t+relay_t,
            'certificate_scope':'piecewise-constant DEM; straight three-stage flights; given deterministic link budget; instantaneous switching',
            'global_optimality_proved':False}
    return report,interval_rows,samples


def clean_json(x):
    if isinstance(x,dict):return {str(k):clean_json(v) for k,v in x.items()}
    if isinstance(x,(tuple,list)):return [clean_json(v) for v in x]
    if isinstance(x,(np.integer,)):return int(x)
    if isinstance(x,(float,np.floating)):return float(x) if math.isfinite(x) else None
    return x

def dump_json(path,obj):
    path.write_text(json.dumps(clean_json(obj),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

def csv_file(path,headers,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(headers);w.writerows(rows)

def snapshot(sol,planner):
    return {'routes':[[{'service_id':s.service_id,'box_ids':s.box_ids} for s in r.stops] for r in sol.routes],
            'release':[sol.release.get(r.signature(),0.) for r in sol.routes],
            'transport_trips':[asdict(t) for t in sol.decoded.trips],
            'relay_sorties':[asdict(s) for s in sol.relay.sorties],
            'relay_sites':{cid:asdict(planner.sites[cid]) for cid in {s.candidate_id for s in sol.relay.sorties}},
            'objective_vector':joint_vector(sol),'normalized_score':sol.score,'milp':sol.relay.diagnostics}

def export_results(out,sol,archive,history,planner,nodes,dtypes,dunits,bats,arcs,comm,audit_step,metadata,plots=True):
    out.mkdir(parents=True,exist_ok=True)
    # Independent object: do not reuse the optimizer's interval-certificate cache.
    fresh=CertifiedCommunication(comm.terrain,nodes,comm.p,comm.extra_margin)
    audit,intervals,samples=audit_solution(sol,nodes,dtypes,dunits,bats,planner.spec,planner.units,arcs,fresh,planner,audit_step)
    mm=sol.metrics;v=joint_vector(sol);rr=sol.relay.sorties
    summary={'weighted_lateness':v[0],'joint_finish_s':v[1],'total_energy_kwh':v[2],'total_sorties':v[3],
             'transport_sorties':mm.trips,'relay_sorties':len(rr),'transport_energy_kwh':mm.energy,
             'relay_energy_kwh':sum(s.energy_kwh for s in rr),'transport_finish_s':mm.makespan,
             'normalized_objective':sol.score,'solver':sol.relay.diagnostics,'audit':audit,'run':metadata}
    dump_json(out/'Q3_summary.json',summary);dump_json(out/'Q3_solution.json',snapshot(sol,planner))
    dump_json(out/'Q3_audit.json',audit);dump_json(out/'Q3_MILP_log.json',planner.logs)
    dump_json(out/'Q3_pareto_solutions.json',[snapshot(s,planner)|{'label':s.label} for s in archive])
    rows=[]
    for t in sol.decoded.trips:
        rows.append([t.trip_id,t.drone_id,t.drone_type,t.battery_id,t.start_time,t.takeoff_time,
                     'O01→'+'→'.join(t.service_sequence)+'→O01',t.return_time,t.energy_kwh,t.end_soc,t.charge_end_time,
                     '|'.join(sol.routes[t.route_index].box_ids())])
    csv_file(out/'Q3_运输架次.csv',['运输架次','无人机','机型','电池','准备开始_s','起飞_s','路线','返回O01_s','能耗_kWh','返航SOC','充满_s','货箱清单'],rows)
    rows=[];boxes=planner.boxes
    for t in sol.decoded.trips:
        for bid,tm in t.box_delivery.items():
            b=boxes[bid];rows.append([bid,b.service_id,b.material_type,t.trip_id,tm,b.expected_time,b.hard_deadline,
                                      b.first_batch,b.priority,max(0.,tm-b.expected_time),tm<=b.expected_time+1e-6])
    csv_file(out/'Q3_逐箱交付.csv',['货箱','服务区','物资类型','运输架次','交付完成_s','期望_s','硬截止_s','首批','优先系数','迟到_s','按期望送达'],sorted(rows))
    rows=[]
    for s in rr:
        ca=planner.sites[s.candidate_id];outt,rett,ee,cruise=relay_flight_profile(ca,comm,planner.spec)
        rows.append([s.sortie_id,s.relay_id,s.component_id,s.start_time,s.lon,s.lat,ca.ground_alt,ca.agl,s.alt,cruise,
                     s.link_complete_time,s.service_end_time,s.return_time,s.return_time+planner.spec.turnaround_time,
                     s.energy_kwh,s.end_soc,s.charge_end_time,'|'.join(s.demand_ids),outt,rett])
    csv_file(out/'Q3_中继架次.csv',['中继架次','中继无人机','能源组件','准备开始_s','经度','纬度','DEM海拔_m','悬停离地_m','悬停海拔_m',
        '转场巡航海拔_m','建链完成_s','服务结束_s','返回O01_s','无人机周转完成_s','能耗_kWh','返航SOC','充满_s','保障运输架次','去程飞行_s','回程飞行_s'],rows)
    csv_file(out/'Q3_通信区间证书.csv',['运输架次','阶段','开始_s','结束_s','保证方式','备用中继架次','证书类型','链路余量下界_dB'],intervals)
    csv_file(out/'Q3_通信逐点审计.csv',['运输架次','时刻_s','阶段','经度','纬度','海拔_m','实际直连优先状态','中继架次','余量_dB'],samples)
    rows=[]
    for t in sol.decoded.trips:
        rows += [['运输无人机',t.drone_id,t.trip_id,t.start_time,t.return_time],
                 ['共享电池任务',t.battery_id,t.trip_id,t.start_time,t.return_time],
                 ['共享电池充电',t.battery_id,t.trip_id,t.return_time,t.charge_end_time]]
    for s in rr:
        rows += [['中继无人机任务',s.relay_id,s.sortie_id,s.start_time,s.return_time],
                 ['中继无人机周转',s.relay_id,s.sortie_id,s.return_time,s.return_time+planner.spec.turnaround_time],
                 ['能源组件任务',s.component_id,s.sortie_id,s.start_time,s.return_time],
                 ['能源组件充电',s.component_id,s.sortie_id,s.return_time,s.charge_end_time]]
    csv_file(out/'Q3_资源占用.csv',['资源类别','资源编号','架次','开始_s','结束_s'],rows)
    csv_file(out/'Q3_搜索日志.csv',['iteration','operator','feasible','accepted','candidate_score','best_score','seconds','reason'],
             [[h[k] for k in ('iteration','operator','feasible','accepted','candidate_score','best_score','seconds','reason')] for h in history])
    csv_file(out/'Q3_非支配解.csv',['方案','加权迟到','联合完成_s','总能耗_kWh','总架次','运输架次','中继架次','归一化目标'],
             [[s.label,*joint_vector(s),s.metrics.trips,len(s.relay.sorties),s.score] for s in sorted(archive,key=lambda x:x.score)])
    arcs.export_csv(out/'Q3_航段库.csv')
    if plots:plot_joint_results(out,sol,archive,planner,nodes,comm)
    print(json.dumps(clean_json({k:v for k,v in summary.items() if k not in ('solver','run')}),ensure_ascii=False,indent=2),flush=True)
    return summary

def plot_joint_results(out,sol,archive,planner,nodes,comm):
    from matplotlib.colors import LinearSegmentedColormap
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
    colors={'A':'#23648A','B':'#178D96','C':'#47AD9C'}
    fig,(ax,gg)=plt.subplots(1,2,figsize=(14,6),gridspec_kw={'width_ratios':[1.1,1]})
    lonlo=min(n.lon for n in nodes.values())-.018;lonhi=max(n.lon for n in nodes.values())+.018
    latlo=min(n.lat for n in nodes.values())-.018;lathi=max(n.lat for n in nodes.values())+.018
    z=comm.dem.copy();z[abs(z-comm.nodata)<1e-6]=np.nan
    cmap=LinearSegmentedColormap.from_list('terrain_bluegreen',['#EDF5F4','#B4D4CC','#6A9D9A','#375D71'])
    ax.imshow(z,extent=[comm.lon.min(),comm.lon.max(),comm.lat.min(),comm.lat.max()],origin='upper',cmap=cmap,alpha=.68)
    for tr in sol.decoded.trips:
        seq=['O01']+tr.service_sequence+['O01'];ax.plot([nodes[x].lon for x in seq],[nodes[x].lat for x in seq],color=colors[tr.drone_type],lw=.7,alpha=.5)
    for nid,n in nodes.items():
        ax.scatter(n.lon,n.lat,s=32 if nid!='O01' else 90,marker='o' if nid!='O01' else '*',c='#13354C',zorder=5)
        ax.annotate(nid,(n.lon,n.lat),xytext=(4,4),textcoords='offset points',fontsize=8,color='#163B52')
    unique={s.candidate_id for s in sol.relay.sorties}
    for i,cid in enumerate(sorted(unique),1):
        ca=planner.sites[cid];ax.scatter(ca.lon,ca.lat,s=48,marker='D',c='#D48747',edgecolor='white',zorder=6)
        ax.annotate(f'H{i}',(ca.lon,ca.lat),xytext=(4,-10),textcoords='offset points',fontsize=8)
        lonlo=min(lonlo,ca.lon-.005);lonhi=max(lonhi,ca.lon+.005);latlo=min(latlo,ca.lat-.005);lathi=max(lathi,ca.lat+.005)
    ax.set(xlim=(lonlo,lonhi),ylim=(latlo,lathi),xlabel='Longitude (deg)',ylabel='Latitude (deg)',title='Transport routes and relay hover sites')
    ax.set_aspect(1/math.cos(math.radians(23)));ax.ticklabel_format(useOffset=False)
    units=sorted({t.drone_id for t in sol.decoded.trips})+sorted({s.relay_id for s in sol.relay.sorties})
    ypos={u:i for i,u in enumerate(units)}
    for t in sol.decoded.trips:gg.barh(ypos[t.drone_id],(t.return_time-t.start_time)/3600,left=t.start_time/3600,color=colors[t.drone_type],height=.64,edgecolor='white',linewidth=.6)
    for s in sol.relay.sorties:
        y=ypos[s.relay_id];gg.barh(y,(s.return_time-s.start_time)/3600,left=s.start_time/3600,color='#DCEBE8',height=.64)
        gg.barh(y,(s.service_end_time-s.link_complete_time)/3600,left=s.link_complete_time/3600,color='#DB985C',height=.64)
    gg.set(yticks=range(len(units)),yticklabels=units,xlabel='Time (h)',title='Joint schedule including return flights');gg.invert_yaxis();gg.grid(axis='x',alpha=.2)
    from matplotlib.patches import Patch
    gg.legend(handles=[Patch(color=colors[g],label='Type '+g) for g in colors]+[Patch(color='#DB985C',label='Relay service'),Patch(color='#DCEBE8',label='Relay prep / flight')],
              loc='upper center',bbox_to_anchor=(.5,-.12),ncol=3,fontsize=8,frameon=False)
    fig.tight_layout()
    for ext in ('svg','png'):fig.savefig(out/f'Q3_routes_and_schedule.{ext}',dpi=220,bbox_inches='tight')
    plt.close(fig)
    if len(archive)>1:
        fig,ax=plt.subplots(figsize=(6.5,4.7));vv=np.array([joint_vector(s) for s in archive])
        same_time=np.ptp(vv[:,1])<1e-6
        xx=np.array([len(s.relay.sorties) for s in archive]) if same_time else vv[:,1]/3600
        pts=ax.scatter(xx,vv[:,2],c=vv[:,0],cmap='winter',s=90,edgecolor='white')
        v=joint_vector(sol);ax.scatter(len(sol.relay.sorties) if same_time else v[1]/3600,v[2],s=180,marker='*',c='#D48747',label='Selected preference')
        ax.set(xlabel='Relay sorties' if same_time else 'Joint completion time (h)',ylabel='Transport + relay energy (kWh)',title='Nondominated solutions found by search')
        if same_time:ax.set_xticks(sorted(set(xx)))
        ax.legend();fig.colorbar(pts,ax=ax,label='Weighted lateness');fig.tight_layout()
        for ext in ('svg','png'):fig.savefig(out/f'Q3_tradeoffs.{ext}',dpi=220,bbox_inches='tight')
        plt.close(fig)

def main():
    parser=argparse.ArgumentParser(description='Q3 joint transport and relay scheduling with continuous interval certificates')
    parser.add_argument('--root',type=Path,default=Path.cwd(),help='Folder containing one consistent version of the 5 XLSX files and DEM MAT')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'output_v3')
    parser.add_argument('--transport-iters',type=int,default=0)
    parser.add_argument('--joint-iters',type=int,default=12)
    parser.add_argument('--seed',type=int,default=20260924)
    parser.add_argument('--seed-solution',type=Path,help='Optional Q3_solution.json or initial checkpoint; routes and release times are re-evaluated')
    parser.add_argument('--grid-cells',type=int,default=64)
    parser.add_argument('--heights',default='120,240,300',help='Candidate AGL levels; local geometric repair also adds levels')
    parser.add_argument('--sites',type=int,default=64,help='Size of restricted relay MILP site pool; increase for sensitivity')
    parser.add_argument('--window-step',type=float,default=300.)
    parser.add_argument('--comm-step',type=float,default=30.,help='Maximum interval length; each interval must be certified, not merely sampled')
    parser.add_argument('--audit-step',type=float,default=1.,help='Independent direct-first point audit in addition to continuous certificates')
    parser.add_argument('--mip-time',type=float,default=20.)
    parser.add_argument('--mip-gap',type=float,default=.01)
    parser.add_argument('--repair-rounds',type=int,default=48)
    parser.add_argument('--max-stops',type=int,default=4,help='Heuristic route-pool restriction, not a task constraint; may be raised to 15')
    parser.add_argument('--extra-margin-db',type=float,default=0.)
    parser.add_argument('--weights',default='.45,.25,.20,.10',help='lateness,makespan,energy,total_sorties; nonnegative and sum to one')
    parser.add_argument('--no-plots',action='store_true')
    args=parser.parse_args();t0=time.time();args.output.mkdir(parents=True,exist_ok=True)
    global MAX_STOPS_PER_ROUTE
    if not 1<=args.max_stops<=15:parser.error('--max-stops must be between 1 and 15.')
    MAX_STOPS_PER_ROUTE=args.max_stops
    if min(args.comm_step,args.audit_step,args.window_step,args.mip_time)<=0 or args.sites<1 or args.grid_cells<1:parser.error('Step sizes, limits and site counts must be positive.')
    w=[float(x) for x in args.weights.split(',')]
    if len(w)!=4 or min(w)<0 or abs(sum(w)-1)>1e-6:parser.error('Provide four nonnegative objective weights summing to one.')
    weights=dict(zip(('lateness','makespan','energy','trips'),w))
    names={'nodes':'调度中心与服务区','boxes':'物资需求与配送时限','drones':'运输无人机数据','relay':'中继无人机数据','comm':'通信链路参数','dem':'镇龙乡及周边30米DEM'}
    files={k:read_input(args.root,n,'.mat' if k=='dem' else '.xlsx') for k,n in names.items()}
    print('[1/6] Read task data and build exact DEM arc library',flush=True)
    nodes=load_nodes(files['nodes']);boxes=load_boxes(files['boxes']);dtypes,dunits,bats=load_drone_data(files['drones'])
    rspec,runits=load_relay_data(files['relay']);cp=load_comm_params(files['comm']);terrain=Terrain(files['dem'])
    arcs=ExactArcLibrary(nodes,dtypes,terrain);evaluator=RouteEvaluator(boxes,dtypes,arcs);decoder=ResourceDecoder(evaluator,dunits,bats,boxes)
    routes=construct_initial_routes(boxes,evaluator)
    routes,decoded,metrics,_=alns_optimize(routes,boxes,nodes,evaluator,decoder,max(0,args.transport_iters),args.seed)
    scales={'lateness':max(1.,metrics.weighted_lateness),'makespan':max(1.,metrics.makespan),'energy':max(1.,metrics.energy),'trips':max(1,metrics.trips)}
    release={}
    if args.seed_solution:
        saved=json.loads(args.seed_solution.read_text(encoding='utf-8'))
        routes=[Route([Stop(**s) for s in r]) for r in saved['routes']]
        ensure_route_integrity(routes,boxes)
        release={r.signature():float(v) for r,v in zip(routes,saved['release'])}
    print('[2/6] Generate 3D relay candidates and certify coverage intervals',flush=True)
    comm=CertifiedCommunication(terrain,nodes,cp,args.extra_margin_db)
    planner=RelayPlanner(comm,rspec,runits,args.grid_cells,tuple(float(h) for h in args.heights.split(',')),args.mip_time,args.mip_gap,args.sites,args.window_step)
    planner.boxes=boxes
    print('[3/6] Joint feasibility repair using resource-constrained relay MILP',flush=True)
    initial,diag=evaluate_joint(routes,release,decoder,nodes,dtypes,arcs,comm,planner,weights,scales,args.comm_step,args.repair_rounds,True)
    if initial is None:
        dump_json(args.output/'Q3_failure.json',{'reason':diag,'solver_calls':planner.logs,'claim':'No feasible solution found in this restricted search; this is not proof that the original problem is infeasible.'})
        raise RuntimeError('No joint feasible solution found; inspect Q3_failure.json. Increase --sites, reduce --window-step, or expand route search.')
    initial.label='initial_joint_feasible';dump_json(args.output/'Q3_initial_checkpoint.json',snapshot(initial,planner))
    print(f'[4/6] Joint neighborhood search; initial vector={joint_vector(initial)}',flush=True)
    best,archive,history=joint_search(initial,decoder,nodes,dtypes,arcs,comm,planner,weights,scales,args.comm_step,max(0,args.joint_iters),args.seed)
    print('[5/6] Independent resource, continuous-interval and pointwise audits',flush=True)
    import scipy,openpyxl
    metadata={'seed':args.seed,'transport_iterations':args.transport_iters,'joint_iterations':args.joint_iters,
              'weights':weights,'normalization_scales':scales,'max_stops_per_route':MAX_STOPS_PER_ROUTE,
              'candidate_grid_cells':args.grid_cells,'candidate_heights_agl':args.heights,'restricted_site_limit':args.sites,
              'window_step_s':args.window_step,'comm_interval_step_s':args.comm_step,'mip_time_s':args.mip_time,
              'mip_relative_gap':args.mip_gap,'extra_margin_db':args.extra_margin_db,'runtime_before_audit_s':time.time()-t0,
              'python':sys.version.split()[0],'numpy':np.__version__,'scipy':scipy.__version__,'openpyxl':openpyxl.__version__,'platform':platform.platform(),
              'sources':{k:{'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for k,p in files.items()}}
    export_results(args.output,best,archive,history,planner,nodes,dtypes,dunits,bats,arcs,comm,args.audit_step,metadata,not args.no_plots)
    print(f'[6/6] Done in {time.time()-t0:.2f}s. Results: {args.output.resolve()}',flush=True)

if __name__=='__main__':main()
