"""问题二：异构无人机多点多架次运输调度改进版。
方法：原非线性能耗精算 + 可复现多起点ALNS + 候选路线/时间索引MILP + 独立审计。
用法：python q2_solver_improved.py --root 数据目录 --output Q2_results
仅需 numpy scipy>=1.11 openpyxl Pillow matplotlib，不依赖商业优化器。
GeoTIFF与MAT均支持；无需结果提交模板；输出CSV、JSON、PDF/SVG图。
最优性：ALNS无全局保证；MILP界和gap仅对记录的路线池、时间网格和时域有效。
本题不添加通信、道路避障、有限充电桩、运输悬停功耗或机队租金。
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
from openpyxl import load_workbook
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import json
import hashlib
import heapq
import itertools
from collections import Counter
from PIL import Image
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import coo_matrix
from dataclasses import asdict


# ============================================================
# 0. 可统一修改的路径与算法参数
# ============================================================

# 固定项目根目录路径
PROJECT_ROOT = Path(__file__).resolve().parent

# 固定输出目录
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "问题二输出"


RANDOM_SEED = 20260924
EARTH_RADIUS_M = 6_371_008.8
G = 9.81
MAX_STOPS_PER_ROUTE = 15               # 最多15个不同服务区；不再施加4点上限
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
        self.stops = [Stop(sid, sorted(acc[sid])) for sid in order if acc[sid]]


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


def continuous_supercover(r0, c0, r1, c1):
    """真实端点在像元边界坐标系中穿过/接触的全部像元，包括角点两侧。"""
    dr, dc = r1-r0, c1-c0
    ts = [0.0, 1.0]
    for start, delta, end in ((r0, dr, r1), (c0, dc, c1)):
        if abs(delta) > 1e-14:
            for k in range(math.ceil(min(start, end)), math.floor(max(start, end))+1):
                t = (k-start)/delta
                if 0 < t < 1:
                    ts.append(t)
    ts = sorted(set(ts))
    ts += [(a+b)/2 for a, b in zip(ts, ts[1:])]
    cells = set()
    for t in ts:
        r, c = r0+dr*t, c0+dc*t
        rr, cc = [math.floor(r)], [math.floor(c)]
        if abs(r-round(r)) < 1e-8:
            rr = [round(r)-1, round(r)]
        if abs(c-round(c)) < 1e-8:
            cc = [round(c)-1, round(c)]
        cells.update((i, j) for i in rr for j in cc)
    return sorted(cells)


class ArcLibrary:
    """以实际经纬度穿越栅格；不可把端点先吸附到最近像元中心。"""
    def __init__(self, nodes, drone_types, dem_path):
        self.nodes, self.drone_types = nodes, drone_types
        self.nodata = -32767.0
        if dem_path.suffix.lower() == '.mat':
            mat = loadmat(dem_path)
            self.dem = np.asarray(mat['dem'], dtype=float)
            self.lat = np.asarray(mat['latitude']).reshape(-1)
            self.lon = np.asarray(mat['longitude']).reshape(-1)
            self.nodata = float(np.asarray(mat.get('nodata', [-32767])).reshape(-1)[0])
        else:
            with Image.open(dem_path) as im:
                tags = dict(im.tag_v2)
                self.dem = np.asarray(im, dtype=float)
                if 34264 in tags or 33550 not in tags or 33922 not in tags:
                    raise ValueError('仅支持规则经纬度GeoTIFF，旋转栅格请先规范化。')
                keys = tags.get(34735, ())
                geo = {keys[i]: keys[i+3] for i in range(4, len(keys), 4) if keys[i+1] == 0}
                if geo.get(2048) != 4326:
                    raise ValueError('本程序要求EPSG:4326的GeoTIFF。')
                dx, dy, _ = tags[33550]
                i, j, _, x, y, _ = tags[33922][:6]
                # RasterPixelIsPoint=2：tiepoint已经是像元中心；PixelIsArea=1：加半格。
                shift = 0.0 if geo.get(1025, 1) == 2 else 0.5
                x0, y0 = x+(shift-i)*dx, y-(shift-j)*dy
                self.lon = x0+np.arange(self.dem.shape[1])*dx
                self.lat = y0-np.arange(self.dem.shape[0])*dy
                if 42113 in tags:
                    self.nodata = float(str(tags[42113]).strip('\x00'))
        if self.dem.shape != (len(self.lat), len(self.lon)):
            raise ValueError('DEM和经纬度维数不一致。')
        self.dx, self.dy = float(self.lon[1]-self.lon[0]), float(self.lat[1]-self.lat[0])
        if not np.allclose(np.diff(self.lon), self.dx) or not np.allclose(np.diff(self.lat), self.dy):
            raise ValueError('DEM经纬度必须为等间距像元中心坐标。')
        self.arcs, self.arc_cells, self._node_rc = {}, {}, {}
        for nid, n in nodes.items():
            r, c = (n.lat-self.lat[0])/self.dy+0.5, (n.lon-self.lon[0])/self.dx+0.5
            if not (0 < r < len(self.lat) and 0 < c < len(self.lon)):
                raise ValueError(f'节点{nid}不在DEM内部。')
            self._node_rc[nid] = (r, c)
        for a, b in itertools.combinations(sorted(nodes), 2):
            cells = continuous_supercover(*self._node_rc[a], *self._node_rc[b])
            if any(not (0 <= r < len(self.lat) and 0 <= c < len(self.lon)) for r,c in cells):
                raise ValueError(f'航段{a}-{b}接触DEM外部。')
            vals = np.array([self.dem[r,c] for r,c in cells])
            if np.any(~np.isfinite(vals)) or np.any(np.isclose(vals, self.nodata)):
                raise ValueError(f'航段{a}-{b}存在NoData，不能忽略未知地形后继续求解。')
            z = float(vals.max())
            d = haversine_m(nodes[a].lon,nodes[a].lat,nodes[b].lon,nodes[b].lat)
            for u,v in ((a,b),(b,a)):
                h = z+50
                if h < max(nodes[u].work_alt, nodes[v].work_alt)-1e-7:
                    raise ValueError(f'{u}-{v}巡航海拔低于节点作业高度，请核对节点与DEM。')
                up, down = h-nodes[u].work_alt, h-nodes[v].work_alt
                times = {g:up/t.climb_speed+d/t.cruise_speed+down/t.descent_speed for g,t in drone_types.items()}
                self.arcs[u,v] = Arc(u,v,d,z,h,nodes[u].work_alt,nodes[v].work_alt,up,down,times)
                self.arc_cells[u,v] = cells

    def __getitem__(self, key):
        return self.arcs[key]

    def export_csv(self, path):
        fields = ['from','to','distance_m','dem_max_m','cruise_alt_m','climb_m','descent_m','n_cells','time_A','time_B','time_C']
        with path.open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.writer(f);w.writerow(fields)
            for (a,b),arc in sorted(self.arcs.items()):
                w.writerow([a,b,arc.distance_m,arc.dem_max_m,arc.cruise_alt_m,arc.climb_m,arc.descent_m,len(self.arc_cells[a,b])]+[arc.flight_time_by_type.get(g,'') for g in ('A','B','C')])


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
    """五点以内全排列；更长路线比较原序和反序，其他顺序由插入搜索生成。"""
    import itertools
    if len(route.stops) <= 1:
        return route.clone()
    best = route.clone()
    best_c = evaluator.proxy_cost(best)
    # 五点以内最多120个排列；更长路线避免阶乘枚举。
    for perm in (itertools.permutations(route.stops) if len(route.stops) <= 5 else [route.stops, list(reversed(route.stops))]):
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
        for bid in sorted(pool):
            b = boxes[bid]
            opts = enumerate_insert_options(routes, b, evaluator)
            if not opts:
                return None
            best_cost = opts[0][0]
            second = opts[1][0] if len(opts) >= 2 else best_cost + 10.0
            regret = second - best_cost
            # 硬时限/早期望箱在 regret 相同情况下优先
            urgency = (b.hard_deadline if b.hard_deadline is not None else b.expected_time)
            key = (regret, -best_cost, b.priority, 1.0 / max(urgency, 1.0))
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


class GreedyDecoder:
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
                    start = max(d.available_time, b.available_time)
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
            t.trip_id = f"Q2T{idx:03d}"
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


def destroy_random(routes: List[Route], q: int, rng: random.Random) -> Tuple[List[Route], List[str]]:
    all_ids = [b for r in routes for b in r.box_ids()]
    q = min(q, len(all_ids))
    rem = set(rng.sample(all_ids, q))
    return remove_boxes_from_routes(routes, rem), sorted(rem)


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
    return remove_boxes_from_routes(routes, rem), sorted(rem)


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
    return remove_boxes_from_routes(routes, rem), sorted(rem)



PROFILES = {
    'balanced': {'lateness': .45, 'makespan': .25, 'energy': .20, 'trips': .10},
    'time': {'lateness': .45, 'makespan': .40, 'energy': .10, 'trips': .05},
    'energy': {'lateness': .45, 'makespan': .10, 'energy': .40, 'trips': .05},
    'trips': {'lateness': .45, 'makespan': .10, 'energy': .10, 'trips': .35},
}


def json_safe(value):
    """标准JSON不允许Infinity；无穷最晚开始表示无硬截止，导出为null。"""
    if isinstance(value,float) and not math.isfinite(value): return None
    if isinstance(value,dict): return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [json_safe(v) for v in value]
    return value


def objective(m, scales, weights):
    if m.hard_violations or not math.isfinite(m.makespan):
        return math.inf
    return (weights['lateness']*m.weighted_lateness/scales['lateness']
            +weights['makespan']*m.makespan/scales['makespan']
            +weights['energy']*m.energy/scales['energy']
            +weights['trips']*m.trips/scales['trips'])


def finish_decoding(trips):
    trips.sort(key=lambda t:(t.start_time,t.drone_id,t.return_time))
    deliveries={}
    for i,t in enumerate(trips,1):
        t.trip_id=f'Q2T{i:03d}'
        for b,tm in t.box_delivery.items():
            if b in deliveries:
                return DecodeResult(False,'DUPLICATE_BOX_AFTER_DECODING')
            deliveries[b]=tm
    return DecodeResult(True,'OK',trips,deliveries)


class PortfolioDecoder(GreedyDecoder):
    """三种列表调度规则取优；失败只表示规则没找到解，不证明实例不可行。"""
    def __init__(self,evaluator,drone_units,batteries,boxes,scales,weights):
        super().__init__(evaluator,drone_units,batteries,boxes)
        self.scales,self.weights=scales,weights

    def one_rule(self,routes,rule):
        da={d.drone_id:0.0 for d in self.drone_units}
        ba={f'BAT-{g}{k:02d}':0.0 for g,s in self.battery_specs.items() for k in range(1,s.count+1)}
        dt={d.drone_id:d.type_id for d in self.drone_units}
        bt={b:b[4] for b in ba}
        evaluations={i:self.evaluator.feasible_types(r) for i,r in enumerate(routes)}
        remaining=set(range(len(routes)));trips=[];current_end=0.0
        while remaining:
            choices=[]
            for i in sorted(remaining):
                opts=[]
                for g,ev in evaluations[i].items():
                    ds=[d for d in da if dt[d]==g];bs=[b for b in ba if bt[b]==g]
                    if not ds or not bs: continue
                    d=min(ds,key=lambda x:(da[x],x));b=min(bs,key=lambda x:(ba[x],x))
                    start=max(da[d],ba[b]);end=start+ev.duration
                    if start>ev.latest_start+1e-8: continue
                    late=sum(self.boxes[x].priority*max(0,start+off-self.boxes[x].expected_time)/self.boxes[x].expected_time for x,off in ev.box_delivery_offsets.items())
                    score=(self.weights['lateness']*late/self.scales['lateness']
                           +self.weights['makespan']*max(0,end-current_end)/self.scales['makespan']
                           +self.weights['energy']*ev.total_energy/self.scales['energy']
                           +.025*end/self.scales['makespan'])
                    opts.append((score,end,g,d,b,start,ev))
                if not opts:
                    return DecodeResult(False,'HEURISTIC_RULE_FAILED',bottleneck_routes=[i])
                opt=min(opts,key=lambda x:(x[0],x[1],x[2]))
                ev,start=opt[6],opt[5]
                if rule==0:
                    priority=(0 if math.isfinite(ev.latest_start) else 1,
                              ev.latest_start-start if math.isfinite(ev.latest_start) else ev.soft_due_start-start)
                else:
                    # 允许紧急的普通物资在宽裕硬时限任务之前。
                    priority=(min(ev.latest_start,ev.soft_due_start)-start,
                              0 if math.isfinite(ev.latest_start) else 1)
                choices.append((priority,i,opt))
            _,i,opt=min(choices,key=lambda x:(x[0],x[1]))
            _,end,g,d,b,start,ev=opt
            charge_end=end+charge_time_seconds(ev.end_soc,self.battery_specs[g].full_charge_time)
            delivery={x:start+off for x,off in ev.box_delivery_offsets.items()}
            trips.append(ScheduledTrip(i,d,g,b,start,start+ev.takeoff_offset,end,
                         ev.total_energy,ev.end_soc,charge_end,routes[i].service_ids(),delivery,ev))
            da[d]=end;ba[b]=charge_end;current_end=max(current_end,end);remaining.remove(i)
        return finish_decoding(trips)

    def decode(self,routes):
        options=[super().decode(routes),self.one_rule(routes,0),self.one_rule(routes,1)]
        good=[d for d in options if d.feasible]
        return min(good,key=lambda d:objective(compute_metrics(d,self.boxes),self.scales,self.weights)) if good else options[0]


def search_routes(initial,boxes,nodes,evaluator,decoder,iterations,seed,scales,weights,pool):
    rng=random.Random(seed)
    current=[r.clone() for r in initial]
    dec=decoder.decode(current)
    if not dec.feasible:
        raise RuntimeError('初始列表调度失败，不能由此推断原问题不可行。请使用更宽的候选池或精确调度。')
    best=[r.clone() for r in current];best_dec=dec
    score=objective(compute_metrics(dec,boxes),scales,weights);best_score=score
    history=[(0,best_score)]
    op_weights=[1.,1.,1.,1.]
    for it in range(1,iterations+1):
        op=rng.choices(range(4),weights=op_weights,k=1)[0]
        q=rng.randint(4,10)
        if op==0: partial,rem=destroy_random(current,q,rng)
        elif op==1: partial,rem=destroy_related(current,q,boxes,nodes,rng)
        elif op==2: partial,rem=destroy_worst(current,q,boxes,dec,rng)
        else:
            rr=rng.choice(current)
            rem=sorted(rr.box_ids())
            partial=remove_boxes_from_routes(current,set(rem))
        cand=regret2_repair(partial,rem,boxes,evaluator)
        if cand is None: continue
        if it%4==0:
            k=rng.randrange(len(cand))
            cand[k]=best_order_for_stops(cand[k],evaluator,evaluator.arcs)
        ensure_route_integrity(cand,boxes)
        # 搜集物理/零时刻硬时限可行的路线；完整调度失败时也可参与后续重组。
        for r in cand: pool.setdefault(r.signature(),r.clone())
        cd=decoder.decode(cand)
        if not cd.feasible: op_weights[op]=max(.2,op_weights[op]*.995);continue
        cs=objective(compute_metrics(cd,boxes),scales,weights)
        temperature=max(.0005,.025*(1-it/max(iterations,1)))
        if cs<score or rng.random()<math.exp(min(0,(score-cs)/temperature)):
            current,dec,score=cand,cd,cs
            op_weights[op]=.995*op_weights[op]+.005*1.5
        if cs<best_score-1e-10:
            best,best_dec,best_score=[r.clone() for r in cand],cd,cs
            op_weights[op]+=.15
        if it%25==0 or it==iterations: history.append((it,best_score))
        # 限制缓存内存；只影响速度，不改变模型。
        if len(evaluator.cache)>150000: evaluator.cache.clear()
    return best,best_dec,history


def candidate_pool(pool,mandatory,boxes,evaluator,limit):
    selected={r.signature():r.clone() for rs in mandatory for r in rs}
    for b in boxes.values():
        r=route_from_box(b)
        if evaluator.feasible_types(r): selected.setdefault(r.signature(),r)
    # 给每个货箱保留若干低代理成本路线，避免只保留大批次导致覆盖偏斜。
    ranked=sorted(pool.values(),key=lambda r:(evaluator.proxy_cost(r)/len(r.box_ids()),r.signature()))
    count=Counter()
    for r in ranked:
        if any(count[b]<2 for b in r.box_ids()):
            selected.setdefault(r.signature(),r.clone());count.update(r.box_ids())
        if len(selected)>=limit: break
    for r in ranked:
        if len(selected)>=limit: break
        selected.setdefault(r.signature(),r.clone())
    return list(selected.values())


def timed_route_milp(routes,evaluator,units,batteries,boxes,scales,weights,step,horizon,time_limit,label):
    """候选路线+开始时间列，集合划分与两类可再生资源容量约束。

    时间单位仍为秒，开始时刻按step离散；资源占用向上取整覆盖到下一个格点。
    Cmax以精确返航时刻约束。gap仅针对本候选路线池和离散时域。
    """
    t0=time.perf_counter(); gids=sorted(evaluator.drone_types);bid=sorted(boxes)
    bindex={b:i for i,b in enumerate(bid)};nb=len(bid)
    slots=int(math.ceil(horizon/step));horizon=slots*step
    n_drone=Counter(u.type_id for u in units)
    # 行:逐箱覆盖;逐箱对应架次结束<=Cmax;各机型无人机及电池逐时容量;机型工作量。
    res0=2*nb;work0=res0+2*len(gids)*slots;nr=work0+len(gids)
    lower=np.full(nr,-np.inf);upper=np.full(nr,np.inf)
    lower[:nb]=1.;upper[:nb]=1.;upper[nb:2*nb]=0.;upper[work0:]=0.
    for gi,g in enumerate(gids):
        upper[res0+2*gi*slots:res0+(2*gi+1)*slots]=n_drone[g]
        upper[res0+(2*gi+1)*slots:res0+(2*gi+2)*slots]=batteries[g].count
    rows=[];cols=[];data=[];cost=[];options=[]
    def add(row,j,val): rows.append(row);cols.append(j);data.append(val)
    for ri,r in enumerate(routes):
        for gi,g in enumerate(gids):
            ev=evaluator.evaluate(r,g)
            if not ev.feasible or n_drone[g]==0 or batteries[g].count==0: continue
            charge=charge_time_seconds(ev.end_soc,batteries[g].full_charge_time)
            nd=int(math.ceil((ev.duration-1e-8)/step))
            nbatt=int(math.ceil((ev.duration+charge-1e-8)/step))
            last=min(horizon-ev.duration,ev.latest_start)
            if last<0: continue
            inds=[bindex[b] for b in r.box_ids()]
            for k in range(int(math.floor((last+1e-8)/step))+1):
                start=k*step;end=start+ev.duration;j=len(cost)
                late=sum(boxes[b].priority*max(0.,start+off-boxes[b].expected_time)/boxes[b].expected_time for b,off in ev.box_delivery_offsets.items())
                c=(weights['lateness']*late/scales['lateness']+weights['energy']*ev.total_energy/scales['energy']+weights['trips']/scales['trips'])
                cost.append(c);options.append((ri,g,start,ev))
                for i in inds: add(i,j,1.);add(nb+i,j,end/scales['makespan'])
                for s in range(k,min(slots,k+nd)): add(res0+2*gi*slots+s,j,1.)
                for s in range(k,min(slots,k+nbatt)): add(res0+(2*gi+1)*slots+s,j,1.)
                add(work0+gi,j,ev.duration/(n_drone[g]*scales['makespan']))
    # Cmax variable expressed in units of makespan scale to keep coefficients comparable.
    nc=len(cost);cost.append(weights['makespan'])
    for i in range(nb): add(nb+i,nc,-1.)
    for gi in range(len(gids)): add(work0+gi,nc,-1.)
    if nc==0: return None,{'label':label,'status':'NO_COLUMNS','global_optimality_proven':False}
    matrix=coo_matrix((data,(rows,cols)),shape=(nr,nc+1)).tocsc()
    bounds=Bounds(np.zeros(nc+1),np.r_[np.ones(nc),horizon/scales['makespan']])
    print(f'  MILP {label}: {len(routes)} routes, {nc} columns, {nr} constraints, limit={time_limit}s',flush=True)
    res=milp(np.asarray(cost),integrality=np.r_[np.ones(nc),0],bounds=bounds,
             constraints=LinearConstraint(matrix,lower,upper),
             options={'time_limit':float(time_limit),'mip_rel_gap':.01,'presolve':True})
    cert={'label':label,'status':int(res.status),'message':res.message,'route_count':len(routes),
          'column_count':nc,'time_step_seconds':step,'horizon_seconds':horizon,
          'objective':float(res.fun) if res.fun is not None else None,
          'restricted_dual_bound':float(res.mip_dual_bound) if getattr(res,'mip_dual_bound',None) is not None else None,
          'restricted_gap':float(res.mip_gap) if getattr(res,'mip_gap',None) is not None else None,
          'elapsed_seconds':time.perf_counter()-t0,'global_optimality_proven':False,
          'bound_scope':'Only this finite route pool, start grid and time horizon; NOT a lower bound for the unrestricted original problem.'}
    if res.x is None: return None,cert
    chosen=[options[j] for j,x in enumerate(res.x[:-1]) if x>.5]
    if any(abs(x-round(x))>1e-4 for x in res.x[:-1]):
        cert['rejected']='Non-integral incumbent';return None,cert
    # Identical resources of each type: interval graph coloring recovers concrete IDs.
    da={g:[(0.,d.drone_id) for d in units if d.type_id==g] for g in gids}
    ba={g:[(0.,f'BAT-{g}{i:02d}') for i in range(1,batteries[g].count+1)] for g in gids}
    for h in list(da.values())+list(ba.values()): heapq.heapify(h)
    trips=[]
    for ri,g,start,ev in sorted(chosen,key=lambda x:(x[2],x[1],x[0])):
        dtime,d=heapq.heappop(da[g]);btime,b=heapq.heappop(ba[g])
        if max(dtime,btime)>start+1e-5:
            raise AssertionError('MILP resource coloring failed')
        end=start+ev.duration
        charge_end=end+charge_time_seconds(ev.end_soc,batteries[g].full_charge_time)
        heapq.heappush(da[g],(end,d));heapq.heappush(ba[g],(charge_end,b))
        delivery={x:start+off for x,off in ev.box_delivery_offsets.items()}
        trips.append(ScheduledTrip(ri,d,g,b,start,start+ev.takeoff_offset,end,ev.total_energy,
                                  ev.end_soc,charge_end,routes[ri].service_ids(),delivery,ev))
    dec=finish_decoding(trips)
    if not dec.feasible or set(dec.box_delivery)!=set(boxes): raise AssertionError('MILP cover check failed')
    cert['recomputed_objective']=objective(compute_metrics(dec,boxes),scales,weights)
    if abs(cert['recomputed_objective']-cert['objective'])>1e-4:
        raise AssertionError('MILP epigraph/objective mismatch')
    return dec,cert


def compact_schedule(decoded,batteries):
    """保持每个资源上的架次顺序，用连续时间向左压紧，恢复网格等待。"""
    da=defaultdict(float);ba=defaultdict(float);trips=[]
    for t in sorted(decoded.trips,key=lambda x:(x.start_time,x.drone_id,x.trip_id)):
        start=max(da[t.drone_id],ba[t.battery_id]);ev=t.route_eval
        end=start+ev.duration;ce=end+charge_time_seconds(ev.end_soc,batteries[t.drone_type].full_charge_time)
        trip=ScheduledTrip(t.route_index,t.drone_id,t.drone_type,t.battery_id,start,start+ev.takeoff_offset,
                           end,ev.total_energy,ev.end_soc,ce,list(t.service_sequence),
                           {b:start+off for b,off in ev.box_delivery_offsets.items()},ev)
        trips.append(trip);da[t.drone_id]=end;ba[t.battery_id]=ce
    return finish_decoding(trips)


def independent_audit(decoded,nodes,boxes,types,units,batteries,arcs):
    """从货箱和航线重新计算，不把RouteEval.feasible/缓存数值当作证明。"""
    errors=[];counts=Counter();drone_ids={u.drone_id:u.type_id for u in units}
    batt_ids={f'BAT-{g}{i:02d}':g for g,s in batteries.items() for i in range(1,s.count+1)}
    di=defaultdict(list);bi=defaultdict(list);min_soc=1.;min_slack=math.inf
    recomputed_deliveries={};energy_total=0.;max_return=0.;leg_records=[];stop_records=[]
    def close(a,b,label,tol=1e-5):
        if not math.isfinite(a) or abs(a-b)>tol: errors.append(f'{label}: {a} != {b}')
    for t in decoded.trips:
        g=t.drone_type
        if g not in types: errors.append(f'Unknown type {g}');continue
        dt=types[g]
        if drone_ids.get(t.drone_id)!=g: errors.append(f'Drone type/inventory {t.trip_id}')
        if batt_ids.get(t.battery_id)!=g: errors.append(f'Battery type/inventory {t.trip_id}')
        if t.start_time<0: errors.append(f'Negative start {t.trip_id}')
        allb=list(t.box_delivery)
        if any(b not in boxes for b in allb): errors.append('Unknown box');continue
        counts.update(allb)
        if len(t.service_sequence)!=len(set(t.service_sequence)): errors.append(f'Repeated service {t.trip_id}')
        if set(t.service_sequence)!={boxes[b].service_id for b in allb}: errors.append(f'Destination mismatch {t.trip_id}')
        q=sum(boxes[b].mass for b in allb);v=sum(boxes[b].volume for b in allb)
        if q>dt.max_payload+1e-8 or v>dt.max_volume+1e-9: errors.append(f'Capacity {t.trip_id}')
        time_now=t.start_time+dt.prep_time+len(allb)*dt.load_time_per_box
        close(t.takeoff_time,time_now,f'Takeoff {t.trip_id}')
        total=0.;prev='O01';remaining=set(allb)
        for dest in t.service_sequence+['O01']:
            if dest not in nodes: errors.append(f'Unknown node {dest}');continue
            arc=arcs[prev,dest]
            # Formula copied from problem rules, not from evaluator. Current suffix load is recalculated.
            q=sum(boxes[b].mass for b in remaining);vol=sum(boxes[b].volume for b in remaining)
            rg=dt.empty_range-(dt.empty_range-dt.full_range)*(q/dt.max_payload)**1.5
            e=dt.battery_energy*arc.distance_m/rg+(dt.empty_mass+q)*9.81*arc.climb_m/(dt.climb_efficiency*3600000.)
            duration=arc.climb_m/dt.climb_speed+arc.distance_m/dt.cruise_speed+arc.descent_m/dt.descent_speed
            leg_start=time_now;time_now+=duration;total+=e
            leg_records.append([t.trip_id,prev,dest,q,vol,arc.distance_m,arc.dem_max_m,arc.cruise_alt_m,arc.climb_m,arc.descent_m,leg_start,time_now,duration,e])
            if dest!='O01':
                delivered=sorted(b for b in remaining if boxes[b].service_id==dest)
                arrival=time_now;time_now+=dt.service_base_time+len(delivered)*dt.service_time_per_box
                stop_records.append([t.trip_id,dest,arrival,time_now,';'.join(delivered)])
                for b in delivered:
                    close(t.box_delivery[b],time_now,f'Delivery {b}')
                    recomputed_deliveries[b]=time_now
                    hd=boxes[b].hard_deadline
                    if hd is not None:
                        min_slack=min(min_slack,hd-time_now)
                        if time_now>hd+1e-5: errors.append(f'Hard deadline {b}')
                remaining.difference_update(delivered)
            prev=dest
        if remaining: errors.append(f'Undelivered boxes {t.trip_id}')
        soc=1.-total/dt.battery_energy
        if soc<dt.reserve_ratio-1e-8: errors.append(f'Reserve {t.trip_id}')
        charge=batteries[g].full_charge_time*(.65*max(0.,.9-soc)/.9+.35*min(.1,max(0.,1.-soc))/.1)
        close(t.energy_kwh,total,f'Energy {t.trip_id}');close(t.end_soc,soc,f'SOC {t.trip_id}')
        close(t.return_time,time_now,f'Return {t.trip_id}');close(t.charge_end_time,time_now+charge,f'Charge {t.trip_id}')
        di[t.drone_id].append((t.start_time,time_now,t.trip_id))
        bi[t.battery_id].append((t.start_time,time_now+charge,t.trip_id))
        min_soc=min(min_soc,soc);energy_total+=total;max_return=max(max_return,time_now)
    for b in boxes:
        if counts[b]!=1: errors.append(f'Box count {b} = {counts[b]}')
    for label,resources in [('drone',di),('battery',bi)]:
        for rid,intervals in resources.items():
            intervals.sort()
            for p,n in zip(intervals,intervals[1:]):
                if p[1]>n[0]+1e-5: errors.append(f'{label} overlap {rid}: {p[2]} / {n[2]}')
    def peak(intervals):
        # 独立复算与调度累加存在约1e-12秒浮点差；统一到微秒以处理交接端点。
        events=[(round(a,6),1) for a,b,c in intervals]+[(round(b,6),-1) for a,b,c in intervals]
        now=mx=0
        for _,change in sorted(events): now+=change;mx=max(mx,now)
        return mx
    peaks={g:{'drone_peak':peak([i for d,ints in di.items() if drone_ids.get(d)==g for i in ints]),
              'battery_peak_including_charge':peak([i for b,ints in bi.items() if batt_ids.get(b)==g for i in ints]),
              'drone_inventory':sum(u.type_id==g for u in units),'battery_inventory':batteries[g].count}
           for g in types}
    for g,p in peaks.items():
        if p['drone_peak']>p['drone_inventory'] or p['battery_peak_including_charge']>p['battery_inventory']:
            errors.append(f'Resource peak exceeds inventory {g}')
    report={'valid':not errors,'errors':errors,'box_count':sum(counts.values()),'unique_boxes':len(counts),
            'hard_deadline_box_count':sum(b.hard_deadline is not None for b in boxes.values()),
            'medical_box_count':sum(b.material_type=='医疗物资' for b in boxes.values()),
            'first_batch_box_count':sum(b.first_batch for b in boxes.values()),
            'minimum_return_soc':min_soc,'minimum_hard_deadline_slack_seconds':min_slack,
            'recomputed_energy_kwh':energy_total,'recomputed_makespan_seconds':max_return,'resource_peaks':peaks}
    return report,leg_records,stop_records


def export_solution(decoded,name,out,nodes,boxes,types,units,batteries,arcs):
    folder=out/name;folder.mkdir(parents=True,exist_ok=True)
    audit,legs,stops=independent_audit(decoded,nodes,boxes,types,units,batteries,arcs)
    if not audit['valid']: raise AssertionError(audit['errors'])
    metrics=asdict(compute_metrics(decoded,boxes))
    metrics.update({'on_time_boxes':sum(decoded.box_delivery[b]<=box.expected_time+1e-6 for b,box in boxes.items()),
                    'multi_stop_trips':sum(len(t.service_sequence)>1 for t in decoded.trips)})
    triprows=[];boxrows=[];battrows=[]
    mapping={b:t for t in decoded.trips for b in t.box_delivery}
    for t in decoded.trips:
        triprows.append([t.trip_id,t.drone_id,t.drone_type,t.battery_id,t.start_time,'→'.join(t.service_sequence),t.return_time,t.energy_kwh,t.takeoff_time,t.end_soc,t.charge_end_time,len(t.box_delivery)])
        battrows.append([t.battery_id,t.drone_type,t.trip_id,t.drone_id,t.start_time,t.return_time,1.,t.end_soc,t.return_time,t.charge_end_time,t.charge_end_time-t.return_time])
    for b,box in sorted(boxes.items()):
        t=mapping[b];tm=t.box_delivery[b]
        boxrows.append([b,t.trip_id,box.service_id,tm,box.material_type,box.mass,box.volume,box.expected_time,box.hard_deadline,box.priority,max(0.,tm-box.expected_time),box.priority*max(0.,tm-box.expected_time)/box.expected_time,box.first_batch])
    tables={
      'Q2_运输架次':(['架次编号','无人机编号','机型编号','电池编号','开始时刻（s）','访问服务区顺序','返回O01时刻（s）','架次能耗（kWh）','起飞时刻（s）','返航SOC','充电完成时刻（s）','箱数'],triprows),
      'Q2_逐箱交付':(['货箱编号','架次编号','服务区编号','交付完成时刻（s）','物资类型','质量（kg）','体积（m3）','期望时刻（s）','硬截止（s）','优先系数','迟到秒数','加权迟到','是否首批'],boxrows),
      'Q2_电池使用':(['电池编号','机型','架次','无人机','任务开始（s）','任务结束（s）','起始SOC','返航SOC','充电开始（s）','充电结束（s）','充电时长（s）'],battrows),
      'Q2_航段详情':(['架次','起点','终点','剩余载荷（kg）','剩余体积（m3）','水平距离（m）','DEM最高（m）','巡航海拔（m）','爬升（m）','下降（m）','航段开始（s）','航段结束（s）','飞行时间（s）','能耗（kWh）'],legs),
      'Q2_停点交接':(['架次','服务区','到达（s）','交接完成（s）','交付货箱'],stops),
    }
    for fn,(headers,rows) in tables.items():
        with (folder/(fn+'.csv')).open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.writer(f);w.writerow(headers);w.writerows(rows)
    (folder/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
    payload=json_safe({'name':name,'metrics':metrics,'audit':audit,'tables':tables,'trips':[asdict(t) for t in decoded.trips]})
    (folder/'solution.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return payload


def plot_results(decoded,nodes,boxes,out,arcs):
    colors={'A':'#28789B','B':'#25A18E','C':'#155E63'}
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'pdf.fonttype':42,'svg.fonttype':'none'})
    def save(fig,name):
        fig.tight_layout()
        for ext in ['pdf','svg','png']:
            target=out/f'{name}.{ext}'
            pending=out/f'.{name}.pending.{ext}'
            fig.savefig(pending,format=ext,dpi=180,bbox_inches='tight')
            pending.replace(target)
        plt.close(fig)
    fig,axs=plt.subplots(1,3,figsize=(15,5),sharex=True,sharey=True)
    xmin=min(n.lon for n in nodes.values())-.012;xmax=max(n.lon for n in nodes.values())+.012
    ymin=min(n.lat for n in nodes.values())-.012;ymax=max(n.lat for n in nodes.values())+.012
    for ax,g in zip(axs,colors):
        ax.imshow(arcs.dem,extent=[arcs.lon.min(),arcs.lon.max(),arcs.lat.min(),arcs.lat.max()],origin='upper' if arcs.dy<0 else 'lower',cmap='GnBu',alpha=.45)
        for t in decoded.trips:
            if t.drone_type!=g: continue
            seq=['O01']+t.service_sequence+['O01']
            for a,b in zip(seq,seq[1:]):
                ax.annotate('',xy=(nodes[b].lon,nodes[b].lat),xytext=(nodes[a].lon,nodes[a].lat),arrowprops={'arrowstyle':'->','color':colors[g],'lw':1.2,'alpha':.65})
        for i,n in nodes.items():
            ax.scatter(n.lon,n.lat,c='#15394B',s=25 if i=='O01' else 12,marker='s' if i=='O01' else 'o')
            ax.annotate(i,(n.lon,n.lat),xytext=(3,3),textcoords='offset points',fontsize=7)
        ax.set(xlim=(xmin,xmax),ylim=(ymin,ymax),xlabel='Longitude',title=f'Type {g}')
    axs[0].set_ylabel('Latitude');save(fig,'routes_by_type')
    fig,ax=plt.subplots(figsize=(11,5))
    ids=sorted({t.drone_id for t in decoded.trips})
    for t in decoded.trips:
        y=ids.index(t.drone_id)
        ax.barh(y,(t.return_time-t.start_time)/60,left=t.start_time/60,color=colors[t.drone_type],height=.6)
        ax.barh(y,(t.takeoff_time-t.start_time)/60,left=t.start_time/60,color='#BFD8DF',height=.6)
        ax.text((t.takeoff_time+t.return_time)/120,y,t.trip_id[-3:],ha='center',va='center',fontsize=7,color='white')
    ax.set(yticks=range(len(ids)),yticklabels=ids,xlabel='Time (min)',title='Drone schedule (light segment: preparation and loading)')
    ax.grid(axis='x',alpha=.15);save(fig,'drone_schedule')
    fig,ax=plt.subplots(figsize=(11,6))
    ids=sorted({t.battery_id for t in decoded.trips})
    for t in decoded.trips:
        y=ids.index(t.battery_id)
        ax.barh(y,(t.return_time-t.start_time)/60,left=t.start_time/60,color=colors[t.drone_type],height=.55)
        ax.barh(y,(t.charge_end_time-t.return_time)/60,left=t.return_time/60,color='#CDE8E2',edgecolor=colors[t.drone_type],height=.4,hatch='//')
    ax.set(yticks=range(len(ids)),yticklabels=ids,xlabel='Time (min)',title='Battery occupation and recharge (hatched)')
    ax.grid(axis='x',alpha=.15);save(fig,'battery_schedule')
    fig,ax=plt.subplots(figsize=(11,4.8))
    order=sorted(boxes,key=lambda b:(boxes[b].expected_time,b))
    ax.plot([decoded.box_delivery[b]/60 for b in order],'.',color='#28789B',label='Delivered')
    ax.plot([boxes[b].expected_time/60 for b in order],color='#25A18E',label='Expected')
    ax.scatter(range(len(order)),[boxes[b].hard_deadline/60 if boxes[b].hard_deadline else np.nan for b in order],marker='x',color='#8B5E3C',label='Hard deadline')
    ax.set(xlabel='Box index (sorted by due time)',ylabel='Time (min)',title=f'All {len(boxes)} boxes');ax.legend();ax.grid(alpha=.15);save(fig,'box_delivery')


def choose_file(root,stem,suffixes):
    found=[p for p in root.rglob(stem+'*') if p.suffix.lower() in suffixes and p.is_file()]
    if not found: raise FileNotFoundError(f'{root} 中未找到 {stem} ({suffixes})')
    # Exact unsuffixed source wins; ambiguous duplicate versions require explicit data folder.
    exact=[p for p in found if p.stem==stem]
    found=exact or found
    found.sort(key=lambda p:(suffixes.index(p.suffix.lower()),str(p)))
    same=[p for p in found if p.suffix.lower()==found[0].suffix.lower()]
    if len(same)>1: raise ValueError(f'{stem} 存在多个同格式版本，请将本次四个输入文件放到独立目录：{same}')
    return found[0]


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--output',type=Path,default=Path('Q2_results'))
    ap.add_argument('--iters',type=int,default=500)
    ap.add_argument('--seeds',type=int,nargs='+',default=[20260924,20260925])
    ap.add_argument('--profiles',nargs='+',choices=list(PROFILES),default=list(PROFILES))
    ap.add_argument('--milp-seconds',type=float,default=90)
    ap.add_argument('--step',type=float,default=60)
    ap.add_argument('--pool-size',type=int,default=240)
    ap.add_argument('--horizon',type=float,default=0,help='0: 1.4 times initial makespan; this is an algorithmic restriction')
    ap.add_argument('--warm-routes',type=Path,help='optional JSON list of [(service_id,box_ids),...] routes')
    ap.add_argument('--no-plots',action='store_true')
    args=ap.parse_args()
    if args.step<=0 or args.iters<0 or args.milp_seconds<0: ap.error('step>0; iters/milp-seconds>=0')
    t0=time.perf_counter();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    paths={k:choose_file(args.root.resolve(),stem,suff) for k,stem,suff in [
      ('nodes','调度中心与服务区',['.xlsx']),('boxes','物资需求与配送时限',['.xlsx']),
      ('drones','运输无人机数据',['.xlsx']),('dem','镇龙乡及周边30米DEM',['.tif','.tiff','.mat'])]}
    nodes=load_nodes(paths['nodes']);boxes=load_boxes(paths['boxes']);types,units,batteries=load_drone_data(paths['drones'])
    arcs=ArcLibrary(nodes,types,paths['dem']);arcs.export_csv(out/'arc_library.csv')
    ev=RouteEvaluator(boxes,types,arcs);initial=construct_initial_routes(boxes,ev)
    old=GreedyDecoder(ev,units,batteries,boxes);initial_dec=old.decode(initial)
    if not initial_dec.feasible: raise RuntimeError(f'初始启发式失败：{initial_dec.reason}，不是全局不可行证明。')
    bm=compute_metrics(initial_dec,boxes)
    scales={'lateness':max(1.,bm.weighted_lateness),'makespan':bm.makespan,'energy':bm.energy,'trips':bm.trips}
    pool={r.signature():r.clone() for r in initial};mandatory=[initial]
    warm=None
    if args.warm_routes:
        warm=[Route([Stop(s,list(b)) for s,b in rr]) for rr in json.loads(args.warm_routes.read_text(encoding='utf-8'))]
        ensure_route_integrity(warm,boxes);mandatory.append(warm)
        for r in warm: pool.setdefault(r.signature(),r.clone())
    archive=[];certs=[];history=[];seed_stats=[]
    # Shared baseline scales are frozen before all seeds and weight scenarios.
    for name in args.profiles:
        weights=PROFILES[name];decoder=PortfolioDecoder(ev,units,batteries,boxes,scales,weights)
        choices=[initial]+([warm] if warm else [])
        choices=[r for r in choices if decoder.decode(r).feasible]
        start=min(choices,key=lambda rs:objective(compute_metrics(decoder.decode(rs),boxes),scales,weights))
        best_dec=decoder.decode(start);best_routes=start
        for seed in args.seeds:
            ts=time.perf_counter()
            r,d,h=search_routes(start,boxes,nodes,ev,decoder,args.iters,seed,scales,weights,pool)
            m=compute_metrics(d,boxes);seed_stats.append({'profile':name,'seed':seed,**asdict(m),'elapsed_seconds':time.perf_counter()-ts})
            history.extend([[name,seed,it,sc] for it,sc in h]);mandatory.append(r)
            if objective(m,scales,weights)<objective(compute_metrics(best_dec,boxes),scales,weights): best_dec,best_routes=d,r
            print(f'{name} seed={seed}: L={m.weighted_lateness:.5f}, C={m.makespan:.2f}s, E={m.energy:.5f}, N={m.trips}',flush=True)
        archive.append((name,'ALNS',best_dec,best_routes))
    horizon=args.horizon or math.ceil(1.4*bm.makespan/args.step)*args.step
    candidates=candidate_pool(pool,mandatory,boxes,ev,args.pool_size)
    # Reoptimize fixed batching first, then allow route recombination in the shared pool.
    for idx,(name,source,dec,routes) in enumerate(list(archive)):
        weights=PROFILES[name]
        if args.milp_seconds>0:
            for stage,rs,seconds in [('fixed_routes',routes,args.milp_seconds*.4),('route_pool',candidates,args.milp_seconds*.6)]:
                md,cert=timed_route_milp(rs,ev,units,batteries,boxes,scales,weights,args.step,horizon,seconds,name+'_'+stage)
                certs.append(cert)
                if md is not None:
                    compact=compact_schedule(md,batteries)
                    audit,_,_=independent_audit(compact,nodes,boxes,types,units,batteries,arcs)
                    if not audit['valid']: raise AssertionError(audit['errors'])
                    if objective(compute_metrics(compact,boxes),scales,weights)<objective(compute_metrics(dec,boxes),scales,weights)-1e-10:
                        dec=compact;source='MILP_'+stage+'_compacted'
        archive[idx]=(name,source,dec,routes)
    # Each requested profile may select the best available solution across the entire archive.
    comparison=[];payloads={};solutions={}
    for name in args.profiles:
        origin,source,dec,_=min(archive,key=lambda x:objective(compute_metrics(x[2],boxes),scales,PROFILES[name]))
        payload=export_solution(dec,name,out,nodes,boxes,types,units,batteries,arcs)
        row={'profile':name,'source':origin+':'+source,**payload['metrics'],'objective':objective(compute_metrics(dec,boxes),scales,PROFILES[name])}
        comparison.append(row);payloads[name]=payload;solutions[name]=dec
    for r in comparison:
        vals=lambda z:(z['weighted_lateness'],z['makespan'],z['energy'],z['trips'])
        r['nondominated_in_reported_set']=not any(all(a<=b+1e-7 for a,b in zip(vals(s),vals(r))) and any(a<b-1e-7 for a,b in zip(vals(s),vals(r))) for s in comparison if s is not r)
    metadata={'algorithm':'Multi-start ALNS + finite-route time-index MILP + continuous left shift',
        'input_sha256':{k:{'name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for k,p in paths.items()},
        'scales':scales,'weights':PROFILES,'iterations_per_seed_profile':args.iters,'seeds':args.seeds,
        'pool_limit_requested':args.pool_size,'pool_size_actual':len(candidates),'time_grid_seconds':args.step,
        'horizon_seconds':horizon,'seed_runs':seed_stats,'comparison':comparison,'milp_certificates':certs,
        'initial_metrics':asdict(bm),'global_optimality_proven':False,'runtime_seconds':time.perf_counter()-t0,
        'assumptions':['Drone and full battery reserved from preparation start to return; battery then charges to full.',
          'Different batteries charge in parallel; no finite charger constraint supplied.',
          'All boxes at one stop complete delivery at the end of that stop handover (conservative).',
          'Energy follows Appendix 2; no separate hover/descent energy is added.',
          'Start-grid, horizon and finite pool are computational restrictions, not requirements of the problem.']}
    (out/'run_summary.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'scenario_comparison.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(comparison[0]));w.writeheader();w.writerows(comparison)
    with (out/'convergence.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['profile','seed','iteration','best_objective']);w.writerows(history)
    chosen='balanced' if 'balanced' in solutions else args.profiles[0]
    (out/'workbook_data.json').write_text(json.dumps({'comparison':comparison,'summary':metadata,'solution':payloads[chosen]},ensure_ascii=False),encoding='utf-8')
    if not args.no_plots: plot_results(solutions[chosen],nodes,boxes,out,arcs)
    print(json.dumps({'results':comparison,'seconds':metadata['runtime_seconds'],'global_optimality_proven':False},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
