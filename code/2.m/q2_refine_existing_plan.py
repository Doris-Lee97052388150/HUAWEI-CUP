"""对已审计的问题二方案做资源感知的路线局部改进。

以已有 solution.json 为起点，保持每个留存架次的具体机型、无人机和电池，
搜索货箱迁移、跨架次交换、整批合并及服务区顺序调整；每个候选解都重算
逐航段载荷/DEM能耗、逐箱送达、返航 SOC、无人机占用和电池充满后复用。
此程序是局部启发式，不提供原问题全局最优性证明。

    python q2_refine_existing_plan.py --iterations 6000 --seed 20260925
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import random

import q2_solver_improved as q2


ROOT = Path(__file__).resolve().parent


@dataclass
class AssignedRoute:
    route: q2.Route
    drone_id: str
    drone_type: str
    battery_id: str
    original_start: float

    def clone(self) -> "AssignedRoute":
        return AssignedRoute(self.route.clone(), self.drone_id, self.drone_type,
                             self.battery_id, self.original_start)


def read_assignments(path: Path) -> list[AssignedRoute]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not result.get("audit", {}).get("valid"):
        raise ValueError("输入解未标记为独立验算通过")
    return [AssignedRoute(
        q2.Route([q2.Stop(s["service_id"], list(s["box_ids"]))
                  for s in trip["route_eval"]["stops"]]),
        trip["drone_id"], trip["drone_type"], trip["battery_id"],
        trip["start_time"])
        for trip in sorted(result["trips"], key=lambda t: (t["start_time"], t["trip_id"]))]


def replay(assignments, evaluator, batteries, boxes):
    """在原有实体和电池先后关系上尽早启动；返回精确评价与解。"""
    drone_free, battery_free, trips = {}, {}, []
    if not assignments:
        return None
    for idx, task in enumerate(assignments):
        ev = evaluator.evaluate(task.route, task.drone_type)
        if not ev.feasible:
            return None
        start = max(drone_free.get(task.drone_id, 0.0),
                    battery_free.get(task.battery_id, 0.0))
        if start > ev.latest_start + 1e-7:
            return None
        finish = start + ev.duration
        charged = finish + q2.charge_time_seconds(
            ev.end_soc, batteries[task.drone_type].full_charge_time)
        delivery = {bid: start + delta for bid, delta in ev.box_delivery_offsets.items()}
        trips.append(q2.ScheduledTrip(
            idx, task.drone_id, task.drone_type, task.battery_id, start,
            start + ev.takeoff_offset, finish, ev.total_energy, ev.end_soc,
            charged, task.route.service_ids(), delivery, ev))
        drone_free[task.drone_id], battery_free[task.battery_id] = finish, charged
    dec = q2.finish_decoding(trips)
    if len(dec.box_delivery) != len(boxes) or set(dec.box_delivery) != set(boxes):
        return None
    return dec


def remove_box(route: q2.Route, bid: str) -> None:
    for stop in route.stops:
        if bid in stop.box_ids:
            stop.box_ids.remove(bid)
            route.stops = [s for s in route.stops if s.box_ids]
            return
    raise ValueError(f"货箱不在路线中：{bid}")


def put_box(route: q2.Route, box: q2.Box, rng: random.Random) -> None:
    matches = [s for s in route.stops if s.service_id == box.service_id]
    if matches:
        matches[0].box_ids.append(box.box_id)
    else:
        route.stops.insert(rng.randrange(len(route.stops) + 1),
                           q2.Stop(box.service_id, [box.box_id]))
    route.normalize()


def neighbor(current, boxes, rng):
    candidate = [task.clone() for task in current]
    kind = rng.choices(("relocate", "swap", "reorder", "merge"),
                       weights=(43, 28, 12, 17))[0]
    if kind == "reorder":
        eligible = [i for i, t in enumerate(candidate) if len(t.route.stops) > 1]
        if not eligible:
            return None, kind
        task = candidate[rng.choice(eligible)]
        i, j = sorted(rng.sample(range(len(task.route.stops)), 2))
        if rng.random() < .5:
            task.route.stops[i:j + 1] = list(reversed(task.route.stops[i:j + 1]))
        else:
            task.route.stops[i], task.route.stops[j] = task.route.stops[j], task.route.stops[i]
    elif kind == "merge":
        if len(candidate) < 2:
            return None, kind
        eligible = [i for i, t in enumerate(candidate) if len(t.route.box_ids()) <= 8]
        if not eligible:
            return None, kind
        src = rng.choice(eligible)
        dst = rng.choice([i for i in range(len(candidate)) if i != src])
        if (candidate[src].drone_type != candidate[dst].drone_type
                and rng.random() < .1):
            return None, kind
        target = candidate[dst].route
        block = [q2.Stop(s.service_id, list(s.box_ids)) for s in candidate[src].route.stops]
        pos = rng.randrange(len(target.stops) + 1)
        target.stops[pos:pos] = block
        target.normalize()
        candidate.pop(src)
    elif kind == "relocate":
        if len(candidate) < 2:
            return None, kind
        src, dst = rng.sample(range(len(candidate)), 2)
        bid = rng.choice(candidate[src].route.box_ids())
        remove_box(candidate[src].route, bid)
        put_box(candidate[dst].route, boxes[bid], rng)
        if not candidate[src].route.stops:
            candidate.pop(src)
    else:
        if len(candidate) < 2:
            return None, kind
        src, dst = rng.sample(range(len(candidate)), 2)
        left = rng.choice(candidate[src].route.box_ids())
        right = rng.choice(candidate[dst].route.box_ids())
        remove_box(candidate[src].route, left)
        remove_box(candidate[dst].route, right)
        put_box(candidate[src].route, boxes[right], rng)
        put_box(candidate[dst].route, boxes[left], rng)
    return candidate, kind


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--solution", type=Path, default=None)
    ap.add_argument("--summary", type=Path, default=None,
                    help="原求解器的 run_summary.json；默认先找输入解所在结果目录")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--iterations", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--profile", choices=list(q2.PROFILES), default="balanced")
    args = ap.parse_args()
    if args.iterations < 0:
        ap.error("iterations 必须非负")
    root = args.root.resolve()
    source = (args.solution or root / "results" / args.profile / "solution.json").resolve()
    out = (args.output or root / "results").resolve()
    nodes = q2.load_nodes(q2.choose_file(root / "data", "调度中心与服务区", [".xlsx"]))
    boxes = q2.load_boxes(q2.choose_file(root / "data", "物资需求与配送时限", [".xlsx"]))
    types, units, batteries = q2.load_drone_data(
        q2.choose_file(root / "data", "运输无人机数据", [".xlsx"]))
    arcs = q2.ArcLibrary(nodes, types,
        q2.choose_file(root / "data", "镇龙乡及周边30米DEM", [".tif", ".tiff", ".mat"]))
    evaluator = q2.RouteEvaluator(boxes, types, arcs)
    source_items = read_assignments(source)
    valid_drones = {u.drone_id: u.type_id for u in units}
    valid_batteries = {f"BAT-{g}{i:02d}": g
                       for g, b in batteries.items() for i in range(1, b.count + 1)}
    for item in source_items:
        if (valid_drones.get(item.drone_id) != item.drone_type
                or valid_batteries.get(item.battery_id) != item.drone_type):
            raise ValueError("输入解引用了错误机型的无人机或电池")
    q2.ensure_route_integrity([t.route for t in source_items], boxes)
    best = replay(source_items, evaluator, batteries, boxes)
    if best is None:
        raise ValueError("输入解无法按照现有资源顺序重放")
    summary_candidates = [args.summary] if args.summary else [
        source.parent.parent / "run_summary.json", root / "results" / "run_summary.json"]
    summary_file = next((p for p in summary_candidates if p is not None and p.exists()), None)
    if summary_file is None:
        raise FileNotFoundError("找不到对应原解的 run_summary.json；请指定 --summary")
    scales = json.loads(summary_file.read_text(encoding="utf-8"))["scales"]
    weights = q2.PROFILES[args.profile]
    before = q2.compute_metrics(best, boxes)
    best_score = q2.objective(before, scales, weights)
    initial = q2.independent_audit(best, nodes, boxes, types, units, batteries, arcs)[0]
    if not initial["valid"]:
        raise ValueError(f"基线重放未通过独立校验：{initial['errors']}")
    rng = random.Random(args.seed)
    best_items = source_items
    counts = {k: 0 for k in ("relocate", "swap", "reorder", "merge")}
    accepted = 0
    for iteration in range(args.iterations):
        trial, kind = neighbor(best_items, boxes, rng)
        if trial is None:
            continue
        counts[kind] += 1
        dec = replay(trial, evaluator, batteries, boxes)
        if dec is None:
            continue
        score = q2.objective(q2.compute_metrics(dec, boxes), scales, weights)
        if score < best_score - 1e-10:
            best, best_items, best_score = dec, trial, score
            accepted += 1
        if len(evaluator.cache) > 150000:
            evaluator.cache.clear()
    final_audit = q2.independent_audit(best, nodes, boxes, types, units, batteries, arcs)[0]
    if not final_audit["valid"]:
        raise AssertionError(final_audit["errors"])
    name = "refined_" + args.profile
    payload = q2.export_solution(best, name, out, nodes, boxes, types, units, batteries, arcs)
    summary = {
        "method": "fixed-resource-order feasible local search",
        "source": str(source), "seed": args.seed, "iterations": args.iterations,
        "neighbors_by_type": counts, "accepted": accepted,
        "before": vars(before), "after": payload["metrics"],
        "before_objective": q2.objective(before, scales, weights),
        "after_objective": best_score, "global_optimality_proven": False,
        "note": "保留原有机型和每个资源的任务先后关系；最终解经过独立物理与资源审计。",
    }
    (out / name / "refinement_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
