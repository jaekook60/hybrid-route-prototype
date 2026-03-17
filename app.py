import math
from datetime import datetime, timedelta

import requests
import streamlit as st

st.set_page_config(page_title="혼합 경로 추천기", page_icon="🚌", layout="wide")

# =========================================================
# 설정
# =========================================================
MAX_TRANSIT_PATHS = 4
MAX_CANDIDATE_POINTS = 12
MAX_MIXED_CARDS = 15
MAX_INTERMEDIATE_CANDIDATES = 12
DIRECT_STOP_WALK_MIN = 3          # 첫/마지막 도보가 3분 이상이면 직접 정류장 택시 검토
DIRECT_STOP_EXTRA_FARE_ALLOW = 3000  # 시간이 더 좋아지면 3천원까지 추가 허용
# 너무 짧은 택시 구간 제거
MIN_TAXI_MIN = 4
MIN_TAXI_KM = 1.5
MIN_TAXI_FARE = 5000
ARRIVE_BUFFER_MIN = 15   # 15분 이상 여유 있으면 '충분히 제시간'으로 간주

# 버스 실시간 반영
REALTIME_BUS_ENABLED = True
REALTIME_BUS_CACHE_TTL = 20
REALTIME_BUS_WAIT_CAP_MIN = 15

# 지하철 시간표 반영
REALTIME_SUBWAY_SCHEDULE_ENABLED = True
SUBWAY_SCHEDULE_CACHE_TTL = 30

# 가성비 점수
VALUE_COST_WEIGHT = 0.30
VALUE_TIME_WEIGHT = 0.70

VALUE_KIND_PENALTY = {
    "transit": 0.03,
    "mixed_first": 0.00,
    "mixed_last": 0.00,
    "taxi": 0.12,
}

# =========================================================
# 혼합 후보 필터 기준
# =========================================================
# 택시만이랑 거의 비슷한 혼합인데, 이점이 없으면 제거
MIXED_NEAR_TAXI_MIN_COST_SAVE = 5000   # 택시만보다 최소 5천원은 싸야 의미 있음
MIXED_NEAR_TAXI_MIN_TIME_SAVE = 6      # 택시만보다 최소 6분 빨라야 의미 있음

# 대중교통보다 충분히 빨라지면 살림
MIXED_KEEP_TIME_SAVE_VS_TRANSIT = 12
MIXED_MAX_EXTRA_COST_VS_TRANSIT = 20000

# 혼합인데 택시 비중이 너무 큰 경우 "거의 택시만"으로 봄
MIXED_HIGH_TAXI_SHARE = 0.75
MIXED_HIGH_TAXI_KM = 20.0

# =========================================================
# ODsay Referer
# 배포 주소가 바뀌면 여기만 바꿔
# =========================================================
ODSAY_REFERER = "https://hybrid-route-prototype-kmwass9s4mjky8yrgn78la.streamlit.app/"

# =========================================================
# Secrets
# =========================================================
try:
    KAKAO_REST_API_KEY = st.secrets["KAKAO_REST_API_KEY"]
except Exception:
    KAKAO_REST_API_KEY = st.secrets["KAKAO_LOCAL_REST_KEY"]

ODSAY_API_KEY = st.secrets["ODSAY_API_KEY"]

# =========================================================
# 공통 유틸
# =========================================================

def make_point(name, x, y, walk_min=0):
    x = safe_float(x, None)
    y = safe_float(y, None)
    if not name or x is None or y is None:
        return None
    return {
        "name": str(name),
        "x": x,
        "y": y,
        "walk_min": safe_int(walk_min, 0),
    }


def get_first_boarding_stop(path):
    """
    경로 시작 부분에서 첫 대중교통 승차 정류장/역을 찾는다.
    앞에 붙은 도보 시간도 같이 계산.
    """
    if not isinstance(path, dict):
        return None

    subpaths = path.get("subPath", [])
    if not isinstance(subpaths, list):
        return None

    leading_walk = 0
    for sp in subpaths:
        if not isinstance(sp, dict):
            continue

        tt = sp.get("trafficType")
        if tt == 3:
            leading_walk += safe_int(sp.get("sectionTime", 0))
            continue

        if tt in (1, 2):
            return make_point(
                sp.get("startName"),
                sp.get("startX"),
                sp.get("startY"),
                walk_min=leading_walk,
            )

    return None


def get_last_alighting_stop(path):
    """
    경로 끝 부분에서 마지막 대중교통 하차 정류장/역을 찾는다.
    뒤에 붙은 도보 시간도 같이 계산.
    """
    if not isinstance(path, dict):
        return None

    subpaths = path.get("subPath", [])
    if not isinstance(subpaths, list):
        return None

    trailing_walk = 0
    for sp in reversed(subpaths):
        if not isinstance(sp, dict):
            continue

        tt = sp.get("trafficType")
        if tt == 3:
            trailing_walk += safe_int(sp.get("sectionTime", 0))
            continue

        if tt in (1, 2):
            return make_point(
                sp.get("endName"),
                sp.get("endX"),
                sp.get("endY"),
                walk_min=trailing_walk,
            )

    return None


def should_replace_with_direct_stop(old_time, old_cost, new_time, new_cost):
    """
    직접 정류장까지 택시로 가는 새 해가 더 낫다고 볼지 판단
    """
    # 완전 우월
    if new_time <= old_time and new_cost <= old_cost:
        return True

    # 시간 2분 이상 개선 + 비용 증가가 3천원 이하
    if new_time + 2 <= old_time and new_cost <= old_cost + DIRECT_STOP_EXTRA_FARE_ALLOW:
        return True

    return False
    
def kakao_headers():
    return {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}


def odsay_headers():
    if ODSAY_REFERER:
        return {"Referer": ODSAY_REFERER}
    return {}


def fmt_won(v):
    try:
        return f"{int(v):,}원"
    except Exception:
        return str(v)


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


def parse_arrive_by(arrive_by: str):
    if not arrive_by:
        return None
    try:
        hh, mm = map(int, arrive_by.strip().split(":"))
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target
    except Exception:
        return None


def calc_arrival_status(total_time_min: int, arrive_by: str):
    target = parse_arrive_by(arrive_by)
    if target is None:
        return {"text": "시간 비교 안 함", "late": False, "diff_min": None}

    recommend_depart = target - timedelta(minutes=total_time_min)
    eta_if_leave_now = datetime.now() + timedelta(minutes=total_time_min)
    diff_if_leave_now = math.floor((target - eta_if_leave_now).total_seconds() / 60)

    if diff_if_leave_now >= 0:
        depart_str = recommend_depart.strftime("%H:%M")
        return {
            "text": f"제시간 도착 (권장 출발: {depart_str})",
            "late": False,
            "diff_min": diff_if_leave_now,
        }

    return {
        "text": f"{abs(diff_if_leave_now)}분 지각 (지금 당장 출발해도 늦음!)",
        "late": True,
        "diff_min": diff_if_leave_now,
    }


