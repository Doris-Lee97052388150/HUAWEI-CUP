# -*- coding: utf-8 -*-
"""
问题一：单点往返运输能力与货箱组批方案
O01 -> Si -> O01

功能：
1) 读取节点、30m DEM、三类运输无人机参数、逐箱货箱清单
2) 计算 O01-Si 直线航段水平距离、沿线最高 DEM、巡航海拔及爬升/下降高度
3) 计算三种机型在 15 个服务区的最大安全载荷
4) 对各服务区分别用递归剪枝构造“机型 + 货箱集合”可行批次
5) 字典序优化：最少架次 -> 最小总能耗 -> 最小累计作业时间
6) 对返航安全余量进行敏感性分析
7) 输出 Excel 与图

依赖：
pip install -U numpy pandas scipy openpyxl matplotlib
"""

from pathlib import Path
import math
import numpy as np
import pandas as pd

try:
    from scipy.io import loadmat
    from scipy.optimize import milp, LinearConstraint, Bounds
    from scipy.sparse import csc_matrix, vstack
except ImportError as e:
    raise ImportError("需要 scipy>=1.9。请运行：pip install -U scipy") from e

import matplotlib.pyplot as plt


# ============================================================
# 0. 只需修改这里
# ============================================================
ROOT = Path(r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP")

# 题目规定：基准返航安全余量下限固定为20%
BASE_RHO = 0.20

# 第3问敏感性分析时改变余量
RHO_LIST = [0.10, 0.15, 0.20, 0.25, 0.30]
DEM_SAMPLE_STEP_M = 10.0

OUTPUT_DIR = ROOT / "问题一输出"
OUTPUT_XLSX = OUTPUT_DIR / "问题一结果.xlsx"


# ============================================================
# 1. 文件定位与数据读取
# ============================================================
def find_file(root: Path, names):
    for name in names:
        hits = list(root.rglob(name))
        if hits:
            return hits[0]
    raise FileNotFoundError(
        f"在 {root} 下找不到文件：{names}\n"
        f"请检查 ROOT 是否设置为题目数据所在的总目录。"
    )


def resolve_input_files():
    node_file = find_file(
        ROOT, ["调度中心与服务区.xlsx", "调度中心与服务区(1).xlsx"]
    )
    box_file = find_file(
        ROOT, ["物资需求与配送时限.xlsx", "物资需求与配送时限(1).xlsx"]
    )
    uav_file = find_file(
        ROOT, ["运输无人机数据.xlsx", "运输无人机数据(1).xlsx"]
    )
    dem_file = find_file(
        ROOT, [
            "镇龙乡及周边30米DEM.mat",
            "镇龙乡及周边30米DEM(1).mat",
            "镇龙乡及周边30米DEM(2).mat",
            "镇龙乡及周边30米DEM .mat",
        ]
    )
    return node_file, box_file, uav_file, dem_file


def load_nodes(node_file: Path):
    raw = pd.read_excel(node_file, sheet_name="数据", header=None)
    first_col = raw.iloc[:, 0].astype(str).str.strip()

    o_rows = raw[first_col.eq("O01")]
    if len(o_rows) != 1:
        raise ValueError("无法唯一定位 O01。")

    r = o_rows.iloc[0]
    origin = {
        "id": "O01",
        "lon": float(r.iloc[2]),
        "lat": float(r.iloc[3]),
        "alt": float(r.iloc[4]),
    }

    service_mask = first_col.str.fullmatch(r"S\d{3}", na=False)
    s = raw[service_mask].copy()

    services = pd.DataFrame({
        "service": s.iloc[:, 0].astype(str).str.strip().to_numpy(),
        "name": s.iloc[:, 1].astype(str).to_numpy(),
        "lon": pd.to_numeric(s.iloc[:, 2]).to_numpy(float),
        "lat": pd.to_numeric(s.iloc[:, 3]).to_numpy(float),
        "alt": pd.to_numeric(s.iloc[:, 4]).to_numpy(float),
    }).sort_values("service").reset_index(drop=True)

    return origin, services


def load_uavs(uav_file: Path):
    df = pd.read_excel(uav_file, sheet_name="数据", header=1)
    # 文件后半部分还有“共享电池参数”等表，其中第一列也可能出现 A/B/C。
    # 这里只保留真正的三类机型参数行：必须同时具有最大载货质量。
    qmax_num = pd.to_numeric(df["最大载货质量（kg）"], errors="coerce")
    df = df[df["机型编号"].isin(["A", "B", "C"]) & qmax_num.notna()].copy()
    df = df.drop_duplicates(subset=["机型编号"], keep="first")
    df = df.set_index("机型编号").reindex(["A", "B", "C"]).reset_index()

    if df["最大载货质量（kg）"].isna().any():
        raise ValueError("未能唯一读取 A/B/C 三类机型参数，请检查运输无人机数据.xlsx 的表结构。")

    rename = {
        "机型编号": "type",
        "机型名称": "name",
        "含电池空载总质量（kg）": "m0",
        "最大载货质量（kg）": "qmax",
        "可用装载体积（m³）": "vmax",
        "计划巡航速度（m/s）": "v_cruise",
        "空载标准航程（m）": "L0",
        "满载标准航程（m）": "LF",
        "电池可用能量（kWh）": "Euse",
        "返航电量下限（%）": "rho",
        "工位固定准备时间（s）": "t_prep",
        "每箱装载时间（s）": "t_load",
        "接收点基础交接时间（s）": "t_hand",
        "每箱增加交接时间（s）": "t_add",
        "最大爬升速度（m/s）": "v_up",
        "最大下降速度（m/s）": "v_down",
        "爬升能耗效率": "eta_up",
    }
    df = df.rename(columns=rename)

    needed = list(rename.values())
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"运输无人机数据缺少字段：{missing}")

    df["rho"] = df["rho"].astype(float).apply(
        lambda x: x / 100.0 if x > 1 else x
    )

    numeric_cols = [
        "m0", "qmax", "vmax", "v_cruise", "L0", "LF", "Euse",
        "t_prep", "t_load", "t_hand", "t_add",
        "v_up", "v_down", "eta_up",
    ]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="raise").astype(float)

    return df


