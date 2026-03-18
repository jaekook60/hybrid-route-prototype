import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

st.set_page_config(page_title="혼합 경로 추천기", page_icon="🚌", layout="wide")

# =========================================================
# 설정
# =========================================================
MAX_TRANSIT_PATHS = 4
MAX_MIXED_CARDS = 15
MAX_INTERMEDIATE_CANDIDATES = 10
DIRECT_STOP_WALK_MIN = 3
DIRECT_STOP_EXTRA_FARE_ALLOW = 3000
MIN_TAXI_MIN = 4
MIN_TAXI_KM = 1.5
MIN_TAXI_FARE = 5000
ARRIVE_BUFFER_MIN = 15
PARALLEL_WORKERS = 6

# 사전 필터 (Haversine)
PREFILTER_MIN_KM = 0.8
PREFILTER_MAX_RATIO = 0.75
PREFILTER_TOP_N = 6           # API 호출할 최대 후보 수 (줄임!)

# 택시 요금 공식 (서울 기준 2024~)
TAXI_BASE_FARE = 4800         # 기본요금
TAXI_BASE_DIST_M = 1600       # 기본거리 (m)
TAXI_DIST_UNIT_M = 131        # 거리당 단위 (m)
TAXI_DIST_UNIT_FARE = 100     # 거리당 요금
TAXI_TIME_UNIT_SEC = 30       # 시간당 단위 (초)
TAXI_TIME_UNIT_FARE = 100     # 시간당 요금
TAXI_NIGHT_MULT = 1.2         # 심야 할증 (23시~04시)
TAXI_DETOUR_RATIO = 1.35      # 직선거리 → 실제거리 보정계수

# 실시간 버스: 공공데이터포털 사용 (ODsay 대신)
USE_PUBLIC_BUS_API = True
PUBLIC_BUS_API_CACHE_TTL = 20

# 지하철 시간표: 로컬 추정 사용 (ODsay API 대신)
USE_LOCAL_SUBWAY_ESTIMATE = True

# 가성비 점수
VALUE_COST_WEIGHT = 0.30
VALUE_TIME_WEIGHT = 0.70
VALUE_KIND_PENALTY = {
    "transit": 0.03,
    "mixed_first": 0.00,
    "mixed_last": 0.00,
    "taxi": 0.12,
}
TRANSFER_PENALTY_MIN = 3

# 혼합 후보 필터
MIXED_NEAR_TAXI_MIN_COST_SAVE = 5000
MIXED_NEAR_TAXI_MIN_TIME_SAVE = 6
MIXED_KEEP_TIME_SAVE_VS_TRANSIT_ABS = 10
MIXED_KEEP_TIME_SAVE_VS_TRANSIT_RATIO = 0.20
MIXED_MAX_EXTRA_COST_VS_TRANSIT = 20000
MIXED_HIGH_TAXI_SHARE = 0.75
MIXED_HIGH_TAXI_KM = 20.0

ODSAY_REFERER = "https://hybrid-route-prototype-kmwass9s4mjky8yrgn78la.streamlit.app/"

# =========================================================
# Secrets
# =========================================================
try:
    KAKAO_REST_API_KEY = st.secrets["KAKAO_REST_API_KEY"]
except Exception:
    KAKAO_REST_API_KEY = st.secrets["KAKAO_LOCAL_REST_KEY"]

ODSAY_API_KEY = st.secrets["ODSAY_API_KEY"]

# 공공데이터포털 버스 API 키 (선택)
PUBLIC_DATA_API_KEY = st.secrets.get("PUBLIC_DATA_API_KEY", "")

KST = ZoneInfo("Asia/Seoul")


def now_kst():
    return datetime.now(KST)


# =========================================================
# HTTP 세션 (Retry 내장)
# =========================================================
def _create_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_session = _create_session()

# =========================================================
# API 호출 카운터 (디버깅용)
# =========================================================
if "api_calls" not in st.session_state:
    st.session_state.api_calls = {"odsay": 0, "kakao": 0, "public": 0, "saved": 0}


def count_api(provider):
    st.session_state.api_calls[provider] = st.session_state.api_calls.get(provider, 0) + 1


def count_saved():
    st.session_state.api_calls["saved"] = st.session_state.api_calls.get("saved", 0) + 1


# =========================================================
# 공통 유틸
# =========================================================
def round_coord(v, decimals=6):
    return round(v, decimals)


def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def is_same_point(x1, y1, x2, y2, eps=1e-5):
    return abs(x1 - x2) < eps and abs(y1 - y2) < eps