def current_day_code():
    # ODsay DAY: 1 평일, 2 토요일, 3 공휴일/일요일
    wd = datetime.now().weekday()  # Mon=0 ... Sun=6
    if wd == 5:
        return 2
    if wd == 6:
        return 3
    return 1


def hhmm_after_offset(offset_min=0):
    t = datetime.now() + timedelta(minutes=offset_min)
    return t.strftime("%H%M")


# =========================================================
# 카카오 Local: 장소 -> 좌표
# =========================================================
@st.cache_data(ttl=300)
def search_place(query: str):
    keyword_url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    r = requests.get(
        keyword_url,
        headers=kakao_headers(),
        params={"query": query, "size": 1},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    docs = data.get("documents", [])

    if isinstance(docs, list) and docs and isinstance(docs[0], dict):
        d = docs[0]
        return {
            "name": d.get("place_name", query),
            "address": d.get("road_address_name") or d.get("address_name") or query,
            "x": float(d["x"]),
            "y": float(d["y"]),
        }

    address_url = "https://dapi.kakao.com/v2/local/search/address.json"
    r = requests.get(
        address_url,
        headers=kakao_headers(),
        params={"query": query, "analyze_type": "similar", "size": 1},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    docs = data.get("documents", [])

    if isinstance(docs, list) and docs and isinstance(docs[0], dict):
        d = docs[0]
        return {
            "name": query,
            "address": query,
            "x": float(d["x"]),
            "y": float(d["y"]),
        }

    raise ValueError(f"'{query}' 위치를 찾지 못했어. 더 구체적으로 입력해줘.")


# =========================================================
# ODsay 공통
# =========================================================
def parse_odsay_error(data):
    err = data.get("error") if isinstance(data, dict) else None

    if err is None:
        return None

    if isinstance(err, list) and len(err) > 0:
        first = err[0]
        if isinstance(first, dict):
            code = first.get("code", "")
            msg = first.get("message", "")
            return f"ODsay 오류 code={code}, message={msg}"
        return f"ODsay 오류: {err}"

    if isinstance(err, dict):
        code = err.get("code", "")
        msg = err.get("message") or err.get("msg") or str(err)
        return f"ODsay 오류 code={code}, message={msg}"

    return f"ODsay 오류: {err}"


# =========================================================
# ODsay: 대중교통 경로
# =========================================================
@st.cache_data(ttl=300)
def get_transit_paths(origin_x, origin_y, dest_x, dest_y):
    url = "https://api.odsay.com/v1/api/searchPubTransPathR"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": origin_x,
        "SY": origin_y,
        "EX": dest_x,
        "EY": dest_y,
        "OPT": 0,
        "lang": 0,
        "output": "json",
    }

    r = requests.get(url, params=params, headers=odsay_headers(), timeout=20)
    r.raise_for_status()
    data = r.json()

    err_msg = parse_odsay_error(data)
    if err_msg:
        raise ValueError(err_msg)

    result = data.get("result", {})
    paths = result.get("path", [])

    if not isinstance(paths, list) or len(paths) == 0:
        raise ValueError("대중교통 경로를 찾지 못했어.")

    return paths


@st.cache_data(ttl=300)
def get_best_transit_path(origin_x, origin_y, dest_x, dest_y):
    try:
        paths = get_transit_paths(origin_x, origin_y, dest_x, dest_y)
    except Exception:
        return None

    best = None
    best_key = None

    for path in paths:
        if not isinstance(path, dict):
            continue
        info = path.get("info", {})
        if not isinstance(info, dict):
            continue

        key = (
            safe_int(info.get("totalTime", 999999)),
            safe_int(info.get("payment", 999999)),
            safe_int(info.get("totalWalk", 999999)),
        )
        if best is None or key < best_key:
            best = path
            best_key = key

    return best


# =========================================================
# ODsay: 시간표 기반 지하철 경로
# =========================================================
@st.cache_data(ttl=SUBWAY_SCHEDULE_CACHE_TTL)
def get_subway_path_schedule(sid, eid, mode=1, day=1, time_hhmm=None, mid=None):
    url = "https://api.odsay.com/v1/api/subwayPathSchedule"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SID": sid,
        "EID": eid,
        "MODE": mode,
        "DAY": day,
        "output": "json",
    }
    if time_hhmm:
        params["TIME"] = time_hhmm
    if mid is not None:
        params["MID"] = mid

    r = requests.get(url, params=params, headers=odsay_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()

    err_msg = parse_odsay_error(data)
    if err_msg:
        return None

    result = data.get("result", {})
    paths = result.get("path", [])
    if not isinstance(paths, list) or not paths:
        return None

    best = None
    best_key = None
    for p in paths:
        if not isinstance(p, dict):
            continue
        info = p.get("info", {})
        if not isinstance(info, dict):
            continue
        key = (
            safe_int(info.get("totalTime", 999999)),
            safe_int(info.get("transferCount", 999999)),
        )
        if best is None or key < best_key:
            best = p
            best_key = key

    return best


# =========================================================
# 카카오모빌리티: 자동차/택시 요약
# =========================================================
@st.cache_data(ttl=300)
def get_car_summary(origin_x, origin_y, dest_x, dest_y):
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    params = {
        "origin": f"{origin_x},{origin_y}",
        "destination": f"{dest_x},{dest_y}",
        "priority": "RECOMMEND",
        "summary": "true",
        "alternatives": "false",
        "road_details": "false",
        "car_fuel": "GASOLINE",
        "car_hipass": "false",
    }
    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
        "Content-Type": "application/json",
    }

    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    routes = data.get("routes", [])
    if not isinstance(routes, list) or len(routes) == 0:
        raise ValueError("자동차 경로를 찾지 못했어.")

    summary = routes[0].get("summary", {})
    fare = summary.get("fare", {}) if isinstance(summary, dict) else {}

    return {
        "duration_min": math.ceil(safe_int(summary.get("duration", 0)) / 60),
        "distance_km": round(safe_int(summary.get("distance", 0)) / 1000, 1),
        "taxi_fare": safe_int(fare.get("taxi", 0)),
        "toll_fare": safe_int(fare.get("toll", 0)),
    }


