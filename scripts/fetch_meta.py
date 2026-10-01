"""
Home and Heal 메타 광고 데이터 수집 스크립트

- accounts.json 에 등록된 광고 계정의 인사이트를 일자 x 광고세트 단위로 가져온다.
- 자사몰 계정(type=own): 일반 전환 지표(actions / action_values)를 사용
- 협력광고 계정(type=collab): 공유 항목 지표(catalog_segment_actions / catalog_segment_value)를 사용
- 결과는 data/daily.csv, data/daily.json 에 저장 (같은 날짜+광고세트는 최신 값으로 덮어씀)

환경 변수
  META_ACCESS_TOKEN  (필수) 시스템 사용자 토큰
  META_API_VERSION   (선택) 기본 v26.0 — Graph API 탐색기의 최신 버전으로 바꿔도 됨
  LOOKBACK_DAYS      (선택) 기본 7 — 기여 기간 동안 전환이 늦게 잡히므로 최근 N일을 매번 다시 받음
  SINCE / UNTIL      (선택) YYYY-MM-DD — 지정하면 LOOKBACK_DAYS 대신 이 기간을 받음 (과거 데이터 백필용)
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
KST = timezone(timedelta(hours=9))

TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
API_VERSION = os.environ.get("META_API_VERSION", "v26.0").strip()
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7") or 7)
SINCE = os.environ.get("SINCE", "").strip()
UNTIL = os.environ.get("UNTIL", "").strip()

BASE = f"https://graph.facebook.com/{API_VERSION}"

FIELDS = [
    "campaign_id", "campaign_name", "adset_id", "adset_name",
    "spend", "impressions", "reach", "clicks", "inline_link_clicks",
    "actions", "action_values",
    "catalog_segment_actions", "catalog_segment_value",
]

# 우선순위대로 찾는다 (첫 번째로 존재하는 값 사용)
PURCHASE_KEYS = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
ATC_KEYS = ["omni_add_to_cart", "add_to_cart", "offsite_conversion.fb_pixel_add_to_cart"]
VIEW_KEYS = ["omni_view_content", "view_content", "offsite_conversion.fb_pixel_view_content"]

COLUMNS = [
    "date", "account_id", "account_name", "account_type",
    "campaign_id", "campaign_name", "adset_id", "adset_name",
    "spend", "impressions", "reach", "clicks", "link_clicks",
    "view_content", "add_to_cart", "add_to_cart_value", "purchases", "purchase_value",
    "pixel_purchases", "pixel_purchase_value",
]


def pick(action_list, keys):
    """actions 형태의 리스트에서 keys 우선순위로 값을 찾아 float 로 반환"""
    if not action_list:
        return 0.0
    by_type = {a.get("action_type"): a.get("value") for a in action_list}
    for k in keys:
        if k in by_type and by_type[k] not in (None, ""):
            try:
                return float(by_type[k])
            except ValueError:
                return 0.0
    return 0.0


def api_get(url, params, retries=5):
    for attempt in range(retries):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {"message": r.text}
        code = err.get("code")
        # 호출 한도 / 일시 오류는 기다렸다가 재시도
        if code in (1, 2, 4, 17, 32, 613, 80000, 80004) or r.status_code >= 500:
            wait = 30 * (attempt + 1)
            print(f"  일시 오류(code={code}), {wait}초 후 재시도: {err.get('message')}")
            time.sleep(wait)
            continue
        raise RuntimeError(f"API 오류 (HTTP {r.status_code}, code={code}): {err.get('message')}")
    raise RuntimeError("재시도 횟수 초과")


def fetch_account(acc, since, until):
    url = f"{BASE}/act_{acc['id']}/insights"
    params = {
        "access_token": TOKEN,
        "level": "adset",
        "fields": ",".join(FIELDS),
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": 1,
        "limit": 500,
    }
    rows = []
    while True:
        res = api_get(url, params)
        rows.extend(res.get("data", []))
        nxt = res.get("paging", {}).get("next")
        if not nxt:
            break
        url, params = nxt, {}  # next URL 에 파라미터가 모두 들어 있음
    return rows


def normalize(raw, acc):
    collab = acc["type"] == "collab"
    actions = raw.get("actions")
    values = raw.get("action_values")
    seg_actions = raw.get("catalog_segment_actions")
    seg_values = raw.get("catalog_segment_value")

    pixel_purchases = pick(actions, PURCHASE_KEYS)
    pixel_purchase_value = pick(values, PURCHASE_KEYS)

    if collab:
        src_a, src_v = seg_actions, seg_values
    else:
        src_a, src_v = actions, values

    return {
        "date": raw.get("date_start"),
        "account_id": acc["id"],
        "account_name": acc["name"],
        "account_type": acc["type"],
        "campaign_id": raw.get("campaign_id", ""),
        "campaign_name": raw.get("campaign_name", ""),
        "adset_id": raw.get("adset_id", ""),
        "adset_name": raw.get("adset_name", ""),
        "spend": float(raw.get("spend", 0) or 0),
        "impressions": int(float(raw.get("impressions", 0) or 0)),
        "reach": int(float(raw.get("reach", 0) or 0)),
        "clicks": int(float(raw.get("clicks", 0) or 0)),
        "link_clicks": int(float(raw.get("inline_link_clicks", 0) or 0)),
        "view_content": pick(src_a, VIEW_KEYS),
        "add_to_cart": pick(src_a, ATC_KEYS),
        "add_to_cart_value": pick(src_v, ATC_KEYS),
        "purchases": pick(src_a, PURCHASE_KEYS),
        "purchase_value": pick(src_v, PURCHASE_KEYS),
        "pixel_purchases": pixel_purchases,
        "pixel_purchase_value": pixel_purchase_value,
    }


def load_existing():
    path = DATA_DIR / "daily.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[(row["date"], row["adset_id"])] = row
    return out


def save(rows_by_key):
    DATA_DIR.mkdir(exist_ok=True)
    rows = sorted(rows_by_key.values(), key=lambda r: (r["date"], r["account_id"], r["campaign_id"], r["adset_id"]))

    with (DATA_DIR / "daily.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})

    num_cols = set(COLUMNS[8:])
    clean = []
    for r in rows:
        c = {}
        for k in COLUMNS:
            v = r.get(k, "")
            if k in num_cols:
                try:
                    v = float(v or 0)
                    v = int(v) if v.is_integer() else round(v, 2)
                except (TypeError, ValueError):
                    v = 0
            c[k] = v
        clean.append(c)

    meta = {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "accounts": json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["accounts"],
        "rows": clean,
    }
    (DATA_DIR / "daily.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return len(rows)


def main():
    if not TOKEN:
        sys.exit("META_ACCESS_TOKEN 이 설정되지 않았습니다.")

    config = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
    today = datetime.now(KST).date()
    if SINCE:
        since, until = SINCE, (UNTIL or (today - timedelta(days=1)).isoformat())
    else:
        until = (today - timedelta(days=1)).isoformat()  # 어제까지 (오늘은 아직 집계 중)
        since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"수집 기간: {since} ~ {until} (API {API_VERSION})")

    existing = load_existing()
    # 이번 수집 기간에 해당하는 기존 행은 지움 (삭제/변경된 광고세트 반영)
    existing = {k: v for k, v in existing.items() if not (since <= k[0] <= until)}

    failed = []
    for acc in config["accounts"]:
        print(f"- {acc['name']} ({acc['id']}, {acc['type']})")
        try:
            raw_rows = fetch_account(acc, since, until)
        except RuntimeError as e:
            print(f"  실패: {e}")
            failed.append(acc["name"])
            continue
        for raw in raw_rows:
            row = normalize(raw, acc)
            existing[(row["date"], row["adset_id"])] = row
        print(f"  {len(raw_rows)}행")

    total = save(existing)
    print(f"저장 완료: 총 {total}행")
    if failed:
        sys.exit(f"일부 계정 수집 실패: {', '.join(failed)}")


if __name__ == "__main__":
    main()
