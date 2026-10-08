"""P6 world model (docs/DESIGN-worldmodel-P6.md): step dataset, action classifier, baselines,
Markov / Zeno layer, JEPA (MLX) and the G1 evaluation.

Optional component: numpy and scipy for the baselines, mlx for the JEPA. Modules are imported on
demand; nothing here imports mlx or numpy at package import time, and nothing in the routing core
imports this package.
"""
