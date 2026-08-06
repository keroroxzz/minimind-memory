#!/bin/bash
# 等 v_only（PID 由 $1 給）跑完後接著跑 k_only 反向對照。
# BRIDGE_NEXT.md 記過：曾不小心啟兩份同參數的 run。這裡**硬性擋重複**。
set -u
cd "$(dirname "$0")"
WAIT_PID="$1"

while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 30; done
sleep 10

# 守門：任何 bridge_delivery.py 的 python 行程還在，就不啟動
if pgrep -f 'envs/sd/bin/python bridge_delivery.py' >/dev/null; then
  echo "ABORT: 已有 bridge_delivery.py 在跑，不啟動 k_only" >&2
  exit 1
fi
if [ -f bridge_delivery_f8k.pth ]; then
  echo "ABORT: bridge_delivery_f8k.pth 已存在，不覆蓋" >&2
  exit 1
fi

exec /home/rtu/miniconda3/envs/sd/bin/python bridge_delivery.py \
  --steps 12000 --bs 32 --n-eval 200 --eval-js 0 1 2 \
  --nfreq 8 --mode k_only --tag _f8k
