import streamlit as st
import datetime as dt
import requests
import csv
import re
import concurrent.futures

# --- 🛠️ 기본 설정 ---
KST = dt.timezone(dt.timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# --- 🌐 데이터 수집 함수 ---
def fetch_yahoo(ticker, debug=None):
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "2d"},
            headers=HEADERS,
            timeout=2,
        )
        if debug is not None:
            debug["status_code"] = r.status_code
        payload = r.json()
        result = payload.get("chart", {}).get("result")
        if not result:
            if debug is not None:
                debug["error"] = f"chart.result 없음 / body: {str(payload)[:200]}"
            return None
        meta = result[0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if price is None:
            if debug is not None:
                debug["error"] = f"regularMarketPrice 없음 / meta keys: {list(meta.keys())}"
            return None
        return {"value": price, "change": (price - prev) if prev is not None else None}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_fred(series_id, debug=None):
    try:
        r = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
            headers=HEADERS,
            timeout=3,
        )
        if debug is not None:
            debug["status_code"] = r.status_code
        rows = [row for row in csv.reader(r.text.splitlines()) if row][1:]
        valid = [float(row[1]) for row in rows if row[1] not in ("", ".")]
        if len(valid) < 2:
            if debug is not None:
                debug["error"] = f"유효값 부족 (valid={len(valid)}) / raw 앞부분: {r.text[:200]}"
            return None
        return {"value": valid[-1], "change": valid[-1] - valid[-2]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_korea_bond(marketindex_cd, debug=None):
    try:
        r = requests.get(
            "https://finance.naver.com/marketindex/interestDailyQuote.naver",
            params={"marketindexCd": marketindex_cd, "page": 1},
            headers={**HEADERS, "Referer": "https://finance.naver.com/marketindex/"},
            timeout=2,
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
            if debug is not None:
                debug["error"] = (
                    f"파싱된 값 부족 (rows={len(rows)}, values={len(values)}) "
                    f"/ 네이버 미지원 코드 가능성 있음. body 앞부분: {r.text[:300]}"
                )
            return None
        return {"value": values[0], "change": values[0] - values[1]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_kr10y(debug_bucket):
    debug_bucket["attempts"] = {}

    d1 = {}
    r1 = fetch_yahoo("KR10YT=RR", debug=d1)
    debug_bucket["attempts"]["yahoo"] = d1 if not r1 else "성공"
    if r1 and r1.get("value") is not None:
        r1["source"] = "Yahoo"
        return r1

    d2 = {}
    r2 = fetch_korea_bond("IRR_GOVT10Y", debug=d2)
    debug_bucket["attempts"]["naver"] = d2 if not r2 else "성공"
    if r2 and r2.get("value") is not None:
        r2["source"] = "Naver"
        return r2

    d3 = {}
    r3 = fetch_fred("IRLTLT01KRM156N", debug=d3)
    debug_bucket["attempts"]["fred_monthly"] = d3 if not r3 else "성공"
    if r3 and r3.get("value") is not None:
        r3["source"] = "FRED(월간)"
        return r3

    return None


# --- 🔄 병렬 로더 ---
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
        "한국 국채 3년": (fetch_korea_bond, "IRR_GOVT03Y"),
        "미국 국채 2년": (fetch_fred, "DGS2"),
        "미국 국채 10년": (fetch_fred, "DGS10"),
    }

    for key, (fn, arg) in simple_specs.items():
        d = {}
        debug_refs[key] = d
        tasks[key] = (lambda fn=fn, arg=arg, d=d: fn(arg, debug=d))

    kr10y_debug = {}
    debug_refs["한국 국채 10년"] = kr10y_debug
    tasks["한국 국채 10년"] = lambda: fetch_kr10y(kr10y_debug)

    results = {}
    st.session_state.setdefault("_last_good", {})
    current_successes = set()

    with st.spinner("⚡ 글로벌 지표 동기화 중..."):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_key = {executor.submit(func): key for key, func in tasks.items()}
            done, not_done = concurrent.futures.wait(future_to_key.keys(), timeout=6)
            for future in done:
                key = future_to_key[future]
                try:
                    res = future.result()
                    if res and res.get("value") is not None:
                        st.session_state["_last_good"][key] = {
                            **res,
                            "cached_at": dt.datetime.now(KST).strftime("%H:%M:%S"),
                        }
                        current_successes.add(key)
                except Exception as e:
                    debug_refs.setdefault(key, {})["fatal_error"] = str(e)

    for key in tasks.keys():
        if key in current_successes:
            results[key] = {**st.session_state["_last_good"][key], "stale": False}
        elif key in st.session_state["_last_good"]:
            results[key] = {**st.session_state["_last_good"][key], "stale": True}
        else:
            results[key] = {"value": None, "change": None, "stale": True, "cached_at": "N/A"}

    st.session_state["_debug"] = debug_refs
    return results


# --- 🖥️ Streamlit 화면 구성 ---
st.set_page_config(page_title="실시간 글로벌 대시보드", page_icon="📈", layout="wide")
st.title("📈 글로벌 시장 지표 실시간 트래커")
st.write(f"조회 시각 (KST): `{dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}`")

st.button("🔄 실시간 지표 새로고침")

data = load_market_data()


def show_metric(col, label, v, fmt, unit=""):
    with col:
        val_str = f"{v['value']:{fmt}}{unit}" if v.get("value") is not None else "N/A"
        delta_str = f"{v['change']:+{fmt}}{unit}" if v.get("change") is not None else None
        tag = ""
        if v.get("source") and v.get("source") != "Yahoo":
            tag = f" [{v['source']}]"
        display_label = f"{label}{tag} ⚠️({v['cached_at']})" if v.get("stale") else f"{label}{tag}"
        st.metric(label=display_label, value=val_str, delta=delta_str)


# --- 섹션 1: 국채 금리 ---
st.write("---")
st.subheader("🏦 한·미 핵심 국채 금리 현황")
b1, b2, b3, b4 = st.columns(4)
show_metric(b1, "🇰🇷 한국 국채 3년물", data["한국 국채 3년"], ",.3f", " %")
show_metric(b2, "🇰🇷 한국 국채 10년물", data["한국 국채 10년"], ",.3f", " %")
show_metric(b3, "🇺🇸 미국 국채 2년물", data["미국 국채 2년"], ",.4f", " %")
show_metric(b4, "🇺🇸 미국 국채 10년물", data["미국 국채 10년"], ",.4f", " %")

# --- 섹션 2: 주가지수 ---
st.write("---")
st.subheader("📊 국내 및 해외 주요 주가지수")
c1, c2, c3, c4, c5 = st.columns(5)
show_metric(c1, "코스피 (KOSPI)", data["KOSPI"], ",.2f")
show_metric(c2, "코스닥 (KOSDAQ)", data["KOSDAQ"], ",.2f")
show_metric(c3, "미국 S&P 500", data["S&P 500"], ",.2f")
show_metric(c4, "미국 나스닥", data["NASDAQ"], ",.2f")
show_metric(c5, "필라델피아 반도체", data["필라델피아 반도체"], ",.2f")

# --- 섹션 3: 환율 ---
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

# --- 섹션 4: 원자재 ---
st.write("---")
st.subheader("🔥 주요 원자재 및 공포지수")
e1, e2, e3 = st.columns(3)
show_metric(e1, "WTI 국제유가", data["유가 (WTI)"], ",.2f", " $")
show_metric(e2, "국제 금 시세 (oz)", data["국제 금"], ",.2f", " $")
show_metric(e3, "VIX 공포지수", data["VIX 공포지수"], ",.2f")

# --- 섹션 5: 진단 정보 ---
st.write("---")
with st.expander("🔧 진단 정보 (N/A 원인 확인용)"):
    st.caption("특정 지표가 N/A로 나올 때, 어떤 소스에서 왜 실패했는지 여기서 확인하세요.")
    st.json(st.session_state.get("_debug", {}))