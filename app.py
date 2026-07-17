import streamlit as st
import datetime as dt
import requests
import re
import concurrent.futures

# --- 🛠️ 기본 설정 ---
KST = dt.timezone(dt.timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

FAST_TIMEOUT = 2.5  # 초 단위 — 느린 소스는 빨리 포기하고 다음으로 넘어감


# =========================================================
# 🌐 단일 소스 fetch 함수 (재시도 없음 - 빠른 실패가 목적)
# =========================================================
def fetch_yahoo(ticker, debug=None, scale=1.0):
    try:
        r = SESSION.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "2d"},
            timeout=FAST_TIMEOUT,
        )
        if debug is not None:
            debug["status_code"] = r.status_code
        result = r.json().get("chart", {}).get("result")
        if not result:
            if debug is not None:
                debug["error"] = "chart.result 없음"
            return None
        meta = result[0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if price is None:
            if debug is not None:
                debug["error"] = "regularMarketPrice 없음"
            return None
        price *= scale
        change = (price - prev * scale) if prev is not None else None
        return {"value": price, "change": change}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_stooq(symbol, debug=None):
    try:
        r = SESSION.get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", timeout=FAST_TIMEOUT)
        if debug is not None:
            debug["status_code"] = r.status_code
        text = r.text
        if "<html" in text[:200].lower():
            if debug is not None:
                debug["error"] = "HTML 응답(심볼 오류 가능)"
            return None
        lines = text.strip().splitlines()
        if len(lines) < 3:
            if debug is not None:
                debug["error"] = "데이터 라인 부족"
            return None
        header = lines[0].split(",")
        close_idx = header.index("Close") if "Close" in header else 4
        closes = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) > close_idx:
                try:
                    closes.append(float(parts[close_idx]))
                except ValueError:
                    continue
        if len(closes) < 2:
            if debug is not None:
                debug["error"] = f"유효 종가 부족 (n={len(closes)})"
            return None
        return {"value": closes[-1], "change": closes[-1] - closes[-2]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_korea_bond(marketindex_cd, debug=None):
    try:
        r = SESSION.get(
            "https://finance.naver.com/marketindex/interestDailyQuote.naver",
            params={"marketindexCd": marketindex_cd, "page": 1},
            headers={**HEADERS, "Referer": "https://finance.naver.com/marketindex/"},
            timeout=FAST_TIMEOUT,
        )
        if debug is not None:
            debug["status_code"] = r.status_code
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
        values = []
        for row in rows:
            nums = re.findall(r'<td[^>]*class="num"[^>]*>\s*([\d,.\-]+)\s*</td>', row)
            if nums:
                try:
                    values.append(float(nums[0].replace(",", "")))
                except ValueError:
                    continue
        if len(values) < 2:
            if debug is not None:
                debug["error"] = f"파싱된 값 부족 (values={len(values)})"
            return None
        return {"value": values[0], "change": values[0] - values[1]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def try_chain(debug_bucket, attempts):
    """최대 2단계 이내로만 구성 - 길게 물리지 않도록"""
    debug_bucket["attempts"] = {}
    for name, fn in attempts:
        d = {}
        try:
            res = fn(d)
        except Exception as e:
            d["error"] = f"{type(e).__name__}: {e}"
            res = None
        debug_bucket["attempts"][name] = d if not res else "성공"
        if res and res.get("value") is not None:
            res["source"] = name
            return res
    return None


def fetch_kr3y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Naver", lambda d: fetch_korea_bond("IRR_GOVT03Y", debug=d)),
    ])


def fetch_us2y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Stooq", lambda d: fetch_stooq("2usy.b", debug=d)),
        ("Yahoo(2YY=F)", lambda d: fetch_yahoo("2YY=F", debug=d)),
    ])


def fetch_us10y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Yahoo(^TNX)", lambda d: fetch_yahoo("^TNX", debug=d, scale=0.1)),
        ("Stooq", lambda d: fetch_stooq("10usy.b", debug=d)),
    ])