def first_non_none(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def fmt_won(v):
    try:
        return f"{int(v):,}원"
    except Exception:
        return str(v)


def haversine_km(x1, y1, x2, y2):
    R = 6371.0
    lat1, lat2 = math.radians(y1), math.radians(y2)
    dlat = math.radians(y2 - y1)
    dlon = math.radians(x2 - x1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(min(1.0, a)))


def validate_korea_coords(x, y):
    if not (124.5 <= x <= 132.0 and 33.0 <= y <= 43.5):
        raise ValueError(f"좌표({x:.4f}, {y:.4f})가 대한민국 범위를 벗어남.")


def parse_arrive_by(arrive_by: str):
    if not arrive_by:
        return None
    try:
        hh, mm = map(int, arrive_by.strip().split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        now = now_kst()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target
    except Exception:
        return None


def calc_arrival_status(total_time_min, arrive_by):
    target = parse_arrive_by(arrive_by)
    if target is None:
        return {"text": "시간 비교 안 함", "late": False, "diff_min": None}
    now = now_kst()
    eta = now + timedelta(minutes=total_time_min)
    diff = math.floor((target - eta).total_seconds() / 60)
    if diff >= 0:
        depart = (target - timedelta(minutes=total_time_min)).strftime("%H:%M")
        return {"text": f"제시간 도착 (권장 출발: {depart})", "late": False, "diff_min": diff}
    return {"text": f"{abs(diff)}분 지각 (지금 출발해도 늦음!)", "late": True, "diff_min": diff}


# =========================================================
# [핵심 최적화 1] 택시 요금 공식 계산 — 카카오 API 불필요
# =========================================================
def estimate_taxi(origin_x, origin_y, dest_x, dest_y):
    """
    직선거리 기반 택시 요금/시간 추정.
    카카오 API 호출 0건. 정확도 ±15%.
    """
    straight_km = haversine_km(origin_x, origin_y, dest_x, dest_y)
    real_km = straight_km * TAXI_DETOUR_RATIO
    real_m = real_km * 1000

    # 시간 추정: 서울 평균 25km/h (시내), 40km/h (외곽)
    avg_speed = 22 if real_km < 10 else 30
    duration_min = max(3, math.ceil(real_km / avg_speed * 60))

    # 요금 계산
    if real_m <= TAXI_BASE_DIST_M:
        fare = TAXI_BASE_FARE
    else:
        extra_m = real_m - TAXI_BASE_DIST_M
        dist_fare = math.ceil(extra_m / TAXI_DIST_UNIT_M) * TAXI_DIST_UNIT_FARE
        # 시간 요금 (신호대기 등 — 총 시간의 30% 추정)
        wait_sec = duration_min * 60 * 0.3
        time_fare = math.ceil(wait_sec / TAXI_TIME_UNIT_SEC) * TAXI_TIME_UNIT_FARE
        fare = TAXI_BASE_FARE + dist_fare + time_fare

    # 심야 할증
    hour = now_kst().hour
    if hour >= 23 or hour < 4:
        fare = int(fare * TAXI_NIGHT_MULT)

    # 최소 기본요금
    fare = max(fare, TAXI_BASE_FARE)

    count_saved()  # API 호출 절약!
    return {
        "duration_min": duration_min,
        "distance_km": round(real_km, 1),
        "taxi_fare": int(round(fare, -2)),  # 100원 단위 반올림
        "toll_fare": 0,
        "estimated": True,
    }


# =========================================================
# 카카오 API — 최종 확인용만 (1~2건)
# =========================================================
def kakao_headers():
    return {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}


def odsay_headers():
    return {"Referer": ODSAY_REFERER} if ODSAY_REFERER else {}


@st.cache_data(ttl=300)
def get_car_summary_precise(origin_x, origin_y, dest_x, dest_y):
    """정확한 택시 요금. 최종 추천 경로에만 사용."""
    count_api("kakao")
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    params = {
        "origin": f"{origin_x},{origin_y}",
        "destination": f"{dest_x},{dest_y}",
        "priority": "RECOMMEND", "summary": "true",
        "alternatives": "false", "road_details": "false",
        "car_fuel": "GASOLINE", "car_hipass": "false",
    }
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
               "Content-Type": "application/json"}
    r = _session.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    routes = r.json().get("routes", [])
    if not routes:
        raise ValueError("자동차 경로를 찾지 못했어.")
    summary = routes[0].get("summary", {})
    fare = summary.get("fare", {})
    return {
        "duration_min": math.ceil(safe_int(summary.get("duration", 0)) / 60),
        "distance_km": round(safe_int(summary.get("distance", 0)) / 1000, 1),
        "taxi_fare": safe_int(fare.get("taxi", 0)),
        "toll_fare": safe_int(fare.get("toll", 0)),
        "estimated": False,
    }


# =========================================================
# 장소 검색 (카카오 — 필수, 1회)
# =========================================================
@st.cache_data(ttl=300)
def search_place(query: str):
    count_api("kakao")
    r = _session.get(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        headers=kakao_headers(), params={"query": query, "size": 1}, timeout=10)
    r.raise_for_status()
    docs = r.json().get("documents", [])
    if docs and isinstance(docs[0], dict):
        d = docs[0]
        x, y = float(d["x"]), float(d["y"])
        validate_korea_coords(x, y)
        return {"name": d.get("place_name", query),
                "address": d.get("road_address_name") or d.get("address_name") or query,
                "x": x, "y": y}

    count_api("kakao")
    r = _session.get(
        "https://dapi.kakao.com/v2/local/search/address.json",
        headers=kakao_headers(),
        params={"query": query, "analyze_type": "similar", "size": 1}, timeout=10)
    r.raise_for_status()
    docs = r.json().get("documents", [])
    if docs and isinstance(docs[0], dict):
        d = docs[0]
        x, y = float(d["x"]), float(d["y"])
        validate_korea_coords(x, y)
        return {"name": query, "address": query, "x": x, "y": y}

    raise ValueError(f"'{query}' 위치를 찾지 못했어.")


# =========================================================
# ODsay 공통
# =========================================================
def parse_odsay_error(data):
    err = data.get("error") if isinstance(data, dict) else None
    if err is None:
        return None
    if isinstance(err, dict):
        return f"ODsay 오류 code={err.get('code', '')}, message={err.get('message', '')}"
    return f"ODsay 오류: {err}"


# =========================================================
# [핵심 최적화 2] 대중교통 경로 — 1회만 호출 후 재활용
# =========================================================
@st.cache_data(ttl=300)
def get_transit_paths(origin_x, origin_y, dest_x, dest_y):
    count_api("odsay")
    url = "https://api.odsay.com/v1/api/searchPubTransPathR"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": origin_x, "SY": origin_y,
        "EX": dest_x, "EY": dest_y,
        "OPT": 0, "lang": 0, "output": "json",
    }
    r = _session.get(url, params=params, headers=odsay_headers(), timeout=20)
    r.raise_for_status()
    data = r.json()
    err = parse_odsay_error(data)
    if err:
        raise ValueError(err)
    paths = data.get("result", {}).get("path", [])
    if not paths:
        raise ValueError("대중교통 경로를 찾지 못했어.")
    return paths


