# -*- coding: utf-8 -*-
"""
问题二：异构无人机多点多架次运输调度
第一阶段可运行版本：
- 读取多表 Excel
- 节点/货箱/机型/无人机/电池解析
- DEM 航段预计算
- 单架次评价器
- 资源调度解码器
- 简单初始解构造
- 输出 CSV
ALNS 后续扩展。
"""

import os
import math
import copy
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False
    print("警告: 未安装 rasterio，DEM 将使用节点海拔近似。")

# ============================================================
# 0. 全局配置
# ============================================================
DATA_DIR = r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP\数据"                     # 你的数据目录
DEM_PATH = os.path.join(DATA_DIR, "镇龙乡地理空间数据", "镇龙乡30米DEM.tif")
OUTPUT_DIR = "output\\2"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 地球半径
R_EARTH = 6371000.0

# ============================================================
# 1. 数据结构
# ============================================================
@dataclass
class Node:
    id: str
    name: str
    lon: float
    lat: float
    alt: float          # 地面海拔 m
    population: int = 0
    x: float = 0.0      # 局部平面坐标
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
    battery_energy: float      # kWh
    return_soc_min: float      # 0.2
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
    full_charge_time: float = 3000.0   # 新增：等效完全充电时间（s）

@dataclass
class Sortie:
    sortie_id: str
    model_id: str
    service_seq: List[str]                 # 访问服务区顺序
    boxes: List[Box]                       # 该架次所有货箱
    boxes_by_service: Dict[str, List[Box]] = field(default_factory=dict)
    total_mass: float = 0.0
    total_volume: float = 0.0
    feasible: bool = True
    reason: str = ""
    # 评价结果
    total_time: float = 0.0
    total_energy: float = 0.0
    return_soc: float = 1.0
    segment_payload: List[float] = field(default_factory=list)
    segment_time: List[float] = field(default_factory=list)
    segment_energy: List[float] = field(default_factory=list)
    delivery_time: Dict[str, float] = field(default_factory=dict)  # box_id -> 送达时刻

# ============================================================
# 2. Excel 读取工具
# ============================================================
def _find_row(df: pd.DataFrame, keyword: str) -> int:
    """在 DataFrame 第0列中查找包含关键词的行索引"""
    mask = df[0].astype(str).str.contains(keyword, na=False)
    idx = df.index[mask]
    if len(idx) == 0:
        raise ValueError(f"未找到关键词: {keyword}")
    return idx[0]

def load_nodes(file_path: str):
    """读取 调度中心与服务区.xlsx"""
    df = pd.read_excel(file_path, header=None)
    # 调度中心
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
    # 服务区
    r = _find_row(df, "服务区编号")
    service_rows = df.iloc[r + 1:].dropna(subset=[0])
    services = []
    for _, row in service_rows.iterrows():
        sid = str(row[0]).strip()
        if sid == "" or sid.lower() == "nan":
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
    """读取 物资需求与配送时限.xlsx 的 逐箱货箱清单 sheet"""
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
        # 硬截止时间
        hard_deadline = float('inf')
        if material == "医疗物资":
            hard_deadline = min(hard_deadline, expected_time)
        if is_first and first_deadline is not None:
            hard_deadline = min(hard_deadline, first_deadline)
        boxes.append(Box(
            box_id=box_id,
            service_id=service_id,
            material=material,
            mass=mass,
            volume=volume,
            is_first_batch=is_first,
            first_deadline=first_deadline,
            expected_time=expected_time,
            priority=priority,
            hard_deadline=hard_deadline
        ))
    return boxes

def load_drone_models(file_path: str):
    """读取 运输无人机数据.xlsx 中的 三类机型参数
    遇到非 A/B/C 行立即停止，避免误读后续标题行。"""
    df = pd.read_excel(file_path, header=None)
    r = _find_row(df, "机型编号")
    models = {}
    for i in range(r + 1, len(df)):
        row = df.iloc[i]
        mid = str(row[0]).strip()
        # 只解析 A / B / C 三种机型
        if mid not in ("A", "B", "C"):
            break
        try:
            models[mid] = DroneModel(
                model_id=mid,
                name=str(row[1]).strip(),
                empty_mass=float(row[2]),
                max_payload=float(row[3]),
                max_volume=float(row[4]),
                cruise_speed=float(row[5]),
                range_empty=float(row[6]),
                range_full=float(row[7]),
                battery_energy=float(row[8]),
                return_soc_min=float(row[9]) / 100.0,   # 20% -> 0.2
                prepare_time=float(row[10]),
                load_time_per_box=float(row[11]),
                service_base_time=float(row[12]),
                service_time_per_box=float(row[13]),
                climb_speed=float(row[14]),
                descent_speed=float(row[15]),
                climb_eff=float(row[16]),
                descent_eff=float(row[17]),
            )
        except (ValueError, TypeError, IndexError):
            # 一旦列数据不是期望的数值，也直接停止
            break
    return models

