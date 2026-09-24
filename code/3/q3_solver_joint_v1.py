# -*- coding: utf-8 -*-
"""
问题三：通信约束下运输与中继联合调度（独立可执行基线）
==========================================================

功能：
1. 自动读取节点、逐箱货箱、运输无人机、DEM 与结果提交模板；
2. 预计算 16 个节点之间的有向航段库；
3. Route Evaluator：容量、逐段载荷、飞行时间、能耗、返航 SOC、逐点交付时刻、最晚开始时刻；
4. 构造一份完整初始路线解；
5. 使用简化 ALNS（destroy + regret-2 repair + SA 接受）改进路线结构；
6. Resource Decoder 自动安排机型、实体无人机、共享电池和开始时刻；
7. 校验全部货箱唯一交付、硬时限、无人机/电池冲突；
8. 输出 Q2_运输架次、Q2_逐箱交付，并回填“结果提交模板.xlsx”；
9. 输出路线图、无人机甘特图、交付及时性图、电池周转图、ALNS 收敛图。

默认目录约定：
    HUAWEI-CUP/
      code/2/q2_solver.py
      code/2/output/              <- 本程序自动创建
      数据/无人机应急物资运输基础数据/*.xlsx
      数据/镇龙乡地理空间数据/镇龙乡及周边地理数据/数字高程模型数据（DEM）/*.mat
      结果提交模板.xlsx

若脚本不在上述位置，可运行：
    python q2_solver.py --root "D:/.../HUAWEI-CUP"

依赖：numpy, scipy, openpyxl, matplotlib
建议 Python 3.10+
"""

from __future__ import annotations

import argparse
import copy
import csv
import math
import random
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Iterable, Set, Any

import numpy as np
from scipy.io import loadmat
from openpyxl import load_workbook, Workbook
import matplotlib.pyplot as plt


# ============================================================
# 0. 可统一修改的路径与算法参数
# ============================================================

SCRIPT_PATH = Path(__file__).resolve()
# 预期脚本位于 PROJECT_ROOT/code/2/q2_solver.py
DEFAULT_PROJECT_ROOT = SCRIPT_PATH.parents[2] if len(SCRIPT_PATH.parents) >= 3 else Path.cwd()

# 如果你希望完全固定路径，也可以改成：
# PROJECT_ROOT = Path(r"D:\SF_DIR\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP")
PROJECT_ROOT = DEFAULT_PROJECT_ROOT

# 默认把结果放在 code/2/output，符合“代码目录下单开 output 文件夹”的要求
DEFAULT_OUTPUT_DIR = SCRIPT_PATH.parent / "output"

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
    """少量 Stop 下用全排列（<=4），选局部代理代价最小的顺序。"""
    import itertools
    if len(route.stops) <= 1:
        return route.clone()
    best = route.clone()
    best_c = evaluator.proxy_cost(best)
    # MAX_STOPS_PER_ROUTE <=4，因此最多 24 个排列，完全可承受
    for perm in itertools.permutations(route.stops):
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
            for ri in unscheduled:
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
# 8. 输出：CSV、模板 Excel、详细 Excel、图表
# ============================================================

def write_csvs(decoded: DecodeResult, boxes: Dict[str, Box], output_dir: Path) -> Tuple[Path, Path]:
    trip_path = output_dir / "Q2_运输架次.csv"
    box_path = output_dir / "Q2_逐箱交付.csv"

    with trip_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["架次编号", "无人机编号", "机型编号", "电池编号", "开始时刻（s）", "访问服务区顺序", "返回O01时刻（s）", "架次能耗（kWh）"])
        for t in decoded.trips:
            w.writerow([
                t.trip_id, t.drone_id, t.drone_type, t.battery_id,
                round(t.start_time, 3), "→".join(t.service_sequence),
                round(t.return_time, 3), round(t.energy_kwh, 6)
            ])

    trip_by_box = {}
    for t in decoded.trips:
        for bid in t.box_delivery:
            trip_by_box[bid] = t.trip_id
    with box_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["货箱编号", "架次编号", "服务区编号", "交付完成时刻（s）"])
        for bid in sorted(boxes):
            w.writerow([bid, trip_by_box[bid], boxes[bid].service_id, round(decoded.box_delivery[bid], 3)])
    return trip_path, box_path


def fill_template(template_path: Path, decoded: DecodeResult, boxes: Dict[str, Box], output_path: Path) -> None:
    shutil.copy2(template_path, output_path)
    wb = load_workbook(output_path)
    ws_trip = wb["Q2_运输架次"]
    ws_box = wb["Q2_逐箱交付"]

    # 清理旧数据（保留第一行表头）
    for ws, max_col in [(ws_trip, 8), (ws_box, 4)]:
        for row in ws.iter_rows(min_row=2, max_col=max_col):
            for c in row:
                c.value = None

    for r, t in enumerate(decoded.trips, start=2):
        vals = [
            t.trip_id, t.drone_id, t.drone_type, t.battery_id,
            round(t.start_time, 3), "→".join(t.service_sequence),
            round(t.return_time, 3), round(t.energy_kwh, 6)
        ]
        for c, v in enumerate(vals, start=1):
            ws_trip.cell(r, c, v)

    trip_by_box = {}
    for t in decoded.trips:
        for bid in t.box_delivery:
            trip_by_box[bid] = t.trip_id
    for r, bid in enumerate(sorted(boxes), start=2):
        vals = [bid, trip_by_box[bid], boxes[bid].service_id, round(decoded.box_delivery[bid], 3)]
        for c, v in enumerate(vals, start=1):
            ws_box.cell(r, c, v)

    wb.save(output_path)


def write_detailed_xlsx(decoded: DecodeResult, boxes: Dict[str, Box], output_path: Path, metrics: Metrics) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "架次详情"
    ws.append(["架次编号", "无人机", "机型", "电池", "开始", "起飞", "返回", "路线", "能耗kWh", "返航SOC", "充电完成"])
    for t in decoded.trips:
        ws.append([t.trip_id, t.drone_id, t.drone_type, t.battery_id, t.start_time, t.takeoff_time, t.return_time,
                   "→".join(t.service_sequence), t.energy_kwh, t.end_soc, t.charge_end_time])

    ws2 = wb.create_sheet("逐箱详情")
    ws2.append(["货箱编号", "服务区", "物资类型", "交付时刻", "期望时刻", "硬截止", "优先系数", "迟到量", "迟到加权"])
    for bid in sorted(boxes):
        b = boxes[bid]; t = decoded.box_delivery[bid]
        late = max(0.0, t - b.expected_time)
        ws2.append([bid, b.service_id, b.material_type, t, b.expected_time, b.hard_deadline, b.priority, late,
                    b.priority * late / max(b.expected_time, 1.0)])

    ws3 = wb.create_sheet("航段详情")
    ws3.append(["架次", "from", "to", "载荷kg", "体积m3", "距离m", "爬升m", "下降m", "飞行时间s", "能耗kWh"])
    for t in decoded.trips:
        for le in t.route_eval.legs:
            ws3.append([t.trip_id, le.from_node, le.to_node, le.payload_mass, le.payload_volume,
                        le.distance_m, le.climb_m, le.descent_m, le.flight_time, le.energy_kwh])

    ws4 = wb.create_sheet("指标汇总")
    ws4.append(["指标", "数值"])
    ws4.append(["加权迟到", metrics.weighted_lateness])
    ws4.append(["全部任务完成时间(s)", metrics.makespan])
    ws4.append(["总运输能耗(kWh)", metrics.energy])
    ws4.append(["总架次数", metrics.trips])
    ws4.append(["硬时限违反数", metrics.hard_violations])
    wb.save(output_path)


def plot_routes(decoded: DecodeResult, nodes: Dict[str, Node], out: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 8))
    for t in decoded.trips:
        seq = ["O01"] + t.service_sequence + ["O01"]
        xs = [nodes[n].lon for n in seq]
        ys = [nodes[n].lat for n in seq]
        ax.plot(xs, ys, alpha=0.55, linewidth=1.2)
    for nid, n in nodes.items():
        ax.scatter(n.lon, n.lat, s=45 if nid == "O01" else 22)
        ax.text(n.lon, n.lat, nid, fontsize=8, ha="left", va="bottom")
    ax.set_title("Q2 Transport Routes")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(alpha=0.25)
    fig.tight_layout(); fig.savefig(out, dpi=180); plt.close(fig)


def plot_drone_gantt(decoded: DecodeResult, out: Path) -> None:
    ids = sorted(set(t.drone_id for t in decoded.trips))
    ymap = {d: i for i, d in enumerate(ids)}
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for t in decoded.trips:
        ax.barh(ymap[t.drone_id], t.return_time - t.start_time, left=t.start_time, height=0.55)
        ax.text((t.start_time + t.return_time) / 2, ymap[t.drone_id], t.trip_id, ha="center", va="center", fontsize=7)
    ax.set_yticks(list(ymap.values()), list(ymap.keys()))
    ax.set_xlabel("Time (s)")
    ax.set_title("Q2 Drone Schedule Gantt")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout(); fig.savefig(out, dpi=180); plt.close(fig)


