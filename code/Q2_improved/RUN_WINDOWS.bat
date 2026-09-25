@echo off
cd /d "%~dp0"
python q2_solver_improved.py --root data --output new_results --iters 500 --seeds 20260924 20260925 --profiles balanced time energy trips --milp-seconds 90 --step 60 --pool-size 240 --warm-routes baseline_routes.json
python verify_q2.py --root data --solution results/balanced/solution.json
pause
