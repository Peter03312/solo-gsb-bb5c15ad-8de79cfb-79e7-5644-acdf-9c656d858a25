#!/usr/bin/env bash
# 端到端演示：登记 -> 加工 -> 残留继承 -> 清洗 -> 撤销 -> 声明更正 -> 标签签发
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
req() { # METHOD PATH JSON
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}

step "1. 登记原料批次（含过敏原 / 待确认）"
req POST /materials '{"batch_id":"RM-MILK","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-OAT","allergens":["gluten"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-MYST","pending":true,"declared_at":"2026-01-02T06:00:00Z"}'

step "2. 登记加工批次（EQ-1 共线：先奶制品，后燕麦品）"
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-1","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-MILK-1"]}'
req POST /processings '{"processing_id":"P2","equipment_id":"EQ-1","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-OAT","RM-MYST"],"outputs":["OUT-OAT-1"]}'

step "3. 溯源 OUT-OAT-1：gluten 来自投入，milk 来自设备残留，pending 来自 RM-MYST"
req GET /batches/OUT-OAT-1/trace

step "4. 登记两次加工之间的已验证清洗 -> 残留被清除"
req POST /cleanings '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:30:00Z","verified":true}'
req GET /batches/OUT-OAT-1/trace

step "5. 撤销清洗 -> 未签发结论实时重算，milk 残留恢复"
req POST /cleanings/1/revoke
req GET /batches/OUT-OAT-1/trace

step "6. 原料声明更正：RM-OAT 实际含 sesame"
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame"]}'
req GET /batches/OUT-OAT-1/trace

step "7. 签发标签（冻结当前结论）；再更正声明后签发关联旧版的新标签"
req POST /batches/OUT-OAT-1/labels '{}'
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame","soy"]}'
req POST /batches/OUT-OAT-1/labels '{"supersedes_id":1}'
step "旧标签保持不变"
req GET /labels/1
step "新标签反映最新结论"
req GET /labels/2

step "8. 非法登记示例（均被拒绝且不影响已有数据）"
req POST /processings '{"processing_id":"P-BAD","equipment_id":"EQ-1","start_utc":"2026-01-02T08:30:00Z","end_utc":"2026-01-02T09:30:00Z","inputs":["RM-OAT"],"outputs":["OUT-X"]}'
req POST /processings '{"processing_id":"P-LOOP","equipment_id":"EQ-9","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-OAT"],"outputs":["RM-OAT"]}'
req POST /processings '{"processing_id":"P-TIME","equipment_id":"EQ-9","start_utc":"2026-01-02T09:00:00Z","end_utc":"2026-01-02T08:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-Y"]}'

step "9. 声明更正预演（不落库）：查看差异与已签发标签偏差，取得提交凭据"
req POST /materials/RM-OAT/declarations/preview '{"allergens":["gluten","sesame","tree-nut"]}'

step "10. 清洗撤销预演（不落库）：设备残留恢复，含下游批次与标签差异"
# 先补一条 EQ-1 的已验证清洗（步骤 5 的清洗已被直接撤销）
CID="$(curl -sS -X POST "$BASE/cleanings" -H 'Content-Type: application/json' \
  -d '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:20:00Z","verified":true}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
req POST "/cleanings/$CID/revoke/preview"

step "11. 凭预演凭据原子提交（取上一步返回的 preview_id）"
PID="$(curl -sS -X POST "$BASE/cleanings/$CID/revoke/preview" | python3 -c 'import sys,json;print(json.load(sys.stdin)["preview_id"])')"
req POST "/previews/$PID/commit"
step "已签发标签仍不改写"
req GET /labels/1
step "凭据一次性：再次提交将被拒绝"
req POST "/previews/$PID/commit"