def plot_delivery(decoded: DecodeResult, boxes: Dict[str, Box], out: Path) -> None:
    ordered = sorted(boxes.values(), key=lambda b: (b.expected_time, -b.priority, b.box_id))
    x = np.arange(len(ordered))
    actual = [decoded.box_delivery[b.box_id] for b in ordered]
    expected = [b.expected_time for b in ordered]
    hard = [b.hard_deadline if b.hard_deadline is not None else np.nan for b in ordered]
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(x, actual, marker=".", linewidth=1.0, label="actual")
    ax.plot(x, expected, linewidth=1.0, label="expected")
    ax.scatter(x, hard, marker="x", label="hard deadline")
    ax.set_xlabel("Boxes sorted by expected time")
    ax.set_ylabel("Time (s)")
    ax.set_title("Q2 Box Delivery Timeliness")
    ax.grid(alpha=0.25); ax.legend()
    fig.tight_layout(); fig.savefig(out, dpi=180); plt.close(fig)


def plot_battery(decoded: DecodeResult, out: Path) -> None:
    ids = sorted(set(t.battery_id for t in decoded.trips))
    ymap = {b: i for i, b in enumerate(ids)}
    fig, ax = plt.subplots(figsize=(11, max(5, 0.35 * len(ids) + 2)))
    for t in decoded.trips:
        y = ymap[t.battery_id]
        # 任务占用
        ax.barh(y, t.return_time - t.start_time, left=t.start_time, height=0.55)
        # 充电期用轮廓线表示
        ax.barh(y, t.charge_end_time - t.return_time, left=t.return_time, height=0.30, fill=False, edgecolor="black", linewidth=0.8)
    ax.set_yticks(list(ymap.values()), list(ymap.keys()))
    ax.set_xlabel("Time (s)")
    ax.set_title("Q2 Battery Use and Recharge")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout(); fig.savefig(out, dpi=180); plt.close(fig)


def plot_alns(history: List[Tuple[int, float]], out: Path) -> None:
    """运输层 ALNS 收敛图。iters=0 时也显示初始点，避免生成空坐标轴。"""
    if not history:
        history = [(0, 1.0)]
    xs = [x for x, _ in history]
    ys = [y for _, y in history]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(xs, ys, marker="o", markersize=4.5, linewidth=1.5)
    if len(history) == 1:
        ax.annotate("feasibility baseline\n(no transport ALNS iterations)",
                    (xs[0], ys[0]), xytext=(14, 12), textcoords="offset points", fontsize=9)
        ax.set_xlim(-0.5, 1.5)
    ax.set_xlabel("Transport-ALNS iteration")
    ax.set_ylabel("Best normalized transport objective")
    ax.set_title("Q3 Transport-layer ALNS Convergence")
    ax.grid(alpha=0.25)
    fig.tight_layout(); fig.savefig(out, dpi=180); plt.close(fig)




# ============================================================
# 9. Q3 通信、中继与联合调度扩展（本文件独立实现，不调用 q2_solver）
# ============================================================

COMM_SAMPLE_STEP = 30.0              # 通信规划采样步长（s）
COMM_AUDIT_STEP = 2.0               # 最终高精度审计步长（s）
RELAY_HEIGHT_LEVELS = (150.0, 225.0, 300.0)
RELAY_LOCAL_GRID_OFFSETS = (-70, -35, 0, 35, 70)
RELAY_MERGE_GAP = 45.0              # 相邻中继需求小间隔可合并（s）
MAX_GAP_SPLIT_DEPTH = 6


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


def _sample_interval(start: float, end: float, step: float) -> List[float]:
    if end <= start + 1e-9: return []
    xs = list(np.arange(start, end, step, dtype=float))
    if not xs or end - xs[-1] > 0.35 * step:
        xs.append(max(start, end - 1e-6))
    return xs


def build_trip_trajectory(trip: ScheduledTrip, nodes: Dict[str, Node], dtypes: Dict[str, DroneType], arcs: ArcLibrary, step: float) -> List[TrajectorySample]:
    dt = dtypes[trip.drone_type]
    seq = ["O01"] + trip.service_sequence + ["O01"]
    t = trip.takeoff_time
    out: List[TrajectorySample] = []
    stop_idx = 0
    for li, le in enumerate(trip.route_eval.legs):
        a, b = seq[li], seq[li + 1]
        na, nb = nodes[a], nodes[b]
        arc = arcs[(a, b)]
        tc = arc.climb_m / dt.climb_speed
        th = arc.distance_m / dt.cruise_speed
        td = arc.descent_m / dt.descent_speed
        # 爬升：水平位置保持起点
        for tm in _sample_interval(t, t + tc, step):
            f = (tm - t) / max(tc, 1e-9)
            out.append(TrajectorySample(trip.trip_id, tm, f"爬升 {a}→{b}", Position3D(na.lon, na.lat, na.work_alt + f * arc.climb_m)))
        t += tc
        # 巡航：在计划巡航海拔做线性水平插值
        for tm in _sample_interval(t, t + th, step):
            f = (tm - t) / max(th, 1e-9)
            out.append(TrajectorySample(trip.trip_id, tm, f"巡航 {a}→{b}", Position3D(na.lon + f * (nb.lon - na.lon), na.lat + f * (nb.lat - na.lat), arc.cruise_alt_m)))
        t += th
        # 下降：水平位置保持终点
        for tm in _sample_interval(t, t + td, step):
            f = (tm - t) / max(td, 1e-9)
            out.append(TrajectorySample(trip.trip_id, tm, f"下降 {a}→{b}", Position3D(nb.lon, nb.lat, arc.cruise_alt_m - f * arc.descent_m)))
        t += td
        if b != "O01":
            service = trip.route_eval.stops[stop_idx].service_duration
            stop_idx += 1
            for tm in _sample_interval(t, t + service, step):
                out.append(TrajectorySample(trip.trip_id, tm, f"投送 {b}", Position3D(nb.lon, nb.lat, nb.work_alt)))
            t += service
    return out


def build_trajectories(decoded: DecodeResult, nodes: Dict[str, Node], dtypes: Dict[str, DroneType], arcs: ArcLibrary, step: float) -> Dict[str, List[TrajectorySample]]:
    return {t.trip_id: build_trip_trajectory(t, nodes, dtypes, arcs, step) for t in decoded.trips}


def mark_direct_coverage(trajectories: Dict[str, List[TrajectorySample]], comm: CommunicationModel) -> None:
    for arr in trajectories.values():
        for s in arr:
            s.direct_ok = comm.link_available(s.pos, comm.gateway, "TG")


def extract_demands(trajectories: Dict[str, List[TrajectorySample]], step: float) -> Tuple[List[CommDemand], Dict[str, TrajectorySample]]:
    demands: List[CommDemand] = []
    sample_map: Dict[str, TrajectorySample] = {}
    counter = 0
    for tid, arr in trajectories.items():
        bad = [(i, s) for i, s in enumerate(arr) if not s.direct_ok]
        if not bad: continue
        groups: List[List[Tuple[int, TrajectorySample]]] = [[bad[0]]]
        for item in bad[1:]:
            if item[1].time - groups[-1][-1][1].time <= 1.6 * step:
                groups[-1].append(item)
            else:
                groups.append([item])
        for g in groups:
            counter += 1
            ids = []
            for i, s in g:
                sid = f"{tid}@{i}"
                sample_map[sid] = s; ids.append(i)
            demands.append(CommDemand(f"D{counter:03d}", tid, g[0][1].time, g[-1][1].time + step, ids))
    return demands, sample_map


