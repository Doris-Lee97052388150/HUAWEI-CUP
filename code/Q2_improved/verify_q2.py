"""独立复算导出的调度结果；与求解器的搜索及RouteEvaluator缓存无关。
python verify_q2.py --root 数据目录 --solution results/balanced/solution.json
"""
from pathlib import Path
from types import SimpleNamespace
import argparse,json
import q2_solver_improved as q

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--solution',type=Path,required=True)
    p.add_argument('--report',type=Path)
    a=p.parse_args()
    nodes=q.load_nodes(q.choose_file(a.root,'调度中心与服务区',['.xlsx']))
    boxes=q.load_boxes(q.choose_file(a.root,'物资需求与配送时限',['.xlsx']))
    types,units,batteries=q.load_drone_data(q.choose_file(a.root,'运输无人机数据',['.xlsx']))
    arcs=q.ArcLibrary(nodes,types,q.choose_file(a.root,'镇龙乡及周边30米DEM',['.tif','.tiff','.mat']))
    payload=json.loads(a.solution.read_text(encoding='utf-8'))
    decoded=SimpleNamespace(trips=[SimpleNamespace(**t) for t in payload['trips']])
    report,_,_=q.independent_audit(decoded,nodes,boxes,types,units,batteries,arcs)
    if a.report: a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['valid'] else 1)

if __name__=='__main__': main()