# =========================================================
# 🔄 캐시된 병렬 로더
#   - ttl=15초: 이 시간 안에는 재실행돼도 네트워크 재요청 없이 즉시 반환
# =========================================================
@st.cache_data(ttl=15, show_spinner=False)
def load_market_data():
    tasks = {}
    debug_refs = {}

    simple_specs = {
        "KOSPI": (fetch_yahoo, "^KS11"),
        "KOSDAQ": (fetch_yahoo, "^KQ11"),
        "S&P 500": (fetch_yahoo, "^GSPC"),
        "NASDAQ": (fetch_yahoo, "^IXIC"),
        "필라델피아 반도체": (fetch_yahoo, "^SOX"),
        "원달러 환율": (fetch_yahoo, "KRW=X"),
        "엔화 환율": (fetch_yahoo, "JPYKRW=X"),
        "유로 환율": (fetch_yahoo, "EURKRW=X"),
        "위안화 환율": (fetch_yahoo, "CNYKRW=X"),
        "달러 인덱스": (fetch_yahoo, "DX-Y.NYB"),
        "유가 (WTI)": (fetch_yahoo, "CL=F"),
        "국제 금": (fetch_yahoo, "GC=F"),
        "VIX 공포지수": (fetch_yahoo, "^VIX"),
    }

    for key, (fn, arg) in simple_specs.items():
        d = {}
        debug_refs[key] = d
        tasks[key] = (lambda fn=fn, arg=arg, d=d: fn(arg, debug=d))

    bond_specs = {
        "한국 국채 3년": fetch_kr3y,
        "미국 국채 2년": fetch_us2y,
        "미국 국채 10년": fetch_us10y,
    }
    for key, fn in bond_specs.items():
        d = {}
        debug_refs[key] = d
        tasks[key] = (lambda fn=fn, d=d: fn(d))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_key = {executor.submit(func): key for key, func in tasks.items()}
        done, not_done = concurrent.futures.wait(future_to_key.keys(), timeout=8)
        for future in done:
            key = future_to_key[future]
            try:
                res = future.result()
            except Exception as e:
                res = None
                debug_refs.setdefault(key, {})["fatal_error"] = str(e)
            results[key] = res
        for future in not_done:
            key = future_to_key[future]
            results[key] = None
            debug_refs.setdefault(key, {})["error"] = "8초 타임아웃"

    fetched_at = dt.datetime.now(KST).strftime("%H:%M:%S")
    for key in results:
        if results[key] and results[key].get("value") is not None:
            results[key] = {**results[key], "stale": False, "cached_at": fetched_at}
        else:
            results[key] = {"value": None, "change": None, "stale": True, "cached_at": "N/A"}

    return results, debug_refs


# =========================================================
# 🖥️ Streamlit 화면 구성
# =========================================================
st.set_page_config(page_title="실시간 글로벌 대시보드", page_icon="📈", layout="wide")
st.title("📈 글로벌 시장 지표 실시간 트래커")
st.write(f"조회 시각 (KST): `{dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}`")

if st.button("🔄 실시간 지표 새로고침 (캐시 무시하고 강제 갱신)"):
    load_market_data.clear()

with st.spinner("⚡ 지표 동기화 중..."):
    data, debug_log = load_market_data()


def show_metric(col, label, v, fmt, unit=""):
    with col:
        val_str = f"{v['value']:{fmt}}{unit}" if v.get("value") is not None else "N/A"
        delta_str = f"{v['change']:+{fmt}}{unit}" if v.get("change") is not None else None
        tag = f" [{v['source']}]" if v.get("source") else ""
        display_label = f"{label}{tag} ⚠️" if v.get("stale") else f"{label}{tag}"
        st.metric(label=display_label, value=val_str, delta=delta_str)


st.write("---")
st.subheader("🏦 한·미 핵심 국채 금리 현황")
b1, b2, b3 = st.columns(3)
show_metric(b1, "🇰🇷 한국 국채 3년물", data["한국 국채 3년"], ",.3f", " %")
show_metric(b2, "🇺🇸 미국 국채 2년물", data["미국 국채 2년"], ",.4f", " %")
show_metric(b3, "🇺🇸 미국 국채 10년물", data["미국 국채 10년"], ",.4f", " %")

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
    lbl = "원/100엔 환율 ⚠️" if v_jpy.get("stale") else "원/100엔 환율"
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

st.write("---")
with st.expander("🔧 진단 정보 (N/A 원인 확인용)"):
    st.caption("15초 캐시가 적용되어 있어, 새로고침 버튼을 눌러도 방금 값이 그대로 보일 수 있습니다(정상).")
    st.json(debug_log)