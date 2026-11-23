#!/usr/bin/env bash
# 演示脚本:对 http://localhost:8000 走一遍完整流程(批次 -> 核对 -> 发布 -> 按工位取数)。
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

echo "== 1. 建批:登记两个工位(新/旧终端)+ 两个项目(对称/非对称)+ 领用 =="
BATCH=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "初始下发",
  "ops": [
    {"op": "upsert_item", "item_code": "ITEM-DIA-40", "name": "主轴孔直径", "nominal_um": 40000, "upper_um": 40008, "lower_um": 39992},
    {"op": "upsert_item", "item_code": "ITEM-LEN-120", "name": "轴肩长度", "nominal_um": 120000, "upper_um": 120010, "lower_um": 119995},
    {"op": "upsert_workstation", "workstation_code": "WS-01", "name": "一号加工中心", "terminal_generation": "new"},
    {"op": "upsert_workstation", "workstation_code": "WS-02", "name": "二号车床", "terminal_generation": "old"},
    {"op": "claim_item", "workstation_code": "WS-01", "item_code": "ITEM-DIA-40"},
    {"op": "claim_item", "workstation_code": "WS-01", "item_code": "ITEM-LEN-120"},
    {"op": "claim_item", "workstation_code": "WS-02", "item_code": "ITEM-DIA-40"}
  ]}')
echo "$BATCH"
BID=$(echo "$BATCH" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo; echo "== 2. 核对 =="
curl -sS -X POST "$BASE/batches/$BID/verify"

echo; echo "== 3. 发布 =="
curl -sS -X POST "$BASE/batches/$BID/publish"

echo; echo "== 4. 旧终端领用非对称项目(ITEM-LEN-120 为 +10/-5)→ 核对退回 =="
BATCH2=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "旧终端领用非对称项目",
  "ops": [{"op": "claim_item", "workstation_code": "WS-02", "item_code": "ITEM-LEN-120"}]}')
BID2=$(echo "$BATCH2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sS -X POST "$BASE/batches/$BID2/verify"

echo; echo "== 5. 按工位取数:新终端拿上下限,旧终端拿对称公差 =="
curl -sS "$BASE/workstations/WS-01/spec"
echo
curl -sS "$BASE/workstations/WS-02/spec"
echo