def load_drones_and_batteries(file_path: str):
    """读取 运输无人机数据.xlsx 中的 逐架无人机清单 和 共享电池库存。
    注意：这两块表格在同一 sheet 里上下排列，必须区分边界。"""
    df = pd.read_excel(file_path, header=None)

    drones = []
    batteries = []

    # ---------- 无人机清单 ----------
    r = _find_row(df, "无人机编号")
    for i in range(r + 1, len(df)):
        row = df.iloc[i]
        did = str(row[0]).strip()
        # U01, U02, ... 才继续；遇到"共享电池库存"等标题就停
        if not did.startswith("U"):
            break
        drones.append(Drone(
            drone_id=did,
            model_id=str(row[1]).strip(),
            init_pos=str(row[2]).strip()
        ))

    # ---------- 共享电池库存 ----------
    r = _find_row(df, "共享电池库存")

    # 在该标题下面几行内找"机型编号"这个表头行
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
        # 只识别 A / B / C 行
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
                available_time=0.0,
                soc=1.0,
                full_charge_time=full_charge_time
            ))

    return drones, batteries

# ============================================================
# 3. 坐标投影与 DEM 航段预计算
# ============================================================
def project_nodes(o01: Node, services: List[Node]):
    """以 O01 为原点做等距圆柱投影，就地修改 Node.x, Node.y"""
    lon0, lat0 = o01.lon, o01.lat
    for n in [o01] + services:
        n.x = R_EARTH * math.radians(n.lon - lon0) * math.cos(math.radians(lat0))
        n.y = R_EARTH * math.radians(n.lat - lat0)

def sample_dem_max(dem_path: str, lon1: float, lat1: float, lon2: float, lat2: float, samples: int = 50):
    """沿经纬度直线采样 DEM，返回最大地面高程。若 rasterio 不可用，返回两端点海拔较大者。"""
    if not HAS_RASTERIO:
        return None
    with rasterio.open(dem_path) as src:
        # 将经纬度转为栅格行列
        def to_rowcol(lon, lat):
            row, col = src.index(lon, lat)
            return row, col
        r1, c1 = to_rowcol(lon1, lat1)
        r2, c2 = to_rowcol(lon2, lat2)
        max_alt = -9999
        for t in np.linspace(0, 1, samples):
            r = int(round(r1 + (r2 - r1) * t))
            c = int(round(c1 + (c2 - c1) * t))
            if 0 <= r < src.height and 0 <= c < src.width:
                val = src.read(1, window=((r, r+1), (c, c+1)))[0, 0]
                if val > max_alt:
                    max_alt = val
        return max_alt if max_alt > -9999 else None

def precompute_segments(o01: Node, services: List[Node], models: Dict[str, DroneModel], dem_path: str):
    """
    预计算所有节点对 i->j 的航段参数。
    返回 seg[(i,j)] = {
        'dist': 水平距离 m,
        'max_alt': 沿线最大地面高程,
        'cruise_alt': 巡航海拔,
        'h_up': 爬升高度,
        'h_down': 下降高度,
        'time': {model_id: 飞行时间}
    }
    """
    nodes = [o01] + services
    node_dict = {n.id: n for n in nodes}
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
            # 作业高度
            work_i = i.alt if i.id == o01.id else i.alt + 30.0
            work_j = j.alt if j.id == o01.id else j.alt + 30.0
            h_up = max(0.0, cruise_alt - work_i)
            h_down = max(0.0, cruise_alt - work_j)
            time_dict = {}
            for mid, m in models.items():
                t = h_up / m.climb_speed + dist / m.cruise_speed + h_down / m.descent_speed
                time_dict[mid] = t
            seg[(i.id, j.id)] = {
                'dist': dist,
                'max_alt': max_alt,
                'cruise_alt': cruise_alt,
                'h_up': h_up,
                'h_down': h_down,
                'time': time_dict
            }
    return seg, node_dict