def valid_taxi_leg(car):
    # 너무 짧은 택시 구간만 제거
    return (
        safe_int(car.get("duration_min"), 0) >= MIN_TAXI_MIN
        or safe_float(car.get("distance_km"), 0.0) >= MIN_TAXI_KM
        or safe_int(car.get("taxi_fare"), 0) >= MIN_TAXI_FARE
    )


def is_dominated(a, b):
    # b가 a보다 시간/비용 둘 다 같거나 더 좋고, 하나 이상 엄격히 좋으면 a는 지배당함
    return (
        b["time_min"] <= a["time_min"]
        and b["cost"] <= a["cost"]
        and (b["time_min"] < a["time_min"] or b["cost"] < a["cost"])
    )


def filter_dominated_candidates(candidates):
    kept = []
    for i, a in enumerate(candidates):
        dominated = False
        for j, b in enumerate(candidates):
            if i == j:
                continue
            if is_dominated(a, b):
                dominated = True
                break
        if not dominated:
            kept.append(a)
    return kept


def mixed_is_reasonable(candidate, taxi_only, best_transit):
    if candidate.get("kind") not in ("mixed_first", "mixed_last"):
        return True

    total_time = safe_int(candidate.get("time_min"), 0)
    total_cost = safe_int(candidate.get("cost"), 0)

    taxi_time = safe_int(candidate.get("taxi_time_min"), 0)
    taxi_km = safe_float(candidate.get("taxi_distance_km"), 0.0)
    taxi_share = taxi_time / max(total_time, 1)

    cost_save_vs_taxi = None
    time_save_vs_taxi = None
    if taxi_only is not None:
        cost_save_vs_taxi = taxi_only["cost"] - total_cost
        time_save_vs_taxi = taxi_only["time_min"] - total_time

    time_save_vs_transit = None
    extra_cost_vs_transit = None
    if best_transit is not None:
        time_save_vs_transit = best_transit["time_min"] - total_time
        extra_cost_vs_transit = total_cost - best_transit["cost"]

    # 1) 택시만보다 충분히 싸면 유지
    if cost_save_vs_taxi is not None and cost_save_vs_taxi >= MIXED_NEAR_TAXI_MIN_COST_SAVE:
        return True

    # 2) 택시만보다 충분히 빠르면 유지
    if time_save_vs_taxi is not None and time_save_vs_taxi >= MIXED_NEAR_TAXI_MIN_TIME_SAVE:
        return True

    # 3) 대중교통보다 충분히 빠르고 추가비용이 과하지 않으면 유지
    if time_save_vs_transit is not None and time_save_vs_transit >= MIXED_KEEP_TIME_SAVE_VS_TRANSIT:
        if extra_cost_vs_transit is None or extra_cost_vs_transit <= MIXED_MAX_EXTRA_COST_VS_TRANSIT:
            return True

    # 4) 대중교통은 지각인데 혼합은 제시간이면 유지
    if best_transit is not None and best_transit.get("late") and not candidate.get("late"):
        return True

    # 5) 거의 택시만 수준인데 이점이 없으면 제거
    if taxi_share >= MIXED_HIGH_TAXI_SHARE or taxi_km >= MIXED_HIGH_TAXI_KM:
        if cost_save_vs_taxi is not None and time_save_vs_taxi is not None:
            # 택시만보다 충분히 안 싸고, 충분히 안 빠르면 제거
            if (
                cost_save_vs_taxi < MIXED_NEAR_TAXI_MIN_COST_SAVE
                and time_save_vs_taxi < MIXED_NEAR_TAXI_MIN_TIME_SAVE
            ):
                return False

    return True


# =========================================================
# ODsay 실시간 버스
# =========================================================
def normalize_result_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    result = data.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ["real", "realtime", "station", "bus", "itemList", "lane", "result"]:
            val = result.get(key)
            if isinstance(val, list):
                return val
        for v in result.values():
            if isinstance(v, list):
                return v
    return []


