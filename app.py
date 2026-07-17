import streamlit as st
import datetime as dt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import time
import concurrent.futures

# --- 🛠️ 기본 설정 ---
KST = dt.timezone(dt.timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json;*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# --- 🔌 초고속 단일 요청 세션 ---
def make_session():
    s = requests.Session()
    retry = Retry(
        total=1,  # 속도를 위해 재시도 1회로 축소
        backoff_factor=0.2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

SESSION = make_session()

def safe_get(url, params=None, timeout=2.0):
    """지연 방지를 위해 타임아웃을 2초로 대폭 제한"""
    return SESSION.get(url, params=params, headers=HEADERS, timeout=timeout)

# =========================================================
# 🌐 데이터 수집 함수 (Yahoo / Naver 개별 최적화)
# =========================================================
def fetch_yahoo(ticker, debug=None, scale=1.0):
    try:
        r = safe_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"interval": "1d", "range": "2d"})
        if debug is not None:
            debug["status_code"] = r.status_code
        payload = r.json()
        result = payload.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if price is None:
            return None
        price *= scale
        change = (price - prev * scale) if prev is not None else None
        return {"value": price, "change": change}
    except Exception as e:
        if debug is not None:
            debug["error"] = str(e)
        return None

def fetch_korea_bond(marketindex_cd, debug=None):
    try:
        r = safe_get(
            "https://finance.naver.com/marketindex/interestDailyQuote.naver",
            params={"marketindexCd": marketindex_cd, "page": 1}
        )
        if debug is not None:
            debug["status_code"] = r.status_code
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
        values = []
        for row in rows:
            nums_in_row = re.findall(r'<td[^>]*class="num"[^>]*>\s*([\d,.\-]+)\s*</td>', row)
            if nums_in_row:
                try:
                    values.append(float(nums_in_row[0].replace(",", "")))
                except ValueError:
                    continue
        if len(values) < 2:
            return None
        return {"value": values[0], "change": values[0] - values[1]}
    except Exception as e:
        if debug is not None:
            debug["error"] = str(e)
        return None

# =========================================================
# 🔄 100% 완전 병렬 로더 (순차 대기 없음)
# =========================================================
def load_market_data():
    tasks = {}
    debug_refs = {}

    # 야후 파이낸스 수집 대상 플래팅
    yahoo_items = {
        "KOSPI": ("^KS11", 1.0),
        "KOSDAQ": ("^KQ11", 1.0),
        "S&P 500": ("^GSPC", 1.0),
        "NASDAQ": ("^IXIC", 1.0),
        "필라델피아 반도체": ("^SOX", 1.0),
        "원달러 환율": ("KRW=X", 1.0),
        "엔화 환율": ("JPYKRW=X", 1.0),
        "유로 환율": ("EURKRW=X", 1.0),
        "위안화 환율": ("CNYKRW=X", 1.0),
        "달러 인덱스": ("DX-Y.NYB", 1.0),
        "유가 (WTI)": ("CL=F", 1.0),
        "국제 금": ("GC=F", 1.0),
        "VIX 공포지수": ("^VIX", 1.0),
        "미국 국채 2년": ("^US2Y", 0.1),  # 야후 금리 인덱스 10배 보정
    }

    for key, (ticker, scale) in yahoo_items.items():
        d = {}
        debug_refs[key] = d
        tasks[key] = lambda t=ticker, s=scale, d=d: fetch_yahoo(t, debug=d, scale=s)

    # 네이버 국내 3년물 수집 대상 추가
    d_kr3y = {}
    debug_refs["한국 국채 3년"] = d_kr3y
    tasks["한국 국채 3년"] = lambda d=d_kr3y: fetch_korea_bond("IRR_GOVT03Y", debug=d)

    results = {}
    st.session_state.setdefault("_last_good", {})
    current_successes = set()

    with st.spinner("⚡ 1초만에 글로벌 지표 동기화 중..."):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_key = {executor.submit(func): key for key, func in tasks.items()}
            # 최대 대기시간을 15초에서 2.5초로 극단적 단축
            done, not_done = concurrent.futures.wait(future_to_key.keys(), timeout=2.5)
            
            for future in done:
                key = future_to_key[future]
                try:
                    res = future.result()
                    if res and res.get("value") is not None:
                        res["source"] = "Naver" if key == "한국 국채 3년" else "Yahoo"
                        st.session_state["_last_good"][key] = {
                            **res,
                            "cached_at": dt.datetime.now(KST).strftime("%H:%M:%S"),
                        }
                        current_successes.add(key)
                except Exception as e:
                    debug_refs.setdefault(key, {})["fatal_error"] = str(e)
                    
            for future in not_done:
                key = future_to_key[future]
                debug_refs.setdefault(key, {})["error"] = "타임아웃 한도 초과"

    for key in tasks.keys():
        if key in current_successes:
            results[key] = {**st.session_state["_last_good"][key], "stale": False}
        elif key in st.session_state["_last_good"]:
            results[key] = {**st.session_state["_last_good"][key], "stale": True}
        else:
            results[key] = {"value": None, "change": None, "stale": True, "cached_at": "N/A", "source": ""}

    st.session_state["_debug"] = debug_refs
    return results

