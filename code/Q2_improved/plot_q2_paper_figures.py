r"""问题二论文插图：完整路线、资源周转、交付时限与目标权衡。

将本文件放在 Q2_improved 根目录，从该目录运行：
    python plot_q2_paper_figures.py
    python plot_q2_paper_figures.py --root D:\my_results\Q2_improved

仅使用已有的 data/节点表和 results/{balanced,trips,baseline_corrected}/solution.json。
默认在 results/paper_figures 生成四幅图，每幅均有 PDF、SVG、PNG。
图展示已有方案，不重新运行优化；交付时刻为停点交接完成，完工时刻为最后返航。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib import font_manager, ticker
import openpyxl


HERE = Path(__file__).resolve().parent
BLUE = "#34789B"
TEAL = "#22958B"
NAVY = "#185661"
INK = "#213B4A"
MUTED = "#607786"
GRID = "#DCE7E9"
PALE = "#D5E8ED"
ORANGE = "#BB7E42"
RED = "#AB5261"
PALETTE = {"A": BLUE, "B": TEAL, "C": NAVY}
SCHEMES = [
    ("baseline_corrected", "原算法", "#8396A0"),
    ("balanced", "均衡方案", BLUE),
    ("trips", "减少架次", TEAL),
]


def prepare_style(root: Path) -> None:
    packaged = root / "fonts" / "NotoSansCJKsc-Regular.otf"
    if packaged.exists():
        font_manager.fontManager.addfont(str(packaged))
        family = font_manager.FontProperties(fname=str(packaged)).get_name()
    else:
        names = {f.name for f in font_manager.fontManager.ttflist}
        family = next(
            (f for f in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC")
             if f in names), None
        )
        if family is None:
            raise RuntimeError("缺少中文字体：请保留完整代码包中的 fonts 目录。")
    plt.rcParams.update({
        "font.family": family, "axes.unicode_minus": False,
        "font.size": 10, "pdf.fonttype": 42, "svg.fonttype": "none",
        "axes.edgecolor": "#9CB0BA", "axes.labelcolor": INK,
        "text.color": INK, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.spines.top": False, "axes.spines.right": False,
        "savefig.facecolor": "white",
    })


def load_nodes(root: Path) -> dict[str, tuple[float, float]]:
    candidates = list((root / "data").glob("调度中心与服务区*.xlsx"))
    if len(candidates) != 1:
        raise ValueError(f"data 目录应恰有一份节点表，当前为 {len(candidates)} 份。")
    wb = openpyxl.load_workbook(candidates[0], read_only=True, data_only=True)
    try:
        raw = {}
        for row in wb.active.iter_rows(values_only=True):
            if (row and isinstance(row[0], str)
                    and (row[0] == "O01" or row[0].startswith("S"))
                    and len(row) >= 4
                    and isinstance(row[2], (int, float))
                    and isinstance(row[3], (int, float))):
                raw[row[0]] = (float(row[2]), float(row[3]))
    finally:
        wb.close()
    if "O01" not in raw:
        raise ValueError("节点表中缺少 O01。")
    lon0, lat0 = raw["O01"]
    radius = 6371.0088
    return {key: (radius * math.cos(math.radians(lat0))
                  * math.radians(lon - lon0),
                  radius * math.radians(lat - lat0))
            for key, (lon, lat) in raw.items()}


def load_plans(root: Path) -> dict[str, dict]:
    plans = {}
    for stem, _, _ in SCHEMES:
        path = root / "results" / stem / "solution.json"
        if not path.exists():
            raise FileNotFoundError(f"缺少方案文件：{path}")
        plan = json.loads(path.read_text(encoding="utf-8"))
        if not plan.get("audit", {}).get("valid", False):
            raise ValueError(f"方案未通过独立校验：{path}")
        trips = plan["trips"]
        box_rows = plan["tables"]["Q2_逐箱交付"][1]
        if (len(box_rows) != plan["audit"]["box_count"]
                or len({r[0] for r in box_rows}) != len(box_rows)):
            raise ValueError(f"逐箱表数量或唯一性有误：{path}")
        if abs(max(t["return_time"] for t in trips)
               - plan["metrics"]["makespan"]) > 1e-5:
            raise ValueError(f"完工时间不是最后返航：{path}")
        plans[stem] = plan
    return plans


def paper_axes(ax) -> None:
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.82)
    ax.set_axisbelow(True)


def save_figure(fig, folder: Path, stem: str) -> None:
    for ext in ("pdf", "svg", "png"):
        path = folder / (stem + "." + ext)
        pending = folder / ("." + stem + ".pending." + ext)
        fig.savefig(pending, format=ext, dpi=240, facecolor="white")
        pending.replace(path)
        print(path)
    plt.close(fig)


def arrow_segment(ax, a: tuple[float, float], b: tuple[float, float],
                  color: str, *, fraction: float = 0.58, scale: int = 8) -> None:
    if math.dist(a, b) < 0.3:
        return
    x0 = a[0] + (fraction - 0.045) * (b[0] - a[0])
    y0 = a[1] + (fraction - 0.045) * (b[1] - a[1])
    x1 = a[0] + (fraction + 0.045) * (b[0] - a[0])
    y1 = a[1] + (fraction + 0.045) * (b[1] - a[1])
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops={"arrowstyle": "-|>", "lw": 0,
                            "mutation_scale": scale, "color": color}, zorder=5)


def plot_routes(nodes: dict, plan: dict, out: Path) -> None:
    trips = plan["trips"]
    fig, axes = plt.subplots(1, 3, figsize=(16.8, 6.5), sharex=True, sharey=True)
    xs = [p[0] for p in nodes.values()]
    ys = [p[1] for p in nodes.values()]
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    for ax, drone_type in zip(axes, PALETTE):
        selected = [t for t in trips if t["drone_type"] == drone_type]
        outward = Counter()
        reverse = Counter()
        delivered = Counter()
        for trip in selected:
            seq = ["O01", *trip["service_sequence"], "O01"]
            for origin, dest in zip(seq, seq[1:]):
                if dest == "O01":
                    reverse[(origin, dest)] += 1
                else:
                    outward[(origin, dest)] += 1
            delivered.update(box.split("-")[0] for box in trip["box_delivery"])

        for site, pos in nodes.items():
            if site != "O01":
                ax.scatter(*pos, s=15, color="#B9C8CC", zorder=1)
        for (origin, dest), count in outward.items():
            start, stop = nodes[origin], nodes[dest]
            multi = origin != "O01"
            color = ORANGE if multi else PALETTE[drone_type]
            ax.plot([start[0], stop[0]], [start[1], stop[1]],
                    color=color, lw=2.15 + 0.58 * (count - 1),
                    alpha=0.82, solid_capstyle="round", zorder=3)
            arrow_segment(ax, start, stop, color)
            if count > 1:
                mx = start[0] * 0.42 + stop[0] * 0.58
                my = start[1] * 0.42 + stop[1] * 0.58
                ax.annotate(f"×{count}", (mx, my), xytext=(3, 4),
                            textcoords="offset points", fontsize=8.0,
                            color=INK, bbox={"facecolor": "white",
                                             "edgecolor": "none", "alpha": 0.82},
                            zorder=7)
        for (origin, dest), count in reverse.items():
            start, stop = nodes[origin], nodes[dest]
            ax.plot([start[0], stop[0]], [start[1], stop[1]],
                    color="#899BA2", lw=0.9 + 0.22 * (count - 1),
                    linestyle=(0, (3.5, 3)), alpha=0.87, zorder=4)
            arrow_segment(ax, start, stop, "#899BA2", fraction=0.28, scale=7)
        for site, nboxes in sorted(delivered.items()):
            x, y = nodes[site]
            ax.scatter(x, y, s=55 + 12 * nboxes, color=PALETTE[drone_type],
                       edgecolors="white", linewidths=1.2, alpha=0.92, zorder=8)
            ax.annotate(site, (x, y), xytext=(4, 5), textcoords="offset points",
                        fontsize=8.5, color=INK, zorder=9,
                        bbox={"facecolor": "white", "edgecolor": "none",
                              "alpha": 0.65, "pad": 0.25})
        ax.scatter(*nodes["O01"], s=104, marker="D", color=INK,
                   edgecolors="white", linewidths=1, zorder=10)
        ax.annotate("O01", nodes["O01"], xytext=(5, -15),
                    textcoords="offset points", fontsize=9, color=INK, zorder=11)
        multi_count = sum(len(t["service_sequence"]) > 1 for t in selected)
        ax.set_title(f"{drone_type}型   {len(selected)} 架次 · {sum(delivered.values())} 箱"
                     f" · {multi_count} 条多点路线", fontsize=11, pad=11)
        ax.set_xlim(min(xs) - .10 * dx, max(xs) + .13 * dx)
        ax.set_ylim(min(ys) - .17 * dy, max(ys) + .10 * dy)
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("相对 O01 向东 / km")
        paper_axes(ax)
    axes[0].set_ylabel("相对 O01 向北 / km")
    handles = [
        Line2D([0], [0], lw=2.8, color=BLUE, label="去程航段"),
        Line2D([0], [0], lw=1.1, color="#899BA2",
               linestyle=(0, (3.5, 3)), label="返回 O01"),
        Line2D([0], [0], lw=2.8, color=ORANGE, label="同架次跨区航段"),
        Line2D([0], [0], marker="o", lw=0, markersize=10, color=TEAL,
               label="停点圆面积对应交付箱数"),
    ]
    fig.legend(handles=handles, ncol=4, loc="upper center",
               bbox_to_anchor=(0.5, .945), frameon=False)
    fig.suptitle("全部运输架次的空间路线", fontsize=15, y=.993)
    fig.subplots_adjust(left=.055, right=.995, bottom=.14, top=.85, wspace=.12)
    fig.text(.5, .035,
             f"共 {len(trips)} 架次、{sum(len(t['box_delivery']) for t in trips)} 箱；"
             "重叠航段标记经过次数，箭头表示行进方向。"
             "经纬度仅为作图转换成 O01 的局部平面坐标。",
             ha="center", color=MUTED, fontsize=9)
    save_figure(fig, out, "图1_全量运输航线")


def plot_resources(plan: dict, out: Path) -> None:
    trips = plan["trips"]
    audit = plan["audit"]
    ids = sorted({t["drone_id"] for t in trips})
    batteries = [
        f"BAT-{typ}{k:02d}"
        for typ, v in audit["resource_peaks"].items()
        for k in range(1, v["battery_inventory"] + 1)
    ]
    peak = audit["resource_peaks"]
    if not set(t["battery_id"] for t in trips).issubset(batteries):
        raise ValueError("电池编号与资源库存不一致。")
    xmax = max(t["charge_end_time"] for t in trips) / 60 + 8
    makespan = plan["metrics"]["makespan"] / 60
    fig = plt.figure(figsize=(16.8, 10.8))
    gs = fig.add_gridspec(2, 2, width_ratios=(5.8, 1.35),
                          height_ratios=(.86, 1.42), left=.107, right=.975,
                          top=.87, bottom=.095, wspace=.055, hspace=.18)
    ax_d = fig.add_subplot(gs[0, 0])
    ax_b = fig.add_subplot(gs[1, 0], sharex=ax_d)
    ax_info = fig.add_subplot(gs[:, 1])
    ax_info.axis("off")
    yy = {name: len(ids) - 1 - i for i, name in enumerate(ids)}
    bb = {name: len(batteries) - 1 - i for i, name in enumerate(batteries)}
    for t in trips:
        x0, takeoff, x1, charge = (
            t[key] / 60 for key in
            ("start_time", "takeoff_time", "return_time", "charge_end_time")
        )
        color = PALETTE[t["drone_type"]]
        drone_y = yy[t["drone_id"]]
        battery_y = bb[t["battery_id"]]
        ax_d.barh(drone_y, x1 - x0, left=x0, height=.69, color=color,
                  edgecolor="white", linewidth=.5)
        ax_d.barh(drone_y, takeoff - x0, left=x0, height=.69,
                  color=PALE, edgecolor="white", linewidth=.4)
        ax_d.text((takeoff + x1) / 2, drone_y, t["trip_id"][-4:],
                  ha="center", va="center", fontsize=7.1, color="white")
        ax_b.barh(battery_y, x1 - x0, left=x0, height=.66, color=color,
                  edgecolor="white", linewidth=.5)
        ax_b.barh(battery_y, charge - x1, left=x1, height=.46,
                  color="#ECF4F2", edgecolor=color, hatch="////",
                  linewidth=.55)
        ax_b.text((x0 + x1) / 2, battery_y, t["trip_id"][-4:],
                  ha="center", va="center", fontsize=6.5, color="white")
    for name in batteries:
        if not any(t["battery_id"] == name for t in trips):
            ax_b.text(5, bb[name], "未启用", va="center", fontsize=8, color="#9DAEB5")
    for ax in (ax_d, ax_b):
        ax.axvline(makespan, color=RED, lw=1.55, linestyle=(0, (5, 3)),
                   zorder=7)
        ax.set_xlim(0, xmax)
        ax.grid(axis="x", color=GRID, lw=.75)
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0)
    ax_d.set_yticks([yy[i] for i in ids], ids)
    ax_b.set_yticks([bb[i] for i in batteries], batteries)
    ax_d.set_ylim(-.65, len(ids) - .35)
    ax_b.set_ylim(-.65, len(batteries) - .35)
    ax_d.set_title("实体无人机：从准备装载至返回 O01", fontsize=11, pad=10)
    ax_b.set_title("共享电池：架次占用与返航后充满", fontsize=11, pad=10)
    ax_b.set_xlabel("距任务开始 / min")
    plt.setp(ax_d.get_xticklabels(), visible=False)
    ax_info.text(.02, .965, "库存与同时占用峰值", weight="bold",
                 fontsize=12, transform=ax_info.transAxes)
    for j, typ in enumerate(PALETTE):
        v = peak[typ]
        ypos = .84 - j * .18
        ax_info.text(.02, ypos, f"{typ}型", fontsize=11, weight="bold",
                     color=PALETTE[typ], transform=ax_info.transAxes)
        ax_info.text(
            .02, ypos - .054,
            f"无人机  {v['drone_peak']} / {v['drone_inventory']}\n"
            f"电池      {v['battery_peak_including_charge']} / {v['battery_inventory']}",
            fontsize=10, linespacing=1.7, transform=ax_info.transAxes
        )
    ax_info.text(.02, .24, f"最后返航：{makespan:.2f} min",
                 fontsize=10.5, color=RED, transform=ax_info.transAxes)
    ax_info.text(
        .02, .18, f"最低返航 SOC：{audit['minimum_return_soc']:.2%}\n"
                    "返航安全底线：20%",
        fontsize=10.5, transform=ax_info.transAxes
    )
    ax_info.text(.02, .07,
                 "竖虚线是任务完工时刻；\n虚线后的充电不计入完工时间。",
                 fontsize=9.1, linespacing=1.55, color=MUTED,
                 transform=ax_info.transAxes)
    key = [
        Patch(color=BLUE, label="A型任务"),
        Patch(color=TEAL, label="B型任务"),
        Patch(color=NAVY, label="C型任务"),
        Patch(color=PALE, label="准备装载"),
        Patch(facecolor="#ECF4F2", edgecolor=TEAL,
              hatch="////", label="独立充电"),
        Line2D([0], [0], color=RED, linestyle=(0, (5, 3)),
               label="最后返航"),
    ]
    fig.suptitle("无人机与共享电池的连续周转", fontsize=15, y=.992)
    fig.legend(handles=key, ncol=6, loc="upper center", frameon=False,
               bbox_to_anchor=(.5, .954), fontsize=9.4)
    save_figure(fig, out, "图2_无人机与电池周转")


def plot_delivery(plan: dict, out: Path) -> None:
    boxes = plan["tables"]["Q2_逐箱交付"][1]
    handovers = plan["tables"]["Q2_停点交接"][1]
    indexed = {row[0]: row for row in boxes}
    zones = sorted({row[2] for row in boxes})
    yzone = {site: len(zones) - 1 - i for i, site in enumerate(zones)}
    hard_count = sum(row[8] is not None for row in boxes)
    medical_count = sum(row[4] == "医疗物资" for row in boxes)
    first_count = sum(bool(row[12]) for row in boxes)
    on_time = sum(row[3] <= row[7] + 1e-6 for row in boxes)
    hard_slack = min((row[8] - row[3]) / 60
                     for row in boxes if row[8] is not None)
    fig, (ax, ax2) = plt.subplots(
        1, 2, figsize=(16.8, 8.1),
        gridspec_kw={"width_ratios": (1.12, .88)}
    )
    for zone in zones:
        y = yzone[zone]
        deadlines = sorted({row[8] / 60 for row in boxes
                            if row[2] == zone and row[8] is not None})
        for deadline in deadlines:
            ax.plot([0, deadline], [y + .12, y + .12],
                    color="#E8DAD9", lw=1.05, zorder=1)
            ax.scatter(deadline, y + .12, marker=">", s=59,
                       facecolor="white", edgecolor=RED, lw=1.4, zorder=5)
        n = sum(row[2] == zone for row in boxes)
        ax.text(193, y, f"{n}箱", va="center", color=MUTED, fontsize=8)
    delivered_by_event = []
    for row in handovers:
        trip, zone, _, completion, serials = row
        box_ids = serials.split(";") if serials else []
        if not box_ids or any(b not in indexed for b in box_ids):
            raise ValueError(f"交接记录箱号异常：{trip} / {zone}")
        has_hard = any(indexed[b][8] is not None for b in box_ids)
        y = yzone[zone] + (.12 if has_hard else -.12)
        ax.scatter(completion / 60, y, s=43 + len(box_ids) * 17,
                   facecolor=NAVY if has_hard else BLUE,
                   edgecolors="white", lw=1.0, alpha=.92, zorder=4)
        delivered_by_event.extend(box_ids)
    if Counter(delivered_by_event) != Counter({box_id: 1 for box_id in indexed}):
        raise ValueError("停点交接事件未恰好覆盖全部货箱。")
    ax.set_yticks([yzone[z] for z in zones], zones)
    ax.set_ylim(-.75, len(zones) - .30)
    ax.set_xlim(0, 207)
    ax.set_xticks([0, 30, 60, 90, 120, 150, 180])
    ax.set_xlabel("从任务开始到交接完成 / min")
    ax.set_title("(a) 逐服务区交接事件与硬截止", loc="left", fontsize=11, pad=11)
    ax.grid(axis="x", color=GRID, lw=.8)
    ax.set_axisbelow(True)
    delivery_legend = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=NAVY,
               markeredgecolor="white", markersize=8,
               label="含硬时限箱的交接"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=BLUE,
               markeredgecolor="white", markersize=8,
               label="仅普通箱的交接"),
        Line2D([0], [0], marker=">", color="none", markerfacecolor="white",
               markeredgecolor=RED, markersize=8, label="硬截止时刻"),
    ]

    # 相同(期望时刻,实际交接时刻)的箱合并为气泡。位置是真实数值，面积表示箱数。
    groups = defaultdict(lambda: {"n": 0, "med": 0, "first": 0})
    for row in boxes:
        key = (round(row[7] / 60, 6), round(row[3] / 60, 6))
        groups[key]["n"] += 1
        groups[key]["med"] += (row[4] == "医疗物资")
        groups[key]["first"] += bool(row[12])
    upper = max(150, max(r[3] for r in boxes) / 60 + 9)
    ax2.plot([0, upper], [0, upper], linestyle=(0, (4, 3)),
             color="#B2BDC3", lw=1.3, zorder=1)
    for (expected, actual), info in sorted(groups.items(),
                                           key=lambda x: -x[1]["n"]):
        has_med, has_first = info["med"] > 0, info["first"] > 0
        ax2.scatter(expected, actual,
                    s=30 + 31 * info["n"], color=TEAL if has_med else BLUE,
                    edgecolor=ORANGE if has_first else "white",
                    linewidth=1.7 if has_first else 1,
                    alpha=.88, zorder=4)
        if info["n"] >= 5:
            ax2.text(expected, actual, str(info["n"]), ha="center",
                     va="center", fontsize=7.0, color="white", zorder=5)
    ax2.set_xlim(0, max(r[7] for r in boxes) / 60 + 17)
    ax2.set_ylim(0, upper)
    ax2.set_xlabel("逐箱期望送达时刻 / min")
    ax2.set_ylabel("逐箱实际交接完成时刻 / min")
    ax2.set_title("(b) 全部货箱的期望与实际送达", loc="left", fontsize=11, pad=11)
    paper_axes(ax2)
    ax2.legend(handles=[
        Line2D([0], [0], marker="o", color="none", markerfacecolor=TEAL,
               markeredgecolor="white", markersize=8, label="含医疗箱"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=BLUE,
               markeredgecolor="white", markersize=8, label="不含医疗箱"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="white",
               markeredgecolor=ORANGE, markeredgewidth=2,
               markersize=8, label="含首批箱"),
    ], loc="upper right", fontsize=8.0, frameon=False)
    fig.suptitle("货箱交付与配送时限", fontsize=15, y=.987)
    fig.legend(handles=delivery_legend, ncol=3, loc="upper left",
               bbox_to_anchor=(.077, .946), fontsize=8.2, frameon=False)
    fig.subplots_adjust(left=.077, right=.978, top=.815, bottom=.17, wspace=.25)
    fig.text(.5, .085,
             f"{len(boxes)}箱中，医疗 {medical_count} 箱、首批 {first_count} 箱"
             f"（合计 {hard_count} 个不同硬时限箱）；"
             f"期望时刻内送达 {on_time}/{len(boxes)} 箱。"
             f"硬时限最小余量 {hard_slack:.2f} min。",
             ha="center", color=INK, fontsize=10)
    fig.text(.5, .042,
             "交接事件的圆面积与该停点当次交付箱数有关；右图同一点的气泡面积对应箱数，"
             "橙色轮廓表示其中含首批箱。期望时刻在横轴，点位于灰虚线下方表示按期。",
             ha="center", color=MUTED, fontsize=8.8)
    save_figure(fig, out, "图3_逐箱配送时效")


def plot_tradeoff(plans: dict, out: Path) -> None:
    base = plans["baseline_corrected"]["metrics"]
    total_boxes = len(plans["balanced"]["tables"]["Q2_逐箱交付"][1])
    others = [("balanced", BLUE, "均衡方案"),
              ("trips", TEAL, "减少架次")]
    specs = [
        ("weighted_lateness", "加权迟到 / 无量纲"),
        ("makespan", "最晚返航 / min"),
        ("energy", "运输能耗 / kWh"),
        ("trips", "执行架次数"),
    ]
    if any(base[field] <= 0 for field, _ in specs):
        raise ValueError("基线指标为零，无法计算相对于基线的比值。")
    fig = plt.figure(figsize=(14.7, 6.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.2, 1], left=.16, right=.975,
                          top=.80, bottom=.16, wspace=.12)
    ax = fig.add_subplot(gs[0, 0])
    ax_table = fig.add_subplot(gs[0, 1])
    ax_table.axis("off")
    for row, (field, _) in enumerate(specs):
        y = 3 - row
        for stem, color, _ in others:
            value = plans[stem]["metrics"][field] / base[field]
            offset = .14 if stem == "balanced" else -.14
            yy = y + offset
            ax.plot([1, value], [yy, yy], color=color,
                    lw=2.25, alpha=.88, solid_capstyle="round")
            ax.scatter(value, yy, s=115, color=color,
                       edgecolors="white", lw=1.4, zorder=4)
    ax.axvline(1, color="#A7B5BD", lw=1.35, linestyle=(0, (4, 3)))
    ax.set_xlim(-.055, 1.18)
    ax.set_ylim(-.5, 3.5)
    ax.set_yticks([3, 2, 1, 0], [title for _, title in specs])
    ax.tick_params(axis="y", length=0, pad=9)
    ax.xaxis.set_major_locator(ticker.MultipleLocator(.2))
    ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.set_xlabel("相对同口径原算法的指标值（原算法 = 1，越小越优）")
    ax.set_title("(a) 同一尺度下的四指标变化", fontsize=11, loc="left", pad=13)
    ax.grid(axis="x", color=GRID, lw=.9)
    ax.set_axisbelow(True)

    headers = ["指标", "原算法", "均衡", "减架次"]
    table_rows = [
        ["加权迟到", f"{base['weighted_lateness']:.2f}",
         f"{plans['balanced']['metrics']['weighted_lateness']:.2f}",
         f"{plans['trips']['metrics']['weighted_lateness']:.2f}"],
        ["最晚返航/min", f"{base['makespan']/60:.2f}",
         f"{plans['balanced']['metrics']['makespan']/60:.2f}",
         f"{plans['trips']['metrics']['makespan']/60:.2f}"],
        ["运输能耗/kWh", f"{base['energy']:.2f}",
         f"{plans['balanced']['metrics']['energy']:.2f}",
         f"{plans['trips']['metrics']['energy']:.2f}"],
        ["执行架次数", str(base["trips"]),
         str(plans["balanced"]["metrics"]["trips"]),
         str(plans["trips"]["metrics"]["trips"])],
        [f"按期箱数/{total_boxes}", str(plans["baseline_corrected"]["metrics"]["on_time_boxes"]),
         str(plans["balanced"]["metrics"]["on_time_boxes"]),
         str(plans["trips"]["metrics"]["on_time_boxes"])],
    ]
    ax_table.set_title("(b) 三种方案的实际数值", fontsize=11,
                       loc="left", pad=13)
    table = ax_table.table(cellText=table_rows, colLabels=headers,
                           cellLoc="center", colLoc="center",
                           colWidths=[.34, .22, .22, .22],
                           bbox=[0, .25, 1, .62])
    table.auto_set_font_size(False)
    table.set_fontsize(9.2)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#DCE6E9")
        cell.set_linewidth(.55)
        cell.set_facecolor("#E9F3F4" if row == 0 else
                           ("#F4F8F8" if row % 2 else "white"))
        if row == 0:
            cell.set_text_props(weight="bold", color=INK)
        if row > 0 and col == 2:
            cell.set_text_props(color=BLUE)
        if row > 0 and col == 3:
            cell.set_text_props(color=TEAL)
    ax_table.text(.01, .155,
                  "均衡方案缩短最后返航时间且80箱按期；\n"
                  "减少架次方案能耗更低，但5箱普通物资晚于期望时间。",
                  transform=ax_table.transAxes, fontsize=9.1,
                  color=INK, linespacing=1.6, va="top")
    fig.suptitle("配送及时性、完工时间、能耗与架次数的权衡",
                 fontsize=15, y=.985)
    fig.legend(handles=[
        Line2D([0], [0], color=BLUE, marker="o", label="均衡方案"),
        Line2D([0], [0], color=TEAL, marker="o", label="减少架次方案"),
        Line2D([0], [0], color="#A7B5BD", linestyle=(0, (4, 3)),
               label="原算法基线"),
    ], loc="upper center", bbox_to_anchor=(.5, .915), ncol=3, frameon=False)
    fig.text(.5, .065,
             "三方案的医疗与首批硬时限均满足。原算法在相同地形口径下复算，"
             "各方案所用求解预算不同，图中对比的是所得方案而非等时间算法性能。",
             ha="center", color=MUTED, fontsize=9)
    save_figure(fig, out, "图4_四指标方案权衡")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=HERE,
                    help="包含 data 和 results 的 Q2_improved 根目录")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="图像输出目录，默认 root/results/paper_figures")
    args = ap.parse_args()
    root = args.root.resolve()
    out = (args.output_dir or root / "results" / "paper_figures").resolve()
    out.mkdir(parents=True, exist_ok=True)
    prepare_style(root)
    nodes = load_nodes(root)
    plans = load_plans(root)
    if len(nodes) != 16:
        raise ValueError(f"节点数应为 16，实际读取到 {len(nodes)}。")
    plot_routes(nodes, plans["balanced"], out)
    plot_resources(plans["balanced"], out)
    plot_delivery(plans["balanced"], out)
    plot_tradeoff(plans, out)
    print("论文图已生成：4幅，每幅 PDF/SVG/PNG。")


if __name__ == "__main__":
    main()