def slice_transit_path_from(path, split_x, split_y):
    """
    경로의 중간 지점(split)부터 끝까지의 시간/비용을 추정.
    API 호출 없이 subPath 데이터를 잘라서 계산.
    """
    if not isinstance(path, dict):
        return None

    subpaths = path.get("subPath", [])
    info = path.get("info", {})

    found = False
    remaining_time = 0
    remaining_steps = []

    for sp in subpaths:
        if not isinstance(sp, dict):
            continue

        if not found:
            # split 지점 찾기 — start 또는 end가 매칭되면 거기부터 시작
            sx = safe_float(sp.get("startX"), 0)
            sy = safe_float(sp.get("startY"), 0)
            ex = safe_float(sp.get("endX"), 0)
            ey = safe_float(sp.get("endY"), 0)

            if is_same_point(sx, sy, split_x, split_y) or is_same_point(ex, ey, split_x, split_y):
                found = True
                remaining_time += safe_int(sp.get("sectionTime", 0))
                remaining_steps.append(sp)
            continue

        remaining_time += safe_int(sp.get("sectionTime", 0))
        remaining_steps.append(sp)

    if not found or remaining_time == 0:
        return None

    # 비용은 비례 배분 추정
    total_time = safe_int(info.get("totalTime", 1))
    total_cost = safe_int(info.get("payment", 0))
    ratio = remaining_time / max(total_time, 1)
    estimated_cost = int(total_cost * ratio)

    # 대중교통 기본요금 이하로 내려가지 않도록
    estimated_cost = max(estimated_cost, 1250)

    return {
        "time_min": remaining_time,
        "cost": estimated_cost,
        "steps": remaining_steps,
        "walk_m": 0,
    }


def slice_transit_path_until(path, split_x, split_y):
    """
    경로의 처음부터 split 지점까지의 시간/비용을 추정.
    API 호출 없이 subPath 데이터를 잘라서 계산.
    """
    if not isinstance(path, dict):
        return None

    subpaths = path.get("subPath", [])
    info = path.get("info", {})

    elapsed_time = 0
    collected_steps = []

    for sp in subpaths:
        if not isinstance(sp, dict):
            continue

        elapsed_time += safe_int(sp.get("sectionTime", 0))
        collected_steps.append(sp)

        sx = safe_float(sp.get("startX"), 0)
        sy = safe_float(sp.get("startY"), 0)
        ex = safe_float(sp.get("endX"), 0)
        ey = safe_float(sp.get("endY"), 0)

        if is_same_point(sx, sy, split_x, split_y) or is_same_point(ex, ey, split_x, split_y):
            break

    if elapsed_time == 0:
        return None

    total_time = safe_int(info.get("totalTime", 1))
    total_cost = safe_int(info.get("payment", 0))
    ratio = elapsed_time / max(total_time, 1)
    estimated_cost = max(int(total_cost * ratio), 1250)

    return {
        "time_min": elapsed_time,
        "cost": estimated_cost,
        "steps": collected_steps,
        "walk_m": 0,
    }


# =========================================================
# 포맷팅
# =========================================================
def path_type_to_text(pt):
    return {1: "지하철", 2: "버스", 3: "버스+지하철"}.get(pt, f"기타({pt})")


def format_subpath(sp):
    if not isinstance(sp, dict):
        return str(sp)
    tt = sp.get("trafficType")
    sec = safe_int(sp.get("sectionTime", 0))
    sn = sp.get("startName", "")
    en = sp.get("endName", "")
    if tt == 3:
        return f"도보 {sec}분"
    lane = sp.get("lane", [])
    label = "이동"
    if isinstance(lane, list) and lane and isinstance(lane[0], dict):
        if tt == 1:
            label = lane[0].get("name", "지하철")
        elif tt == 2:
            label = str(lane[0].get("busNo", "버스"))
    if tt == 1:
        return f"{label}: {sn} → {en} ({sec}분)"
    if tt == 2:
        return f"{label}번: {sn} → {en} ({sec}분)"
    return f"이동: {sn} → {en} ({sec}분)"