@st.cache_data(ttl=REALTIME_BUS_CACHE_TTL)
def get_realtime_station(station_id):
    url = "https://api.odsay.com/v1/api/realtimeStation"
    params = {
        "apiKey": ODSAY_API_KEY,
        "stationID": station_id,
    }
    try:
        r = requests.get(url, params=params, headers=odsay_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        err_msg = parse_odsay_error(data) if isinstance(data, dict) else None
        if err_msg:
            return []
        return normalize_result_list(data)
    except Exception:
        return []


@st.cache_data(ttl=REALTIME_BUS_CACHE_TTL)
def get_realtime_route(bus_id):
    url = "https://api.odsay.com/v1/api/realtimeRoute"
    params = {
        "apiKey": ODSAY_API_KEY,
        "busID": bus_id,
    }
    try:
        r = requests.get(url, params=params, headers=odsay_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        err_msg = parse_odsay_error(data) if isinstance(data, dict) else None
        if err_msg:
            return []
        return normalize_result_list(data)
    except Exception:
        return []


def extract_station_id_from_subpath(sp):
    return first_non_none(
        sp,
        [
            "startID",
            "startStationID",
            "stationID",
            "localStationID",
            "startLocalStationID",
        ],
        None,
    )


def extract_bus_lane_meta(sp):
    if not isinstance(sp, dict) or sp.get("trafficType") != 2:
        return None

    lane = sp.get("lane", [])
    if not isinstance(lane, list) or not lane:
        return None

    l0 = lane[0]
    if not isinstance(l0, dict):
        return None

    bus_no = first_non_none(l0, ["busNo", "name", "busNum"], None)
    bus_id = first_non_none(l0, ["busID", "busLocalBlID", "localBusID", "busLocalBlId"], None)

    return {
        "bus_no": str(bus_no) if bus_no is not None else None,
        "bus_id": str(bus_id) if bus_id is not None else None,
    }


def parse_arrival_minutes(item):
    if not isinstance(item, dict):
        return None

    keys = [
        "arrivalSec",
        "arrivalSec1",
        "leftTime",
        "arrivalTime",
        "arriveTime",
        "predictTime1",
    ]

    for key in keys:
        if key not in item:
            continue
        raw = item.get(key)
        val = safe_int(raw, None)
        if val is None:
            continue

        if "Sec" in key or val > 60:
            return max(1, math.ceil(val / 60))
        return max(1, val)

    return None


def matches_bus(item, bus_no=None, bus_id=None):
    if not isinstance(item, dict):
        return False

    if bus_id is not None:
        for key in ["busID", "localBusID", "busLocalBlID", "busLocalBlId"]:
            if str(item.get(key, "")).strip() == str(bus_id).strip():
                return True

    if bus_no is not None:
        for key in ["busNo", "routeNo", "routeNm", "name"]:
            if str(item.get(key, "")).strip() == str(bus_no).strip():
                return True

    return False


def pick_best_realtime_arrival(arrivals, bus_no=None, bus_id=None):
    if not isinstance(arrivals, list) or not arrivals:
        return None

    matched = [a for a in arrivals if matches_bus(a, bus_no=bus_no, bus_id=bus_id)]
    pool = matched if matched else arrivals

    best = None
    best_min = None
    for item in pool:
        arr_min = parse_arrival_minutes(item)
        if arr_min is None:
            continue
        if best is None or arr_min < best_min:
            best = item
            best_min = arr_min

    return best


def get_bus_live_info_for_subpath(sp):
    if not REALTIME_BUS_ENABLED:
        return None

    if not isinstance(sp, dict) or sp.get("trafficType") != 2:
        return None

    lane_meta = extract_bus_lane_meta(sp)
    station_id = extract_station_id_from_subpath(sp)

    if lane_meta is None or station_id is None:
        return None

    bus_no = lane_meta.get("bus_no")
    bus_id = lane_meta.get("bus_id")

    arrivals = get_realtime_station(str(station_id))
    best_arrival_item = pick_best_realtime_arrival(arrivals, bus_no=bus_no, bus_id=bus_id)
    wait_min = parse_arrival_minutes(best_arrival_item) if best_arrival_item else None

    bus_positions = get_realtime_route(str(bus_id)) if bus_id else []
    bus_count = len(bus_positions) if isinstance(bus_positions, list) else None

    if wait_min is None and bus_count is None:
        return None

    return {
        "bus_no": bus_no,
        "bus_id": bus_id,
        "station_id": station_id,
        "wait_min": min(wait_min, REALTIME_BUS_WAIT_CAP_MIN) if wait_min is not None else None,
        "bus_count": bus_count,
    }


def compute_transit_live_adjustment(path, start_offset_min=0):
    if not REALTIME_BUS_ENABLED or not isinstance(path, dict):
        return {"extra_wait_min": 0, "notes": []}

    if start_offset_min > 0:
        return {"extra_wait_min": 0, "notes": []}

    subpaths = path.get("subPath", [])
    if not isinstance(subpaths, list):
        return {"extra_wait_min": 0, "notes": []}

    extra_wait = 0
    notes = []

    for sp in subpaths:
        if not isinstance(sp, dict) or sp.get("trafficType") != 2:
            continue

        live = get_bus_live_info_for_subpath(sp)
        if not live:
            continue

        start_name = sp.get("startName", "")
        bus_label = live.get("bus_no") or "버스"

        pieces = []
        if live.get("wait_min") is not None:
            extra_wait += live["wait_min"]
            pieces.append(f"도착 {live['wait_min']}분")
        if live.get("bus_count") is not None:
            pieces.append(f"운행 {live['bus_count']}대")

        if pieces:
            notes.append(f"{bus_label}번 ({start_name}) 실시간: " + ", ".join(pieces))

    return {
        "extra_wait_min": extra_wait,
        "notes": notes,
    }


# =========================================================
# 지하철 시간표 보정
# =========================================================
def extract_subway_ids(sp):
    sid = first_non_none(sp, ["startID", "startStationID"], None)
    eid = first_non_none(sp, ["endID", "endStationID"], None)
    return sid, eid


def extract_subway_line_name(sp):
    lane = sp.get("lane", [])
    if isinstance(lane, list) and lane and isinstance(lane[0], dict):
        return first_non_none(lane[0], ["name", "laneName"], "지하철")
    return "지하철"


def compute_subway_schedule_adjustment(path, start_offset_min=0):
    if not REALTIME_SUBWAY_SCHEDULE_ENABLED or not isinstance(path, dict):
        return {"delta_min": 0, "notes": []}

    subpaths = path.get("subPath", [])
    if not isinstance(subpaths, list):
        return {"delta_min": 0, "notes": []}

    delta_total = 0
    notes = []
    cumulative_min = start_offset_min
    day_code = current_day_code()

    for sp in subpaths:
        if not isinstance(sp, dict):
            continue

        base_section = safe_int(sp.get("sectionTime", 0))
        traffic_type = sp.get("trafficType")

        if traffic_type != 1:
            cumulative_min += base_section
            continue

        sid, eid = extract_subway_ids(sp)
        if sid is None or eid is None:
            cumulative_min += base_section
            continue

        time_hhmm = hhmm_after_offset(cumulative_min)
        sched_path = get_subway_path_schedule(
            sid=sid,
            eid=eid,
            mode=1,
            day=day_code,
            time_hhmm=time_hhmm,
        )

        if not isinstance(sched_path, dict):
            cumulative_min += base_section
            continue

        info = sched_path.get("info", {})
        if not isinstance(info, dict):
            cumulative_min += base_section
            continue

        sched_total = safe_int(info.get("totalTime", base_section))
        dep = info.get("departureTime")
        arr = info.get("arrivalTime")

        delta = sched_total - base_section
        delta_total += delta

        start_name = sp.get("startName", "")
        end_name = sp.get("endName", "")
        line_name = extract_subway_line_name(sp)

        pieces = [f"{line_name} {start_name}→{end_name}"]
        if dep:
            pieces.append(f"{dep} 출발")
        if arr:
            pieces.append(f"{arr} 도착")
        if delta != 0:
            sign = "+" if delta > 0 else ""
            pieces.append(f"{sign}{delta}분 보정")

        notes.append(" / ".join(pieces))
        cumulative_min += sched_total

    return {
        "delta_min": delta_total,
        "notes": notes,
    }


# =========================================================
# 포맷팅
# =========================================================
def path_type_to_text(path_type):
    mapping = {
        1: "지하철",
        2: "버스",
        3: "버스+지하철",
    }
    return mapping.get(path_type, f"기타({path_type})")


def format_subpath(sp):
    if not isinstance(sp, dict):
        return str(sp)

    traffic_type = sp.get("trafficType")
    section_time = safe_int(sp.get("sectionTime", 0))
    start_name = sp.get("startName", "")
    end_name = sp.get("endName", "")

    if traffic_type == 3:
        return f"도보 {section_time}분"

    lane = sp.get("lane", [])
    lane_label = "이동"

    if isinstance(lane, list) and len(lane) > 0:
        first_lane = lane[0]
        if isinstance(first_lane, dict):
            if traffic_type == 1:
                lane_label = first_lane.get("name", "지하철")
            elif traffic_type == 2:
                lane_label = str(first_lane.get("busNo", "버스"))
        else:
            lane_label = str(first_lane)

    if traffic_type == 1:
        return f"{lane_label}: {start_name} → {end_name} ({section_time}분)"
    if traffic_type == 2:
        return f"{lane_label}번: {start_name} → {end_name} ({section_time}분)"
    return f"기타 이동: {start_name} → {end_name} ({section_time}분)"


def summarize_subpaths(subpaths):
    lines = []
    if not isinstance(subpaths, list):
        return lines
    for sp in subpaths:
        lines.append(format_subpath(sp))
    return lines


def path_to_summary(path, start_offset_min=0):
    if path is None or not isinstance(path, dict):
        return None

    info = path.get("info", {})
    subpaths = path.get("subPath", [])

    bus_live = compute_transit_live_adjustment(path, start_offset_min=start_offset_min)
    subway_sched = compute_subway_schedule_adjustment(path, start_offset_min=start_offset_min)

    base_time = safe_int(info.get("totalTime", 0))
    bus_extra = safe_int(bus_live.get("extra_wait_min", 0))
    subway_delta = safe_int(subway_sched.get("delta_min", 0))

    return {
        "time_min": base_time + bus_extra + subway_delta,
        "base_time_min": base_time,
        "bus_live_extra_min": bus_extra,
        "subway_sched_delta_min": subway_delta,
        "cost": safe_int(info.get("payment", 0)),
        "walk_m": safe_int(info.get("totalWalk", 0)),
        "bus_transit_count": safe_int(info.get("busTransitCount", 0)),
        "subway_transit_count": safe_int(info.get("subwayTransitCount", 0)),
        "path_type": path_type_to_text(path.get("pathType")),
        "steps": summarize_subpaths(subpaths),
        "live_notes": bus_live.get("notes", []) + subway_sched.get("notes", []),
    }
def normalize_candidate_point(name, x, y):
    if not name:
        return None

    x = safe_float(x, None)
    y = safe_float(y, None)

    if x is None or y is None:
        return None

    return {
        "name": str(name),
        "x": x,
        "y": y,
    }


def get_pass_stop_points(sp):
    """
    ODsay subPath 안의 passStopList에서 중간 정류장/역 후보를 뽑는다.
    버스/지하철 응답 구조가 조금 달라도 최대한 유연하게 처리.
    """
    if not isinstance(sp, dict):
        return []

    pass_stop_list = sp.get("passStopList")
    if not isinstance(pass_stop_list, dict):
        return []

    raw_items = None
    for key in ["stations", "station", "list", "items"]:
        val = pass_stop_list.get(key)
        if isinstance(val, list):
            raw_items = val
            break

    if not isinstance(raw_items, list):
        return []

    points = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = first_non_none(
            item,
            ["stationName", "name", "stopName", "startName", "endName"],
            None,
        )
        x = first_non_none(
            item,
            ["x", "X", "gpsX", "lon", "lng", "longitude"],
            None,
        )
        y = first_non_none(
            item,
            ["y", "Y", "gpsY", "lat", "latitude"],
            None,
        )

        p = normalize_candidate_point(name, x, y)
        if p is not None:
            points.append(p)

    return points


def collect_subpath_points(sp):
    """
    한 subPath에서 start / passStopList / end 를 모두 모은다.
    """
    if not isinstance(sp, dict):
        return []

    points = []

    start_p = normalize_candidate_point(
        sp.get("startName"),
        sp.get("startX"),
        sp.get("startY"),
    )
    if start_p is not None:
        points.append(start_p)

    points.extend(get_pass_stop_points(sp))

    end_p = normalize_candidate_point(
        sp.get("endName"),
        sp.get("endX"),
        sp.get("endY"),
    )
    if end_p is not None:
        points.append(end_p)

    # 중복 제거
    dedup = []
    seen = set()
    for p in points:
        key = (round(p["x"], 6), round(p["y"], 6), p["name"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(p)

    return dedup

# =========================================================
# 후보 지점 추출
# =========================================================
def extract_board_candidates(paths, origin):
    """
    출발지에서 택시를 타고 갈 '탑승 후보'를 추출.
    기존: 각 subPath의 start만
    변경: start + 중간 정류장/역 + end 전부 후보화
    """
    candidates = []
    seen = set()

    for path in paths[:MAX_TRANSIT_PATHS]:
        if not isinstance(path, dict):
            continue

        subpaths = path.get("subPath", [])
        if not isinstance(subpaths, list):
            continue

        for sp in subpaths:
            if not isinstance(sp, dict):
                continue

            traffic_type = sp.get("trafficType")
            if traffic_type not in (1, 2):  # 지하철/버스만
                continue

            points = collect_subpath_points(sp)

            for p in points:
                if is_same_point(p["x"], p["y"], origin["x"], origin["y"]):
                    continue

                key = (round(p["x"], 6), round(p["y"], 6), p["name"])
                if key in seen:
                    continue
                seen.add(key)

                candidates.append(p)

    return candidates[:MAX_INTERMEDIATE_CANDIDATES]


def extract_split_candidates(paths, destination):
    """
    목적지 직전에 택시로 전환할 '하차 후보'를 추출.
    기존: 각 subPath의 end만
    변경: start + 중간 정류장/역 + end 전부 후보화
    """
    candidates = []
    seen = set()

    for path in paths[:MAX_TRANSIT_PATHS]:
        if not isinstance(path, dict):
            continue

        subpaths = path.get("subPath", [])
        if not isinstance(subpaths, list):
            continue

        for sp in subpaths:
            if not isinstance(sp, dict):
                continue

            traffic_type = sp.get("trafficType")
            if traffic_type not in (1, 2):
                continue

            points = collect_subpath_points(sp)

            for p in points:
                if is_same_point(p["x"], p["y"], destination["x"], destination["y"]):
                    continue

                key = (round(p["x"], 6), round(p["y"], 6), p["name"])
                if key in seen:
                    continue
                seen.add(key)

                candidates.append(p)

    return candidates[:MAX_INTERMEDIATE_CANDIDATES]


# =========================================================
# 후보 생성
# =========================================================
def build_route_candidates(origin, destination, arrive_by):
    base_transit_paths = get_transit_paths(
        origin["x"], origin["y"], destination["x"], destination["y"]
    )
    car_full = get_car_summary(
        origin["x"], origin["y"], destination["x"], destination["y"]
    )

    candidates = []

    # 1) 택시만
    taxi_status = calc_arrival_status(car_full["duration_min"], arrive_by)
    candidates.append({
        "kind": "taxi",
        "title": "택시만",
        "subtitle": None,
        "time_min": car_full["duration_min"],
        "cost": car_full["taxi_fare"],
        "distance_km": car_full["distance_km"],
        "walk_m": None,
        "status": taxi_status["text"],
        "late": taxi_status["late"],
        "late_diff": taxi_status["diff_min"],
        "reason": "가장 빠르지만 비용이 큼",
        "steps": ["출발지에서 바로 택시 이용"],
        "live_notes": [],
    })

    # 2) 대중교통만
    for idx, path in enumerate(base_transit_paths[:MAX_TRANSIT_PATHS], start=1):
        if not isinstance(path, dict):
            continue

        summary = path_to_summary(path, start_offset_min=0)
        if summary is None:
            continue

        transit_status = calc_arrival_status(summary["time_min"], arrive_by)

        reason = "가장 저렴한 선택지에 가까움"
        if summary["bus_live_extra_min"] > 0:
            reason += f" / 버스 실시간 대기 {summary['bus_live_extra_min']}분 반영"
        if summary["subway_sched_delta_min"] != 0:
            sign = "+" if summary["subway_sched_delta_min"] > 0 else ""
            reason += f" / 지하철 시간표 {sign}{summary['subway_sched_delta_min']}분 보정"

        candidates.append({
            "kind": "transit",
            "title": "대중교통만",
            "subtitle": f"{idx}번 경로 · {summary['path_type']}",
            "time_min": summary["time_min"],
            "cost": summary["cost"],
            "distance_km": None,
            "walk_m": summary["walk_m"],
            "status": transit_status["text"],
            "late": transit_status["late"],
            "late_diff": transit_status["diff_min"],
            "reason": reason,
            "steps": summary["steps"],
            "bus_transit_count": summary["bus_transit_count"],
            "subway_transit_count": summary["subway_transit_count"],
            "live_notes": summary["live_notes"],
        })

    # 3) 택시 -> 대중교통
    board_candidates = extract_board_candidates(base_transit_paths, origin)
    for board in board_candidates:
        try:
            car_head = get_car_summary(
                origin["x"], origin["y"], board["x"], board["y"]
            )
            if not valid_taxi_leg(car_head):
                continue

            transit_from_board = get_best_transit_path(
                board["x"], board["y"], destination["x"], destination["y"]
            )
            if transit_from_board is None:
                continue

            # 첫 승차 정류장까지 도보가 길면 직접 그 정류장까지 택시
            first_stop = get_first_boarding_stop(transit_from_board)
            if (
                first_stop is not None
                and first_stop["walk_min"] >= DIRECT_STOP_WALK_MIN
                and not is_same_point(
                    board["x"], board["y"], first_stop["x"], first_stop["y"]
                )
            ):
                try:
                    car_head_direct = get_car_summary(
                        origin["x"], origin["y"], first_stop["x"], first_stop["y"]
                    )
                    if valid_taxi_leg(car_head_direct):
                        transit_from_direct = get_best_transit_path(
                            first_stop["x"], first_stop["y"],
                            destination["x"], destination["y"]
                        )
                        if transit_from_direct is not None:
                            old_summary = path_to_summary(
                                transit_from_board,
                                start_offset_min=car_head["duration_min"]
                            )
                            new_summary = path_to_summary(
                                transit_from_direct,
                                start_offset_min=car_head_direct["duration_min"]
                            )

                            if old_summary is not None and new_summary is not None:
                                old_total_time = (
                                    car_head["duration_min"] + old_summary["time_min"]
                                )
                                old_total_cost = (
                                    car_head["taxi_fare"] + old_summary["cost"]
                                )

                                new_total_time = (
                                    car_head_direct["duration_min"] + new_summary["time_min"]
                                )
                                new_total_cost = (
                                    car_head_direct["taxi_fare"] + new_summary["cost"]
                                )

                                if should_replace_with_direct_stop(
                                    old_total_time, old_total_cost,
                                    new_total_time, new_total_cost
                                ):
                                    board = {
                                        "name": first_stop["name"],
                                        "x": first_stop["x"],
                                        "y": first_stop["y"],
                                    }
                                    car_head = car_head_direct
                                    transit_from_board = transit_from_direct
                except Exception:
                    pass

            transit_summary = path_to_summary(
                transit_from_board, start_offset_min=car_head["duration_min"]
            )
            if transit_summary is None:
                continue

            total_mix_time = car_head["duration_min"] + transit_summary["time_min"]
            total_mix_cost = car_head["taxi_fare"] + transit_summary["cost"]
            mix_status = calc_arrival_status(total_mix_time, arrive_by)

            reason = f"초반만 택시로 이동하고 {board['name']}부터 대중교통 최적 경로 이용"
            if transit_summary["subway_sched_delta_min"] != 0:
                sign = "+" if transit_summary["subway_sched_delta_min"] > 0 else ""
                reason += f" / 지하철 시간표 {sign}{transit_summary['subway_sched_delta_min']}분 보정"

            candidates.append({
                "kind": "mixed_first",
                "title": "택시 → 대중교통",
                "subtitle": f"{board['name']}에서 대중교통 탑승",
                "time_min": total_mix_time,
                "cost": total_mix_cost,
                "distance_km": car_head["distance_km"],
                "walk_m": transit_summary["walk_m"],
                "status": mix_status["text"],
                "late": mix_status["late"],
                "late_diff": mix_status["diff_min"],
                "reason": reason,
                "steps": [f"출발지 → {board['name']} 택시 {car_head['duration_min']}분"] + transit_summary["steps"],
                "board_name": board["name"],
                "live_notes": transit_summary["live_notes"],
                "taxi_time_min": car_head["duration_min"],
                "taxi_distance_km": car_head["distance_km"],
                "taxi_cost": car_head["taxi_fare"],
                "transit_time_min": transit_summary["time_min"],
                "transit_cost": transit_summary["cost"],
            })
        except Exception:
            continue

    # 4) 대중교통 -> 택시
    split_candidates = extract_split_candidates(base_transit_paths, destination)
    for split in split_candidates:
        try:
            transit_to_split = get_best_transit_path(
                origin["x"], origin["y"], split["x"], split["y"]
            )
            if transit_to_split is None:
                continue

            transit_summary = path_to_summary(transit_to_split, start_offset_min=0)
            if transit_summary is None:
                continue

            car_tail = get_car_summary(
                split["x"], split["y"], destination["x"], destination["y"]
            )
            if not valid_taxi_leg(car_tail):
                continue

            # 마지막 하차 후 도보가 길면 실제 마지막 하차 정류장에서 바로 택시
            last_stop = get_last_alighting_stop(transit_to_split)
            if (
                last_stop is not None
                and last_stop["walk_min"] >= DIRECT_STOP_WALK_MIN
                and not is_same_point(
                    split["x"], split["y"], last_stop["x"], last_stop["y"]
                )
            ):
                try:
                    transit_to_direct = get_best_transit_path(
                        origin["x"], origin["y"], last_stop["x"], last_stop["y"]
                    )
                    car_tail_direct = get_car_summary(
                        last_stop["x"], last_stop["y"],
                        destination["x"], destination["y"]
                    )

                    if transit_to_direct is not None and valid_taxi_leg(car_tail_direct):
                        new_transit_summary = path_to_summary(
                            transit_to_direct, start_offset_min=0
                        )
                        if new_transit_summary is not None:
                            old_total_time = (
                                transit_summary["time_min"] + car_tail["duration_min"]
                            )
                            old_total_cost = (
                                transit_summary["cost"] + car_tail["taxi_fare"]
                            )

                            new_total_time = (
                                new_transit_summary["time_min"] + car_tail_direct["duration_min"]
                            )
                            new_total_cost = (
                                new_transit_summary["cost"] + car_tail_direct["taxi_fare"]
                            )

                            if should_replace_with_direct_stop(
                                old_total_time, old_total_cost,
                                new_total_time, new_total_cost
                            ):
                                split = {
                                    "name": last_stop["name"],
                                    "x": last_stop["x"],
                                    "y": last_stop["y"],
                                }
                                transit_to_split = transit_to_direct
                                transit_summary = new_transit_summary
                                car_tail = car_tail_direct
                except Exception:
                    pass

            total_mix_time = transit_summary["time_min"] + car_tail["duration_min"]
            total_mix_cost = transit_summary["cost"] + car_tail["taxi_fare"]
            mix_status = calc_arrival_status(total_mix_time, arrive_by)

            reason = f"{split['name']}까지 대중교통 후 마지막 구간만 택시"
            if transit_summary["bus_live_extra_min"] > 0:
                reason += f" / 버스 실시간 대기 {transit_summary['bus_live_extra_min']}분 반영"
            if transit_summary["subway_sched_delta_min"] != 0:
                sign = "+" if transit_summary["subway_sched_delta_min"] > 0 else ""
                reason += f" / 지하철 시간표 {sign}{transit_summary['subway_sched_delta_min']}분 보정"

            candidates.append({
                "kind": "mixed_last",
                "title": "대중교통 → 택시",
                "subtitle": f"{split['name']}에서 택시 전환",
                "time_min": total_mix_time,
                "cost": total_mix_cost,
                "distance_km": car_tail["distance_km"],
                "walk_m": transit_summary["walk_m"],
                "status": mix_status["text"],
                "late": mix_status["late"],
                "late_diff": mix_status["diff_min"],
                "reason": reason,
                "steps": transit_summary["steps"] + [f"{split['name']} → 목적지 택시 {car_tail['duration_min']}분"],
                "split_name": split["name"],
                "live_notes": transit_summary["live_notes"],
                "taxi_time_min": car_tail["duration_min"],
                "taxi_distance_km": car_tail["distance_km"],
                "taxi_cost": car_tail["taxi_fare"],
                "transit_time_min": transit_summary["time_min"],
                "transit_cost": transit_summary["cost"],
            })
        except Exception:
            continue

    # 중복 제거
    unique = []
    seen = set()
    for c in candidates:
        key = (c["kind"], c.get("subtitle"), c["time_min"], c["cost"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    taxi_only = next((c for c in unique if c["kind"] == "taxi"), None)

    transit_candidates = [c for c in unique if c["kind"] == "transit"]
    best_transit = None
    if transit_candidates:
        best_transit = sorted(
            transit_candidates,
            key=lambda x: (x["time_min"], x["cost"])
        )[0]

    filtered = [
        c for c in unique
        if mixed_is_reasonable(c, taxi_only, best_transit)
    ]

    filtered = filter_dominated_candidates(filtered)

    return filtered

# =========================================================
# 정렬 / 추천
# =========================================================
def normalize_score(v, arr):
    mn = min(arr)
    mx = max(arr)
    if mx == mn:
        return 0.0
    return (v - mn) / (mx - mn)


def value_score(c, candidates, arrive_by=None):
    late_penalty = 1 if c["late"] else 0
    kind_penalty = VALUE_KIND_PENALTY.get(c["kind"], 0)

    costs = [x["cost"] for x in candidates]
    cost_norm = normalize_score(c["cost"], costs)

    # 도착 희망 시간이 없으면 기존처럼 시간/비용 혼합
    if not arrive_by:
        times = [x["time_min"] for x in candidates]
        time_norm = normalize_score(c["time_min"], times)
        return late_penalty + kind_penalty + (0.30 * cost_norm) + (0.70 * time_norm)

    # 도착 희망 시간이 있으면:
    # 1) 늦는 후보는 강하게 불이익
    # 2) 제시간 후보는 "얼마나 더 빨리 도착하느냐"보다
    #    "여유가 충분한가"만 보고, 충분하면 비용 중심으로 비교
    urgency_values = []
    for x in candidates:
        if x["late"]:
            urgency = 1000 + abs(x["late_diff"]) if x["late_diff"] is not None else 1000
        else:
            slack = max(0, x["diff_min"] if x["diff_min"] is not None else 0)
            urgency = max(0, ARRIVE_BUFFER_MIN - slack)
        urgency_values.append(urgency)

    if c["late"]:
        my_urgency = 1000 + abs(c["late_diff"]) if c["late_diff"] is not None else 1000
    else:
        slack = max(0, c["diff_min"] if c["diff_min"] is not None else 0)
        my_urgency = max(0, ARRIVE_BUFFER_MIN - slack)

    urgency_norm = normalize_score(my_urgency, urgency_values)

    # 도착 희망 시간이 있을 때는 비용 중심
    return (1.5 * late_penalty) + kind_penalty + (0.75 * cost_norm) + (0.25 * urgency_norm)


def pick_best(candidates, priority, arrive_by=None):
    if not candidates:
        return None

    def cost_key(c):
        return (1 if c["late"] else 0, c["cost"], c["time_min"])

    def ontime_key(c):
        if not c["late"]:
            return (0, c["time_min"], c["cost"])
        return (1, abs(c["late_diff"]) if c["late_diff"] is not None else 999999, c["time_min"], c["cost"])

   def value_key(c):
    return (value_score(c, candidates, arrive_by=arrive_by), c["cost"], c["time_min"])

    if priority == "최저비용":
        return sorted(candidates, key=cost_key)[0]
    elif priority == "제시간 도착":
        return sorted(candidates, key=ontime_key)[0]
    else:
        return sorted(candidates, key=value_key)[0]


def pick_best_by_kind(candidates, kind, priority, arrive_by=None):
    subset = [c for c in candidates if c["kind"] == kind]
    if not subset:
        return None
    return pick_best(subset, priority, arrive_by=arrive_by))


# =========================================================
# UI
# =========================================================
st.title("혼합 경로 추천기")
st.caption("실제 대중교통 + 택시 + 버스 실시간 + 지하철 시간표 반영")

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

if st.button("실제 혼합 경로 검색", use_container_width=True):
    if not origin_text or not destination_text:
        st.warning("출발지와 목적지를 입력해줘.")
    else:
        try:
            with st.spinner("장소 검색 중..."):
                origin = search_place(origin_text)
                destination = search_place(destination_text)

            with st.spinner("대중교통 / 택시 / 혼합 경로 계산 중..."):
                all_candidates = build_route_candidates(origin, destination, arrive_by)

            if not all_candidates:
                st.error("경로를 만들지 못했어.")
                st.stop()

            best_overall = pick_best(all_candidates, priority, arrive_by=arrive_by)
            best_transit = pick_best_by_kind(all_candidates, "transit", priority, arrive_by=arrive_by)
            best_taxi = pick_best_by_kind(all_candidates, "taxi", priority, arrive_by=arrive_by)
            best_mixed_first = pick_best_by_kind(all_candidates, "mixed_first", priority, arrive_by=arrive_by)
            best_mixed_last = pick_best_by_kind(all_candidates, "mixed_last", priority, arrive_by=arrive_by)
            
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
                    if best_overall.get("distance_km") is not None:
                        c3.metric("차량 거리", f"{best_overall['distance_km']}km")
                    else:
                        c3.metric("구분", best_overall["kind"])

                    if best_overall["late"]:
                        st.error(best_overall["status"])
                    else:
                        st.success(best_overall["status"])

                    st.write(f"추천 이유: {best_overall['reason']}")

                    if best_overall["kind"] in ("mixed_first", "mixed_last"):
                        taxi_time = safe_int(best_overall.get("taxi_time_min"), 0)
                        total_time = max(safe_int(best_overall.get("time_min"), 1), 1)
                        taxi_share = round(taxi_time / total_time * 100)
                        st.write(f"택시 비중: {taxi_share}%")

                    if best_overall.get("live_notes"):
                        st.write("실시간/시간표 정보")
                        for note in best_overall["live_notes"][:6]:
                            st.write(f"- {note}")

                    st.write("세부 흐름")
                    for line in best_overall.get("steps", [])[:12]:
                        st.write(f"- {line}")

            st.subheader("비교 카드")
            compare_routes = [
                ("대중교통만", best_transit),
                ("택시 → 대중교통", best_mixed_first),
                ("대중교통 → 택시", best_mixed_last),
                ("택시만", best_taxi),
            ]

            for label, route in compare_routes:
                if route is None:
                    continue

                with st.container(border=True):
                    st.markdown(f"### {label}")
                    if route.get("subtitle"):
                        st.caption(route["subtitle"])

                    c1, c2, c3 = st.columns(3)
                    c1.metric("총 시간", f"{route['time_min']}분")
                    c2.metric("총 비용", fmt_won(route["cost"]))
                    if route.get("distance_km") is not None:
                        c3.metric("차량 거리", f"{route['distance_km']}km")
                    else:
                        walk_m = route.get("walk_m")
                        c3.metric("총 도보", f"{walk_m}m" if walk_m is not None else "-")

                    if route["late"]:
                        st.error(route["status"])
                    else:
                        st.success(route["status"])

                    st.write(f"설명: {route['reason']}")

                    if route["kind"] == "transit":
                        st.write(
                            f"환승: 버스 {route.get('bus_transit_count', 0)}회 / "
                            f"지하철 {route.get('subway_transit_count', 0)}회"
                        )

                    if route["kind"] in ("mixed_first", "mixed_last"):
                        taxi_time = safe_int(route.get("taxi_time_min"), 0)
                        total_time = max(safe_int(route.get("time_min"), 1), 1)
                        taxi_share = round(taxi_time / total_time * 100)
                        st.write(f"택시 비중: {taxi_share}%")

                    if route.get("live_notes"):
                        st.write("실시간/시간표 정보")
                        for note in route["live_notes"][:6]:
                            st.write(f"- {note}")

                    st.write("세부 경로")
                    for line in route.get("steps", [])[:12]:
                        st.write(f"- {line}")

            mixed_all = [c for c in all_candidates if c["kind"] in ("mixed_first", "mixed_last")]
            mixed_all_sorted = sorted(
                mixed_all,
                key=lambda x: (value_score(x, all_candidates), x["cost"], x["time_min"])
            )

            with st.expander("혼합 후보 전체 보기"):
                if not mixed_all_sorted:
                    st.write("혼합 후보가 아직 생성되지 않았어.")
                else:
                    for i, c in enumerate(mixed_all_sorted[:MAX_MIXED_CARDS], start=1):
                        taxi_time = safe_int(c.get("taxi_time_min"), 0)
                        total_time = max(safe_int(c.get("time_min"), 1), 1)
                        taxi_share = round(taxi_time / total_time * 100)

                        st.write(
                            f"{i}. [{c['title']}] {c.get('subtitle', '')} | "
                            f"{c['time_min']}분 | {fmt_won(c['cost'])} | "
                            f"택시비중 {taxi_share}% | {c['status']}"
                        )
                        if c.get("live_notes"):
                            for note in c["live_notes"][:3]:
                                st.write(f"   - {note}")

            with st.expander("전체 후보 보기 (점수 디버깅)"):
                all_sorted = sorted(
                    all_candidates,
                    key=lambda x: (value_score(x, all_candidates), x["cost"], x["time_min"])
                )
                for i, c in enumerate(all_sorted, start=1):
                    score = value_score(c, all_candidates)
                    extra = ""
                    if c["kind"] in ("mixed_first", "mixed_last"):
                        taxi_time = safe_int(c.get("taxi_time_min"), 0)
                        total_time = max(safe_int(c.get("time_min"), 1), 1)
                        taxi_share = round(taxi_time / total_time * 100)
                        extra = f" | 🚕 비중 {taxi_share}%"

                    st.write(
                        f"{i}. [{c['kind']}] {c['title']}"
                        f"{' / ' + c['subtitle'] if c.get('subtitle') else ''}"
                        f" | ⏳ {c['time_min']}분 | 💸 {fmt_won(c['cost'])}"
                        f"{extra}"
                        f" | 🎯 점수: {score:.3f} | {c['status']}"
                    )

            st.caption("버스 실시간 값이 잡히면 대기시간을 더하고, 지하철 구간은 시간표 기반 경로검색으로 보정해요. 정보가 없으면 기본 경로값으로 fallback 합니다.")

        except Exception as e:
            st.error(f"오류: {e}")
            with st.expander("에러 상세 보기"):
                st.exception(e)