def load_boxes(box_file: Path):
    df = pd.read_excel(box_file, sheet_name="逐箱货箱清单")
    rename = {
        "货箱编号": "box_id",
        "服务区编号": "service",
        "物资类型": "material",
        "单箱质量（kg）": "weight",
        "单箱体积（m³）": "volume",
    }
    df = df.rename(columns=rename)

    needed = ["box_id", "service", "material", "weight", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"逐箱货箱清单缺少字段：{missing}")

    df = df.dropna(subset=["box_id", "service"]).copy()
    df["box_id"] = df["box_id"].astype(str).str.strip()
    df["service"] = df["service"].astype(str).str.strip()
    df["weight"] = pd.to_numeric(df["weight"], errors="raise").astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="raise").astype(float)
    return df


def load_dem_data(dem_file: Path):
    try:
        d = loadmat(dem_file)
        dem = np.asarray(d["dem"], dtype=float)
        latitude = np.asarray(d["latitude"], dtype=float).reshape(-1)
        longitude = np.asarray(d["longitude"], dtype=float).reshape(-1)
        nodata = float(np.asarray(d["nodata"]).squeeze())
    except NotImplementedError:
        try:
            import h5py
        except ImportError as e:
            raise ImportError(
                "该 MAT 文件为 v7.3，需要 h5py：pip install h5py"
            ) from e

        with h5py.File(dem_file, "r") as f:
            dem = np.asarray(f["dem"], dtype=float)
            latitude = np.asarray(f["latitude"], dtype=float).reshape(-1)
            longitude = np.asarray(f["longitude"], dtype=float).reshape(-1)
            nodata = float(np.asarray(f["nodata"]).squeeze())

    if dem.shape == (len(longitude), len(latitude)):
        dem = dem.T

    if dem.shape != (len(latitude), len(longitude)):
        raise ValueError(
            f"DEM 尺寸 {dem.shape} 与 latitude/longitude "
            f"({len(latitude)}, {len(longitude)}) 不匹配。"
        )

    return dem, latitude, longitude, nodata


