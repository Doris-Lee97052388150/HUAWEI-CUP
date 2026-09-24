# -*- coding: utf-8 -*-
"""
问题二完整版：异构无人机多点多架次运输调度
流程：数据读取 -> 航段预计算 -> Regret 初始解 -> ALNS 优化 -> 输出 Q2 模板 + 图表
"""

import os
import math
import copy
import random
import time as time_module
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False
    print("警告: 未安装 rasterio，DEM 将使用节点海拔近似。")

# ============================================================
# 全局配置
# ============================================================
DATA_DIR = "数据"
DEM_PATH = os.path.join(DATA_DIR, "镇龙乡地理空间数据", "数字高程模型数据（DEM）", "镇龙乡30米DEM.tif")
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

R_EARTH = 6371000.0
SEED = 42
random.seed(SEED)
np.random.seed(SEED)

# 权重（可调整）
W_LATE = 0.35
W_CMAX = 0.25
W_ENERGY = 0.25
W_SORTIES = 0.15

# ALNS 参数
ALNS_MAX_ITER = 800
ALNS_TIME_LIMIT = 120.0
ALNS_T0 = 0.5
ALNS_ALPHA = 0.995


# ============================================================
# 1. 数据结构
# ============================================================
@dataclass
class Node:
    id: str
    name: str
    lon: float
    lat: float
    alt: float
    population: int = 0
    x: float = 0.0
    y: float = 0.0


@dataclass
class Box:
    box_id: str
    service_id: str
    material: str
    mass: float
    volume: float
    is_first_batch: bool
    first_deadline: Optional[float]
    expected_time: float
    priority: float
    hard_deadline: float = float('inf')


@dataclass
class DroneModel:
    model_id: str
    name: str
    empty_mass: float
    max_payload: float
    max_volume: float
    cruise_speed: float
    range_empty: float
    range_full: float
    battery_energy: float
    return_soc_min: float
    prepare_time: float
    load_time_per_box: float
    service_base_time: float
    service_time_per_box: float
    climb_speed: float
    descent_speed: float
    climb_eff: float
    descent_eff: float


@dataclass
class Drone:
    drone_id: str
    model_id: str
    init_pos: str
    available_time: float = 0.0


@dataclass
class Battery:
    battery_id: str
    model_id: str
    available_time: float = 0.0
    soc: float = 1.0
    full_charge_time: float = 3000.0


@dataclass
class Sortie:
    sortie_id: str
    model_id: str
    service_seq: List[str]                    # 访问服务区顺序
    boxes: List[Box]                          # 架次携带货箱
    boxes_by_service: Dict[str, List[Box]] = field(default_factory=dict)
    total_mass: float = 0.0
    total_volume: float = 0.0
    feasible: bool = True
    reason: str = ""
    total_time: float = 0.0
    total_energy: float = 0.0
    return_soc: float = 1.0
    segment_payload: List[float] = field(default_factory=list)
    segment_time: List[float] = field(default_factory=list)
    segment_energy: List[float] = field(default_factory=list)
    delivery_time: Dict[str, float] = field(default_factory=dict)


# ============================================================
# 2. Excel 读取
# ============================================================
def _find_row(df: pd.DataFrame, keyword: str) -> int:
    mask = df[0].astype(str).str.contains(keyword, na=False)
    idx = df.index[mask]
    if len(idx) == 0:
        raise ValueError(f"未找到关键词: {keyword}")
    return idx[0]


def load_nodes(file_path: str):
    df = pd.read_excel(file_path, header=None)
    r = _find_row(df, "调度中心编号")
    o01_row = df.iloc[r + 1]
    o01 = Node(
        id=str(o01_row[0]).strip(),
        name=str(o01_row[1]).strip(),
        lon=float(o01_row[2]),
        lat=float(o01_row[3]),
        alt=float(o01_row[4]),
        population=0
    )
    r = _find_row(df, "服务区编号")
    service_rows = df.iloc[r + 1:].dropna(subset=[0])
    services = []
    for _, row in service_rows.iterrows():
        sid = str(row[0]).strip()
        if sid == "" or sid.lower() == "nan" or not sid.startswith("S"):
            continue
        services.append(Node(
            id=sid,
            name=str(row[1]).strip(),
            lon=float(row[2]),
            lat=float(row[3]),
            alt=float(row[4]),
            population=int(row[5]) if len(row) > 5 and not pd.isna(row[5]) else 0
        ))
    return o01, services


def load_boxes(file_path: str):
    df = pd.read_excel(file_path, sheet_name="逐箱货箱清单", header=0)
    boxes = []
    for _, row in df.iterrows():
        box_id = str(row["货箱编号"]).strip()
        if box_id == "" or box_id.lower() == "nan":
            continue
        service_id = str(row["服务区编号"]).strip()
        material = str(row["物资类型"]).strip()
        mass = float(row["单箱质量（kg）"])
        volume = float(row["单箱体积（m³）"])
        is_first = str(row["是否首批保障"]).strip() == "是"
        first_deadline = float(row["首批截止时间（s）"]) if not pd.isna(row["首批截止时间（s）"]) else None
        expected_time = float(row["期望送达时间（s）"])
        priority = float(row["应急优先系数"])
        hard_deadline = float('inf')
        if material == "医疗物资":
            hard_deadline = min(hard_deadline, expected_time)
        if is_first and first_deadline is not None:
            hard_deadline = min(hard_deadline, first_deadline)
        boxes.append(Box(
            box_id=box_id, service_id=service_id, material=material,
            mass=mass, volume=volume, is_first_batch=is_first,
            first_deadline=first_deadline, expected_time=expected_time,
            priority=priority, hard_deadline=hard_deadline
        ))
    return boxes