# ============================================================
# 4. 单架次评价器
# ============================================================
class Evaluator:
    def __init__(self, models: Dict[str, DroneModel], seg: dict, node_dict: Dict[str, Node]):
        self.models = models
        self.seg = seg
        self.node_dict = node_dict

    def _energy_segment(self, model: DroneModel, q: float, dist: float, h_up: float) -> float:
        """计算一个航段能耗 kWh。q 为当前载荷 kg。"""
        # 等效航程
        Q = model.max_payload
        if Q <= 0:
            L = model.range_empty
        else:
            ratio = min(q / Q, 1.0)
            L = model.range_empty - (model.range_empty - model.range_full) * (ratio ** 1.5)
        L = max(L, 1.0)
        # 水平能耗
        E_hor = model.battery_energy * dist / L
        # 爬升附加能耗：势能 / 效率，转换为 kWh
        total_mass = model.empty_mass + q
        E_up = (total_mass * 9.8 * h_up) / (3.6e6 * model.climb_eff)
        return E_hor + E_up

    def evaluate_sortie(self, sortie: Sortie) -> Sortie:
        """对 Sortie 进行完整评价，就地填充结果。"""
        m = self.models[sortie.model_id]
        # 按服务区整理货箱
        boxes_by_service = {}
        for b in sortie.boxes:
            boxes_by_service.setdefault(b.service_id, []).append(b)
        sortie.boxes_by_service = boxes_by_service
        sortie.total_mass = sum(b.mass for b in sortie.boxes)
        sortie.total_volume = sum(b.volume for b in sortie.boxes)
        # 容量检查
        if sortie.total_mass > m.max_payload:
            sortie.feasible = False
            sortie.reason = f"超质量: {sortie.total_mass:.1f} > {m.max_payload}"
            return sortie
        if sortie.total_volume > m.max_volume:
            sortie.feasible = False
            sortie.reason = f"超体积: {sortie.total_volume:.3f} > {m.max_volume}"
            return sortie

        # 路径节点序列：O01 -> s1 -> s2 -> ... -> O01
        path = ["O01"] + sortie.service_seq + ["O01"]
        current_payload = sortie.total_mass
        current_time = 0.0
        total_energy = 0.0
        segment_payload = []
        segment_time = []
        segment_energy = []
        delivery_time = {}

        # 准备与装载
        current_time += m.prepare_time + len(sortie.boxes) * m.load_time_per_box

        for idx in range(len(path) - 1):
            i = path[idx]
            j = path[idx + 1]
            seg = self.seg[(i, j)]
            # 飞行时间
            fly_time = seg['time'][sortie.model_id]
            # 能耗
            energy = self._energy_segment(m, current_payload, seg['dist'], seg['h_up'])
            total_energy += energy
            segment_payload.append(current_payload)
            segment_time.append(fly_time)
            segment_energy.append(energy)
            current_time += fly_time

            # 如果到达服务区，进行交接
            if j != "O01":
                service_boxes = boxes_by_service.get(j, [])
                n_box = len(service_boxes)
                service_time = m.service_base_time + n_box * m.service_time_per_box
                # 逐箱送达时刻：到达时间 + 基础交接 + 第k箱增加交接
                arrive_time = current_time
                for k, b in enumerate(service_boxes, start=1):
                    delivery_time[b.box_id] = arrive_time + m.service_base_time + k * m.service_time_per_box
                current_time += service_time
                # 投送后卸下该服务区货箱
                current_payload -= sum(b.mass for b in service_boxes)
                current_payload = max(0.0, current_payload)

        # 返航后剩余 SOC
        return_soc = 1.0 - total_energy / m.battery_energy
        sortie.total_time = current_time
        sortie.total_energy = total_energy
        sortie.return_soc = return_soc
        sortie.segment_payload = segment_payload
        sortie.segment_time = segment_time
        sortie.segment_energy = segment_energy
        sortie.delivery_time = delivery_time

        if return_soc < m.return_soc_min:
            sortie.feasible = False
            sortie.reason = f"返航 SOC {return_soc:.3f} < {m.return_soc_min}"
        # 硬时限检查
        for b in sortie.boxes:
            t = delivery_time.get(b.box_id, float('inf'))
            if t > b.hard_deadline:
                sortie.feasible = False
                sortie.reason = f"货箱 {b.box_id} 超硬时限: {t:.1f} > {b.hard_deadline}"
                break
        return sortie

