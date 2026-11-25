#!/usr/bin/env bash
# End-to-end smoke test against a running server (default 127.0.0.1:8000).
# Covers: batch accept, bidirectional query, whole-batch conflict rejection
# with earliest date and two rule paths, inclusive boundary days, and
# concurrent submissions.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
ITEM="E2E-$(date +%s)"
pass=0; fail=0

check() { # desc, condition(0=ok)
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; pass=$((pass+1)); else echo "FAIL: $1"; fail=$((fail+1)); fi
}

post() { curl -s -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' -d "$1"; }
get() { curl -s -G "$BASE/api/v1/conversion" --data-urlencode "item_code=$ITEM" "$@"; }

echo "== 1. 报送基础规则 1件=8盒, 1盒=6套 =="
body=$(post "{\"item_code\":\"$ITEM\",\"rules\":[
 {\"rule_code\":\"R1\",\"from_unit\":\"件\",\"to_unit\":\"盒\",\"from_amount\":1,\"to_amount\":8,\"start_date\":\"2026-01-01\",\"end_date\":\"2026-12-31\"},
 {\"rule_code\":\"R2\",\"from_unit\":\"盒\",\"to_unit\":\"套\",\"from_amount\":1,\"to_amount\":6,\"start_date\":\"2026-01-01\",\"end_date\":\"2026-12-31\"}]}")
echo "$body"
echo "$body" | python3 -c "import sys,json; assert json.load(sys.stdin)['status']=='accepted'" && check "批次1 accepted" 0 || check "批次1 accepted" 1

echo "== 2. 多跳精确倍率 件->套 = 48/1，反向 套->件 = 1/48 =="
get --data-urlencode "shipping_date=2026-06-01" --data-urlencode "from_unit=件" --data-urlencode "to_unit=套" | tee /tmp/e2e_q1.json
python3 -c "import json; assert json.load(open('/tmp/e2e_q1.json'))['factor']=='48/1'" && check "件->套=48/1" 0 || check "件->套=48/1" 1
get --data-urlencode "shipping_date=2026-06-01" --data-urlencode "from_unit=套" --data-urlencode "to_unit=件" | tee /tmp/e2e_q2.json
python3 -c "import json; assert json.load(open('/tmp/e2e_q2.json'))['factor']=='1/48'" && check "套->件=1/48" 0 || check "套->件=1/48" 1

echo "== 3. 冲突批次整批退回，给出最早冲突日与两条路径 =="
body=$(post "{\"item_code\":\"$ITEM\",\"rules\":[
 {\"rule_code\":\"R3\",\"from_unit\":\"件\",\"to_unit\":\"套\",\"from_amount\":1,\"to_amount\":12,\"start_date\":\"2026-03-01\",\"end_date\":\"2026-10-01\"}]}")
echo "$body" | python3 -m json.tool
echo "$body" | python3 -c "import sys,json; assert json.load(sys.stdin)['status']=='rejected'" && check "冲突批次 rejected" 0 || check "冲突批次 rejected" 1
echo "$body" | python3 -c "import sys,json; assert json.load(sys.stdin)['conflict']['conflict_date']=='2026-03-01'" && check "最早冲突日 2026-03-01" 0 || check "最早冲突日" 1
echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['conflict']['path_a']['rule_codes']==['R1','R2']" && check "路径A规则 R1,R2" 0 || check "路径A" 1
echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['conflict']['path_b']['rule_codes']==['R3']" && check "路径B规则 R3" 0 || check "路径B" 1

echo "== 4. 无可用换算给出提示 =="
get --data-urlencode "shipping_date=2026-06-01" --data-urlencode "from_unit=件" --data-urlencode "to_unit=托" | tee /tmp/e2e_q3.json
python3 -c "import json; assert json.load(open('/tmp/e2e_q3.json'))['available'] is False" && check "无换算 available=false" 0 || check "无换算" 1
python3 -c "import json; assert '没有可从' in json.load(open('/tmp/e2e_q3.json'))['message']" && check "无换算提示语" 0 || check "提示语" 1

echo "== 5. 首尾日相碰：新规则起日 = 旧规则止日，当天冲突 =="
ITEM2="E2E-B-${ITEM}"
post "{\"item_code\":\"$ITEM2\",\"rules\":[
 {\"rule_code\":\"B1\",\"from_unit\":\"件\",\"to_unit\":\"盒\",\"from_amount\":1,\"to_amount\":8,\"start_date\":\"2026-01-01\",\"end_date\":\"2026-06-30\"}]}" >/dev/null
body=$(post "{\"item_code\":\"$ITEM2\",\"rules\":[
 {\"rule_code\":\"B2\",\"from_unit\":\"件\",\"to_unit\":\"盒\",\"from_amount\":1,\"to_amount\":10,\"start_date\":\"2026-06-30\",\"end_date\":\"2026-12-31\"}]}")
echo "$body"
echo "$body" | python3 -c "import sys,json; assert json.load(sys.stdin)['conflict']['conflict_date']=='2026-06-30'" && check "首尾相碰冲突日=止日当天" 0 || check "首尾相碰" 1

echo "== 6. 同时送审两个冲突批次（15 轮并发）=="
ok_accept=0; ok_reject=0
for round in $(seq 1 15); do
  IC="E2E-C-${ITEM}-${round}"
  b1="{\"item_code\":\"$IC\",\"rules\":[{\"rule_code\":\"C1\",\"from_unit\":\"件\",\"to_unit\":\"盒\",\"from_amount\":1,\"to_amount\":8,\"start_date\":\"2026-01-01\",\"end_date\":\"2026-12-31\"}]}"
  b2="{\"item_code\":\"$IC\",\"rules\":[{\"rule_code\":\"C2\",\"from_unit\":\"件\",\"to_unit\":\"盒\",\"from_amount\":1,\"to_amount\":10,\"start_date\":\"2026-01-01\",\"end_date\":\"2026-12-31\"}]}"
  f1="/tmp/e2e_c1_$round.json"; f2="/tmp/e2e_c2_$round.json"
  curl -s -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' -d "$b1" -o "$f1" &
  curl -s -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' -d "$b2" -o "$f2" &
  wait
  s1=$(python3 -c "import json;print(json.load(open('$f1'))['status'])" 2>/dev/null || echo ERROR)
  s2=$(python3 -c "import json;print(json.load(open('$f2'))['status'])" 2>/dev/null || echo ERROR)
  if { [ "$s1" = accepted ] && [ "$s2" = rejected ]; } || { [ "$s2" = accepted ] && [ "$s1" = rejected ]; }; then
    ok_accept=$((ok_accept+1))
  else
    ok_reject=$((ok_reject+1)); echo "  轮次$round 异常: $s1 / $s2"
  fi
done
[ "$ok_reject" -eq 0 ] && check "15 轮并发均恰有一接受一退回 ($ok_accept/15)" 0 || check "并发串行化 ($ok_accept/15)" 1

echo "== 7. /docs 与 openapi.json 可访问 =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/docs")
[ "$code" = 200 ] && check "/docs HTTP 200" 0 || check "/docs ($code)" 1
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/openapi.json")
[ "$code" = 200 ] && check "/openapi.json HTTP 200" 0 || check "/openapi.json ($code)" 1

echo
echo "结果: PASS=$pass FAIL=$fail"
exit "$fail"