def load_drone_models(file_path: str):
    df = pd.read_excel(file_path, header=None)
    r = _find_row(df, "机型编号")
    models = {}
    for i in range(r + 1, len(df)):
        row = df.iloc[i]
        mid = str(row[0]).strip()
        if mid not in ("A", "B", "C"):
            break
        try:
            models[mid] = DroneModel(
                model_id=mid, name=str(row[1]).strip(),
                empty_mass=float(row[2]), max_payload=float(row[3]),
                max_volume=float(row[4]), cruise_speed=float(row[5]),
                range_empty=float(row[6]), range_full=float(row[7]),
                battery_energy=float(row[8]),
                return_soc_min=float(row[9]) / 100.0,
                prepare_time=float(row[10]),
                load_time_per_box=float(row[11]),
                service_base_time=float(row[12]),
                service_time_per_box=float(row[13]),
                climb_speed=float(row[14]), descent_speed=float(row[15]),
                climb_eff=float(row[16]), descent_eff=float(row[17])
            )
        except (ValueError, TypeError, IndexError):
            break
    return models


def load_drones_and_batteries(file_path: str):
    df = pd.read_excel(file_path, header=None)
    drones = []
    batteries = []
    r = _find_row(df, "无人机编号")
    for i in range(r + 1, len(df)):
        row = df.iloc[i]
        did = str(row[0]).strip()
        if not did.startswith("U"):
            break
        drones.append(Drone(drone_id=did, model_id=str(row[1]).strip(), init_pos=str(row[2]).strip()))

    r = _find_row(df, "共享电池库存")
    header_r = None
    for i in range(r + 1, min(r + 5, len(df))):
        if "机型编号" in str(df.iloc[i, 0]):
            header_r = i
            break
    if header_r is None:
        return drones, batteries
    for i in range(header_r + 1, len(df)):
        row = df.iloc[i]
        mid = str(row[0]).strip()
        if mid not in ("A", "B", "C"):
            continue
        try:
            count = int(row[1])
            full_charge_time = float(row[2])
        except (ValueError, TypeError):
            continue
        for j in range(1, count + 1):
            batteries.append(Battery(
                battery_id=f"BAT-{mid}-{j:02d}",
                model_id=mid,
                full_charge_time=full_charge_time
            ))
    return drones, batteries


# ============================================================
# 3. 坐标投影与 DEM 预计算
# ============================================================
def project_nodes(o01: Node, services: List[Node]):
    lon0, lat0 = o01.lon, o01.lat
    for n in [o01] + services:
        n.x = R_EARTH * math.radians(n.lon - lon0) * math.cos(math.radians(lat0))
        n.y = R_EARTH * math.radians(n.lat - lat0)


def sample_dem_max(dem_path, lon1, lat1, lon2, lat2, samples=50):
    if not HAS_RASTERIO:
        return None
    with rasterio.open(dem_path) as src:
        r1, c1 = src.index(lon1, lat1)
        r2, c2 = src.index(lon2, lat2)
        max_alt = -9999
        for t in np.linspace(0, 1, samples):
            r = int(round(r1 + (r2 - r1) * t))
            c = int(round(c1 + (c2 - c1) * t))
            if 0 <= r < src.height and 0 <= c < src.width:
                val = src.read(1, window=((r, r + 1), (c, c + 1)))[0, 0]
                if val > max_alt:
                    max_alt = val
        return max_alt if max_alt > -9999 else None


def precompute_segments(o01, services, models, dem_path):
    nodes = [o01] + services
    seg = {}
    for i in nodes:
        for j in nodes:
            if i.id == j.id:
                continue
            dist = math.hypot(i.x - j.x, i.y - j.y)
            max_alt = sample_dem_max(dem_path, i.lon, i.lat, j.lon, j.lat)
            if max_alt is None:
                max_alt = max(i.alt, j.alt)
            cruise_alt = max_alt + 50.0
            work_i = i.alt if i.id == o01.id else i.alt + 30.0
            work_j = j.alt if j.id == o01.id else j.alt + 30.0
            h_up = max(0.0, cruise_alt - work_i)
            h_down = max(0.0, cruise_alt - work_j)
            time_dict = {}
            for mid, m in models.items():
                t = h_up / m.climb_speed + dist / m.cruise_speed + h_down / m.descent_speed
                time_dict[mid] = t
            seg[(i.id, j.id)] = {
                'dist': dist, 'max_alt': max_alt, 'cruise_alt': cruise_alt,
                'h_up': h_up, 'h_down': h_down, 'time': time_dict
            }
    return seg