# ============================================================
# 2. 航线与 DEM
# ============================================================
def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * R * math.asin(min(1.0, math.sqrt(a)))


def nearest_index(vec, query):
    vec = np.asarray(vec, dtype=float)
    query = np.asarray(query, dtype=float)

    order = np.argsort(vec)
    sv = vec[order]

    pos = np.searchsorted(sv, query, side="left")
    pos = np.clip(pos, 0, len(sv) - 1)
    left = np.clip(pos - 1, 0, len(sv) - 1)

    choose_left = np.abs(query - sv[left]) <= np.abs(query - sv[pos])
    chosen = np.where(choose_left, left, pos)
    return order[chosen]


def max_dem_on_line(
    dem, latitude, longitude, nodata,
    lon1, lat1, lon2, lat2, distance_m,
    step_m=DEM_SAMPLE_STEP_M
):
    """
    按题意在 O01-Si 直线上生成全部采样点，并查询每个采样点
    对应的 DEM 像元高程，最后直接取所有采样点 DEM 值的最大值。

    这里不对采样点对应的 DEM 索引做 np.unique() 去重；
    即使多个采样点落在同一 DEM 像元，也全部保留。
    """
    n = max(2, int(math.ceil(distance_m / step_m)) + 1)

    lon_q = np.linspace(lon1, lon2, n)
    lat_q = np.linspace(lat1, lat2, n)

    rows = nearest_index(latitude, lat_q)
    cols = nearest_index(longitude, lon_q)

    # 严格保留每一个采样点对应的 DEM 高程
    z = dem[rows, cols].astype(float)

    if np.isfinite(nodata):
        z[np.isclose(z, nodata)] = np.nan

    z[~np.isfinite(z)] = np.nan
    z = z[np.isfinite(z)]

    if z.size == 0:
        raise ValueError("航线上没有找到有效 DEM 高程。")

    return float(np.max(z))


def build_route_table(origin, services, dem, lat_vec, lon_vec, nodata):
    rows = []

    for _, s in services.iterrows():
        d = haversine_distance(
            origin["lat"], origin["lon"], s["lat"], s["lon"]
        )

        zmax = max_dem_on_line(
            dem, lat_vec, lon_vec, nodata,
            origin["lon"], origin["lat"],
            s["lon"], s["lat"], d
        )

        H = zmax + 50.0

        h_up_out = max(0.0, H - origin["alt"])
        h_down_out = max(0.0, H - (s["alt"] + 30.0))
        h_up_back = max(0.0, H - (s["alt"] + 30.0))
        h_down_back = max(0.0, H - origin["alt"])

        rows.append({
            "service": s["service"],
            "distance_m": d,
            "max_dem_m": zmax,
            "cruise_altitude_m": H,
            "out_climb_m": h_up_out,
            "out_descent_m": h_down_out,
            "back_climb_m": h_up_back,
            "back_descent_m": h_down_back,
        })

    return pd.DataFrame(rows)


# ============================================================
# 3. 航程、能耗、时间
# ============================================================
def effective_range(uav, q):
    q = max(0.0, min(float(q), float(uav["qmax"])))
    return (
        uav["L0"]
        - (uav["L0"] - uav["LF"])
        * (q / uav["qmax"]) ** 1.5
    )


def segment_energy(uav, q, distance_m, climb_m):
    """
    单航段运输能耗只包含：
    1. 水平巡航能耗 E_hor = Euse * d / L(q)
    2. 爬升附加能耗 E_up = (m0+q) * g * h / (eta_up * 3.6e6)

    按题目规则，下降阶段不单独计算附加运输能耗。
    """
    g0 = 9.81

    Lq = effective_range(uav, q)
    E_hor = uav["Euse"] * distance_m / Lq

    mass = uav["m0"] + q
    E_up = mass * g0 * climb_m / (uav["eta_up"] * 3.6e6)

    return float(E_hor + E_up)


def roundtrip_energy(uav, route, payload_kg):
    e_out = segment_energy(
        uav, payload_kg,
        route["distance_m"], route["out_climb_m"]
    )
    e_back = segment_energy(
        uav, 0.0,
        route["distance_m"], route["back_climb_m"]
    )
    return e_out + e_back