def local_candidate_pool(demand: CommDemand, traj: List[TrajectorySample], comm: CommunicationModel, relay_spec: RelaySpec) -> Dict[str, RelayCandidate]:
    bad_samples = [traj[i] for i in demand.sample_indices]
    mid = bad_samples[len(bad_samples)//2].pos
    r0, c0 = comm.nearest_rc(mid.lon, mid.lat)
    local_rc = set()
    for dr in RELAY_LOCAL_GRID_OFFSETS:
        for dc in RELAY_LOCAL_GRID_OFFSETS:
            local_rc.add((r0 + dr, c0 + dc))
    for ss in bad_samples[::max(1, len(bad_samples)//5)]:
        local_rc.add(comm.nearest_rc(ss.pos.lon, ss.pos.lat))
    for n in comm.nodes.values():
        if n.node_id != "O01": local_rc.add(comm.nearest_rc(n.lon, n.lat))

    # 稀疏全局共享高点：仅取最大允许高度，控制计算量。
    global_rc = {(rr,cc) for rr in range(0,comm.dem.shape[0],100) for cc in range(0,comm.dem.shape[1],100)}
    pool: Dict[str, RelayCandidate] = {}
    local_levels = [h for h in RELAY_HEIGHT_LEVELS if h <= relay_spec.max_hover_agl + 1e-9]
    if relay_spec.max_hover_agl not in local_levels: local_levels.append(relay_spec.max_hover_agl)
    for r,c in local_rc:
        for agl in local_levels:
            cand=comm.candidate(r,c,agl)
            if cand is not None and comm.relay_backhaul_ok(cand): pool[cand.candidate_id]=cand
    for r,c in global_rc:
        cand=comm.candidate(r,c,relay_spec.max_hover_agl)
        if cand is not None and comm.relay_backhaul_ok(cand): pool[cand.candidate_id]=cand
    return pool

def candidate_covers_samples(cand: RelayCandidate, samples: List[TrajectorySample], comm: CommunicationModel) -> Tuple[bool, float]:
    cp = comm.candidate_pos(cand)
    min_margin = math.inf
    for s in samples:
        margin = comm.link_margin_db(s.pos, cp, "TR")
        min_margin = min(min_margin, margin)
        if margin < -1e-9:
            return False, min_margin
    return True, min_margin


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


def feasible_options_for_demand(demand: CommDemand, traj: List[TrajectorySample], comm: CommunicationModel, spec: RelaySpec) -> Tuple[List[str], Dict[str, RelayCandidate]]:
    samples = [traj[i] for i in demand.sample_indices]
    pool = local_candidate_pool(demand, traj, comm, spec)
    scored = []
    for cid, cand in pool.items():
        cp = comm.candidate_pos(cand)
        # 先用自由空间极限做廉价距离剪枝，避免大量无意义 DEM LOS 扫描。
        if any(comm.distance3d_m(x.pos, cp) > 6800.0 for x in samples[::max(1, len(samples)//6)]):
            continue
        ok, margin = candidate_covers_samples(cand, samples, comm)
        if not ok: continue
        energy, out_t, ret_t = relay_sortie_energy(cand, demand.end_time-demand.start_time, comm, spec)
        if energy > (1.0-spec.reserve_ratio)*spec.energy_kwh + 1e-9: continue
        required_start = demand.start_time - spec.link_time - out_t - spec.prep_time
        if required_start < -1e-9: continue
        scored.append((-(margin), energy, out_t, cid))
    scored.sort()
    return [x[3] for x in scored[:100]], pool


def split_and_option_demands(demands: List[CommDemand], trajectories: Dict[str, List[TrajectorySample]], comm: CommunicationModel, spec: RelaySpec, step: float) -> Tuple[List[CommDemand], Dict[str, RelayCandidate]]:
    """先把长失联段切成约 120 s 的通信服务片段，再为每段搜索固定中继点。

    这样允许运输机在不同时间片之间切换中继，但任一时刻仍只由一个中继保障；
    同时可以让两架中继分别占据两个共享悬停点，覆盖多个并发运输架次。
    """
    result: List[CommDemand] = []
    all_candidates: Dict[str, RelayCandidate] = {}
    counter = 0
    max_samples = max(4, int(round(120.0 / step)))

    seeds: List[CommDemand] = []
    for d in demands:
        idx = d.sample_indices
        for k in range(0, len(idx), max_samples):
            part = idx[k:k+max_samples]
            arr = trajectories[d.trip_id]
            seeds.append(CommDemand("", d.trip_id, arr[part[0]].time, arr[part[-1]].time + step, part))

    def rec(d: CommDemand, depth: int) -> None:
        nonlocal counter
        opts, pool = feasible_options_for_demand(d, trajectories[d.trip_id], comm, spec)
        all_candidates.update(pool)
        if opts:
            counter += 1; d.demand_id=f"D{counter:03d}"; d.options=opts; result.append(d); return
        if depth >= MAX_GAP_SPLIT_DEPTH or len(d.sample_indices) <= 2:
            raise RuntimeError(f"通信缺口 {d.trip_id}[{d.start_time:.1f},{d.end_time:.1f}] 找不到可行中继悬停点")
        k = len(d.sample_indices)//2
        left_idx=d.sample_indices[:k]; right_idx=d.sample_indices[k:]
        arr=trajectories[d.trip_id]
        dl=CommDemand("",d.trip_id,arr[left_idx[0]].time,arr[left_idx[-1]].time+step,left_idx)
        dr=CommDemand("",d.trip_id,arr[right_idx[0]].time,arr[right_idx[-1]].time+step,right_idx)
        rec(dl,depth+1); rec(dr,depth+1)

    for d in seeds: rec(d,0)
    return result, all_candidates

def temporal_clusters(demands: List[CommDemand]) -> List[List[CommDemand]]:
    ds=sorted(demands,key=lambda d:(d.start_time,d.end_time))
    if not ds:return []
    clusters=[]; cur=[ds[0]]; end=ds[0].end_time
    for d in ds[1:]:
        if d.start_time <= end + RELAY_MERGE_GAP:
            cur.append(d); end=max(end,d.end_time)
        else:
            clusters.append(cur); cur=[d]; end=d.end_time
    clusters.append(cur); return clusters


def choose_sessions_for_cluster(cluster: List[CommDemand], candidates: Dict[str, RelayCandidate], comm: CommunicationModel, spec: RelaySpec) -> List[RelaySession]:
    """在一个时间簇中精确搜索 1~2 个共享悬停点覆盖全部通信需求。"""
    all_ids = {d.demand_id for d in cluster}
    cover: Dict[str, Set[str]] = defaultdict(set)
    for d in cluster:
        for cid in d.options:
            cover[cid].add(d.demand_id)
    keys = [cid for cid, ss in cover.items() if ss]
    # 单点覆盖优先。
    singles = [cid for cid in keys if cover[cid] >= all_ids]
    if singles:
        chosen = [min(singles, key=lambda c: relay_sortie_energy(candidates[c], max(d.end_time for d in cluster)-min(d.start_time for d in cluster), comm, spec)[0])]
    else:
        # 精确枚举候选对。候选集来自每个需求已验证的可行点，因此这里只做集合运算，速度很快。
        chosen = []
        best_pair = None
        best_cost = math.inf
        for i, a in enumerate(keys):
            ca = cover[a]
            if len(ca) == 0: continue
            for b in keys[i+1:]:
                if (ca | cover[b]) >= all_ids:
                    # 用总服务窗口的中继能耗作为并列时的选择标准。
                    da = [d for d in cluster if d.demand_id in ca]
                    db = [d for d in cluster if d.demand_id not in ca and d.demand_id in cover[b]]
                    cost = 0.0
                    if da:
                        sa, ea = min(d.start_time for d in da), max(d.end_time for d in da)
                        cost += relay_sortie_energy(candidates[a], ea-sa, comm, spec)[0]
                    if db:
                        sb, eb = min(d.start_time for d in db), max(d.end_time for d in db)
                        cost += relay_sortie_energy(candidates[b], eb-sb, comm, spec)[0]
                    if cost < best_cost:
                        best_cost = cost; best_pair = (a,b)
        if best_pair is not None:
            chosen = list(best_pair)
    if not chosen:
        # 若一个传递时间簇无法由两个固定点整体覆盖，则进一步按“同时重叠”切分，
        # 而不是直接为每个需求单独起飞。
        ordered = sorted(cluster, key=lambda d:(d.start_time,d.end_time))
        subclusters=[]; cur=[]; current_end=-math.inf
        for d in ordered:
            if not cur or d.start_time <= current_end + 1e-9:
                cur.append(d); current_end=max(current_end,d.end_time)
            else:
                subclusters.append(cur); cur=[d]; current_end=d.end_time
        if cur: subclusters.append(cur)
        if len(subclusters)>1:
            out=[]
            for sc in subclusters: out.extend(choose_sessions_for_cluster(sc,candidates,comm,spec))
            return out
        # 最后退化为逐需求会话；资源解码若发现 >2 架同时需要，会明确报错。
        return [RelaySession(d.options[0], d.start_time, d.end_time, [d.demand_id]) for d in cluster]

    # 将每个需求分给覆盖它的所选候选点，优先平衡两个中继会话的时间跨度。
    assigned={cid:[] for cid in chosen}
    for d in sorted(cluster,key=lambda x:(x.start_time,x.end_time)):
        feasible=[cid for cid in chosen if d.demand_id in cover[cid]]
        if not feasible:
            raise RuntimeError(f"内部错误：需求 {d.demand_id} 未被候选点覆盖")
        if len(feasible)==1:
            assigned[feasible[0]].append(d)
        else:
            def span_if(cid):
                xs=assigned[cid]+[d]
                return max(x.end_time for x in xs)-min(x.start_time for x in xs)
            assigned[min(feasible,key=span_if)].append(d)
    sessions=[]
    for cid,ds in assigned.items():
        if not ds: continue
        cur=[]
        for d in ds:
            test=cur+[d]; ss=min(x.start_time for x in test); ee=max(x.end_time for x in test)
            en,_,_=relay_sortie_energy(candidates[cid],ee-ss,comm,spec)
            if cur and en>(1-spec.reserve_ratio)*spec.energy_kwh+1e-9:
                s0=min(x.start_time for x in cur); e0=max(x.end_time for x in cur)
                sessions.append(RelaySession(cid,s0,e0,[x.demand_id for x in cur])); cur=[d]
            else:
                cur=test
        if cur:
            s0=min(x.start_time for x in cur); e0=max(x.end_time for x in cur)
            sessions.append(RelaySession(cid,s0,e0,[x.demand_id for x in cur]))
    return sessions

def merge_compatible_sessions(sessions: List[RelaySession], candidates: Dict[str, RelayCandidate], comm: CommunicationModel, spec: RelaySpec) -> List[RelaySession]:
    out=[]
    for s in sorted(sessions,key=lambda x:(x.candidate_id,x.service_start)):
        if out and out[-1].candidate_id==s.candidate_id and s.service_start<=out[-1].service_end+RELAY_MERGE_GAP:
            ns=min(out[-1].service_start,s.service_start); ne=max(out[-1].service_end,s.service_end)
            en,_,_=relay_sortie_energy(candidates[s.candidate_id],ne-ns,comm,spec)
            if en <= (1-spec.reserve_ratio)*spec.energy_kwh+1e-9:
                out[-1].service_start=ns; out[-1].service_end=ne; out[-1].demand_ids.extend(s.demand_ids); continue
        out.append(s)
    return sorted(out,key=lambda x:x.service_start)


class RelayResourceConflict(RuntimeError):
    def __init__(self, message: str, demand_ids: List[str], delay_needed: float):
        super().__init__(message)
        self.demand_ids = list(demand_ids)
        self.delay_needed = max(1.0, float(delay_needed))


def decode_relay_sessions(sessions: List[RelaySession], candidates: Dict[str, RelayCandidate], comm: CommunicationModel, spec: RelaySpec, units: List[RelayUnit]) -> List[RelaySortie]:
    unit_avail={u.relay_id:0.0 for u in units}
    comp_avail={f"RE-{i:02d}":0.0 for i in range(1,spec.component_count+1)}
    sorties=[]
    for sess in sorted(sessions,key=lambda x:x.service_start):
        cand=candidates[sess.candidate_id]
        energy,out_t,ret_t=relay_sortie_energy(cand,sess.service_end-sess.service_start,comm,spec)
        required_start=sess.service_start-spec.prep_time-out_t-spec.link_time
        if required_start < -1e-6:
            raise RuntimeError(f"中继 {sess.candidate_id} 无法在 t={sess.service_start:.1f}s 前完成建链")
        unit_candidates=[u for u,t in unit_avail.items() if t<=required_start+1e-9]
        comp_candidates=[c for c,t in comp_avail.items() if t<=required_start+1e-9]
        if not unit_candidates:
            raise RelayResourceConflict(f"中继无人机资源冲突：t={required_start:.1f}s 前无可用 R01/R02", sess.demand_ids, min(unit_avail.values())-required_start+30.0)
        if not comp_candidates:
            raise RelayResourceConflict(f"中继能源组件资源冲突：t={required_start:.1f}s 前无可用组件", sess.demand_ids, min(comp_avail.values())-required_start+30.0)
        rid=min(unit_candidates,key=lambda x:(unit_avail[x],x)); cid=min(comp_candidates,key=lambda x:(comp_avail[x],x))
        start=max(0.0,required_start)
        link_complete=start+spec.prep_time+out_t+spec.link_time
        # 数值上应 <= service_start，允许提前到位并悬停等待；等待也计能耗。
        wait=max(0.0,sess.service_start-link_complete)
        if wait>0:
            energy += (spec.hover_power_kw+spec.comm_power_kw)*wait/3600.0
            link_complete=sess.service_start
        ret=sess.service_end+ret_t
        if energy>(1-spec.reserve_ratio)*spec.energy_kwh+1e-9:
            raise RuntimeError(f"中继架次能量超限：{energy:.3f} kWh")
        soc=1-energy/spec.energy_kwh
        chg=charge_time_seconds(soc,spec.full_charge_time)
        charge_end=ret+chg
        unit_avail[rid]=ret+spec.turnaround_time
        comp_avail[cid]=charge_end
        sorties.append(RelaySortie("",rid,cid,sess.candidate_id,start,link_complete,sess.service_end,ret,energy,soc,charge_end,cand.lon,cand.lat,cand.alt,list(sess.demand_ids)))
    sorties.sort(key=lambda x:(x.start_time,x.relay_id))
    for i,s in enumerate(sorties,1): s.sortie_id=f"Q3R{i:03d}"
    return sorties


def assign_relay_to_samples(trajectories: Dict[str,List[TrajectorySample]], demands: List[CommDemand], sorties: List[RelaySortie]) -> None:
    demand_to_sortie={d:s.sortie_id for s in sorties for d in s.demand_ids}
    demand_map={d.demand_id:d for d in demands}
    for d in demands:
        sid=demand_to_sortie.get(d.demand_id)
        if not sid: raise RuntimeError(f"需求 {d.demand_id} 未分配中继架次")
        arr=trajectories[d.trip_id]
        for i in d.sample_indices:
            if not arr[i].direct_ok: arr[i].relay_sortie_id=sid


def build_comm_records(trajectories: Dict[str,List[TrajectorySample]], step: float) -> List[List[Any]]:
    rows=[]
    for tid,arr in trajectories.items():
        if not arr: continue
        # 按每个采样点的阶段+保障方式压缩连续区间。
        cur=None
        for s in arr:
            method="直连" if s.direct_ok else "中继"
            relay="" if s.direct_ok else s.relay_sortie_id
            key=(s.phase,method,relay)
            if cur is None:
                cur=[tid,s.phase,s.time,s.time+step,method,relay]; curkey=key
            elif key==curkey and s.time<=cur[3]+0.6*step:
                cur[3]=s.time+step
            else:
                rows.append(cur); cur=[tid,s.phase,s.time,s.time+step,method,relay]; curkey=key
        if cur: rows.append(cur)
    return rows



class RelayCoverageConflict(RuntimeError):
    def __init__(self, message: str, trip_ids: List[str], cluster_end: float, earliest_uncovered: float):
        super().__init__(message)
        self.trip_ids = list(trip_ids)
        self.cluster_end = float(cluster_end)
        self.earliest_uncovered = float(earliest_uncovered)


def build_global_outage_clusters(trajectories: Dict[str,List[TrajectorySample]], gap: float = 90.0) -> List[List[Tuple[str,int,TrajectorySample]]]:
    bad=[]
    for tid,arr in trajectories.items():
        for i,x in enumerate(arr):
            if not x.direct_ok: bad.append((tid,i,x))
    bad.sort(key=lambda z:z[2].time)
    if not bad: return []
    out=[]; cur=[bad[0]]; last=bad[0][2].time
    for x in bad[1:]:
        if x[2].time-last <= gap:
            cur.append(x)
        else:
            out.append(cur); cur=[x]
        last=x[2].time
    out.append(cur); return out


def cluster_candidate_bank(cluster: List[Tuple[str,int,TrajectorySample]], comm: CommunicationModel, spec: RelaySpec) -> Dict[str,RelayCandidate]:
    pool={}
    # 稀疏全局高点约 2 km 间距；共享候选的主要目的，是让一架中继同时服务多架运输机。
    for r in range(0,comm.dem.shape[0],70):
        for c in range(0,comm.dem.shape[1],70):
            ca=comm.candidate(r,c,spec.max_hover_agl)
            if ca and comm.relay_backhaul_ok(ca): pool[ca.candidate_id]=ca
    # 15 个服务区上空补充解释性候选（150/225/300 m）。
    for n in comm.nodes.values():
        if n.node_id=="O01": continue
        r,c=comm.nearest_rc(n.lon,n.lat)
        for h in (150.0,225.0,spec.max_hover_agl):
            if h>spec.max_hover_agl+1e-9: continue
            ca=comm.candidate(r,c,h)
            if ca and comm.relay_backhaul_ok(ca): pool[ca.candidate_id]=ca
    return pool

def plan_relay_by_time_clusters(trajectories: Dict[str,List[TrajectorySample]], comm: CommunicationModel, spec: RelaySpec, step: float) -> Tuple[List[RelaySession],Dict[str,RelayCandidate]]:
    sessions=[]; all_candidates={}
    for ci,cl in enumerate(build_global_outage_clusters(trajectories)):
        samples=[x[2] for x in cl]; n=len(samples); full=(1<<n)-1
        t0=min(x.time for x in samples); t1=max(x.time for x in samples)+step
        pool=cluster_candidate_bank(cl,comm,spec); all_candidates.update(pool)
        masks=[]
        for cid,ca in pool.items():
            # 候选点必须能在簇开始前完成准备、飞抵并建链，且整簇服务能量可行。
            en,out_t,ret_t=relay_sortie_energy(ca,t1-t0,comm,spec)
            if en>(1-spec.reserve_ratio)*spec.energy_kwh+1e-9: continue
            if t0-spec.prep_time-out_t-spec.link_time < -1e-9: continue
            cp=comm.candidate_pos(ca); mask=0
            for j,x in enumerate(samples):
                if comm.distance3d_m(x.pos,cp)>6800.0: continue
                if comm.link_available(x.pos,cp,"TR"): mask|=(1<<j)
            if mask: masks.append((mask.bit_count(),cid,mask))
        if not masks:
            raise RelayCoverageConflict(f"时间簇 {ci} 无任何可行中继点", sorted({x[0] for x in cl}), t1, t0)
        masks.sort(reverse=True)
        chosen=None
        for cnt,cid,mask in masks:
            if mask==full:
                chosen=[(cid,mask)]; break
        if chosen is None:
            # 只需枚举覆盖率较高的前若干候选；用位集进行精确两点覆盖搜索。
            top=masks[:min(180,len(masks))]
            best_pair=None; best_cov=-1
            for i,a in enumerate(top):
                for b in top[i+1:]:
                    union=a[2]|b[2]; cov=union.bit_count()
                    if cov>best_cov:
                        best_cov=cov; best_pair=(a,b,union)
                    if union==full: break
                if best_pair is not None and best_pair[2]==full: break
            if best_pair is not None and best_pair[2]==full:
                chosen=[(best_pair[0][1],best_pair[0][2]),(best_pair[1][1],best_pair[1][2])]
            else:
                # 粗网格仍有少量未覆盖点时，仅围绕这些轨迹局部加密 DEM 候选点。
                union=best_pair[2] if best_pair is not None else masks[0][2]
                unc=[j for j in range(n) if not ((union>>j)&1)]
                refined={}
                for j in unc:
                    r0,c0=comm.nearest_rc(samples[j].pos.lon,samples[j].pos.lat)
                    for dr in (-35,-17,0,17,35):
                        for dc in (-35,-17,0,17,35):
                            for h in (225.0,spec.max_hover_agl):
                                if h>spec.max_hover_agl+1e-9: continue
                                ca=comm.candidate(r0+dr,c0+dc,h)
                                if ca and ca.candidate_id not in pool and comm.relay_backhaul_ok(ca):
                                    refined[ca.candidate_id]=ca
                if refined:
                    pool.update(refined); all_candidates.update(refined)
                    for cid,ca in refined.items():
                        en,out_t,ret_t=relay_sortie_energy(ca,t1-t0,comm,spec)
                        if en>(1-spec.reserve_ratio)*spec.energy_kwh+1e-9: continue
                        if t0-spec.prep_time-out_t-spec.link_time < -1e-9: continue
                        cp=comm.candidate_pos(ca); mask=0
                        for j,x in enumerate(samples):
                            if comm.distance3d_m(x.pos,cp)>6800.0: continue
                            if comm.link_available(x.pos,cp,"TR"): mask|=(1<<j)
                        if mask: masks.append((mask.bit_count(),cid,mask))
                    masks.sort(reverse=True)
                    top=masks[:min(320,len(masks))]
                    best_pair=None; best_cov=-1
                    for i,a in enumerate(top):
                        for b in top[i+1:]:
                            uu=a[2]|b[2]; cov=uu.bit_count()
                            if cov>best_cov: best_cov=cov; best_pair=(a,b,uu)
                            if uu==full: break
                        if best_pair is not None and best_pair[2]==full: break
                    if best_pair is not None and best_pair[2]==full:
                        chosen=[(best_pair[0][1],best_pair[0][2]),(best_pair[1][1],best_pair[1][2])]
                if chosen is None:
                    union=best_pair[2] if best_pair is not None else masks[0][2]
                    unc=[j for j in range(n) if not ((union>>j)&1)]
                    # 不能只移动“未覆盖”的架次：可能该架次时限最紧。
                    # 把整个并发簇的运输架次交给外层，由硬时限裕量决定谁最适合错峰。
                    trips=sorted({tid for tid,_,_ in cl})
                    earliest=min(samples[j].time for j in unc)
                    raise RelayCoverageConflict(f"时间簇 {ci} 需要超过2个空间中继簇：覆盖 {n-len(unc)}/{n}",trips,t1,earliest)
        # 为每个选中候选生成一个固定悬停会话；仅覆盖其实际承担样本的时间范围。
        assigned=[[] for _ in chosen]
        for j,x in enumerate(samples):
            feasible=[k for k,(_,mask) in enumerate(chosen) if (mask>>j)&1]
            k=feasible[0] if len(feasible)==1 else min(feasible,key=lambda z:len(assigned[z]))
            assigned[k].append((cl[j][0],x.time))
        for k,(cid,mask) in enumerate(chosen):
            if not assigned[k]: continue
            ss=min(t for _,t in assigned[k]); ee=max(t for _,t in assigned[k])+step
            trips=sorted({tid for tid,_ in assigned[k]})
            sessions.append(RelaySession(cid,ss,ee,trips))
    return merge_compatible_sessions(sessions,all_candidates,comm,spec),all_candidates


def assign_relay_sorties_by_link(trajectories: Dict[str,List[TrajectorySample]], sorties: List[RelaySortie], candidates: Dict[str,RelayCandidate], comm: CommunicationModel) -> None:
    for arr in trajectories.values():
        for x in arr:
            if x.direct_ok: continue
            feasible=[]
            for s in sorties:
                if s.link_complete_time-1e-6<=x.time<=s.service_end_time+1e-6:
                    ca=candidates[s.candidate_id]
                    if comm.relay_backhaul_ok(ca) and comm.link_available(x.pos,comm.candidate_pos(ca),"TR"):
                        feasible.append(s)
            if not feasible:
                x.relay_sortie_id=""
            else:
                x.relay_sortie_id=min(feasible,key=lambda s:(s.service_end_time-s.link_complete_time,s.sortie_id)).sortie_id


def validate_comm_solution(trajectories: Dict[str,List[TrajectorySample]], sorties: List[RelaySortie], candidates: Dict[str,RelayCandidate], comm: CommunicationModel) -> None:
    smap={s.sortie_id:s for s in sorties}
    outage=[]
    for tid,arr in trajectories.items():
        for x in arr:
            if x.direct_ok: continue
            if not x.relay_sortie_id or x.relay_sortie_id not in smap:
                outage.append((tid,x.time,"NO_RELAY")); continue
            rs=smap[x.relay_sortie_id]
            if not (rs.link_complete_time-1e-6<=x.time<=rs.service_end_time+1e-6):
                outage.append((tid,x.time,"RELAY_NOT_ACTIVE")); continue
            cand=candidates[rs.candidate_id]
            if not comm.relay_backhaul_ok(cand) or not comm.link_available(x.pos,comm.candidate_pos(cand),"TR"):
                outage.append((tid,x.time,"LINK_FAIL"))
    if outage:
        raise AssertionError(f"通信审计失败，共 {len(outage)} 个采样点中断，示例={outage[:5]}")


def fill_q3_template(template_path: Path, sorties: List[RelaySortie], comm_rows: List[List[Any]], output_path: Path) -> None:
    shutil.copy2(template_path, output_path)
    wb=load_workbook(output_path)
    ws=wb["Q3_中继架次"]; wc=wb["Q3_通信保障"]
    for row in ws.iter_rows(min_row=2,max_col=11):
        for c in row: c.value=None
    for row in wc.iter_rows(min_row=2,max_col=6):
        for c in row: c.value=None
    for r,s in enumerate(sorties,2):
        vals=[s.sortie_id,s.relay_id,s.component_id,round(s.start_time,3),round(s.lon,7),round(s.lat,7),round(s.alt,3),round(s.link_complete_time,3),round(s.service_end_time,3),round(s.return_time,3),round(s.energy_kwh,6)]
        for c,v in enumerate(vals,1): ws.cell(r,c,v)
    for r,row in enumerate(comm_rows,2):
        vals=[row[0],row[1],round(row[2],3),round(row[3],3),row[4],row[5]]
        for c,v in enumerate(vals,1): wc.cell(r,c,v)
    wb.save(output_path)


def write_q3_detailed(decoded: DecodeResult, boxes: Dict[str,Box], sorties: List[RelaySortie], comm_rows: List[List[Any]], output_path: Path, metrics: Metrics) -> None:
    wb=Workbook(); ws=wb.active; ws.title="Q3_运输架次"
    ws.append(["运输架次编号","运输无人机","机型","共享电池","开始时刻","起飞时刻","服务区序列","返回O01","运输能耗kWh","返航SOC","充电完成"])
    for t in decoded.trips: ws.append([t.trip_id,t.drone_id,t.drone_type,t.battery_id,t.start_time,t.takeoff_time,"→".join(t.service_sequence),t.return_time,t.energy_kwh,t.end_soc,t.charge_end_time])
    w2=wb.create_sheet("Q3_逐箱交付"); w2.append(["货箱编号","运输架次编号","服务区","交付时刻","期望时刻","硬截止"])
    trip_by_box={b:t.trip_id for t in decoded.trips for b in t.box_delivery}
    for bid in sorted(boxes):
        b=boxes[bid]; w2.append([bid,trip_by_box[bid],b.service_id,decoded.box_delivery[bid],b.expected_time,b.hard_deadline])
    w3=wb.create_sheet("Q3_中继架次"); w3.append(["中继架次编号","中继无人机","能源组件","开始","悬停经度","悬停纬度","悬停海拔","建链完成","服务结束","返回","能耗kWh","返航SOC","充电完成","覆盖需求"])
    for s in sorties: w3.append([s.sortie_id,s.relay_id,s.component_id,s.start_time,s.lon,s.lat,s.alt,s.link_complete_time,s.service_end_time,s.return_time,s.energy_kwh,s.end_soc,s.charge_end_time,",".join(s.demand_ids)])
    w4=wb.create_sheet("Q3_通信保障"); w4.append(["运输架次编号","通信阶段","开始时刻","结束时刻","保障方式","中继架次编号"])
    for r in comm_rows: w4.append(r)
    w5=wb.create_sheet("Q3_指标汇总"); w5.append(["指标","数值"])
    relay_energy=sum(s.energy_kwh for s in sorties); joint=max(metrics.makespan,max((s.return_time for s in sorties),default=0.0))
    vals=[("加权迟到",metrics.weighted_lateness),("运输完成时间(s)",metrics.makespan),("联合完成时间(s)",joint),("运输能耗(kWh)",metrics.energy),("中继能耗(kWh)",relay_energy),("运输+中继总能耗(kWh)",metrics.energy+relay_energy),("运输架次数",metrics.trips),("中继架次数",len(sorties)),("硬时限违反数",metrics.hard_violations),("通信中断采样点",0)]
    for x in vals:w5.append(list(x))
    wb.save(output_path)


def plot_q3_routes(decoded: DecodeResult,nodes:Dict[str,Node],sorties:List[RelaySortie],out:Path)->None:
    fig,ax=plt.subplots(figsize=(10,8))
    for t in decoded.trips:
        seq=["O01"]+t.service_sequence+["O01"]
        ax.plot([nodes[n].lon for n in seq],[nodes[n].lat for n in seq],alpha=.4,linewidth=1)
    for nid,n in nodes.items():
        ax.scatter(n.lon,n.lat,s=45 if nid=="O01" else 20); ax.text(n.lon,n.lat,nid,fontsize=7)
    for s in sorties: ax.scatter(s.lon,s.lat,marker="^",s=70); ax.text(s.lon,s.lat,s.sortie_id,fontsize=7)
    ax.set_title("Q3 Transport Routes and Relay Hover Points"); ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.grid(alpha=.25)
    fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)


def plot_q3_joint_gantt(decoded:DecodeResult,sorties:List[RelaySortie],out:Path)->None:
    labels=sorted({t.drone_id for t in decoded.trips})+sorted({s.relay_id for s in sorties}); y={x:i for i,x in enumerate(labels)}
    fig,ax=plt.subplots(figsize=(12,6))
    for t in decoded.trips: ax.barh(y[t.drone_id],t.return_time-t.start_time,left=t.start_time,height=.5); ax.text((t.start_time+t.return_time)/2,y[t.drone_id],t.trip_id,fontsize=6,ha="center")
    for s in sorties: ax.barh(y[s.relay_id],s.return_time-s.start_time,left=s.start_time,height=.5); ax.text((s.start_time+s.return_time)/2,y[s.relay_id],s.sortie_id,fontsize=6,ha="center")
    ax.set_yticks(list(y.values()),list(y.keys())); ax.set_xlabel("Time (s)"); ax.set_title("Q3 Joint Transport-Relay Schedule"); ax.grid(axis="x",alpha=.25)
    fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)


def plot_q3_comm_timeline(comm_rows:List[List[Any]],out:Path)->None:
    tids=sorted({r[0] for r in comm_rows}); y={x:i for i,x in enumerate(tids)}
    fig,ax=plt.subplots(figsize=(12,max(6,.32*len(tids)+2)))
    for r in comm_rows:
        ax.barh(y[r[0]],r[3]-r[2],left=r[2],height=.6,alpha=.85 if r[4]=="中继" else .45)
        if r[4]=="中继": ax.text((r[2]+r[3])/2,y[r[0]],r[5],fontsize=5,ha="center",va="center")
    ax.set_yticks(list(y.values()),list(y.keys())); ax.set_xlabel("Time (s)"); ax.set_title("Q3 Communication Assurance Timeline (direct / relay)"); ax.grid(axis="x",alpha=.2)
    fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)


def plot_q3_relay_energy(sorties:List[RelaySortie],spec:RelaySpec,out:Path)->None:
    fig,ax=plt.subplots(figsize=(10,4.5)); xs=np.arange(len(sorties)); vals=[s.energy_kwh for s in sorties]
    ax.bar(xs,vals); ax.axhline((1-spec.reserve_ratio)*spec.energy_kwh,linestyle="--",linewidth=1,label="usable energy limit")
    ax.set_xticks(xs,[s.sortie_id for s in sorties],rotation=45,ha="right"); ax.set_ylabel("Energy (kWh)"); ax.set_title("Q3 Relay Sortie Energy"); ax.legend(); ax.grid(axis="y",alpha=.2)
    fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)


def plot_q3_comm_ratio(trajectories:Dict[str,List[TrajectorySample]],out:Path)->None:
    tids=sorted(trajectories); direct=[]; relay=[]
    for tid in tids:
        a=trajectories[tid]; direct.append(sum(x.direct_ok for x in a)); relay.append(sum(not x.direct_ok for x in a))
    x=np.arange(len(tids)); fig,ax=plt.subplots(figsize=(12,5)); ax.bar(x,direct,label="direct"); ax.bar(x,relay,bottom=direct,label="relay")
    ax.set_xticks(x,tids,rotation=45,ha="right"); ax.set_ylabel("Communication samples"); ax.set_title("Q3 Direct vs Relay Communication Share"); ax.legend(); ax.grid(axis="y",alpha=.2)
    fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)



@dataclass
class JointTimeState:
    release: Dict[int, float]
    decoded: DecodeResult
    metrics: Metrics
    trajectories: Dict[str, List[TrajectorySample]]
    candidates: Dict[str, RelayCandidate]
    sorties: List[RelaySortie]
    score: float = math.inf


def _joint_components(metrics: Metrics, sorties: List[RelaySortie]) -> Tuple[float, float, int]:
    relay_energy = sum(s.energy_kwh for s in sorties)
    relay_end = max((s.return_time for s in sorties), default=0.0)
    joint_makespan = max(metrics.makespan, relay_end)
    total_energy = metrics.energy + relay_energy
    total_trips = metrics.trips + len(sorties)
    return joint_makespan, total_energy, total_trips


def joint_score(metrics: Metrics, sorties: List[RelaySortie], base: Tuple[float,float,float,int]) -> float:
    base_late, base_ms, base_energy, base_trips = base
    jm, te, nt = _joint_components(metrics, sorties)
    if metrics.hard_violations > 0:
        return math.inf
    return (0.45 * metrics.weighted_lateness / max(base_late, 1.0)
            + 0.25 * jm / max(base_ms, 1.0)
            + 0.20 * te / max(base_energy, 1e-9)
            + 0.10 * nt / max(base_trips, 1))


def evaluate_joint_time_state(routes: List[Route], release: Dict[int,float], decoder: ResourceDecoder,
                              boxes: Dict[str,Box], nodes: Dict[str,Node], dtypes: Dict[str,DroneType],
                              arcs: ArcLibrary, comm: CommunicationModel, rspec: RelaySpec,
                              runits: List[RelayUnit], base: Tuple[float,float,float,int]) -> Optional[JointTimeState]:
    """严格评价一个时间协同候选；不可行就返回 None，不做隐式补救。"""
    try:
        decoder.set_route_release(release)
        decoded = decoder.decode(routes)
        if not decoded.feasible:
            return None
        for i,t in enumerate(sorted(decoded.trips,key=lambda x:(x.start_time,x.drone_id,x.return_time)),1):
            t.trip_id=f"Q3T{i:03d}"
        validate_final_solution(routes,decoded,boxes,dtypes)
        metrics=compute_metrics(decoded,boxes)
        if metrics.hard_violations:
            return None
        traj=build_trajectories(decoded,nodes,dtypes,arcs,COMM_SAMPLE_STEP)
        mark_direct_coverage(traj,comm)
        sessions,cands=plan_relay_by_time_clusters(traj,comm,rspec,COMM_SAMPLE_STEP)
        sorties=decode_relay_sessions(sessions,cands,comm,rspec,runits)
        assign_relay_sorties_by_link(traj,sorties,cands,comm)
        validate_comm_solution(traj,sorties,cands,comm)
        st=JointTimeState(dict(release),decoded,metrics,traj,cands,sorties)
        st.score=joint_score(metrics,sorties,base)
        return st
    except (RelayCoverageConflict, RelayResourceConflict, AssertionError, RuntimeError, ValueError):
        return None


def joint_time_alns(initial: JointTimeState, routes: List[Route], decoder: ResourceDecoder,
                    boxes: Dict[str,Box], nodes: Dict[str,Node], dtypes: Dict[str,DroneType], arcs: ArcLibrary,
                    comm: CommunicationModel, rspec: RelaySpec, runits: List[RelayUnit],
                    iterations: int, seed: int) -> Tuple[JointTimeState,List[Tuple[int,float]],List[List[Any]]]:
    """Joint-ALNS v1：通信感知的运输开始时刻联合改进。\n
    邻域只改变通信反馈产生的 release time；路线结构保持不变。每个候选都重新进行\n    运输资源解码、通信覆盖搜索、中继无人机和能源组件解码，因此接受解保持联合可行。\n    """
    jm0,te0,nt0=_joint_components(initial.metrics,initial.sorties)
    base=(initial.metrics.weighted_lateness,jm0,te0,nt0)
    initial.score=joint_score(initial.metrics,initial.sorties,base)
    current=initial; best=initial
    rng=random.Random(seed+3103)
    hist=[(0,best.score)]
    log=[]
    ops=["advance_one","advance_pair","reset_one","exchange_shift"]
    weights={o:1.0 for o in ops}
    for it in range(1,iterations+1):
        avail=[ri for ri,v in current.release.items() if v>1e-6]
        if not avail:
            hist.append((it,best.score)); log.append([it,"none",0,"",best.score]); continue
        op=rng.choices(ops,weights=[weights[o] for o in ops],k=1)[0]
        rel=dict(current.release)
        if op=="exchange_shift":
            # 把中继冲突造成的等待从一个架次转移给另一个时限更宽松架次：
            # 一进一退，比单纯提前更容易保持两架中继的并发可行性。
            advance_ri=rng.choice(avail)
            trip_by_route={t.route_index:t for t in current.decoded.trips}
            donor=[]
            for ri,tr in trip_by_route.items():
                if ri==advance_ri: continue
                ls=tr.route_eval.latest_start
                slack=(ls-tr.start_time) if math.isfinite(ls) else 3600.0
                if slack>180.0: donor.append((slack,ri,tr))
            if not donor:
                hist.append((it,best.score)); log.append([it,op,0,"",best.score]); continue
            donor.sort(reverse=True,key=lambda z:z[0])
            _,delay_ri,delay_tr=rng.choice(donor[:min(6,len(donor))])
            delta=rng.choice([180.0,300.0,450.0,600.0])
            old=rel.get(advance_ri,0.0)
            new=max(0.0,old-delta)
            if new<=1e-6: rel.pop(advance_ri,None)
            else: rel[advance_ri]=new
            proposed=delay_tr.start_time+delta
            if math.isfinite(delay_tr.route_eval.latest_start):
                proposed=min(proposed,delay_tr.route_eval.latest_start)
            rel[delay_ri]=max(rel.get(delay_ri,0.0),proposed)
        else:
            k=2 if op=="advance_pair" and len(avail)>=2 else 1
            chosen=rng.sample(avail,k)
            for ri in chosen:
                old=rel.get(ri,0.0)
                if op=="reset_one":
                    new=0.0
                else:
                    frac=rng.choice([0.55,0.70,0.82,0.90])
                    back=rng.choice([120.0,240.0,360.0,600.0])
                    new=max(0.0,min(old*frac,old-back))
                if new<=1e-6: rel.pop(ri,None)
                else: rel[ri]=new
        cand=evaluate_joint_time_state(routes,rel,decoder,boxes,nodes,dtypes,arcs,comm,rspec,runits,base)
        if cand is None:
            weights[op]=max(0.2,weights[op]*0.98)
            hist.append((it,best.score)); log.append([it,op,0,"",best.score]); continue
        temp=max(0.005,0.08*(1-it/max(iterations,1)))
        delta=cand.score-current.score
        accept=(delta<=0) or (rng.random()<math.exp(-delta/temp))
        if accept:
            current=cand; weights[op]*=1.01
        else:
            weights[op]=max(0.2,weights[op]*0.995)
        if cand.score < best.score-1e-10:
            best=cand; weights[op]+=0.15
        hist.append((it,best.score)); log.append([it,op,int(accept),cand.score,best.score])
    return best,hist,log


def plot_joint_alns(history: List[Tuple[int,float]], out: Path) -> None:
    if not history: history=[(0,1.0)]
    xs=[x for x,_ in history]; ys=[y for _,y in history]
    fig,ax=plt.subplots(figsize=(8.5,4.8))
    ax.plot(xs,ys,marker="o",markersize=3.5,linewidth=1.5)
    ax.scatter([xs[0]],[ys[0]],s=44,label="feasible baseline",zorder=3)
    ax.scatter([xs[-1]],[ys[-1]],s=60,marker="*",label="best joint solution",zorder=3)
    if len(history)==1:
        ax.annotate("Joint-ALNS not run",(xs[0],ys[0]),xytext=(12,10),textcoords="offset points")
        ax.set_xlim(-0.5,1.5)
    ax.set_xlabel("Joint-ALNS iteration")
    ax.set_ylabel("Best normalized joint objective")
    ax.set_title("Q3 Communication-aware Joint-ALNS Convergence")
    ax.grid(alpha=.25); ax.legend(); fig.tight_layout(); fig.savefig(out,dpi=180); plt.close(fig)

def run_q3(project_root:Path,output_dir:Path,alns_iters:int,joint_iters:int,seed:int)->None:
    t0=time.time(); output_dir.mkdir(parents=True,exist_ok=True)
    node_file=resolve_input_file(project_root,"调度中心与服务区",".xlsx")
    box_file=resolve_input_file(project_root,"物资需求与配送时限",".xlsx")
    drone_file=resolve_input_file(project_root,"运输无人机数据",".xlsx")
    relay_file=resolve_input_file(project_root,"中继无人机数据",".xlsx")
    comm_file=resolve_input_file(project_root,"通信链路参数",".xlsx")
    dem_file=resolve_input_file(project_root,"镇龙乡及周边30米DEM",".mat")
    template_file=resolve_input_file(project_root,"结果提交模板",".xlsx")
    print("[1/10] 从原始附件独立读取 Q3 全部数据...")
    nodes=load_nodes(node_file); boxes=load_boxes(box_file); dtypes,dunits,bats=load_drone_data(drone_file); rspec,runits=load_relay_data(relay_file); cparams=load_comm_params(comm_file)
    print("[2/10] 构建运输航段库与 Q3 运输优化器...")
    arcs=ArcLibrary(nodes,dtypes,dem_file); evaluator=RouteEvaluator(boxes,dtypes,arcs); decoder=ResourceDecoder(evaluator,dunits,bats,boxes)
    initial_routes=construct_initial_routes(boxes,evaluator)
    print(f"[3/10] Q3 内部独立运行运输 ALNS（{alns_iters} iterations）...")
    routes,decoded,metrics,history=alns_optimize(initial_routes,boxes,nodes,evaluator,decoder,alns_iters,seed)
    if not decoded.feasible: raise RuntimeError(decoded.reason)

    # 通信资源反馈修复：固定当前路线结构，联合调整各路线最早开始时刻。
    comm=CommunicationModel(dem_file,nodes,cparams)
    route_release: Dict[int,float] = {}
    sorties=[]; trajectories={}; candidates={}
    MAX_REPAIR=40
    for repair_iter in range(MAX_REPAIR):
        decoder.set_route_release(route_release)
        decoded=decoder.decode(routes)
        if not decoded.feasible:
            raise RuntimeError(f"通信反馈后的运输调度不可行：{decoded.reason}；release={route_release}")
        for i,t in enumerate(sorted(decoded.trips,key=lambda x:(x.start_time,x.drone_id,x.return_time)),1): t.trip_id=f"Q3T{i:03d}"
        validate_final_solution(routes,decoded,boxes,dtypes)
        metrics=compute_metrics(decoded,boxes)
        if repair_iter==0:
            print(f"  初始Q3运输架次={metrics.trips}, makespan={metrics.makespan:.1f}s, energy={metrics.energy:.3f}kWh")
        print(f"[4-7/10] 通信联合修复轮次 {repair_iter+1}/{MAX_REPAIR} ...")
        trajectories=build_trajectories(decoded,nodes,dtypes,arcs,COMM_SAMPLE_STEP)
        mark_direct_coverage(trajectories,comm)
        try:
            sessions,candidates=plan_relay_by_time_clusters(trajectories,comm,rspec,COMM_SAMPLE_STEP)
            sorties=decode_relay_sessions(sessions,candidates,comm,rspec,runits)
            assign_relay_sorties_by_link(trajectories,sorties,candidates,comm)
            validate_comm_solution(trajectories,sorties,candidates,comm)
            print(f"  联合修复成功：中继架次={len(sorties)}, 中继能耗={sum(x.energy_kwh for x in sorties):.3f}kWh")
            break
        except RelayCoverageConflict as e:
            trip_lookup={t.trip_id:t for t in decoded.trips}
            affected=[trip_lookup[tid] for tid in e.trip_ids if tid in trip_lookup]
            if not affected: raise
            def slack(tr):
                ls=tr.route_eval.latest_start
                return (ls-tr.start_time) if math.isfinite(ls) else 1e12
            target=max(affected,key=slack)
            shift=max(600.0,e.cluster_end-e.earliest_uncovered+600.0)
            if math.isfinite(target.route_eval.latest_start):
                # 第一版可行性优先：把造成第三空间簇的硬时限架次尽量推到其允许的最晚开始附近。
                proposed=max(target.start_time+shift, target.route_eval.latest_start-30.0)
                proposed=min(proposed,target.route_eval.latest_start)
            else:
                proposed=max(target.start_time+shift,e.cluster_end+900.0)
            if proposed<=target.start_time+1e-6: raise RuntimeError(f"无法错峰消除通信空间冲突：{e}")
            route_release[target.route_index]=max(route_release.get(target.route_index,0.0),proposed)
            print(f"  {e}; 自动推迟 {target.trip_id} (route#{target.route_index}) 至 >= {proposed:.1f}s")
        except RelayResourceConflict as e:
            trip_lookup={t.trip_id:t for t in decoded.trips}
            affected=[trip_lookup[tid] for tid in e.demand_ids if tid in trip_lookup]
            if not affected: raise
            def slack2(tr):
                ls=tr.route_eval.latest_start
                return (ls-tr.start_time) if math.isfinite(ls) else 1e12
            target=max(affected,key=slack2)
            proposed=target.start_time+max(180.0,e.delay_needed)
            if math.isfinite(target.route_eval.latest_start): proposed=min(proposed,target.route_eval.latest_start)
            if proposed<=target.start_time+1e-6: raise RuntimeError(f"无法错峰消除中继资源冲突：{e}")
            route_release[target.route_index]=max(route_release.get(target.route_index,0.0),proposed)
            print(f"  {e}; 自动推迟 {target.trip_id} (route#{target.route_index}) 至 >= {proposed:.1f}s")
    else:
        raise RuntimeError("达到通信联合修复最大轮次，仍未得到连续通信可行方案")
    # 第二阶段：通信感知 Joint-ALNS v1。当前版本先优化运输开始时刻与中继资源的协同，
    # 路线结构邻域将在下一层继续加入。
    jm0,te0,nt0=_joint_components(metrics,sorties)
    baseline=JointTimeState(dict(route_release),decoded,metrics,trajectories,candidates,sorties,1.0)
    print(f"[8/12] 通信感知 Joint-ALNS（{joint_iters} iterations）...")
    best_joint,joint_history,joint_log=joint_time_alns(
        baseline,routes,decoder,boxes,nodes,dtypes,arcs,comm,rspec,runits,joint_iters,seed)
    route_release=dict(best_joint.release); decoded=best_joint.decoded; metrics=best_joint.metrics
    trajectories=best_joint.trajectories; candidates=best_joint.candidates; sorties=best_joint.sorties
    jm,te,nt=_joint_components(metrics,sorties)
    print(f"  Joint-ALNS结果：score={best_joint.score:.6f}, 联合完工={jm:.1f}s, 总能耗={te:.3f}kWh, 总架次={nt}")
    comm_rows=build_comm_records(trajectories,COMM_SAMPLE_STEP)
    print("[9/12] 回填官方 Q3 Sheet，并写出详细审计文件...")
    fill_q3_template(template_file,sorties,comm_rows,output_dir/"Q3_结果.xlsx")
    write_q3_detailed(decoded,boxes,sorties,comm_rows,output_dir/"Q3_详细结果.xlsx",metrics)
    print("[10/12] 输出 CSV、Joint-ALNS日志与摘要...")
    with (output_dir/"Q3_中继架次.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["中继架次编号","中继无人机编号","能源组件编号","开始时刻","悬停经度","悬停纬度","悬停海拔","建链完成","服务结束","返回O01","能耗kWh"])
        for s in sorties:w.writerow([s.sortie_id,s.relay_id,s.component_id,s.start_time,s.lon,s.lat,s.alt,s.link_complete_time,s.service_end_time,s.return_time,s.energy_kwh])
    with (output_dir/"Q3_通信保障.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["运输架次编号","通信阶段","开始时刻","结束时刻","保障方式","中继架次编号"]); w.writerows(comm_rows)
    with (output_dir/"Q3_Joint_ALNS日志.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["iteration","operator","accepted","candidate_score","best_score"]); w.writerows(joint_log)
    joint=max(metrics.makespan,max((s.return_time for s in sorties),default=0.0)); relay_e=sum(s.energy_kwh for s in sorties)
    with (output_dir/"Q3_summary.txt").open("w",encoding="utf-8") as f:
        f.write("问题三独立求解结果摘要\n"+"="*48+"\n"); f.write(f"运输架次数: {metrics.trips}\n中继架次数: {len(sorties)}\n联合任务完成时间: {joint:.3f} s\n运输能耗: {metrics.energy:.6f} kWh\n中继能耗: {relay_e:.6f} kWh\n总能耗: {metrics.energy+relay_e:.6f} kWh\n加权迟到: {metrics.weighted_lateness:.6f}\n硬时限违反数: {metrics.hard_violations}\n通信中断采样点: 0\nTransport-ALNS迭代数: {alns_iters}\nJoint-ALNS迭代数: {joint_iters}\nJoint归一化目标: {best_joint.score:.8f}\n")
    print("[11-12/12] 生成 Q3 必要图表...")
    plot_q3_routes(decoded,nodes,sorties,output_dir/"fig_q3_1_routes_relay.png"); plot_q3_joint_gantt(decoded,sorties,output_dir/"fig_q3_2_joint_gantt.png"); plot_q3_comm_timeline(comm_rows,output_dir/"fig_q3_3_comm_timeline.png"); plot_q3_relay_energy(sorties,rspec,output_dir/"fig_q3_4_relay_energy.png"); plot_q3_comm_ratio(trajectories,output_dir/"fig_q3_5_comm_ratio.png")
    plot_alns(history,output_dir/"fig_q3_6_transport_alns.png")
    plot_joint_alns(joint_history,output_dir/"fig_q3_7_joint_alns.png")
    print(f"完成，用时 {time.time()-t0:.2f}s；输出目录：{output_dir}")


def parse_q3_args()->argparse.Namespace:
    p=argparse.ArgumentParser(description="Q3 通信约束下运输与中继联合调度（独立运行，不调用Q2 solver）")
    p.add_argument("--root",type=str,default=str(PROJECT_ROOT),help="HUAWEI-CUP项目根目录")
    p.add_argument("--output",type=str,default=str(SCRIPT_PATH.parent/"output"),help="Q3输出目录")
    p.add_argument("--iters",type=int,default=0,help="运输层预优化迭代数；0 时收敛图显示基线点而不是空图")
    p.add_argument("--joint-iters",type=int,default=6,help="通信感知 Joint-ALNS 时间协同迭代数；建议先6~20")
    p.add_argument("--seed",type=int,default=RANDOM_SEED,help="随机种子")
    return p.parse_args()


if __name__=="__main__":
    args=parse_q3_args(); run_q3(Path(args.root).resolve(),Path(args.output).resolve(),max(0,args.iters),max(0,args.joint_iters),args.seed)