# =========================================================
# 🖥️ Streamlit 화면 구성 (초슬림 배치)
# =========================================================
st.set_page_config(page_title="실시간 글로벌 대시보드", page_icon="📈", layout="wide")
st.title("📈 글로벌 시장 지표 실시간 트래커")
st.write(f"조회 시각 (KST): `{dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}`")

st.button("🔄 실시간 지표 새로고침")

data = load_market_data()

def show_metric(col, label, v, fmt, unit=""):
    with col:
        val_str = f"{v['value']:{fmt}}{unit}" if v.get("value") is not None else "N/A"
        delta_str = f"{v['change']:+{fmt}}{unit}" if v.get("change") is not None else None
        tag = f" [{v['source']}]" if v.get("source") else ""
        display_label = f"{label}{tag} ⚠️({v['cached_at']})" if v.get("stale") else f"{label}{tag}"
        st.metric(label=display_label, value=val_str, delta=delta_str)

st.write("---")
st.subheader("🏦 한·미 핵심 단기 국채 금리 현황")
b1, b2 = st.columns(2)
show_metric(b1, "🇰🇷 한국 국채 3년물", data["한국 국채 3년"], ",.3f", " %")
show_metric(b2, "🇺🇸 미국 국채 2년물", data["미국 국채 2년"], ",.4f", " %")

st.write("---")
st.subheader("📊 국내 및 해외 주요 주가지수")
c1, c2, c3, c4, c5 = st.columns(5)
show_metric(c1, "코스피 (KOSPI)", data["KOSPI"], ",.2f")
show_metric(c2, "코스닥 (KOSDAQ)", data["KOSDAQ"], ",.2f")
show_metric(c3, "미국 S&P 500", data["S&P 500"], ",.2f")
show_metric(c4, "미국 나스닥", data["NASDAQ"], ",.2f")
show_metric(c5, "필라델피아 반도체", data["필라델피아 반도체"], ",.2f")

st.write("---")
st.subheader("💵 주요 환율 및 달러 인덱스")
h1, h2, h3, h4, h5 = st.columns(5)
show_metric(h1, "원/달러 환율", data["원달러 환율"], ",.2f", " 원")

v_jpy = data["엔화 환율"]
with h2:
    val = v_jpy["value"] * 100 if v_jpy.get("value") is not None else None
    chg = v_jpy["change"] * 100 if v_jpy.get("change") is not None else None
    lbl = f"원/100엔 환율 ⚠️({v_jpy['cached_at']})" if v_jpy.get("stale") else "원/100엔 환율"
    st.metric(
        label=lbl,
        value=f"{val:,.2f} 원" if val is not None else "N/A",
        delta=f"{chg:+,.2f} 원" if chg is not None else None,
    )

show_metric(h3, "원/유로 환율", data["유로 환율"], ",.2f", " 원")
show_metric(h4, "원/위안화 환율", data["위안화 환율"], ",.2f", " 원")
show_metric(h5, "달러 인덱스 (DXY)", data["달러 인덱스"], ",.2f")

st.write("---")
st.subheader("🔥 주요 원자재 및 공포지수")
e1, e2, e3 = st.columns(3)
show_metric(e1, "WTI 국제유가", data["유가 (WTI)"], ",.2f", " $")
show_metric(e2, "국제 금 시세 (oz)", data["국제 금"], ",.2f", " $")
show_metric(e3, "VIX 공포지수", data["VIX 공포지수"], ",.2f")