def roundtrip_flight_time(uav, route):
    out_time = (
        route["out_climb_m"] / uav["v_up"]
        + route["distance_m"] / uav["v_cruise"]
        + route["out_descent_m"] / uav["v_down"]
    )

    back_time = (
        route["back_climb_m"] / uav["v_up"]
        + route["distance_m"] / uav["v_cruise"]
        + route["back_descent_m"] / uav["v_down"]
    )

    return float(out_time + back_time)


def operation_time(uav, route, n_boxes):
    return float(
        uav["t_prep"]
        + n_boxes * uav["t_load"]
        + roundtrip_flight_time(uav, route)
        + uav["t_hand"]
        + n_boxes * uav["t_add"]
    )


# ============================================================
# 4. 最大安全载荷
# ============================================================
def safe_payload(uav, route, rho, tol=1e-7):
    limit = (1.0 - rho) * uav["Euse"]
    Q = uav["qmax"]

    e0 = roundtrip_energy(uav, route, 0.0)
    if e0 > limit + 1e-12:
        return 0.0

    e_full = roundtrip_energy(uav, route, Q)
    if e_full <= limit + 1e-12:
        return float(Q)

    lo, hi = 0.0, float(Q)

    for _ in range(80):
        mid = 0.5 * (lo + hi)
        e = roundtrip_energy(uav, route, mid)

        if e <= limit:
            lo = mid
        else:
            hi = mid

        if hi - lo <= tol:
            break

    return float(lo)


def safe_payload_table(uavs, routes, rho_override=None):
    rows = []

    for _, route in routes.iterrows():
        row = {"service": route["service"]}

        for _, uav in uavs.iterrows():
            rho = uav["rho"] if rho_override is None else rho_override
            row[uav["type"]] = safe_payload(uav, route, rho)

        rows.append(row)

    return pd.DataFrame(rows)


# ============================================================
# 5. 构造“机型 + 货箱集合”可行批次
# ============================================================
def iter_feasible_subsets(weights, volumes, mass_cap, volume_cap):
    """
    递归剪枝生成满足重量、体积约束的非空货箱集合。

    与一次性构造 2^n × n 的 membership 大矩阵不同，
    这里只沿可行分支递归，并逐个 yield 可行组合，
    因此显著降低内存占用。

    说明：
    - 该方法仍然是“显式 pattern 枚举”，数学模型不变；
    - 但通过容量剪枝避免生成大量明显不可行的子集；
    - 本题按服务区分别求解，通常足以应对约 15~20 箱规模。
    """
    n = len(weights)
    chosen = []

    def dfs(start, total_w, total_v):
        for j in range(start, n):
            new_w = total_w + weights[j]
            new_v = total_v + volumes[j]

            # 加入该箱后已超容量，则该 include 分支直接剪掉
            if new_w > mass_cap + 1e-9:
                continue
            if new_v > volume_cap + 1e-12:
                continue

            chosen.append(j)

            # 当前集合本身就是一个可行批次
            yield tuple(chosen), new_w, new_v

            # 继续向后加入更多货箱
            yield from dfs(j + 1, new_w, new_v)

            chosen.pop()

    yield from dfs(0, 0.0, 0.0)


