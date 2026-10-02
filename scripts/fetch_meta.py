"""
Home and Heal 메타 광고 데이터 수집 스크립트

수집하는 것
  1) 일자 x 광고세트 성과      -> data/daily.json, data/daily.csv
  2) 일자 x 광고(소재) 성과    -> data/ads.json
  3) 소재 정보(이미지/영상 구분) + 썸네일 이미지 -> data/creatives.json, data/creatives/*.jpg

지표 기준
  - 자사몰 계정(type=own): 일반 전환 지표(actions / action_values)
  - 협력광고 계정(type=collab): 공유 항목 포함 지표(catalog_segment_actions / catalog_segment_value)

환경 변수
  META_ACCESS_TOKEN  (필수) 액세스 토큰
  META_API_VERSION   (선택) 기본 v26.0
  LOOKBACK_DAYS      (선택) 기본 7 — 늦게 잡히는 전환을 반영하려고 최근 N일을 매번 다시 받음
  SINCE / UNTIL      (선택) YYYY-MM-DD — 지정하면 이 기간을 받음 (과거 데이터 백필용)
"""

import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
THUMB_DIR = DATA_DIR / "creatives"
KST = timezone(timedelta(hours=9))

TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
API_VERSION = (os.environ.get("META_API_VERSION") or "").strip() or "v26.0"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7") or 7)
SINCE = (os.environ.get("SINCE") or "").strip()
UNTIL = (os.environ.get("UNTIL") or "").strip()

BASE = f"https://graph.facebook.com/{API_VERSION}"

METRIC_FIELDS = [
    "spend", "impressions", "reach", "clicks", "inline_link_clicks",
    "actions", "action_values",
    "catalog_segment_actions", "catalog_segment_value",
]
ADSET_FIELDS = ["campaign_id", "campaign_name", "adset_id", "adset_name"] + METRIC_FIELDS
AD_FIELDS = ["campaign_id", "campaign_name", "adset_id", "ad_id", "ad_name"] + METRIC_FIELDS

# 우선순위대로 찾는다 (첫 번째로 존재하는 값 사용)
PURCHASE_KEYS = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
ATC_KEYS = ["omni_add_to_cart", "add_to_cart", "offsite_conversion.fb_pixel_add_to_cart"]
VIEW_KEYS = ["omni_view_content", "view_content", "offsite_conversion.fb_pixel_view_content"]
VIDEO_VIEW_KEYS = ["video_view"]  # 3초 이상 재생

COLUMNS = [
    "date", "account_id", "account_name", "account_type",
    "campaign_id", "campaign_name", "adset_id", "adset_name",
    "spend", "impressions", "reach", "clicks", "link_clicks",
    "view_content", "add_to_cart", "add_to_cart_value", "purchases", "purchase_value",
    "pixel_purchases", "pixel_purchase_value",
]
NUM_COLS = set(COLUMNS[8:])

CREATIVE_FIELDS = (
    "name,creative.thumbnail_width(600).thumbnail_height(600)"
    "{id,object_type,thumbnail_url,image_url,image_hash,video_id,"
    "object_story_spec,asset_feed_spec,product_set_id}"
)


# ---------------------------------------------------------------- 공통

def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def pick(action_list, keys):
    """actions 형태의 리스트에서 keys 우선순위로 값을 찾는다"""
    if not action_list:
        return 0.0
    by_type = {a.get("action_type"): a.get("value") for a in action_list}
    for k in keys:
        if by_type.get(k) not in (None, ""):
            return num(by_type[k])
    return 0.0


def compact(v):
    v = num(v)
    return int(v) if v.is_integer() else round(v, 2)