# ============================================================
# 4. 单架次评价器
# ============================================================
class Evaluator:
    def __init__(self, models, seg):
        self.models = models
        self.seg = seg

    def _energy_segment(self, model, q, dist, h_up):
        Q = model.max_payload
        if Q <= 0:
            L = model.range_empty
        else:
            ratio = min(q / Q, 1.0)
            L = model.range_empty - (model.range_empty - model.range_full) * (ratio ** 1.5)
        L = max(L, 1.0)
        E_hor = model.battery_energy * dist / L
        total_mass = model.empty_mass + q
        E_up = (total_mass * 9.8 * h_up) / (3.6e6 * model.climb_eff)
        return E_hor + E_up

    def evaluate_sortie(self, sortie: Sortie) -> Sortie:
        m = self.models[sortie.model_id]
        boxes_by_service = {}
        for b in sortie.boxes:
            boxes_by_service.setdefault(b.service_id, []).append(b)
        sortie.boxes_by_service = boxes_by_service
        sortie.total_mass = sum(b.mass for b in sortie.boxes)
        sortie.total_volume = sum(b.volume for b in sortie.boxes)
        if sortie.total_mass > m.max_payload + 1e-6:
            sortie.feasible = False
            sortie.reason = f"超质量 {sortie.total_mass:.1f}>{m.max_payload}"
            return sortie
        if sortie.total_volume > m.max_volume + 1e-6:
            sortie.feasible = False
            sortie.reason = f"超体积 {sortie.total_volume:.3f}>{m.max_volume}"
            return sortie

        path = ["O01"] + sortie.service_seq + ["O01"]
        current_payload = sortie.total_mass
        current_time = 0.0
        total_energy = 0.0
        sortie.segment_payload, sortie.segment_time, sortie.segment_energy = [], [], []
        delivery_time = {}

        current_time += m.prepare_time + len(sortie.boxes) * m.load_time_per_box

        for idx in range(len(path) - 1):
            i, j = path[idx], path[idx + 1]
            s = self.seg[(i, j)]
            fly_time = s['time'][sortie.model_id]
            energy = self._energy_segment(m, current_payload, s['dist'], s['h_up'])
            total_energy += energy
            sortie.segment_payload.append(current_payload)
            sortie.segment_time.append(fly_time)
            sortie.segment_energy.append(energy)
            current_time += fly_time
            if j != "O01":
                service_boxes = boxes_by_service.get(j, [])
                n_box = len(service_boxes)
                arrive = current_time
                for k, b in enumerate(service_boxes, start=1):
                    delivery_time[b.box_id] = arrive + m.service_base_time + k * m.service_time_per_box
                current_time += m.service_base_time + n_box * m.service_time_per_box
                current_payload -= sum(b.mass for b in service_boxes)
                current_payload = max(0.0, current_payload)

        sortie.total_time = current_time
        sortie.total_energy = total_energy
        sortie.return_soc = 1.0 - total_energy / m.battery_energy
        sortie.delivery_time = delivery_time

        if sortie.return_soc < m.return_soc_min - 1e-6:
            sortie.feasible = False
            sortie.reason = f"返航SOC {sortie.return_soc:.3f}<{m.return_soc_min}"
            return sortie
        for b in sortie.boxes:
            t = delivery_time.get(b.box_id, float('inf'))
            if t > b.hard_deadline + 1e-6:
                sortie.feasible = False
                sortie.reason = f"货箱 {b.box_id} 超硬时限 {t:.0f}>{b.hard_deadline:.0f}"
                return sortie
        sortie.feasible = True
        sortie.reason = ""
        return sortie


# ============================================================
# 5. 服务区访问顺序优化
# ============================================================
def sequence_energy(seq, boxes, o01_id, evaluator, model_id):
    if not seq:
        return 0.0
    temp = Sortie(sortie_id="TMP", model_id=model_id, service_seq=list(seq), boxes=boxes)
    temp = evaluator.evaluate_sortie(temp)
    if not temp.feasible:
        return float('inf')
    return temp.total_energy


def best_service_sequence(boxes, o01_id, evaluator, model_id):
    services = list(dict.fromkeys(b.service_id for b in boxes))
    if len(services) <= 1:
        return services
    # 最近邻
    current = o01_id
    remaining = set(services)
    seq = []
    while remaining:
        nearest = min(remaining, key=lambda s: evaluator.seg[(current, s)]['dist'])
        seq.append(nearest)
        remaining.remove(nearest)
        current = nearest
    # 2-opt
    improved = True
    iters = 0
    while improved and iters < 30:
        improved = False
        iters += 1
        for i in range(len(seq) - 1):
            for j in range(i + 1, len(seq)):
                new_seq = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                if sequence_energy(new_seq, boxes, o01_id, evaluator, model_id) < \
                   sequence_energy(seq, boxes, o01_id, evaluator, model_id) - 1e-6:
                    seq = new_seq
                    improved = True
    return seq


# ============================================================
# 6. 资源调度 Decoder
# ============================================================
class Decoder:
    def __init__(self, drone_specs, battery_specs, models):
        self.drone_specs = drone_specs      # [(id, model_id, init_pos), ...]
        self.battery_specs = battery_specs  # [(id, model_id, full_charge_time), ...]
        self.models = models

    @staticmethod
    def _charge_time(battery, soc):
        T = battery.full_charge_time
        if soc < 0.9:
            return 0.65 * T * (0.9 - soc) / 0.9 + 0.35 * T
        return 0.35 * T * (1.0 - soc) / 0.1

    def schedule(self, sorties: List[Sortie]):
        """对已评价的架次集合进行资源调度"""
        drones = [Drone(drone_id=s[0], model_id=s[1], init_pos=s[2]) for s in self.drone_specs]
        batteries = [Battery(battery_id=s[0], model_id=s[1], full_charge_time=s[2]) for s in self.battery_specs]
        drones_by_model = {}
        for d in drones:
            drones_by_model.setdefault(d.model_id, []).append(d)
        batteries_by_model = {}
        for b in batteries:
            batteries_by_model.setdefault(b.model_id, []).append(b)

        def urgency(s):
            d = min((b.hard_deadline for b in s.boxes if b.hard_deadline < float('inf')), default=float('inf'))
            return d - s.total_time

        ordered = sorted(sorties, key=urgency)
        records = []
        for s in ordered:
            m = self.models[s.model_id]
            drone_list = drones_by_model.get(s.model_id, [])
            battery_list = batteries_by_model.get(s.model_id, [])
            if not drone_list or not battery_list:
                raise ValueError(f"机型 {s.model_id} 无可用无人机或电池")
            # 找最优组合：min(end_time)
            best = None
            for d in drone_list:
                for b in battery_list:
                    st = max(d.available_time, b.available_time)
                    et = st + s.total_time
                    if best is None or et < best[0]:
                        best = (et, st, d, b)
            et, st, d, b = best
            d.available_time = et
            soc_end = 1.0 - s.total_energy / m.battery_energy
            b.soc = soc_end
            b.available_time = et + self._charge_time(b, soc_end)
            records.append({
                "sortie_id": s.sortie_id,
                "model_id": s.model_id,
                "drone_id": d.drone_id,
                "battery_id": b.battery_id,
                "start_time": st,
                "end_time": et,
                "total_time": s.total_time,
                "total_energy": s.total_energy,
                "return_soc": s.return_soc,
                "service_seq": "->".join(s.service_seq),
                "boxes": [bx.box_id for bx in s.boxes]
            })

        cmax = max((r["end_time"] for r in records), default=0.0)
        total_energy = sum(r["total_energy"] for r in records)
        n_sorties = len(records)
        # 软迟到
        late_penalty = 0.0
        for s in sorties:
            for bx in s.boxes:
                t = s.delivery_time.get(bx.box_id, float('inf'))
                if bx.hard_deadline == float('inf') and t > bx.expected_time:
                    late_penalty += bx.priority * (t - bx.expected_time) / bx.expected_time
        # 硬约束违反（若 Decoder 无法满足，这里可以加罚）
        hard_violation = 0.0
        for r in records:
            s = next((x for x in sorties if x.sortie_id == r["sortie_id"]), None)
            if s is None:
                continue
            for bx in s.boxes:
                if bx.hard_deadline < float('inf'):
                    t = s.delivery_time.get(bx.box_id, float('inf'))
                    if t > bx.hard_deadline:
                        hard_violation += 1e4

        metrics = {
            "Cmax": cmax,
            "total_energy": total_energy,
            "n_sorties": n_sorties,
            "late_penalty": late_penalty,
            "hard_violation": hard_violation
        }
        return records, metrics