def build_patterns(
    service_boxes, service_id, uavs, route, qsafe_row
):
    """
    构造可行批次 (g, B)。

    每个 pattern 同时包含：
    - 机型 g
    - 同一服务区的货箱集合 B
    - 总重量、总体积
    - 往返运输能耗
    - 累计作业时间
    - 临界返航安全余量
    """
    service_boxes = service_boxes.reset_index(drop=True)

    weights = service_boxes["weight"].to_numpy(float)
    volumes = service_boxes["volume"].to_numpy(float)
    n = len(service_boxes)

    patterns = []

    for _, uav in uavs.iterrows():
        qsafe = float(qsafe_row[uav["type"]])

        mass_cap = min(float(uav["qmax"]), qsafe)
        volume_cap = float(uav["vmax"])

        # 如果该机型连最轻单箱或最小体积单箱都无法承载，可直接跳过
        if mass_cap <= 0:
            continue

        flight_time = roundtrip_flight_time(uav, route)

        for indices, W, V in iter_feasible_subsets(
            weights, volumes, mass_cap, volume_cap
        ):
            mask = np.zeros(n, dtype=bool)
            mask[list(indices)] = True

            n_box = len(indices)

            E = roundtrip_energy(uav, route, W)

            T = float(
                uav["t_prep"]
                + n_box * uav["t_load"]
                + flight_time
                + uav["t_hand"]
                + n_box * uav["t_add"]
            )

            rho_crit = 1.0 - E / uav["Euse"]

            patterns.append({
                "service": service_id,
                "uav_type": uav["type"],
                "mask": mask,
                "weight": float(W),
                "volume": float(V),
                "energy": float(E),
                "time": float(T),
                "rho_crit": float(rho_crit),
            })

    return patterns


# ============================================================
# 6. 字典序 0-1 整数规划
# ============================================================
def patterns_to_cover_matrix(patterns, n_boxes):
    row_idx = []
    col_idx = []

    for k, p in enumerate(patterns):
        boxes = np.flatnonzero(p["mask"])
        row_idx.extend(boxes.tolist())
        col_idx.extend([k] * len(boxes))

    data = np.ones(len(row_idx), dtype=float)

    return csc_matrix(
        (data, (row_idx, col_idx)),
        shape=(n_boxes, len(patterns))
    )


def run_milp(c, A, lb_con, ub_con):
    m = len(c)
    res = milp(
        c=np.asarray(c, dtype=float),
        integrality=np.ones(m, dtype=int),
        bounds=Bounds(np.zeros(m), np.ones(m)),
        constraints=LinearConstraint(A, lb_con, ub_con),
        options={"disp": False},
    )

    if not res.success:
        raise RuntimeError(
            f"MILP 求解失败：status={res.status}, message={res.message}"
        )

    return res


def solve_one_service(
    service_boxes, service_id, uavs, route, qsafe_row
):
    service_boxes = service_boxes.reset_index(drop=True)
    n = len(service_boxes)

    patterns = build_patterns(
        service_boxes, service_id, uavs, route, qsafe_row
    )

    if not patterns:
        raise RuntimeError(f"{service_id} 没有任何可行批次。")

    A_cover = patterns_to_cover_matrix(patterns, n)

    cover_count = np.asarray(A_cover.sum(axis=1)).reshape(-1)
    bad = np.where(cover_count == 0)[0]
    if len(bad):
        bad_ids = service_boxes.iloc[bad]["box_id"].tolist()
        raise RuntimeError(
            f"{service_id} 存在无任何机型可运输的货箱：{bad_ids}"
        )

    m = len(patterns)
    ones_row = csc_matrix(np.ones((1, m)))

    energy = np.array([p["energy"] for p in patterns], dtype=float)
    times = np.array([p["time"] for p in patterns], dtype=float)

    # 第一级：最少架次
    res1 = run_milp(
        np.ones(m),
        A_cover,
        np.ones(n),
        np.ones(n)
    )
    n_star = int(round(res1.fun))

    # 第二级：固定最少架次，最小能耗
    A2 = vstack([A_cover, ones_row], format="csc")
    lb2 = np.r_[np.ones(n), n_star]
    ub2 = np.r_[np.ones(n), n_star]

    res2 = run_milp(energy, A2, lb2, ub2)
    e_star = float(res2.fun)

    # 第三级：固定架次，能耗维持最优值，最小累计作业时间
    energy_row = csc_matrix(energy.reshape(1, -1))
    A3 = vstack([A_cover, ones_row, energy_row], format="csc")

    e_tol = max(1e-6, 1e-6 * max(1.0, abs(e_star)))
    lb3 = np.r_[np.ones(n), n_star, -np.inf]
    ub3 = np.r_[np.ones(n), n_star, e_star + e_tol]

    res3 = run_milp(times, A3, lb3, ub3)

    selected = np.where(res3.x > 0.5)[0]

    result_rows = []

    for trip_no, k in enumerate(selected, start=1):
        p = patterns[k]
        chosen_box_ids = service_boxes.loc[
            p["mask"], "box_id"
        ].tolist()

        result_rows.append({
            "service": service_id,
            "trip": trip_no,
            "uav_type": p["uav_type"],
            "boxes": ",".join(chosen_box_ids),
            "box_count": len(chosen_box_ids),
            "weight_kg": p["weight"],
            "volume_m3": p["volume"],
            "energy_kWh": p["energy"],
            "operation_time_s": p["time"],
            "critical_reserve_percent": 100.0 * p["rho_crit"],
        })

    result_df = pd.DataFrame(result_rows)

    summary = {
        "service": service_id,
        "trips": len(result_df),
        "energy_kWh": result_df["energy_kWh"].sum(),
        "cumulative_time_s": result_df["operation_time_s"].sum(),
    }

    return result_df, summary