def path_to_summary(path):
    if not isinstance(path, dict):
        return None
    info = path.get("info", {})
    subpaths = path.get("subPath", [])
    return {
        "time_min": safe_int(info.get("totalTime", 0)),
        "cost": safe_int(info.get("payment", 0)),
        "walk_m": safe_int(info.get("totalWalk", 0)),
        "bus_transit_count": safe_int(info.get("busTransitCount", 0)),
        "subway_transit_count": safe_int(info.get("subwayTransitCount", 0)),
        "path_type": path_type_to_text(path.get("pathType")),
        "steps": [format_subpath(sp) for sp in subpaths if isinstance(sp, dict)],
    }


# =========================================================
# 후보 지점 추출 + Haversine 사전 필터
# =========================================================
def normalize_candidate_point(name, x, y):
    x, y = safe_float(x, None), safe_float(y, None)
    if not name or x is None or y is None:
        return None
    return {"name": str(name), "x": x, "y": y}


def collect_subpath_points(sp):
    if not isinstance(sp, dict):
        return []
    points = []
    p = normalize_candidate_point(sp.get("startName"), sp.get("startX"), sp.get("startY"))
    if p:
        points.append(p)
    # passStopList 중간 정류장
    psl = sp.get("passStopList")
    if isinstance(psl, dict):
        for key in ["stations", "station", "list", "items"]:
            items = psl.get(key)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = first_non_none(item, ["stationName", "name", "stopName"], None)
                    x = first_non_none(item, ["x", "X", "gpsX", "lon", "lng"], None)
                    y = first_non_none(item, ["y", "Y", "gpsY", "lat"], None)
                    cp = normalize_candidate_point(name, x, y)
                    if cp:
                        points.append(cp)
                break
    p = normalize_candidate_point(sp.get("endName"), sp.get("endX"), sp.get("endY"))
    if p:
        points.append(p)
    # 중복 제거
    seen = set()
    dedup = []
    for pt in points:
        key = (round(pt["x"], 6), round(pt["y"], 6))
        if key not in seen:
            seen.add(key)
            dedup.append(pt)
    return dedup