# ============================================================
# 7. 初始解：Regret-2 插入
# ============================================================
def try_add_box_to_sortie(sortie, box, evaluator, models, o01_id):
    """尝试把 box 加到 sortie 中；返回新 Sortie 或 None"""
    m = models[sortie.model_id]
    if sortie.total_mass + box.mass > m.max_payload + 1e-6:
        return None
    if sortie.total_volume + box.volume > m.max_volume + 1e-6:
        return None
    new_boxes = sortie.boxes + [box]
    new_seq = best_service_sequence(new_boxes, o01_id, evaluator, sortie.model_id)
    cand = Sortie(sortie_id=sortie.sortie_id, model_id=sortie.model_id,
                  service_seq=new_seq, boxes=new_boxes)
    cand = evaluator.evaluate_sortie(cand)
    if not cand.feasible:
        return None
    return cand


def make_new_sortie_for_box(box, models, evaluator, o01_id, sid):
    """为单个 box 尝试不同机型创建新 sortie"""
    for mid in ("C", "B", "A"):
        cand = Sortie(sortie_id=f"S{sid:03d}", model_id=mid,
                      service_seq=[box.service_id], boxes=[box])
        cand = evaluator.evaluate_sortie(cand)
        if cand.feasible:
            return cand
    return None


def insertion_cost(sortie):
    """对单个 sortie 的局部代价"""
    return sortie.total_energy + 0.001 * sortie.total_time


def build_initial_solution(boxes, o01, models, evaluator):
    """
    Regret-2 初始解：优先处理最紧急货箱，用 regret 选择插入对象。
    返回 (sorties, unassigned_boxes)
    """
    unassigned = list(boxes)
    # 按硬截止时间紧急度排序
    unassigned.sort(key=lambda b: (b.hard_deadline, -b.priority))
    sorties = []
    sid = 0
    MAX_ITER = 5000
    iters = 0
    while unassigned and iters < MAX_ITER:
        iters += 1
        # 计算每个未分配货箱的最优和第二优插入代价
        best_box = None
        best_regret = -1.0
        best_action = None
        for b in unassigned:
            cand_list = []
            # 尝试插入已有架次
            for idx, s in enumerate(sorties):
                cand = try_add_box_to_sortie(s, b, evaluator, models, o01.id)
                if cand is not None:
                    delta = insertion_cost(cand) - insertion_cost(s)
                    cand_list.append((delta, "insert", idx, cand))
            # 尝试新开架次
            new_s = make_new_sortie_for_box(b, models, evaluator, o01.id, sid + 1)
            if new_s is not None:
                delta = insertion_cost(new_s)
                cand_list.append((delta, "new", None, new_s))
            if not cand_list:
                continue
            cand_list.sort(key=lambda x: x[0])
            c1 = cand_list[0][0]
            c2 = cand_list[1][0] if len(cand_list) > 1 else c1 + 1e3
            regret = c2 - c1
            # 紧迫度加权
            urgency = 1.0 / max(1.0, b.hard_deadline) + b.priority / 100.0
            score = regret + 0.5 * urgency
            if score > best_regret:
                best_regret = score
                best_box = b
                best_action = cand_list[0]
        if best_box is None or best_action is None:
            break
        _, action_type, idx, cand = best_action
        if action_type == "insert":
            sorties[idx] = cand
        else:
            sid += 1
            cand.sortie_id = f"S{sid:03d}"
            sorties.append(cand)
        unassigned.remove(best_box)

    # 如果还有剩余，逐个强制构造
    while unassigned:
        b = unassigned.pop(0)
        new_s = make_new_sortie_for_box(b, models, evaluator, o01.id, sid + 1)
        if new_s is not None:
            sid += 1
            new_s.sortie_id = f"S{sid:03d}"
            sorties.append(new_s)
    return sorties, unassigned