def solve_all_services(boxes, services, uavs, routes, qsafe):
    all_batches = []
    summaries = []

    for _, s in services.iterrows():
        sid = s["service"]
        sb = boxes[boxes["service"] == sid].copy()

        if sb.empty:
            continue

        route = routes[routes["service"] == sid].iloc[0]
        qrow = qsafe[qsafe["service"] == sid].iloc[0]

        print(f"  求解 {sid}：{len(sb)} 个货箱 ...")

        batch_df, summary = solve_one_service(
            sb, sid, uavs, route, qrow
        )

        all_batches.append(batch_df)
        summaries.append(summary)

    batch_result = pd.concat(all_batches, ignore_index=True)
    summary_result = pd.DataFrame(summaries)

    return batch_result, summary_result


# ============================================================
# 7. 安全余量敏感性
# ============================================================
def sensitivity_analysis(boxes, services, uavs, routes, rho_list):
    summary_rows = []
    payload_rows = []

    for rho in rho_list:
        print(f"\n安全余量 {rho * 100:.0f}%：")

        qsafe = safe_payload_table(
            uavs, routes, rho_override=rho
        )

        for _, r in qsafe.iterrows():
            payload_rows.append({
                "reserve_percent": rho * 100.0,
                "service": r["service"],
                "A_kg": r["A"],
                "B_kg": r["B"],
                "C_kg": r["C"],
            })

        _, service_summary = solve_all_services(
            boxes, services, uavs, routes, qsafe
        )

        summary_rows.append({
            "reserve_percent": rho * 100.0,
            "total_trips": int(service_summary["trips"].sum()),
            "total_energy_kWh": service_summary["energy_kWh"].sum(),
            "cumulative_time_s": service_summary["cumulative_time_s"].sum(),
            "cumulative_time_h":
                service_summary["cumulative_time_s"].sum() / 3600.0,
            "mean_safe_payload_A_kg": qsafe["A"].mean(),
            "mean_safe_payload_B_kg": qsafe["B"].mean(),
            "mean_safe_payload_C_kg": qsafe["C"].mean(),
        })

    return pd.DataFrame(summary_rows), pd.DataFrame(payload_rows)


# ============================================================
# 8. 作图
# ============================================================
def make_plots(qsafe, sensitivity, output_dir):
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "Arial Unicode MS"
    ]
    plt.rcParams["axes.unicode_minus"] = False

    x = np.arange(len(qsafe))

    plt.figure(figsize=(11, 6))
    for t, marker in zip(["A", "B", "C"], ["o", "s", "^"]):
        plt.plot(
            x, qsafe[t].to_numpy(),
            marker=marker, linewidth=1.5, label=f"{t}型"
        )
    plt.xticks(x, qsafe["service"], rotation=45)
    plt.xlabel("服务区")
    plt.ylabel("最大安全载荷 / kg")
    plt.title("基准安全余量下三种机型最大安全载荷")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "最大安全载荷.png", dpi=200)
    plt.close()

    plt.figure(figsize=(8, 5))
    plt.plot(
        sensitivity["reserve_percent"],
        sensitivity["total_trips"],
        marker="o", linewidth=1.5
    )
    plt.xlabel("返航安全余量 / %")
    plt.ylabel("总架次数")
    plt.title("返航安全余量对总架次数的影响")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_dir / "安全余量_架次数.png", dpi=200)
    plt.close()

    plt.figure(figsize=(8, 5))
    for col, label, marker in [
        ("mean_safe_payload_A_kg", "A型", "o"),
        ("mean_safe_payload_B_kg", "B型", "s"),
        ("mean_safe_payload_C_kg", "C型", "^"),
    ]:
        plt.plot(
            sensitivity["reserve_percent"],
            sensitivity[col],
            marker=marker, linewidth=1.5, label=label
        )
    plt.xlabel("返航安全余量 / %")
    plt.ylabel("15个服务区平均最大安全载荷 / kg")
    plt.title("安全余量对最大安全载荷的影响")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "安全余量_安全载荷.png", dpi=200)
    plt.close()