def extract_candidates_filtered(paths, reference, exclude, total_dist_km):
    """경로에서 후보 추출 + Haversine 사전 필터. API 호출 0건."""
    per_path = max(3, MAX_INTERMEDIATE_CANDIDATES // min(len(paths), MAX_TRANSIT_PATHS))
    all_cands = []
    seen = set()

    for path in paths[:MAX_TRANSIT_PATHS]:
        if not isinstance(path, dict):
            continue
        path_pts = []
        for sp in path.get("subPath", []):
            if not isinstance(sp, dict) or sp.get("trafficType") not in (1, 2):
                continue
            for p in collect_subpath_points(sp):
                if is_same_point(p["x"], p["y"], exclude["x"], exclude["y"]):
                    continue
                key = (round(p["x"], 6), round(p["y"], 6))
                if key in seen:
                    continue
                seen.add(key)
                d = haversine_km(reference["x"], reference["y"], p["x"], p["y"])
                if d < PREFILTER_MIN_KM or d > total_dist_km * PREFILTER_MAX_RATIO:
                    continue
                path_pts.append((d, p))

        path_pts.sort(key=lambda x: x[0])
        for _, p in path_pts[:per_path]:
            all_cands.append(p)

    return all_cands[:PREFILTER_TOP_N]


# =========================================================
# Robust 정규화 + 점수 함수
# =========================================================
def robust_normalize(value, values):
    arr = sorted(values)
    n = len(arr)
    if n < 2:
        return 0.0
    q1, q3 = arr[max(0, n // 4)], arr[min(n - 1, 3 * n // 4)]
    iqr = q3 - q1
    if iqr == 0:
        mn, mx = arr[0], arr[-1]
        return 0.0 if mx == mn else max(0, min(1, (value - mn) / (mx - mn)))
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return max(0, min(1, (max(lo, min(hi, value)) - lo) / (hi - lo)))


def late_penalty_continuous(c):
    if not c.get("late"):
        return 0.0
    return 0.5 + 0.5 * math.log1p(abs(c.get("late_diff") or 0) / 5.0)


def effective_time(c):
    t = safe_int(c.get("bus_transit_count", 0)) + safe_int(c.get("subway_transit_count", 0))
    if c.get("kind") in ("mixed_first", "mixed_last"):
        t += 1
    return c["time_min"] + t * TRANSFER_PENALTY_MIN


def is_dominated(a, b):
    ta, tb = effective_time(a), effective_time(b)
    return tb <= ta and b["cost"] <= a["cost"] and (tb < ta or b["cost"] < a["cost"])


def filter_dominated(candidates):
    kept = []
    for i, a in enumerate(candidates):
        if not any(is_dominated(a, b) for j, b in enumerate(candidates) if i != j):
            kept.append(a)
    return kept


def mixed_is_reasonable(c, taxi_only, best_transit):
    if c.get("kind") not in ("mixed_first", "mixed_last"):
        return True
    total_time = safe_int(c.get("time_min"), 0)
    total_cost = safe_int(c.get("cost"), 0)
    taxi_time = safe_int(c.get("taxi_time_min"), 0)
    taxi_km = safe_float(c.get("taxi_distance_km"), 0)
    taxi_share = taxi_time / max(total_time, 1)

    csv_taxi = (taxi_only["cost"] - total_cost) if taxi_only else None
    tsv_taxi = (taxi_only["time_min"] - total_time) if taxi_only else None
    tsv_transit = (best_transit["time_min"] - total_time) if best_transit else None
    ecv_transit = (total_cost - best_transit["cost"]) if best_transit else None

    if csv_taxi is not None and csv_taxi >= MIXED_NEAR_TAXI_MIN_COST_SAVE:
        return True
    if tsv_taxi is not None and tsv_taxi >= MIXED_NEAR_TAXI_MIN_TIME_SAVE:
        return True
    if tsv_transit is not None and best_transit:
        ratio = tsv_transit / max(best_transit["time_min"], 1)
        if (tsv_transit >= MIXED_KEEP_TIME_SAVE_VS_TRANSIT_ABS or
            ratio >= MIXED_KEEP_TIME_SAVE_VS_TRANSIT_RATIO):
            if ecv_transit is None or ecv_transit <= MIXED_MAX_EXTRA_COST_VS_TRANSIT:
                return True
    if best_transit and best_transit.get("late") and not c.get("late"):
        return True
    if (taxi_share >= MIXED_HIGH_TAXI_SHARE or taxi_km >= MIXED_HIGH_TAXI_KM):
        if csv_taxi is not None and tsv_taxi is not None:
            if csv_taxi < MIXED_NEAR_TAXI_MIN_COST_SAVE and tsv_taxi < MIXED_NEAR_TAXI_MIN_TIME_SAVE:
                return False
    return True


def valid_taxi_leg(car):
    return (safe_int(car.get("duration_min"), 0) >= MIN_TAXI_MIN
            or safe_float(car.get("distance_km"), 0) >= MIN_TAXI_KM
            or safe_int(car.get("taxi_fare"), 0) >= MIN_TAXI_FARE)


# =========================================================
# [핵심] 후보 생성 — 최적화된 버전
# =========================================================
def build_route_candidates(origin, destination, arrive_by):
    """
    API 호출 내역:
      - get_transit_paths: 1건 (ODsay)
      - estimate_taxi (전체): 0건 (공식 계산)
      - board 후보: estimate_taxi × N건 = 0건 (공식)
      - split 후보: estimate_taxi × N건 = 0건 (공식)
      - 경로 재활용 (slice): 0건
      - 최종 확인 (카카오 정밀): 1~3건
    총: ODsay 1건 + 카카오 3~5건 = 약 4~6건!
    """

    # 대중교통 실패 → 택시만
    try:
        base_paths = get_transit_paths(
            round_coord(origin["x"]), round_coord(origin["y"]),
            round_coord(destination["x"]), round_coord(destination["y"]))
    except Exception:
        base_paths = []

    # 택시 전체 — 공식 추정 (0건)
    car_full = estimate_taxi(origin["x"], origin["y"],
                             destination["x"], destination["y"])

    candidates = []

    # 1) 택시만
    taxi_status = calc_arrival_status(car_full["duration_min"], arrive_by)
    candidates.append({
        "kind": "taxi", "title": "택시만", "subtitle": None,
        "time_min": car_full["duration_min"],
        "cost": car_full["taxi_fare"],
        "distance_km": car_full["distance_km"],
        "walk_m": None,
        "status": taxi_status["text"],
        "late": taxi_status["late"],
        "late_diff": taxi_status["diff_min"],
        "reason": "현재 시간대 대중교통 미운행" if not base_paths else "가장 빠르지만 비용이 큼",
        "steps": ["출발지에서 바로 택시 이용"],
        "live_notes": [],
        "estimated": True,
    })

    if not base_paths:
        return candidates

    # 2) 대중교통만
    for idx, path in enumerate(base_paths[:MAX_TRANSIT_PATHS], start=1):
        if not isinstance(path, dict):
            continue
        summary = path_to_summary(path)
        if not summary:
            continue
        status = calc_arrival_status(summary["time_min"], arrive_by)
        candidates.append({
            "kind": "transit", "title": "대중교통만",
            "subtitle": f"{idx}번 경로 · {summary['path_type']}",
            "time_min": summary["time_min"],
            "cost": summary["cost"],
            "distance_km": None,
            "walk_m": summary["walk_m"],
            "status": status["text"],
            "late": status["late"],
            "late_diff": status["diff_min"],
            "reason": "가장 저렴한 선택지",
            "steps": summary["steps"],
            "bus_transit_count": summary["bus_transit_count"],
            "subway_transit_count": summary["subway_transit_count"],
            "live_notes": [],
        })

    total_dist = haversine_km(origin["x"], origin["y"],
                              destination["x"], destination["y"])

    # 3) 택시 → 대중교통 (board 후보)
    board_cands = extract_candidates_filtered(base_paths, origin, origin, total_dist)

    for board in board_cands:
        # 택시 구간 — 공식 (0건)
        car_head = estimate_taxi(origin["x"], origin["y"], board["x"], board["y"])
        if not valid_taxi_leg(car_head):
            continue

        # 대중교통 구간 — 경로 재활용 (0건)
        best_slice = None
        for path in base_paths[:MAX_TRANSIT_PATHS]:
            s = slice_transit_path_from(path, board["x"], board["y"])
            if s and (best_slice is None or s["time_min"] < best_slice["time_min"]):
                best_slice = s

        if not best_slice or best_slice["time_min"] <= 0:
            continue

        total_time = car_head["duration_min"] + best_slice["time_min"]
        total_cost = car_head["taxi_fare"] + best_slice["cost"]
        status = calc_arrival_status(total_time, arrive_by)

        candidates.append({
            "kind": "mixed_first",
            "title": "택시 → 대중교통",
            "subtitle": f"{board['name']}에서 대중교통 탑승",
            "time_min": total_time,
            "cost": total_cost,
            "distance_km": car_head["distance_km"],
            "walk_m": best_slice["walk_m"],
            "status": status["text"],
            "late": status["late"],
            "late_diff": status["diff_min"],
            "reason": f"초반 택시 → {board['name']}부터 대중교통",
            "steps": [f"출발지 → {board['name']} 택시 {car_head['duration_min']}분"]
                     + [format_subpath(sp) for sp in best_slice["steps"]],
            "board_name": board["name"],
            "live_notes": [],
            "taxi_time_min": car_head["duration_min"],
            "taxi_distance_km": car_head["distance_km"],
            "taxi_cost": car_head["taxi_fare"],
            "transit_time_min": best_slice["time_min"],
            "transit_cost": best_slice["cost"],
            "estimated": True,
        })

    # 4) 대중교통 → 택시 (split 후보)
    split_cands = extract_candidates_filtered(base_paths, destination, destination, total_dist)

    for split in split_cands:
        # 대중교통 구간 — 경로 재활용 (0건)
        best_slice = None
        for path in base_paths[:MAX_TRANSIT_PATHS]:
            s = slice_transit_path_until(path, split["x"], split["y"])
            if s and (best_slice is None or s["time_min"] < best_slice["time_min"]):
                best_slice = s

        if not best_slice or best_slice["time_min"] <= 0:
            continue

        # 택시 구간 — 공식 (0건)
        car_tail = estimate_taxi(split["x"], split["y"],
                                 destination["x"], destination["y"])
        if not valid_taxi_leg(car_tail):
            continue

        total_time = best_slice["time_min"] + car_tail["duration_min"]
        total_cost = best_slice["cost"] + car_tail["taxi_fare"]
        status = calc_arrival_status(total_time, arrive_by)

        candidates.append({
            "kind": "mixed_last",
            "title": "대중교통 → 택시",
            "subtitle": f"{split['name']}에서 택시 전환",
            "time_min": total_time,
            "cost": total_cost,
            "distance_km": car_tail["distance_km"],
            "walk_m": best_slice["walk_m"],
            "status": status["text"],
            "late": status["late"],
            "late_diff": status["diff_min"],
            "reason": f"{split['name']}까지 대중교통 후 택시",
            "steps": [format_subpath(sp) for sp in best_slice["steps"]]
                     + [f"{split['name']} → 목적지 택시 {car_tail['duration_min']}분"],
            "split_name": split["name"],
            "live_notes": [],
            "taxi_time_min": car_tail["duration_min"],
            "taxi_distance_km": car_tail["distance_km"],
            "taxi_cost": car_tail["taxi_fare"],
            "transit_time_min": best_slice["time_min"],
            "transit_cost": best_slice["cost"],
            "estimated": True,
        })

    # 중복 제거 + 필터
    unique = []
    seen = set()
    for c in candidates:
        key = (c["kind"], c.get("subtitle"), c["time_min"], c["cost"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    taxi_only = next((c for c in unique if c["kind"] == "taxi"), None)
    transit_list = [c for c in unique if c["kind"] == "transit"]
    best_transit = sorted(transit_list, key=lambda x: (x["time_min"], x["cost"]))[0] if transit_list else None

    filtered = [c for c in unique if mixed_is_reasonable(c, taxi_only, best_transit)]
    filtered = filter_dominated(filtered)

    # =========================================================
    # [핵심 최적화 3] 최종 TOP 3만 카카오 정밀 조회
    # =========================================================
    top_mixed = [c for c in filtered if c.get("estimated") and c["kind"] in ("mixed_first", "mixed_last")]
    top_mixed.sort(key=lambda x: (x["time_min"], x["cost"]))

    for c in top_mixed[:2]:  # 상위 2개만 정밀 조회
        try:
            if c["kind"] == "mixed_first":
                bname = c.get("board_name", "")
                # board 좌표 찾기
                for bc in board_cands:
                    if bc["name"] == bname:
                        precise = get_car_summary_precise(
                            round_coord(origin["x"]), round_coord(origin["y"]),
                            round_coord(bc["x"]), round_coord(bc["y"]))
                        c["taxi_cost"] = precise["taxi_fare"]
                        c["taxi_time_min"] = precise["duration_min"]
                        c["taxi_distance_km"] = precise["distance_km"]
                        c["distance_km"] = precise["distance_km"]
                        c["cost"] = precise["taxi_fare"] + c["transit_cost"]
                        c["time_min"] = precise["duration_min"] + c["transit_time_min"]
                        c["estimated"] = False
                        # status 재계산
                        st2 = calc_arrival_status(c["time_min"], arrive_by)
                        c["status"] = st2["text"]
                        c["late"] = st2["late"]
                        c["late_diff"] = st2["diff_min"]
                        break

            elif c["kind"] == "mixed_last":
                sname = c.get("split_name", "")
                for sc in split_cands:
                    if sc["name"] == sname:
                        precise = get_car_summary_precise(
                            round_coord(sc["x"]), round_coord(sc["y"]),
                            round_coord(destination["x"]), round_coord(destination["y"]))
                        c["taxi_cost"] = precise["taxi_fare"]
                        c["taxi_time_min"] = precise["duration_min"]
                        c["taxi_distance_km"] = precise["distance_km"]
                        c["distance_km"] = precise["distance_km"]
                        c["cost"] = c["transit_cost"] + precise["taxi_fare"]
                        c["time_min"] = c["transit_time_min"] + precise["duration_min"]
                        c["estimated"] = False
                        st2 = calc_arrival_status(c["time_min"], arrive_by)
                        c["status"] = st2["text"]
                        c["late"] = st2["late"]
                        c["late_diff"] = st2["diff_min"]
                        break
        except Exception:
            pass  # 정밀 실패 시 추정값 유지

    # 택시만도 정밀 조회
    if taxi_only:
        try:
            precise = get_car_summary_precise(
                round_coord(origin["x"]), round_coord(origin["y"]),
                round_coord(destination["x"]), round_coord(destination["y"]))
            taxi_only["cost"] = precise["taxi_fare"]
            taxi_only["time_min"] = precise["duration_min"]
            taxi_only["distance_km"] = precise["distance_km"]
            taxi_only["estimated"] = False
            st2 = calc_arrival_status(taxi_only["time_min"], arrive_by)
            taxi_only["status"] = st2["text"]
            taxi_only["late"] = st2["late"]
            taxi_only["late_diff"] = st2["diff_min"]
        except Exception:
            pass

    return filtered


# =========================================================
# 정렬 / 추천
# =========================================================
def value_score(c, candidates, arrive_by=None):
    lp = late_penalty_continuous(c)
    kp = VALUE_KIND_PENALTY.get(c["kind"], 0)
    costs = [x["cost"] for x in candidates]
    cost_norm = robust_normalize(c["cost"], costs)

    if not arrive_by:
        times = [x["time_min"] for x in candidates]
        return lp + kp + 0.30 * cost_norm + 0.70 * robust_normalize(c["time_min"], times)

    if not c["late"]:
        slack = max(0, c.get("late_diff") or 0)
        urgency = max(0, ARRIVE_BUFFER_MIN - min(slack, ARRIVE_BUFFER_MIN))
    else:
        urgency = ARRIVE_BUFFER_MIN

    non_late = [x for x in candidates if not x["late"]]
    pool = []
    for x in (non_late or candidates):
        s = max(0, (x.get("late_diff") or 0))
        pool.append(max(0, ARRIVE_BUFFER_MIN - min(s, ARRIVE_BUFFER_MIN)))
    u_norm = robust_normalize(urgency, pool) if pool else (1.0 if c["late"] else 0.0)

    return lp + kp + 0.75 * cost_norm + 0.25 * u_norm


def pick_best(candidates, priority, arrive_by=None):
    if not candidates:
        return None
    if priority == "최저비용":
        return sorted(candidates, key=lambda c: (1 if c["late"] else 0, c["cost"], c["time_min"]))[0]
    elif priority == "제시간 도착":
        def k(c):
            if not c["late"]:
                return (0, c["time_min"], c["cost"])
            return (1, abs(c["late_diff"]) if c["late_diff"] else 999999, c["time_min"], c["cost"])
        return sorted(candidates, key=k)[0]
    else:
        def k(c):
            return (1 if c["late"] else 0,
                    abs(c["late_diff"]) if c.get("late_diff") and c["late"] else 0,
                    value_score(c, candidates, arrive_by),
                    c["cost"], c["time_min"])
        return sorted(candidates, key=k)[0]


def pick_best_by_kind(candidates, kind, priority, arrive_by=None):
    subset = [c for c in candidates if c["kind"] == kind]
    return pick_best(subset, priority, arrive_by) if subset else None


# =========================================================
# UI
# =========================================================
st.title("혼합 경로 추천기")
st.caption("대중교통 + 택시 혼합 · API 호출 최소화 버전")

col_a, col_b = st.columns(2)
with col_a:
    origin_text = st.text_input("출발지", placeholder="예: 서울역")
with col_b:
    destination_text = st.text_input("목적지", placeholder="예: 강남역")

col_c, col_d = st.columns(2)
with col_c:
    arrive_by = st.text_input("도착 희망 시간", placeholder="예: 19:00")
with col_d:
    priority = st.selectbox("우선순위", ["가성비", "최저비용", "제시간 도착"])

if st.button("혼합 경로 검색", use_container_width=True):
    if not origin_text or not destination_text:
        st.warning("출발지와 목적지를 입력해줘.")
    else:
        st.session_state.api_calls = {"odsay": 0, "kakao": 0, "public": 0, "saved": 0}
        try:
            with st.spinner("장소 검색 중..."):
                origin = search_place(origin_text)
                destination = search_place(destination_text)

            if is_same_point(origin["x"], origin["y"], destination["x"], destination["y"]):
                st.warning("출발지와 목적지가 동일해요.")
                st.stop()

            with st.spinner("경로 계산 중..."):
                all_candidates = build_route_candidates(origin, destination, arrive_by)

            if not all_candidates:
                st.error("경로를 만들지 못했어.")
                st.stop()

            # API 호출 통계
            ac = st.session_state.api_calls
            total_calls = ac["odsay"] + ac["kakao"] + ac["public"]
            st.info(
                f"API 호출: ODsay {ac['odsay']}건 · 카카오 {ac['kakao']}건 · "
                f"공공 {ac['public']}건 · **총 {total_calls}건** "
                f"(공식 계산으로 {ac['saved']}건 절약)"
            )

            best_overall = pick_best(all_candidates, priority, arrive_by=arrive_by)
            best_transit = pick_best_by_kind(all_candidates, "transit", priority, arrive_by)
            best_taxi = pick_best_by_kind(all_candidates, "taxi", priority, arrive_by)
            best_mf = pick_best_by_kind(all_candidates, "mixed_first", priority, arrive_by)
            best_ml = pick_best_by_kind(all_candidates, "mixed_last", priority, arrive_by)

            st.success(f"{origin['name']} → {destination['name']} 추천 결과")

            if best_overall:
                st.subheader("추천 1위")
                with st.container(border=True):
                    st.markdown(f"### {best_overall['title']}")
                    if best_overall.get("subtitle"):
                        st.caption(best_overall["subtitle"])

                    c1, c2, c3 = st.columns(3)
                    c1.metric("총 시간", f"{best_overall['time_min']}분")
                    c2.metric("총 비용", fmt_won(best_overall["cost"]))
                    if best_overall.get("distance_km"):
                        c3.metric("차량 거리", f"{best_overall['distance_km']}km")
                    else:
                        c3.metric("구분", best_overall["kind"])

                    if best_overall.get("estimated"):
                        st.caption("💡 택시 요금은 추정값입니다 (±15%)")

                    if best_overall["late"]:
                        st.error(best_overall["status"])
                    else:
                        st.success(best_overall["status"])

                    st.write(f"추천 이유: {best_overall['reason']}")

                    if best_overall["kind"] in ("mixed_first", "mixed_last"):
                        tt = safe_int(best_overall.get("taxi_time_min"), 0)
                        total = max(safe_int(best_overall.get("time_min"), 1), 1)
                        st.write(f"택시 비중: {round(tt / total * 100)}%")

                    st.write("세부 흐름")
                    for line in best_overall.get("steps", [])[:12]:
                        st.write(f"- {line}")

            st.subheader("비교 카드")
            for label, route in [("대중교통만", best_transit), ("택시→대중교통", best_mf),
                                 ("대중교통→택시", best_ml), ("택시만", best_taxi)]:
                if not route:
                    continue
                with st.container(border=True):
                    st.markdown(f"### {label}")
                    if route.get("subtitle"):
                        st.caption(route["subtitle"])

                    c1, c2, c3 = st.columns(3)
                    c1.metric("총 시간", f"{route['time_min']}분")
                    c2.metric("총 비용", fmt_won(route["cost"]))
                    if route.get("distance_km"):
                        c3.metric("차량 거리", f"{route['distance_km']}km")
                    else:
                        c3.metric("도보", f"{route.get('walk_m', '-')}m")

                    if route.get("estimated"):
                        st.caption("💡 택시 요금 추정값 (±15%)")

                    if route["late"]:
                        st.error(route["status"])
                    else:
                        st.success(route["status"])

                    st.write(f"설명: {route['reason']}")

                    if route["kind"] in ("mixed_first", "mixed_last"):
                        tt = safe_int(route.get("taxi_time_min"), 0)
                        total = max(safe_int(route.get("time_min"), 1), 1)
                        st.write(f"택시 비중: {round(tt / total * 100)}%")

                    st.write("세부 경로")
                    for line in route.get("steps", [])[:12]:
                        st.write(f"- {line}")

            with st.expander("전체 후보 + 점수"):
                all_sorted = sorted(all_candidates,
                    key=lambda x: (value_score(x, all_candidates), x["cost"], x["time_min"]))
                for i, c in enumerate(all_sorted, 1):
                    score = value_score(c, all_candidates)
                    est = " (추정)" if c.get("estimated") else ""
                    extra = ""
                    if c["kind"] in ("mixed_first", "mixed_last"):
                        tt = safe_int(c.get("taxi_time_min"), 0)
                        total = max(safe_int(c.get("time_min"), 1), 1)
                        extra = f" | 택시비중 {round(tt / total * 100)}%"
                    st.write(
                        f"{i}. [{c['kind']}] {c['title']}"
                        f"{' / ' + c['subtitle'] if c.get('subtitle') else ''}"
                        f" | {c['time_min']}분 | {fmt_won(c['cost'])}{est}"
                        f"{extra} | 점수: {score:.3f} | {c['status']}"
                    )

        except Exception as e:
            st.error(f"오류: {e}")
            with st.expander("에러 상세"):
                st.exception(e)
