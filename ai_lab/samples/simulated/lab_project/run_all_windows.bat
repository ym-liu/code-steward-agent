@echo off
REM same as run_all.sh but for the lab Windows PC
set CONFIG=configs\base.yaml
if not "%1"=="" set CONFIG=%1
for %%s in (1 2 3) do python train.py --config %CONFIG% --seed %%s
python evaluate.py checkpoints\model_seed1.pt checkpoints\model_seed2.pt checkpoints\model_seed3.pt --config %CONFIG%
pause