# ============================================================
# 9. 主程序
# ============================================================
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    node_file, box_file, uav_file, dem_file = resolve_input_files()

    print("读取文件：")
    print("  节点：", node_file)
    print("  货箱：", box_file)
    print("  机型：", uav_file)
    print("  DEM ：", dem_file)

    origin, services = load_nodes(node_file)
    uavs = load_uavs(uav_file)
    boxes = load_boxes(box_file)
    dem, lat_vec, lon_vec, nodata = load_dem_data(dem_file)

    print(f"\nDEM 尺寸：{dem.shape}")
    print(f"服务区数量：{len(services)}")
    print(f"货箱数量：{len(boxes)}")

    print("\n[1/4] 计算航线 DEM 与飞行几何参数 ...")
    routes = build_route_table(
        origin, services, dem, lat_vec, lon_vec, nodata
    )

    print(f"[2/4] 计算基准最大安全载荷（返航安全余量固定为 {BASE_RHO*100:.0f}%） ...")
    qsafe = safe_payload_table(
        uavs, routes, rho_override=BASE_RHO
    )

    print("\n基准最大安全载荷 / kg：")
    print(qsafe.round(3).to_string(index=False))

    print("\n[3/4] 求解基准组批方案 ...")
    batches, service_summary = solve_all_services(
        boxes, services, uavs, routes, qsafe
    )

    total_trips = int(service_summary["trips"].sum())
    total_energy = float(service_summary["energy_kWh"].sum())
    total_time = float(service_summary["cumulative_time_s"].sum())

    overall = pd.DataFrame([{
        "base_reserve_percent": BASE_RHO * 100.0,
        "total_trips": total_trips,
        "total_energy_kWh": total_energy,
        "cumulative_time_s": total_time,
        "cumulative_time_h": total_time / 3600.0,
    }])

    print(f"\n基准方案汇总（返航安全余量={BASE_RHO*100:.0f}%）：")
    print(f"  总架次数       = {total_trips}")
    print(f"  总运输能耗     = {total_energy:.6f} kWh")
    print(f"  累计作业时间   = {total_time:.2f} s")
    print(f"  累计作业时间   = {total_time / 3600.0:.4f} h")

    print("\n[4/4] 返航安全余量敏感性分析 ...")
    sensitivity, sensitivity_payload = sensitivity_analysis(
        boxes, services, uavs, routes, RHO_LIST
    )

    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
        routes.to_excel(
            writer, sheet_name="航线参数", index=False
        )
        qsafe.to_excel(
            writer, sheet_name="基准最大安全载荷", index=False
        )
        batches.to_excel(
            writer, sheet_name="基准最优组批", index=False
        )
        service_summary.to_excel(
            writer, sheet_name="服务区统计", index=False
        )
        overall.to_excel(
            writer, sheet_name="总体结果", index=False
        )
        sensitivity.to_excel(
            writer, sheet_name="安全余量汇总", index=False
        )
        sensitivity_payload.to_excel(
            writer, sheet_name="安全余量_各区载荷", index=False
        )

    make_plots(qsafe, sensitivity, OUTPUT_DIR)

    print("\n计算完成。")
    print("Excel：", OUTPUT_XLSX)
    print("图片目录：", OUTPUT_DIR)


if __name__ == "__main__":
    main()