# ============================================================
# 5. 资源调度解码器
# ============================================================
class Decoder:
    def __init__(self, drones: List[Drone], batteries: List[Battery], models: Dict[str, DroneModel]):
        self.drones = drones
        self.batteries = batteries
        self.models = models
        # 按机型分组
        self.drones_by_model = {}
        for d in drones:
            self.drones_by_model.setdefault(d.model_id, []).append(d)
        self.batteries_by_model = {}
        for b in batteries:
            self.batteries_by_model.setdefault(b.model_id, []).append(b)

    def _charge_time(self, battery: Battery, soc: float) -> float:
        """两阶段等效充电模型，使用电池自身的等效完全充电时间。"""
        T_full = battery.full_charge_time
        if soc < 0.9:
            return 0.65 * T_full * (0.9 - soc) / 0.9 + 0.35 * T_full
        else:
            return 0.35 * T_full * (1.0 - soc) / 0.1

    def schedule(self, sorties: List[Sortie]) -> Tuple[List[dict], Dict[str, float]]:
        """
        输入已评价的架次列表，返回调度记录和指标。
        调度记录包含每个架次的无人机、电池、开始/结束时刻。
        """
        # 按硬时限紧急程度排序
        def urgency(s):
            min_deadline = min((b.hard_deadline for b in s.boxes), default=float('inf'))
            return min_deadline - s.total_time
        sorted_sorties = sorted(sorties, key=urgency)

        records = []
        for s in sorted_sorties:
            m = self.models[s.model_id]
            # 找同机型最早可用无人机
            drones = self.drones_by_model.get(s.model_id, [])
            if not drones:
                raise ValueError(f"没有可用机型 {s.model_id} 的无人机")
            drone = min(drones, key=lambda d: d.available_time)
            # 找同机型最早可用电池
            batteries = self.batteries_by_model.get(s.model_id, [])
            if not batteries:
                raise ValueError(f"没有可用机型 {s.model_id} 的电池")
            battery = min(batteries, key=lambda b: b.available_time)

            start_time = max(drone.available_time, battery.available_time)
            end_time = start_time + s.total_time

            # 更新无人机
            drone.available_time = end_time
            # 更新电池
            soc_end = 1.0 - s.total_energy / m.battery_energy
            battery.soc = soc_end
            charge_t = self._charge_time(battery, soc_end)
            battery.available_time = end_time + charge_t    

            records.append({
                "sortie_id": s.sortie_id,
                "model_id": s.model_id,
                "drone_id": drone.drone_id,
                "battery_id": battery.battery_id,
                "start_time": start_time,
                "end_time": end_time,
                "total_time": s.total_time,
                "total_energy": s.total_energy,
                "return_soc": s.return_soc,
                "service_seq": "->".join(s.service_seq),
                "boxes": ",".join(b.box_id for b in s.boxes)
            })

        # 汇总指标
        if records:
            cmax = max(r["end_time"] for r in records)
        else:
            cmax = 0.0
        total_energy = sum(r["total_energy"] for r in records)
        n_sorties = len(records)
        # 软迟到指标
        late_penalty = 0.0
        for s in sorties:
            for b in s.boxes:
                t = s.delivery_time.get(b.box_id, float('inf'))
                if t > b.expected_time and b.hard_deadline == float('inf'):
                    late_penalty += b.priority * (t - b.expected_time) / b.expected_time
        metrics = {
            "Cmax": cmax,
            "total_energy": total_energy,
            "n_sorties": n_sorties,
            "late_penalty": late_penalty
        }
        return records, metrics

# ============================================================
# 6. 简单初始解构造
# ============================================================
def build_initial_solution(boxes: List[Box], services: List[Node], models: Dict[str, DroneModel],
                           evaluator: Evaluator) -> List[Sortie]:
    """
    简单初始解：按服务区聚合，每个服务区尝试用最大机型 C 单点往返。
    若容量/能量不足，则拆成多个架次。
    这里先不跨服务区合并，保证可行。
    """
    sorties = []
    sid = 0
    # 按服务区分组
    by_service = {}
    for b in boxes:
        by_service.setdefault(b.service_id, []).append(b)

    for service_id, sboxes in by_service.items():
        # 按质量从大到小排序
        sboxes_sorted = sorted(sboxes, key=lambda x: -x.mass)
        # 先用 C 型，如果 C 型不可用再用 B/A
        for model_id in ["C", "B", "A"]:
            m = models[model_id]
            remaining = sboxes_sorted[:]
            while remaining:
                # 贪心装箱
                current_boxes = []
                current_mass = 0.0
                current_vol = 0.0
                for b in remaining[:]:
                    if current_mass + b.mass <= m.max_payload and current_vol + b.volume <= m.max_volume:
                        current_boxes.append(b)
                        current_mass += b.mass
                        current_vol += b.volume
                        remaining.remove(b)
                if not current_boxes:
                    break
                sid += 1
                sortie = Sortie(
                    sortie_id=f"S{sid:03d}",
                    model_id=model_id,
                    service_seq=[service_id],
                    boxes=current_boxes
                )
                sortie = evaluator.evaluate_sortie(sortie)
                if not sortie.feasible:
                    # 如果 C 型不行，换 B 型再试
                    break
                sorties.append(sortie)
            if remaining:
                continue
            else:
                break
    return sorties