def api_get(url, params, retries=5):
    for attempt in range(retries):
        r = requests.get(url, params=params, timeout=90)
        if r.status_code == 200:
            return r.json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {"message": r.text[:200]}
        code = err.get("code")
        # 호출 한도 / 일시 오류는 기다렸다가 재시도
        # 파라미터/권한 오류(100, 190, 200 등)는 기다려도 안 풀리므로 바로 실패 처리
        transient = code in (1, 2, 4, 17, 32, 341, 613, 80000, 80004)
        if transient or (r.status_code >= 500 and code not in (100, 190, 200, 10)):
            wait = 30 * (attempt + 1)
            print(f"  일시 오류(code={code}), {wait}초 후 재시도: {err.get('message')}")
            time.sleep(wait)
            continue
        raise RuntimeError(f"API 오류 (HTTP {r.status_code}, code={code}): {err.get('message')}")
    raise RuntimeError("재시도 횟수 초과")


def month_chunks(since, until):
    """긴 기간을 월 단위로 나눈다 (한 번에 많이 요청하면 API 가 거절함)"""
    s, end = date.fromisoformat(since), date.fromisoformat(until)
    while s <= end:
        nxt_month = (s.replace(day=1) + timedelta(days=32)).replace(day=1)
        e = min(end, nxt_month - timedelta(days=1))
        yield s.isoformat(), e.isoformat()
        s = e + timedelta(days=1)


def fetch_insights(acc, level, fields, since, until, breakdowns=None):
    rows = []
    for s, e in month_chunks(since, until):
        url = f"{BASE}/act_{acc['id']}/insights"
        params = {
            "access_token": TOKEN,
            "level": level,
            "fields": ",".join(fields),
            "time_range": json.dumps({"since": s, "until": e}),
            "time_increment": 1,
            "limit": 500,
        }
        if breakdowns:
            params["breakdowns"] = breakdowns
        part = []
        while True:
            res = api_get(url, params)
            part.extend(res.get("data", []))
            nxt = res.get("paging", {}).get("next")
            if not nxt:
                break
            url, params = nxt, {}  # next URL 에 파라미터가 모두 들어 있음
        print(f"    [{level}{' / ' + breakdowns if breakdowns else ''}] {s} ~ {e}: {len(part)}행")
        rows.extend(part)
    return rows


def metrics(raw, acc):
    """계정 유형에 맞는 전환 지표를 골라 공통 형태로 만든다"""
    actions, values = raw.get("actions"), raw.get("action_values")
    if acc["type"] == "collab":
        src_a, src_v = raw.get("catalog_segment_actions"), raw.get("catalog_segment_value")
    else:
        src_a, src_v = actions, values
    return {
        "spend": num(raw.get("spend")),
        "impressions": int(num(raw.get("impressions"))),
        "reach": int(num(raw.get("reach"))),
        "clicks": int(num(raw.get("clicks"))),
        "link_clicks": int(num(raw.get("inline_link_clicks"))),
        "view_content": pick(src_a, VIEW_KEYS),
        "add_to_cart": pick(src_a, ATC_KEYS),
        "add_to_cart_value": pick(src_v, ATC_KEYS),
        "purchases": pick(src_a, PURCHASE_KEYS),
        "purchase_value": pick(src_v, PURCHASE_KEYS),
        "pixel_purchases": pick(actions, PURCHASE_KEYS),
        "pixel_purchase_value": pick(values, PURCHASE_KEYS),
        "video_views": pick(actions, VIDEO_VIEW_KEYS),
    }


# ---------------------------------------------------------------- 광고세트 일별

def load_adset_rows():
    path = DATA_DIR / "daily.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        return {(r["date"], r["adset_id"]): r for r in csv.DictReader(f)}


