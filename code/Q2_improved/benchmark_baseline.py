"""统一地形后复现用户原算法基线；不执行原文件的固定路径主程序。"""
from pathlib import Path
import argparse,importlib.util,json,os,sys
import q2_solver_improved as q

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent/'data')
    ap.add_argument('--output',type=Path,default=Path('baseline_reproduction'))
    ap.add_argument('--iters',type=int,default=800);ap.add_argument('--seed',type=int,default=20260924)
    a=ap.parse_args()
    if os.environ.get('PYTHONHASHSEED')!='0':
        print('提示：原程序使用无序字符串集合；复现本包基线请先设置 PYTHONHASHSEED=0。')
    source=Path(__file__).resolve().parent/'reference/q2_solver_original.py'
    spec=importlib.util.spec_from_file_location('q2_original',source)
    old=importlib.util.module_from_spec(spec);sys.modules['q2_original']=old;spec.loader.exec_module(old)
    n=q.load_nodes(q.choose_file(a.root,'调度中心与服务区',['.xlsx']))
    b=q.load_boxes(q.choose_file(a.root,'物资需求与配送时限',['.xlsx']))
    dt,du,bs=q.load_drone_data(q.choose_file(a.root,'运输无人机数据',['.xlsx']))
    arcs=q.ArcLibrary(n,dt,q.choose_file(a.root,'镇龙乡及周边30米DEM',['.tif','.tiff','.mat']))
    ev=old.RouteEvaluator(b,dt,arcs);r=old.construct_initial_routes(b,ev);decoder=old.ResourceDecoder(ev,du,bs,b)
    r,d,m,h=old.alns_optimize(r,b,n,ev,decoder,a.iters,a.seed)
    q.export_solution(d,'baseline_corrected',a.output,n,b,dt,du,bs,arcs)
    (a.output/'baseline_routes.json').write_text(json.dumps([[(s.service_id,s.box_ids) for s in rr.stops] for rr in r]),encoding='utf-8')
    print(m)

if __name__=='__main__':main()