# ============================================================
# 7. 主程序
# ============================================================
def main():
    print("=" * 60)
    print("问题二：异构无人机多点多架次运输调度 - 初始解")
    print("=" * 60)

    # 读取数据
    node_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "调度中心与服务区.xlsx")
    box_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "物资需求与配送时限.xlsx")
    drone_file = os.path.join(DATA_DIR, "无人机应急物资运输基础数据", "运输无人机数据.xlsx")

    print("读取节点数据...")
    o01, services = load_nodes(node_file)
    print(f"  调度中心: {o01.id} {o01.name}")
    print(f"  服务区数量: {len(services)}")

    print("读取货箱数据...")
    boxes = load_boxes(box_file)
    print(f"  货箱数量: {len(boxes)}")

    print("读取运输无人机数据...")
    models = load_drone_models(drone_file)
    drones, batteries = load_drones_and_batteries(drone_file)
    print(f"  机型: {list(models.keys())}")
    print(f"  无人机数量: {len(drones)}")
    print(f"  电池数量: {len(batteries)}")

    # 坐标投影
    project_nodes(o01, services)
    node_dict = {o01.id: o01}
    for s in services:
        node_dict[s.id] = s

    # 航段预计算
    print("预计算航段...")
    seg, _ = precompute_segments(o01, services, models, DEM_PATH)
    print(f"  航段数量: {len(seg)}")

    # 评价器
    evaluator = Evaluator(models, seg, node_dict)

    # 初始解
    print("构造初始解...")
    sorties = build_initial_solution(boxes, services, models, evaluator)
    print(f"  初始架次数: {len(sorties)}")
    feasible_sorties = [s for s in sorties if s.feasible]
    print(f"  可行架次数: {len(feasible_sorties)}")

    # 解码调度
    print("资源调度解码...")
    decoder = Decoder(drones, batteries, models)
    records, metrics = decoder.schedule(feasible_sorties)

    # 输出
    print("\n调度指标:")
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")

    # 保存 CSV
    df_records = pd.DataFrame(records)
    df_records.to_csv(os.path.join(OUTPUT_DIR, "sortie_schedule.csv"), index=False, encoding="utf-8-sig")
    print(f"\n架次调度已保存至 {os.path.join(OUTPUT_DIR, 'sortie_schedule.csv')}")

    # 逐箱送达
    box_rows = []
    for s in feasible_sorties:
        for b in s.boxes:
            box_rows.append({
                "box_id": b.box_id,
                "service_id": b.service_id,
                "material": b.material,
                "sortie_id": s.sortie_id,
                "model_id": s.model_id,
                "delivery_time": s.delivery_time.get(b.box_id, None),
                "expected_time": b.expected_time,
                "hard_deadline": b.hard_deadline,
                "is_first_batch": b.is_first_batch,
                "mass": b.mass,
                "volume": b.volume,
                "priority": b.priority
            })
    df_boxes = pd.DataFrame(box_rows)
    df_boxes.to_csv(os.path.join(OUTPUT_DIR, "box_delivery.csv"), index=False, encoding="utf-8-sig")
    print(f"逐箱送达已保存至 {os.path.join(OUTPUT_DIR, 'box_delivery.csv')}")

    # 简单可行性检查
    print("\n可行性检查:")
    all_box_ids = set(b.box_id for b in boxes)
    delivered_box_ids = set()
    for s in feasible_sorties:
        for b in s.boxes:
            delivered_box_ids.add(b.box_id)
    missing = all_box_ids - delivered_box_ids
    if missing:
        print(f"  警告: 未安排货箱 {missing}")
    else:
        print("  所有货箱均已安排。")
    print("  完成。")

if __name__ == "__main__":
    main()