# ============================================================
# 8. ALNS
# ============================================================
class ALNS:
    def __init__(self, boxes, o01, models, evaluator, decoder):
        self.boxes = boxes
        self.o01 = o01
        self.models = models
        self.evaluator = evaluator
        self.decoder = decoder
        # 基准值（用于归一化）
        self.ref = {"late": 1.0, "cmax": 1.0, "energy": 1.0, "n": 1.0}

    def evaluate(self, sorties):
        """返回 (score, metrics)"""
        feasible = []
        for s in sorties:
            self.evaluator.evaluate_sortie(s)
            if s.feasible:
                feasible.append(s)
        if len(feasible) < len(sorties):
            return float('inf'), None
        records, metrics = self.decoder.schedule(feasible)
        score = (W_LATE * metrics["late_penalty"] / self.ref["late"]
                 + W_CMAX * metrics["Cmax"] / self.ref["cmax"]
                 + W_ENERGY * metrics["total_energy"] / self.ref["energy"]
                 + W_SORTIES * metrics["n_sorties"] / self.ref["n"]
                 + metrics["hard_violation"])
        return score, metrics

    def set_reference(self, sorties):
        score, metrics = self.evaluate(sorties)
        if metrics is None:
            return
        self.ref["late"] = max(metrics["late_penalty"], 1e-6)
        self.ref["cmax"] = max(metrics["Cmax"], 1e-6)
        self.ref["energy"] = max(metrics["total_energy"], 1e-6)
        self.ref["n"] = max(metrics["n_sorties"], 1)

    # ---------- Destroy ----------
    def destroy_random(self, sorties, k):
        all_boxes = [(i, b) for i, s in enumerate(sorties) for b in s.boxes]
        if not all_boxes:
            return sorties, []
        random.shuffle(all_boxes)
        removed = all_boxes[:k]
        new_sorties = [copy.deepcopy(s) for s in sorties]
        for i, b in removed:
            new_sorties[i].boxes = [x for x in new_sorties[i].boxes if x.box_id != b.box_id]
        new_sorties = [s for s in new_sorties if s.boxes]
        for s in new_sorties:
            s.service_seq = best_service_sequence(s.boxes, self.o01.id, self.evaluator, s.model_id)
        return new_sorties, [b for _, b in removed]

    def destroy_worst(self, sorties, k):
        # 计算每个货箱的贡献度（能耗/时间/优先级）
        contrib = []
        for i, s in enumerate(sorties):
            for b in s.boxes:
                score = b.mass * 0.1 + b.priority * 0.5
                contrib.append((score, i, b))
        contrib.sort(key=lambda x: -x[0])
        removed = contrib[:k]
        new_sorties = [copy.deepcopy(s) for s in sorties]
        for _, i, b in removed:
            new_sorties[i].boxes = [x for x in new_sorties[i].boxes if x.box_id != b.box_id]
        new_sorties = [s for s in new_sorties if s.boxes]
        for s in new_sorties:
            s.service_seq = best_service_sequence(s.boxes, self.o01.id, self.evaluator, s.model_id)
        return new_sorties, [b for _, _, b in removed]

    def destroy_related(self, sorties, k):
        """按服务区地理相关性移除"""
        all_boxes = [(i, b) for i, s in enumerate(sorties) for b in s.boxes]
        if not all_boxes:
            return sorties, []
        seed = random.choice(all_boxes)
        _, seed_box = seed
        # 按到 seed 服务区的距离排序
        def dist_to_seed(b):
            return self.evaluator.seg[(seed_box.service_id, b.service_id)]['dist'] \
                if seed_box.service_id != b.service_id else 0.0
        all_boxes.sort(key=lambda x: dist_to_seed(x[1]))
        removed = all_boxes[:k]
        new_sorties = [copy.deepcopy(s) for s in sorties]
        for i, b in removed:
            new_sorties[i].boxes = [x for x in new_sorties[i].boxes if x.box_id != b.box_id]
        new_sorties = [s for s in new_sorties if s.boxes]
        for s in new_sorties:
            s.service_seq = best_service_sequence(s.boxes, self.o01.id, self.evaluator, s.model_id)
        return new_sorties, [b for _, b in removed]

    def destroy_sortie(self, sorties, k):
        """移除整个架次"""
        if len(sorties) <= 1:
            return sorties, []
        n_remove = max(1, k // 5)
        idxs = random.sample(range(len(sorties)), min(n_remove, len(sorties)))
        removed = []
        new_sorties = []
        for i, s in enumerate(sorties):
            if i in idxs:
                removed.extend(s.boxes)
            else:
                new_sorties.append(copy.deepcopy(s))
        return new_sorties, removed

    # ---------- Repair ----------
    def repair_greedy(self, sorties, removed_boxes):
        """逐个按最优插入位置放回"""
        removed_boxes = list(removed_boxes)
        # 按紧急度排序
        removed_boxes.sort(key=lambda b: (b.hard_deadline, -b.priority))
        current = [copy.deepcopy(s) for s in sorties]
        # 重新评估
        for s in current:
            self.evaluator.evaluate_sortie(s)
        sid = len(current) + 1
        for b in removed_boxes:
            best = None
            for idx, s in enumerate(current):
                cand = try_add_box_to_sortie(s, b, self.evaluator, self.models, self.o01.id)
                if cand is not None:
                    delta = insertion_cost(cand) - insertion_cost(s)
                    if best is None or delta < best[0]:
                        best = (delta, "insert", idx, cand)
            new_s = make_new_sortie_for_box(b, self.models, self.evaluator, self.o01.id, sid)
            if new_s is not None:
                delta = insertion_cost(new_s)
                if best is None or delta < best[0]:
                    best = (delta, "new", None, new_s)
            if best is None:
                continue
            _, action, idx, cand = best
            if action == "insert":
                current[idx] = cand
            else:
                sid += 1
                cand.sortie_id = f"S{sid:03d}"
                current.append(cand)
        return current

    def repair_regret2(self, sorties, removed_boxes):
        removed = list(removed_boxes)
        current = [copy.deepcopy(s) for s in sorties]
        for s in current:
            self.evaluator.evaluate_sortie(s)
        sid = len(current) + 1
        while removed:
            best_box = None
            best_score = -1.0
            best_action = None
            for b in removed:
                cands = []
                for idx, s in enumerate(current):
                    cand = try_add_box_to_sortie(s, b, self.evaluator, self.models, self.o01.id)
                    if cand is not None:
                        delta = insertion_cost(cand) - insertion_cost(s)
                        cands.append((delta, "insert", idx, cand))
                new_s = make_new_sortie_for_box(b, self.models, self.evaluator, self.o01.id, sid)
                if new_s is not None:
                    cands.append((insertion_cost(new_s), "new", None, new_s))
                if not cands:
                    continue
                cands.sort(key=lambda x: x[0])
                c1 = cands[0][0]
                c2 = cands[1][0] if len(cands) > 1 else c1 + 1e3
                score = (c2 - c1) + 0.3 * b.priority / 100.0
                if score > best_score:
                    best_score = score
                    best_box = b
                    best_action = cands[0]
            if best_box is None:
                removed = []
                break
            _, action, idx, cand = best_action
            if action == "insert":
                current[idx] = cand
            else:
                sid += 1
                cand.sortie_id = f"S{sid:03d}"
                current.append(cand)
            removed.remove(best_box)
        return current

    # ---------- 主循环 ----------
    def run(self, initial_sorties):
        self.set_reference(initial_sorties)
        current = [copy.deepcopy(s) for s in initial_sorties]
        for s in current:
            self.evaluator.evaluate_sortie(s)
        current_score, current_metrics = self.evaluate(current)
        best = copy.deepcopy(current)
        best_score = current_score
        best_metrics = current_metrics
        history = [{"iter": 0, "score": current_score,
                    "cmax": current_metrics["Cmax"],
                    "energy": current_metrics["total_energy"],
                    "n_sorties": current_metrics["n_sorties"],
                    "late": current_metrics["late_penalty"]}]

        T = ALNS_T0
        t_start = time_module.time()
        n_total_boxes = sum(len(s.boxes) for s in current)
        for it in range(1, ALNS_MAX_ITER + 1):
            if time_module.time() - t_start > ALNS_TIME_LIMIT:
                break
            k = random.randint(max(2, n_total_boxes // 20), max(4, n_total_boxes // 5))
            # 随机选 destroy
            d_op = random.choice(["random", "worst", "related", "sortie"])
            if d_op == "random":
                partial, removed = self.destroy_random(current, k)
            elif d_op == "worst":
                partial, removed = self.destroy_worst(current, k)
            elif d_op == "related":
                partial, removed = self.destroy_related(current, k)
            else:
                partial, removed = self.destroy_sortie(current, k)
            if not removed:
                continue
            # 随机选 repair
            r_op = random.choice(["greedy", "regret2"])
            if r_op == "greedy":
                new_sol = self.repair_greedy(partial, removed)
            else:
                new_sol = self.repair_regret2(partial, removed)
            new_score, new_metrics = self.evaluate(new_sol)
            if new_metrics is None:
                continue
            # SA 接受
            if new_score < current_score or random.random() < math.exp(-(new_score - current_score) / max(T, 1e-6)):
                current = new_sol
                current_score = new_score
                current_metrics = new_metrics
                if new_score < best_score:
                    best = copy.deepcopy(new_sol)
                    best_score = new_score
                    best_metrics = new_metrics
            T *= ALNS_ALPHA
            if it % 20 == 0 or it == 1:
                history.append({"iter": it, "score": best_score,
                                "cmax": best_metrics["Cmax"],
                                "energy": best_metrics["total_energy"],
                                "n_sorties": best_metrics["n_sorties"],
                                "late": best_metrics["late_penalty"]})
                print(f"[ALNS] iter {it}: best_score={best_score:.4f}, "
                      f"Cmax={best_metrics['Cmax']:.0f}s, "
                      f"E={best_metrics['total_energy']:.2f}kWh, "
                      f"N={best_metrics['n_sorties']}, "
                      f"late={best_metrics['late_penalty']:.3f}")
        # 最终评估
        for s in best:
            self.evaluator.evaluate_sortie(s)
        return best, best_metrics, pd.DataFrame(history)


# ============================================================
# 9. 输出模板格式
# ============================================================
def export_q2_format(sorties, records, boxes, o01, output_dir):
    """按结果提交模板 xlsx 的 Q2 格式输出"""
    # Q2_运输架次
    rec_map = {r["sortie_id"]: r for r in records}
    rows_transport = []
    for s in sorties:
        r = rec_map.get(s.sortie_id)
        if r is None:
            continue
        rows_transport.append({
            "架次编号": s.sortie_id,
            "无人机编号": r["drone_id"],
            "机型编号": s.model_id,
            "电池编号": r["battery_id"],
            "开始时刻（s）": round(r["start_time"], 2),
            "访问服务区顺序": "->".join(["O01"] + s.service_seq + ["O01"]),
            "返回O01时刻（s）": round(r["end_time"], 2),
            "架次能耗（kWh）": round(s.total_energy, 4),
        })
    df_transport = pd.DataFrame(rows_transport)
    df_transport = df_transport.sort_values("开始时刻（s）").reset_index(drop=True)

    # Q2_逐箱交付
    rows_delivery = []
    for s in sorties:
        for b in s.boxes:
            rows_delivery.append({
                "货箱编号": b.box_id,
                "架次编号": s.sortie_id,
                "服务区编号": b.service_id,
                "交付完成时刻（s）": round(s.delivery_time.get(b.box_id, 0.0), 2),
            })
    df_delivery = pd.DataFrame(rows_delivery)
    df_delivery = df_delivery.sort_values("交付完成时刻（s）").reset_index(drop=True)

    # 写到 xlsx，保留两个 sheet
    out_path = os.path.join(output_dir, "Q2_结果.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df_transport.to_excel(writer, sheet_name="Q2_运输架次", index=False)
        df_delivery.to_excel(writer, sheet_name="Q2_逐箱交付", index=False)

    # 同时输出 CSV 便于查看
    df_transport.to_csv(os.path.join(output_dir, "Q2_运输架次.csv"), index=False, encoding="utf-8-sig")
    df_delivery.to_csv(os.path.join(output_dir, "Q2_逐箱交付.csv"), index=False, encoding="utf-8-sig")
    return df_transport, df_delivery


# ============================================================
# 10. 图表
# ============================================================
def plot_routes(sorties, o01, output_dir):
    fig, ax = plt.subplots(figsize=(10, 8))
    # 节点
    ax.scatter(o01.x, o01.y, marker="*", s=300, c="red", zorder=5, label="调度中心 O01")
    # 服务区
    service_ids = set()
    for s in sorties:
        for sid in s.service_seq:
            service_ids.add(sid)
    # 画路线
    cmap = plt.cm.tab20
    for i, s in enumerate(sorties):
        xs = [o01.x]
        ys = [o01.y]
        node_map = {n.id: n for n in [o01] + []}
        # 手工从 sortie 的 service_seq 画：需要服务区坐标
        # 这里我们从一个全局字典取
        # 由外部传入
    plt.close()


def plot_routes_with_nodes(sorties, o01, services, output_dir):
    node_map = {n.id: n for n in [o01] + services}
    fig, ax = plt.subplots(figsize=(11, 9))
    # 全部服务区
    for n in services:
        ax.scatter(n.x, n.y, s=40, c="lightgray", zorder=3)
        ax.annotate(n.id, (n.x, n.y), fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.scatter(o01.x, o01.y, marker="*", s=300, c="red", zorder=5, label="调度中心 O01")
    # 逐架次画路线
    cmap = plt.cm.tab20
    for i, s in enumerate(sorties):
        seq = [o01.id] + s.service_seq + [o01.id]
        xs = [node_map[nid].x for nid in seq]
        ys = [node_map[nid].y for nid in seq]
        color = cmap(i % 20)
        ax.plot(xs, ys, "-o", color=color, lw=1.5, markersize=4,
                label=f"{s.sortie_id}({s.model_id})")
    ax.set_xlabel("局部平面坐标 X (m)")
    ax.set_ylabel("局部平面坐标 Y (m)")
    ax.set_title("图 1  问题二多点多架次运输路线图")
    ax.legend(loc="best", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out = os.path.join(output_dir, "fig1_routes.png")
    plt.savefig(out, dpi=150)
    plt.close()
    return out


def plot_gantt(records, output_dir):
    fig, ax = plt.subplots(figsize=(12, 6))
    drones = sorted(set(r["drone_id"] for r in records))
    y_map = {d: i for i, d in enumerate(drones)}
    cmap = plt.cm.tab10
    for r in records:
        y = y_map[r["drone_id"]]
        ax.barh(y, r["total_time"], left=r["start_time"],
                color=cmap(hash(r["sortie_id"]) % 10), edgecolor="black", alpha=0.75)
        ax.text(r["start_time"] + r["total_time"] / 2, y,
                r["sortie_id"], ha="center", va="center", fontsize=6, color="white")
    ax.set_yticks(range(len(drones)))
    ax.set_yticklabels(drones)
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel("运输无人机编号")
    ax.set_title("图 2  运输无人机任务甘特图")
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()
    out = os.path.join(output_dir, "fig2_gantt.png")
    plt.savefig(out, dpi=150)
    plt.close()
    return out


def plot_delivery_vs_deadline(sorties, output_dir):
    rows = []
    for s in sorties:
        for b in s.boxes:
            t = s.delivery_time.get(b.box_id)
            if t is None:
                continue
            rows.append({
                "box_id": b.box_id,
                "service": b.service_id,
                "material": b.material,
                "delivery": t,
                "expected": b.expected_time,
                "hard": b.hard_deadline if b.hard_deadline < float('inf') else None,
                "priority": b.priority,
            })
    df = pd.DataFrame(rows).sort_values("expected").reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(12, 5))
    x = range(len(df))
    ax.scatter(x, df["expected"], c="blue", s=15, label="期望送达时间")
    hard_mask = df["hard"].notna()
    if hard_mask.any():
        ax.scatter(df.index[hard_mask], df.loc[hard_mask, "hard"], c="red", s=15,
                   marker="s", label="硬截止时间")
    ax.scatter(x, df["delivery"], c="green", s=20, marker="^", label="实际送达时间")
    ax.set_xlabel("货箱（按期望时间排序）")
    ax.set_ylabel("时间 (s)")
    ax.set_title("图 3  逐箱送达时间 vs 期望/硬截止时间")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out = os.path.join(output_dir, "fig3_delivery.png")
    plt.savefig(out, dpi=150)
    plt.close()
    return out


def plot_battery_schedule(records, batteries_spec, output_dir):
    """画出每块电池的任务/充电时间线"""
    # 从 records 中重建每块电池的任务段
    fig, ax = plt.subplots(figsize=(12, 6))
    battery_ids = sorted(set(r["battery_id"] for r in records))
    y_map = {b: i for i, b in enumerate(battery_ids)}
    for r in records:
        y = y_map[r["battery_id"]]
        ax.barh(y, r["total_time"], left=r["start_time"],
                color="steelblue", alpha=0.8, edgecolor="black")
        # 充电段
        chg_start = r["end_time"]
        # 需要用 full_charge_time 推算；粗略显示为固定长度
        ax.barh(y, r["total_time"] * 0.3, left=chg_start,
                color="orange", alpha=0.6, edgecolor="black", hatch="//")
    ax.set_yticks(range(len(battery_ids)))
    ax.set_yticklabels(battery_ids)
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel("共享电池编号")
    ax.set_title("图 4  共享电池任务与充电时间线（蓝=任务，橙=充电）")
    legend_elems = [Patch(facecolor="steelblue", label="任务占用"),
                    Patch(facecolor="orange", hatch="//", label="充电")]
    ax.legend(handles=legend_elems, loc="best")
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()
    out = os.path.join(output_dir, "fig4_battery.png")
    plt.savefig(out, dpi=150)
    plt.close()
    return out


def plot_alns_history(history_df, output_dir):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    axes[0, 0].plot(history_df["iter"], history_df["score"], "b-")
    axes[0, 0].set_title("ALNS 目标函数收敛")
    axes[0, 0].set_xlabel("迭代")
    axes[0, 0].set_ylabel("加权目标值")

    axes[0, 1].plot(history_df["iter"], history_df["cmax"], "r-")
    axes[0, 1].set_title("全部任务完成时间 Cmax")
    axes[0, 1].set_xlabel("迭代")
    axes[0, 1].set_ylabel("Cmax (s)")

    axes[1, 0].plot(history_df["iter"], history_df["energy"], "g-")
    axes[1, 0].set_title("总运输能耗")
    axes[1, 0].set_xlabel("迭代")
    axes[1, 0].set_ylabel("能耗 (kWh)")

    axes[1, 1].plot(history_df["iter"], history_df["n_sorties"], "m-")
    axes[1, 1].set_title("架次数")
    axes[1, 1].set_xlabel("迭代")
    axes[1, 1].set_ylabel("架次数")

    for ax in axes.ravel():
        ax.grid(True, alpha=0.3)
    plt.suptitle("图 5  ALNS 各目标演化", fontsize=13)
    plt.tight_layout()
    out = os.path.join(output_dir, "fig5_alns.png")
    plt.savefig(out, dpi=150)
    plt.close()
    return out


# ============================================================
# 11. 主程序
# ============================================================
def main():
    print("=" * 60)
    print("问题二：异构无人机多点多架次运输调度（完整版）")
    print("=" * 60)

    node_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "调度中心与服务区.xlsx")
    box_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "物资需求与配送时限.xlsx")
    drone_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "运输无人机数据.xlsx")

    print("读取数据...")
    o01, services = load_nodes(node_file)
    boxes = load_boxes(box_file)
    models = load_drone_models(drone_file)
    drones, batteries = load_drones_and_batteries(drone_file)
    print(f"  服务区: {len(services)}, 货箱: {len(boxes)}, "
          f"机型: {list(models.keys())}, 无人机: {len(drones)}, 电池: {len(batteries)}")

    project_nodes(o01, services)
    print("预计算航段...")
    seg = precompute_segments(o01, services, models, DEM_PATH)
    evaluator = Evaluator(models, seg)

    # 初始解
    print("构造 Regret-2 初始解...")
    t0 = time_module.time()
    init_sorties, _ = build_initial_solution(boxes, o01, models, evaluator)
    print(f"  初始架次数: {len(init_sorties)}  用时 {time_module.time()-t0:.1f}s")
    feasible_init = [s for s in init_sorties if s.feasible]
    print(f"  可行架次数: {len(feasible_init)}")

    # Decoder
    drone_specs = [(d.drone_id, d.model_id, d.init_pos) for d in drones]
    battery_specs = [(b.battery_id, b.model_id, b.full_charge_time) for b in batteries]
    decoder = Decoder(drone_specs, battery_specs, models)

    init_records, init_metrics = decoder.schedule(feasible_init)
    print("初始解指标:")
    for k, v in init_metrics.items():
        print(f"  {k}: {v}")

    # ALNS
    print("\n启动 ALNS 优化...")
    alns = ALNS(boxes, o01, models, evaluator, decoder)
    alns.set_reference(init_sorties)
    best_sorties, best_metrics, history_df = alns.run(init_sorties)
    print("ALNS 最优指标:")
    for k, v in best_metrics.items():
        print(f"  {k}: {v}")

    # 最终调度
    print("\n最终资源调度...")
    final_records, final_metrics = decoder.schedule(best_sorties)
    print("最终指标:")
    for k, v in final_metrics.items():
        print(f"  {k}: {v}")

    # 输出模板格式
    print("\n输出 Q2 模板格式...")
    df_transport, df_delivery = export_q2_format(best_sorties, final_records, boxes, o01, OUTPUT_DIR)
    print(f"  {os.path.join(OUTPUT_DIR, 'Q2_结果.xlsx')}")
    print(f"  运输架次数: {len(df_transport)}  逐箱交付记录: {len(df_delivery)}")

    # 图表
    print("\n绘制图表...")
    p1 = plot_routes_with_nodes(best_sorties, o01, services, OUTPUT_DIR)
    p2 = plot_gantt(final_records, OUTPUT_DIR)
    p3 = plot_delivery_vs_deadline(best_sorties, OUTPUT_DIR)
    p4 = plot_battery_schedule(final_records, battery_specs, OUTPUT_DIR)
    p5 = plot_alns_history(history_df, OUTPUT_DIR)
    for p in [p1, p2, p3, p4, p5]:
        print(f"  {p}")

    print("\n完成。")


if __name__ == "__main__":
    main()