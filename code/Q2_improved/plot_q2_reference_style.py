"""用本题真实结果绘制参考图风格的 2×2 路线对比图。

在解压后的 Q2_improved 目录运行：
    python plot_q2_reference_style.py

可选：
    python plot_q2_reference_style.py --root . --output results/q2_route_compare

读取 data/调度中心与服务区*.xlsx 与三个 results/*/solution.json。
图中的分钟数是该架次在相应停点完成交接的时刻，非架次起飞时刻。
其余架次及逐箱送达时间应查阅原结果表，不用这四个局部图代替全局方案。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
import openpyxl


ROOT = Path(__file__).resolve().parent
PAIRS = [
    ("S001", "S013"),
    ("S001", "S002"),
    ("S007", "S003"),
    ("S011", "S008"),
]
STYLES = {
    "baseline_corrected": {
        "label": "原 ALNS（统一地形口径）",
        "color": "#CB5B62",
        "linestyle": (0, (5.2, 2.8)),
        "linewidth": 2.3,
        "zorder": 4,
    },
    "balanced": {
        "label": "改进均衡方案",
        "color": "#286184",
        "linestyle": "solid",
        "linewidth": 4.2,
        "zorder": 2,
    },
    "trips": {
        "label": "减少架次方案",
        "color": "#219783",
        "linestyle": (0, (1.2, 2.0)),
        "linewidth": 2.9,
        "zorder": 5,
    },
}


def set_chinese_font(root: Path) -> None:
    """优先使用上次完整包内的可移植字体。"""
    included = root / "fonts" / "NotoSansCJKsc-Regular.otf"
    if included.is_file():
        font_manager.fontManager.addfont(str(included))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(included)).get_name()
    else:
        installed = {f.name for f in font_manager.fontManager.ttflist}
        for family in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC"):
            if family in installed:
                plt.rcParams["font.family"] = family
                break
        else:
            raise RuntimeError("找不到中文字体：请保留原压缩包的 fonts 目录。")
    plt.rcParams.update(
        {"font.size": 10.5, "axes.unicode_minus": False, "pdf.fonttype": 42,
         "svg.fonttype": "none", "savefig.facecolor": "white"}
    )


def load_coordinates(folder: Path) -> dict[str, tuple[float, float]]:
    matches = sorted((folder / "data").glob("调度中心与服务区*.xlsx"))
    if len(matches) != 1:
        raise ValueError(f"需要一个节点表，实际找到 {len(matches)} 个：{matches}")
    wb = openpyxl.load_workbook(matches[0], read_only=True, data_only=True)
    try:
        coords = {}
        for row in wb.active.iter_rows(values_only=True):
            if row and isinstance(row[0], str) and (row[0] == "O01" or row[0].startswith("S")):
                if len(row) >= 4 and isinstance(row[2], (float, int)) and isinstance(row[3], (float, int)):
                    coords[row[0]] = (float(row[2]), float(row[3]))
        if "O01" not in coords or len(coords) < 3:
            raise ValueError("无法从节点表读取 O01 和服务区经纬度。")
        return coords
    finally:
        wb.close()


def local_xy(coords: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """仅把图的坐标换成相对 O01 的局部东、北方向米数；不用于重新求航程。"""
    lon0, lat0 = coords["O01"]
    phi0 = math.radians(lat0)
    radius = 6_371_008.8
    return {
        name: (radius * math.cos(phi0) * math.radians(lon - lon0),
               radius * math.radians(lat - lat0))
        for name, (lon, lat) in coords.items()
    }


def load_plans(folder: Path) -> dict[str, list[dict]]:
    plans = {}
    for name in STYLES:
        file = folder / "results" / name / "solution.json"
        if not file.is_file():
            raise FileNotFoundError(f"找不到方案文件：{file}")
        data = json.loads(file.read_text(encoding="utf-8"))
        if not data.get("audit", {}).get("valid"):
            raise ValueError(f"方案没有通过资源校验：{file}")
        plans[name] = sorted(data["trips"], key=lambda t: (t["start_time"], t["trip_id"]))
    return plans


def representative_trips(trips: list[dict], pair: tuple[str, str]) -> list[dict]:
    """两区同架次时取最早的同架次；否则每区各取最早服务的一架次。"""
    first, second = pair
    together = [t for t in trips if first in t["service_sequence"] and second in t["service_sequence"]]
    if together:
        return [together[0]]
    selected = []
    for site in pair:
        found = next((t for t in trips if site in t["service_sequence"]), None)
        if found is None:
            raise ValueError(f"方案未覆盖 {site}")
        if found not in selected:
            selected.append(found)
    return selected


def balanced_stop_time(trips: list[dict], site: str) -> float:
    times = [float(time) for trip in trips for box, time in trip["box_delivery"].items()
             if box.startswith(site + "-")]
    if not times:
        raise ValueError(f"选定的架次中找不到 {site} 的交接记录")
    # 本题模型规定每架次在同一停点交接完成后统一记送达。
    return min(times) / 60.0


def draw_trip(ax, trip: dict, xy: dict, style: dict) -> None:
    route = ["O01", *trip["service_sequence"], "O01"]
    for origin, destination in zip(route, route[1:]):
        xa, ya = xy[origin]
        xb, yb = xy[destination]
        ax.plot((xa, xb), (ya, yb), color=style["color"],
                ls=style["linestyle"], lw=style["linewidth"],
                solid_capstyle="round", alpha=0.86, zorder=style["zorder"])
        # 在实际直线航段上加小箭头，避免将返航误画成绕行曲线。
        if math.hypot(xb - xa, yb - ya) > 150:
            frac = 0.57
            px, py = xa + frac * (xb - xa), ya + frac * (yb - ya)
            ax.annotate("", xy=(px + 0.05 * (xb - xa), py + 0.05 * (yb - ya)),
                        xytext=(px - 0.05 * (xb - xa), py - 0.05 * (yb - ya)),
                        arrowprops={"arrowstyle": "-|>", "lw": 0,
                                    "mutation_scale": 8, "color": style["color"]},
                        zorder=style["zorder"] + 0.1)


def draw_panel(ax, pair: tuple[str, str], plans: dict, xy: dict,
               letter: str, xlim: tuple[float, float], ylim: tuple[float, float]) -> None:
    selected = {name: representative_trips(trips, pair) for name, trips in plans.items()}
    # 宽蓝线垫底，再画红虚线和绿点线，同路径重合时三种方法仍可辨认。
    for name in ("balanced", "baseline_corrected", "trips"):
        for trip in selected[name]:
            draw_trip(ax, trip, xy, STYLES[name])

    x0, y0 = xy["O01"]
    ax.scatter([x0], [y0], s=90, c="#203A53", marker="D", zorder=10,
               edgecolors="white", linewidths=0.6)
    ax.annotate("O01", (x0, y0), xytext=(6, -13), textcoords="offset points",
                color="#203A53", fontsize=9, weight="bold", zorder=11)
    visited_elsewhere = {
        site for subplan in selected.values() for trip in subplan
        for site in trip["service_sequence"] if site not in pair
    }
    # 薄灰点仅标示位置，代表性架次额外访问的服务区则明确标注。
    for site in xy:
        if site not in ("O01", *pair):
            x, y = xy[site]
            if site in visited_elsewhere:
                ax.scatter(x, y, s=39, facecolors="#EEF2F4",
                           edgecolors="#607986", linewidths=1, zorder=10)
                ax.annotate(site, (x, y), xytext=(5, 6),
                            textcoords="offset points", color="#607986", fontsize=8, zorder=11)
            else:
                ax.scatter(x, y, s=13, c="#AAB8C5", alpha=0.7, zorder=1)
    for site in pair:
        x, y = xy[site]
        ax.scatter([x], [y], s=82, facecolors="white", marker="s",
                   edgecolors="#B35359", linewidths=1.7, zorder=10)
        time_min = balanced_stop_time(selected["balanced"], site)
        # S007 位于两条航段之间，右上标注避免盖住 S003 -> S007 的路径。
        put_right = (x >= x0 or site == "S007")
        shift = (7, 8) if put_right else (-7, 8)
        ax.annotate(f"{site}\n{time_min:.1f} min", (x, y), xytext=shift,
                    textcoords="offset points", ha="left" if put_right else "right",
                    va="bottom", fontsize=8.3, color="#A04449", zorder=12)

    a, b, c = (len(selected[name]) for name in STYLES)
    ax.text(0.03, 0.04,
            f"代表架次：原算法 {a}  |  均衡 {b}  |  精简 {c}",
            transform=ax.transAxes, va="bottom", fontsize=8.2,
            bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "#DDE4E8",
                  "boxstyle": "round,pad=0.35"}, zorder=15)
    ax.set_title(f"({letter})  {pair[0]} 与 {pair[1]}", fontsize=12, pad=11)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("相对 O01 向东 / m")
    ax.set_ylabel("相对 O01 向北 / m")
    ax.grid(color="#A9BCC9", lw=0.65, alpha=0.32)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#879EAD")
        spine.set_linewidth(0.8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="解压后含 data、results 的目录")
    parser.add_argument("--output", type=Path, default=None, help="输出前缀（无后缀）")
    args = parser.parse_args()
    folder = args.root.resolve()
    output = args.output or folder / "results" / "Q2_四组服务区路线对比"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    set_chinese_font(folder)
    xy = local_xy(load_coordinates(folder))
    plans = load_plans(folder)
    xs, ys = zip(*xy.values())
    xpad = (max(xs) - min(xs)) * 0.13
    ypad = (max(ys) - min(ys)) * 0.13
    xlim = (min(xs) - xpad, max(xs) + xpad)
    ylim = (min(ys) - ypad, max(ys) + ypad)

    fig, axes = plt.subplots(2, 2, figsize=(14.8, 11.5), constrained_layout=False)
    for ax, pair, letter in zip(axes.flat, PAIRS, "abcd"):
        draw_panel(ax, pair, plans, xy, letter, xlim, ylim)
    handles = [Line2D([0], [0], color=style["color"], linestyle=style["linestyle"],
                      lw=style["linewidth"], label=style["label"])
               for style in STYLES.values()]
    handles.extend([
        Line2D([0], [0], marker="D", color="none", markerfacecolor="#203A53",
               markeredgecolor="white", markersize=8, label="调度中心 O01"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="white",
               markeredgecolor="#B35359", markersize=8, label="所比服务区"),
    ])
    fig.suptitle("问题二运输无人机路线对比：四组服务区", fontsize=16, y=0.988)
    fig.legend(handles=handles, ncol=3, loc="upper center", frameon=True,
               bbox_to_anchor=(0.5, 0.946), fontsize=10,
               facecolor="white", edgecolor="#C5D1D8")
    fig.subplots_adjust(left=0.065, right=0.98, top=0.84, bottom=0.085,
                        wspace=0.21, hspace=0.20)
    fig.text(0.5, 0.018,
             "仅显示各方案覆盖图中两区的代表架次；节点旁分钟数为均衡方案所选架次的交接完成时刻。"
             "全部货箱与架次见逐箱交付表。",
             ha="center", fontsize=9, color="#465D6A")
    for ext in ("pdf", "svg", "png"):
        dest = output.with_suffix("." + ext)
        pending = output.parent / ("." + output.name + ".pending." + ext)
        fig.savefig(pending, format=ext, dpi=200, facecolor="white")
        pending.replace(dest)
        print(dest)
    plt.close(fig)


if __name__ == "__main__":
    main()
