# 问题二改进代码与模型

已核对题目与两篇文献，并使用本次附件运行完成。推荐查看 Q2_model.pdf 和 Q2_results.xlsx。

## 主要结果

|方案|架次|最晚返航（分钟）|能耗（kWh）|期望时刻内送达箱数|
|---|---:|---:|---:|---:|
|原ALNS，统一地形|20|159.72|64.5185|77/80|
|改进均衡|20|137.48|61.8969|80/80|
|减少架次|17|155.22|60.6636|75/80|

所有方案的医疗和首批硬时限均满足。均衡方案最低SOC为 20.971%。不同预算的实跑对比不是同时间算法排名；本程序未证明原问题全局最优。

## 运行

Python 3.10及以上，依赖 numpy、scipy>=1.11、openpyxl（读取输入）、Pillow、matplotlib。无需Gurobi、CPLEX、MATLAB或结果提交模板。

```bash
python -m pip install -r requirements.txt
python q2_solver_improved.py --root data --output new_results --iters 500 --seeds 20260924 20260925 --profiles balanced time energy trips --milp-seconds 90 --step 60 --pool-size 240 --warm-routes baseline_routes.json
```

Windows可直接使用 RUN_WINDOWS.bat。数据目录包含三个Excel，以及数值一致的TIF和MAT；程序优先读取TIF，MAT不是必需输入。也支持单独使用带经纬度数组的MAT。不要把多个同格式数据版本放在同一个输入目录。

较快试跑：
```bash
python q2_solver_improved.py --root data --output quick_results --iters 100 --seeds 20260924 --profiles balanced --milp-seconds 20 --step 120
```

不提供 `--warm-routes` 时可独立构造初始解。`--milp-seconds 0` 只运行路线搜索；`--no-plots` 关闭绘图。程序输出CSV与JSON，Excel可直接打开CSV。本包的XLSX是本次已计算结果，不会因修改单元格自动重跑优化。

## 独立复算

```bash
python verify_q2.py --root data --solution results/balanced/solution.json
```

返回 valid=true 才通过。检查全部80箱恰好一次、载质量/体积、原非线性能耗、硬时限、具体ID合法性、SOC、充电及资源占用。该审计重新计算，不信任路线评价缓存。

## 文件说明

- q2_solver_improved.py：可单独复制使用的完整求解器。
- verify_q2.py：导出方案复算入口。
- Q2_model.tex / Q2_results_tables.tex / Q2_model.pdf：完整模型、文献适用性、代码核查及本次结果。在本目录使用XeLaTeX编译模型两遍；所需中文字体及其许可证已放在 fonts 目录。
- Q2_results.xlsx：均衡方案明细及对比；其他偏好逐箱与架次表见 results 各子目录。
- results/balanced：推荐均衡方案；time、energy与其相同，trips为17架次权衡方案。
- results/baseline_corrected：保留原ALNS与解码器，仅统一地形计算的对比方案。
- results/run_summary.json：参数、尺度、种子、输入SHA256及各MILP子问题证书。
- results/arc_library.csv：240条有向航段及实际经过像元数量。
- results 中的PDF/SVG/PNG：按机型分图的路线、无人机甘特、电池甘特和逐箱时限。
- plot_q2_reference_style.py 与 results/Q2_四组服务区路线对比.*：参考所给四宫格示例制作的局部路线对比图，输出可直接插入论文的PDF/SVG和预览PNG。依次比较 S001–S013、S001–S002、S007–S003、S011–S008。红虚线代表原ALNS，蓝实线代表改进均衡，绿点线代表17架次方案；所写分钟数仅指均衡方案所选代表架次在该停点的交接完成时间。脚本在本目录运行：`python plot_q2_reference_style.py`。四幅图只显示覆盖相应两区的代表架次，完整20/20/17架次路线和全部逐箱送达时间见结果表。
- plot_q2_paper_figures.py：论文主图的完整绘图代码。在本目录运行 `python plot_q2_paper_figures.py`，读取现有三方案JSON与节点表，生成 results/paper_figures 下的四幅PDF、SVG、PNG。图1按A/B/C机型绘出全部20架次航段，重叠航段标频次；图2列出8架实体无人机、全部14块共享电池及充电周转；图3列出15区的全部停点交接事件、硬截止和80箱期望与实际时间；图4同时展示迟到、最后返航、能耗、架次数的相对值与原始值。图1的平面坐标仅用于作图，不替代DEM航段的地形能耗计算；图2红色竖线为最后返航，不是所有电池充满时间。
- benchmark_baseline.py / reference/q2_solver_original.py：复现原算法基线，读取相同数据但统一真实端点地形口径。

## 实现边界

1. 医疗期望时间和首批截止时间为硬约束，其他期望时间为迟到评价，不一律硬化。
2. 按实际像元最高高程+50米逐航段计算；服务区每次从地面+30米重新爬升。
3. 电池从准备开始占用，任务后充满后才能复用；不同电池并行充电，不虚构有限充电桩。
4. 同一停点货箱统一在该点交接结束时送达，这是保守口径。
5. 候选路线允许最多15个不同服务区，当前不允许同架次重复服务区，不声称此限制已证明无损。
6. 大于5点时不做全排列；由插入移除算子探索顺序。
7. MILP的gap仅适用于该候选路线池、开始时间网格和时域，不是原问题全局gap。压紧后的连续开始时刻不能直接套用压紧前gap。
8. 本次大候选池阶段未在时限内找到可用整数解；实际改进来自ALNS与固定路线机型/时序优化。所有阶段保留原可行解，避免超时覆盖有效结果。
9. 两篇论文的能耗或充电模型不替代题目规则，也未添加通信、道路、租金或运输悬停功耗。
10. 启发式固定种子可复现；有限时间整数规划可能受CPU与求解器版本影响，已保存方案可直接校验，无需重跑。