def save_adset_rows(rows_by_key, accounts):
    rows = sorted(rows_by_key.values(), key=lambda r: (r["date"], r["account_id"], r["campaign_id"], r["adset_id"]))
    with (DATA_DIR / "daily.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
    # 용량을 줄이려고 열 이름은 한 번만 쓰고 값만 배열로 저장
    packed = [[(compact(r.get(k)) if k in NUM_COLS else r.get(k, "")) for k in COLUMNS] for r in rows]
    meta = {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "accounts": accounts,
        "columns": COLUMNS,
        "rows": packed,
    }
    (DATA_DIR / "daily.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(rows)


# ---------------------------------------------------------------- 광고(소재) 일별

AD_KEEP = ["spend", "impressions", "link_clicks", "add_to_cart", "purchases", "purchase_value", "video_views"]


def load_ad_rows():
    path = DATA_DIR / "ads.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    cols = data["columns"]
    out = {}
    for vals in data["rows"]:
        r = dict(zip(cols, vals))
        out[(r["date"], r["ad_id"])] = r
    return out


def save_ad_rows(rows_by_key):
    cols = ["date", "account_id", "campaign_id", "campaign_name", "ad_id", "ad_name"] + AD_KEEP
    rows = sorted(rows_by_key.values(), key=lambda r: (r["date"], r["account_id"], r["ad_id"]))
    # 용량을 줄이려고 열 이름은 한 번만 쓰고 값만 배열로 저장
    packed = [[(compact(r.get(c)) if c in AD_KEEP else r.get(c, "")) for c in cols] for r in rows]
    (DATA_DIR / "ads.json").write_text(
        json.dumps({"columns": cols, "rows": packed}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(rows)


# ---------------------------------------------------------------- 소재 정보 / 썸네일

def classify(creative):
    """소재 유형(이미지/영상/카탈로그)과 같은 소재를 묶을 키, 썸네일 URL 을 정한다"""
    c = creative or {}
    oss = c.get("object_story_spec") or {}
    afs = c.get("asset_feed_spec") or {}
    link = oss.get("link_data") or {}
    vdata = oss.get("video_data") or {}

    video_id = c.get("video_id") or vdata.get("video_id")
    if not video_id and afs.get("videos"):
        video_id = (afs["videos"][0] or {}).get("video_id")
    image_hash = c.get("image_hash") or link.get("image_hash")
    if not image_hash and afs.get("images"):
        image_hash = (afs["images"][0] or {}).get("hash")

    is_catalog = bool(c.get("product_set_id") or oss.get("template_data"))

    if is_catalog:
        ctype, key = "카탈로그", f"c_{c.get('id', '')}"
    elif video_id or c.get("object_type") == "VIDEO":
        ctype, key = "영상", f"v_{video_id or c.get('id', '')}"
    elif link.get("child_attachments"):
        ctype, key = "이미지", f"k_{c.get('id', '')}"  # 캐러셀
    else:
        ctype, key = "이미지", f"i_{image_hash or c.get('id', '')}"

    # 이미지 소재는 원본(image_url)이 더 선명, 나머지는 썸네일
    thumb = (c.get("image_url") if ctype == "이미지" else None) or c.get("thumbnail_url") or vdata.get("image_url") or link.get("picture")
    return ctype, key, thumb


def fetch_ad_creatives(ad_ids):
    """광고별 소재 정보를 가져온다.
    v26.0 부터 여러 ID 를 한 번에 묻는 ids 파라미터가 막혀서, 광고 하나씩 동시에 몇 개씩 요청한다"""
    def one(ad_id):
        try:
            return ad_id, api_get(f"{BASE}/{ad_id}", {"access_token": TOKEN, "fields": CREATIVE_FIELDS}, retries=3)
        except RuntimeError as e:
            print(f"    소재 정보 조회 실패 ({ad_id}): {e}")
            return ad_id, None

    out = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for ad_id, res in pool.map(one, ad_ids):
            if res:
                out[ad_id] = res
    return out


def update_creatives(ad_ids):
    path = DATA_DIR / "creatives.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    todo = [a for a in sorted(ad_ids) if a not in known or not known[a].get("thumb")]
    print(f"- 소재 정보: 새로 확인할 광고 {len(todo)}개")
    downloaded = 0
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        info = fetch_ad_creatives(batch)
        for ad_id in batch:
            ad = info.get(ad_id)
            if not ad:
                known.setdefault(ad_id, {"type": "기타", "key": f"a_{ad_id}", "thumb": ""})
                continue
            ctype, key, thumb_url = classify(ad.get("creative"))
            fname = f"{key}.jpg".replace("/", "_")
            fpath = THUMB_DIR / fname
            if thumb_url and not fpath.exists():
                try:
                    img = requests.get(thumb_url, timeout=60)
                    if img.status_code == 200 and img.content:
                        fpath.write_bytes(img.content)
                        downloaded += 1
                except requests.RequestException as e:
                    print(f"    썸네일 다운로드 실패 ({ad_id}): {e}")
            known[ad_id] = {
                "type": ctype,
                "key": key,
                "thumb": f"data/creatives/{fname}" if fpath.exists() else "",
            }
    path.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    print(f"  썸네일 {downloaded}개 새로 저장")


# ---------------------------------------------------------------- 연령/성별/노출위치/기기

BREAKDOWNS = {
    # 파일 이름: (메타 breakdowns 값, 화면에 쓸 축 이름을 만드는 함수)
    "age": ("age", lambda r: r.get("age", "")),
    "gender": ("gender", lambda r: r.get("gender", "")),
    "placement": ("publisher_platform,platform_position",
                  lambda r: f"{r.get('publisher_platform', '')}|{r.get('platform_position', '')}"),
    "device": ("device_platform", lambda r: r.get("device_platform", "")),
}
BD_FIELDS = ["campaign_id", "campaign_name"] + METRIC_FIELDS
BD_COLS = ["date", "account_id", "campaign_id", "campaign_name", "dim",
           "spend", "impressions", "link_clicks", "add_to_cart", "purchases", "purchase_value", "ok"]
BD_NUM = {"spend", "impressions", "link_clicks", "add_to_cart", "purchases", "purchase_value", "ok"}


def load_packed(name, key_cols):
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for vals in data["rows"]:
        r = dict(zip(data["columns"], vals))
        out[tuple(r[c] for c in key_cols)] = r
    return out


def save_packed(name, rows_by_key, cols, num_cols):
    rows = sorted(rows_by_key.values(), key=lambda r: tuple(str(r.get(c, "")) for c in cols[:5]))
    packed = [[(compact(r.get(c)) if c in num_cols else r.get(c, "")) for c in cols] for r in rows]
    (DATA_DIR / f"{name}.json").write_text(
        json.dumps({"columns": cols, "rows": packed}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(rows)


def collect_breakdowns(accounts, since, until):
    """연령·성별·노출위치·기기별 성과를 캠페인 단위로 받는다. 실패해도 다른 데이터에는 영향 없음"""
    in_range = lambda d: since <= d <= until
    for name, (bd, dim_fn) in BREAKDOWNS.items():
        key_cols = ["date", "campaign_id", "dim"]
        rows = load_packed(f"bd_{name}", key_cols)
        for acc in accounts:
            seg_ok = True
            try:
                raw = fetch_insights(acc, "campaign", BD_FIELDS, since, until, bd)
            except RuntimeError as e:
                # 공유 항목 지표가 이 분석 축을 지원하지 않으면 공유 항목 필드 없이 다시 시도
                if acc["type"] != "collab":
                    print(f"  [{name}] {acc['name']} 실패: {e}")
                    continue
                print(f"  [{name}] {acc['name']}: 공유 항목 지표 미지원 → 노출·클릭 지표만 수집")
                seg_ok = False
                try:
                    raw = fetch_insights(acc, "campaign", [f for f in BD_FIELDS if not f.startswith("catalog_segment")], since, until, bd)
                except RuntimeError as e2:
                    print(f"  [{name}] {acc['name']} 실패: {e2}")
                    continue
            rows = {k: v for k, v in rows.items() if not (in_range(k[0]) and v["account_id"] == acc["id"])}
            for r in raw:
                m = metrics(r, acc)
                row = {"date": r.get("date_start"), "account_id": acc["id"],
                       "campaign_id": r.get("campaign_id", ""), "campaign_name": r.get("campaign_name", ""),
                       "dim": dim_fn(r), "ok": 1 if seg_ok or acc["type"] != "collab" else 0,
                       **{k: m[k] for k in ["spend", "impressions", "link_clicks", "add_to_cart", "purchases", "purchase_value"]}}
                if not row["ok"]:
                    row.update(add_to_cart=0, purchases=0, purchase_value=0)
                rows[(row["date"], row["campaign_id"], row["dim"])] = row
        n = save_packed(f"bd_{name}", rows, BD_COLS, BD_NUM)
        print(f"  {name}: {n}행 저장")


# ---------------------------------------------------------------- 실행

def main():
    if not TOKEN:
        sys.exit("META_ACCESS_TOKEN 이 설정되지 않았습니다.")

    config = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
    today = datetime.now(KST).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    if SINCE:
        since, until = SINCE, (UNTIL or yesterday)
    else:
        since, until = (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), yesterday
    print(f"수집 기간: {since} ~ {until} (API {API_VERSION})")

    DATA_DIR.mkdir(exist_ok=True)
    adset_rows = load_adset_rows()
    ad_rows = load_ad_rows()
    in_range = lambda k: since <= k[0] <= until

    failed, ad_ids = [], set()
    for acc in config["accounts"]:
        print(f"- {acc['name']} ({acc['id']}, {acc['type']})")
        try:
            raw_adsets = fetch_insights(acc, "adset", ADSET_FIELDS, since, until)
            raw_ads = fetch_insights(acc, "ad", AD_FIELDS, since, until)
        except RuntimeError as e:
            print(f"  실패: {e}")
            failed.append(acc["name"])
            continue  # 실패한 계정은 기존 데이터를 그대로 둔다

        # 성공한 계정만 이번 기간의 기존 행을 지우고 새로 채운다 (삭제/변경된 광고 반영)
        adset_rows = {k: v for k, v in adset_rows.items() if not (in_range(k) and v["account_id"] == acc["id"])}
        ad_rows = {k: v for k, v in ad_rows.items() if not (in_range(k) and v["account_id"] == acc["id"])}

        base = {"account_id": acc["id"], "account_name": acc["name"], "account_type": acc["type"]}
        for raw in raw_adsets:
            row = {**base, "date": raw.get("date_start"),
                   "campaign_id": raw.get("campaign_id", ""), "campaign_name": raw.get("campaign_name", ""),
                   "adset_id": raw.get("adset_id", ""), "adset_name": raw.get("adset_name", ""),
                   **metrics(raw, acc)}
            adset_rows[(row["date"], row["adset_id"])] = row
        for raw in raw_ads:
            row = {"account_id": acc["id"], "date": raw.get("date_start"),
                   "campaign_id": raw.get("campaign_id", ""), "campaign_name": raw.get("campaign_name", ""),
                   "ad_id": raw.get("ad_id", ""), "ad_name": raw.get("ad_name", ""),
                   **metrics(raw, acc)}
            ad_rows[(row["date"], row["ad_id"])] = row
            if row["spend"] > 0:
                ad_ids.add(row["ad_id"])

    n1 = save_adset_rows(adset_rows, config["accounts"])
    n2 = save_ad_rows(ad_rows)
    print(f"저장 완료: 광고세트 {n1}행, 광고 {n2}행")

    print("- 연령·성별·노출위치·기기별 수집")
    ok_accounts = [a for a in config["accounts"] if a["name"] not in failed]
    try:
        collect_breakdowns(ok_accounts, since, until)
    except Exception as e:  # 부가 데이터라 실패해도 성과 데이터는 저장
        print(f"분석 축 수집 중 오류 (성과 데이터는 저장됨): {e}")

    try:
        update_creatives(ad_ids)
    except Exception as e:  # 소재 정보는 부가 기능이라 실패해도 성과 데이터는 저장
        print(f"소재 정보 갱신 중 오류 (성과 데이터는 저장됨): {e}")

    if failed:
        sys.exit(f"일부 계정 수집 실패: {', '.join(failed)}")


if __name__ == "__main__":
    main()
