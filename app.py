import streamlit as st
import datetime as dt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import csv
import re
import time
import concurrent.futures

# --- 🛠️ 기본 설정 ---
KST = dt.timezone(dt.timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json,text/csv,*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

ECOS_DEFAULT_TABLE = "817Y002"
ECOS_DEFAULT_KEYWORD = "국고채(10년)"


# --- 🔌 재시도가 내장된 공용 세션 ---
def make_session():
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


def safe_get(url, params=None, headers=None, timeout=4, attempts=2):
    """네트워크 hiccup에 대응하는 재시도 GET"""
    last_exc = None
    for i in range(attempts):
        try:
            return SESSION.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
        except Exception as e:
            last_exc = e
            time.sleep(0.3)
    raise last_exc


# =========================================================
# 🌐 개별 소스 fetch 함수
# =========================================================
def fetch_yahoo(ticker, debug=None, scale=1.0):
    try:
        r = safe_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "2d"},
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
        price *= scale
        change = (price - prev * scale) if prev is not None else None
        return {"value": price, "change": change}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_fred(series_id, debug=None):
    """FRED는 클라우드 IP에서 차단될 수 있어 최후 안전망으로만 사용"""
    try:
        r = safe_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=5)
        if debug is not None:
            debug["status_code"] = r.status_code
        content_type = r.headers.get("Content-Type", "")
        text = r.text
        if "<html" in text[:200].lower() or "text/csv" not in content_type:
            if debug is not None:
                debug["error"] = (
                    f"CSV가 아닌 응답 수신(차단/캡차 추정) / Content-Type={content_type} "
                    f"/ body 앞부분: {text[:150]}"
                )
            return None
        rows = [row for row in csv.reader(text.splitlines()) if row][1:]
        valid = [float(row[1]) for row in rows if row[1] not in ("", ".")]
        if len(valid) < 2:
            if debug is not None:
                debug["error"] = f"유효값 부족 (n={len(valid)})"
            return None
        return {"value": valid[-1], "change": valid[-1] - valid[-2]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_stooq(symbol, debug=None):
    """Stooq: 봇 차단이 거의 없는 안정적인 무료 CSV 소스 (주요 만기 국채 수익률 제공)"""
    try:
        r = safe_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", timeout=4)
        if debug is not None:
            debug["status_code"] = r.status_code
        text = r.text
        if "<html" in text[:200].lower():
            if debug is not None:
                debug["error"] = f"HTML 응답(심볼 오류 가능) / body: {text[:150]}"
            return None
        reader = list(csv.DictReader(text.splitlines()))
        closes = []
        for row in reader:
            val = row.get("Close")
            if val not in (None, "", "N/D"):
                try:
                    closes.append(float(val))
                except ValueError:
                    continue
        if len(closes) < 2:
            if debug is not None:
                debug["error"] = f"유효 종가 부족 (n={len(closes)}) / 심볼 미지원 가능"
            return None
        return {"value": closes[-1], "change": closes[-1] - closes[-2]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_korea_bond(marketindex_cd, debug=None):
    try:
        r = safe_get(
            "https://finance.naver.com/marketindex/interestDailyQuote.naver",
            params={"marketindexCd": marketindex_cd, "page": 1},
            headers={**HEADERS, "Referer": "https://finance.naver.com/marketindex/"},
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
                debug["error"] = f"파싱된 값 부족 (rows={len(rows)}, values={len(values)})"
            return None
        return {"value": values[0], "change": values[0] - values[1]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


# =========================================================
# 🏦 ECOS (한국은행) — 선택적, API 키가 있을 때만 최우선 시도
# =========================================================
def ecos_discover_item_code(api_key, table_code, keyword, debug=None):
    try:
        url = f"https://ecos.bok.or.kr/api/StatisticItemList/{api_key}/json/kr/1/1000/{table_code}"
        r = safe_get(url, timeout=5)
        if debug is not None:
            debug["status_code"] = r.status_code
        payload = r.json()
        if "StatisticItemList" not in payload:
            if debug is not None:
                debug["error"] = f"응답 오류: {str(payload)[:300]}"
            return None
        rows = payload["StatisticItemList"]["row"]
        norm_keyword = keyword.replace(" ", "")
        for row in rows:
            if norm_keyword in (row.get("ITEM_NAME") or "").replace(" ", ""):
                return row.get("ITEM_CODE")
        if debug is not None:
            debug["error"] = f"'{keyword}' 항목을 찾지 못함"
        return None
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_ecos_daily(api_key, table_code, item_code, debug=None):
    try:
        end = dt.datetime.now(KST)
        start = end - dt.timedelta(days=20)
        url = (
            f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/50/"
            f"{table_code}/D/{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/{item_code}"
        )
        r = safe_get(url, timeout=5)
        if debug is not None:
            debug["status_code"] = r.status_code
        payload = r.json()
        if "StatisticSearch" not in payload:
            if debug is not None:
                debug["error"] = f"응답 오류: {str(payload)[:300]}"
            return None
        rows = sorted(payload["StatisticSearch"]["row"], key=lambda x: x["TIME"])
        values = [float(row["DATA_VALUE"]) for row in rows if row.get("DATA_VALUE") not in (None, "")]
        if len(values) < 2:
            if debug is not None:
                debug["error"] = f"유효 데이터 부족 (n={len(values)})"
            return None
        return {"value": values[-1], "change": values[-1] - values[-2]}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


# =========================================================
# 🔗 다중 소스 폴백 콤비네이터
# =========================================================
def try_chain(debug_bucket, attempts):
    """
    attempts: [(이름, 호출가능한함수), ...] 순서대로 시도.
    각 시도 결과와 실패 사유를 debug_bucket['attempts']에 기록.
    """
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


def fetch_us10y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Yahoo(^TNX)", lambda d: fetch_yahoo("^TNX", debug=d, scale=0.1)),
        ("Stooq", lambda d: fetch_stooq("10usy.b", debug=d)),
        ("FRED", lambda d: fetch_fred("DGS10", debug=d)),
    ])


def fetch_us2y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Stooq", lambda d: fetch_stooq("2usy.b", debug=d)),
        ("Yahoo(2YY=F)", lambda d: fetch_yahoo("2YY=F", debug=d)),
        ("FRED", lambda d: fetch_fred("DGS2", debug=d)),
    ])


def fetch_kr10y(debug_bucket, ecos_key="", ecos_table=ECOS_DEFAULT_TABLE, ecos_keyword=ECOS_DEFAULT_KEYWORD):
    attempts = []

    if ecos_key:
        def _ecos(d):
            item_code = st.session_state.get("_ecos_item_code")
            if not item_code:
                item_code = ecos_discover_item_code(ecos_key, ecos_table, ecos_keyword, debug=d)
                if item_code:
                    st.session_state["_ecos_item_code"] = item_code
            if not item_code:
                return None
            return fetch_ecos_daily(ecos_key, ecos_table, item_code, debug=d)

        attempts.append(("ECOS", _ecos))

    attempts += [
        ("Yahoo(KR10YT=RR)", lambda d: fetch_yahoo("KR10YT=RR", debug=d)),
        ("Naver", lambda d: fetch_korea_bond("IRR_GOVT10Y", debug=d)),
        ("Stooq", lambda d: fetch_stooq("10kry.b", debug=d)),
        ("FRED(월간)", lambda d: fetch_fred("IRLTLT01KRM156N", debug=d)),
    ]
    return try_chain(debug_bucket, attempts)


def fetch_kr3y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Naver", lambda d: fetch_korea_bond("IRR_GOVT03Y", debug=d)),
        ("Stooq", lambda d: fetch_stooq("3kry.b", debug=d)),
    ])


# =========================================================
# 🔄 병렬 로더
# =========================================================
def load_market_data(ecos_key, ecos_table, ecos_keyword):
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

    # 국채 금리류는 전용 다중 소스 체인 사용
    bond_chains = {
        "한국 국채 3년": lambda d: fetch_kr3y(d),
        "한국 국채 10년": lambda d: fetch_kr10y(d, ecos_key, ecos_table, ecos_keyword),
        "미국 국채 2년": lambda d: fetch_us2y(d),
        "미국 국채 10년": lambda d: fetch_us10y(d),
    }
    for key, fn in bond_chains.items():
        d = {}
        debug_refs[key] = d
        tasks[key] = (lambda fn=fn, d=d: fn(d))

    results = {}
    st.session_state.setdefault("_last_good", {})
    current_successes = set()

    with st.spinner("⚡ 글로벌 지표 동기화 중... (국채 금리는 다중 소스 폴백으로 다소 시간이 걸릴 수 있습니다)"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_key = {executor.submit(func): key for key, func in tasks.items()}
            done, not_done = concurrent.futures.wait(future_to_key.keys(), timeout=15)
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
            for future in not_done:
                key = future_to_key[future]
                debug_refs.setdefault(key, {})["error"] = "타임아웃(15초 초과) — 모든 소스가 응답 지연 중"

    for key in tasks.keys():
        if key in current_successes:
            results[key] = {**st.session_state["_last_good"][key], "stale": False}
        elif key in st.session_state["_last_good"]:
            results[key] = {**st.session_state["_last_good"][key], "stale": True}
        else:
            results[key] = {"value": None, "change": None, "stale": True, "cached_at": "N/A"}

    st.session_state["_debug"] = debug_refs
    return results


# =========================================================
# 🖥️ Streamlit 화면 구성
# =========================================================
st.set_page_config(page_title="실시간 글로벌 대시보드", page_icon="📈", layout="wide")
st.title("📈 글로벌 시장 지표 실시간 트래커")
st.write(f"조회 시각 (KST): `{dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}`")

with st.sidebar:
    st.header("⚙️ ECOS(한국은행) 설정 (선택)")
    default_key = ""
    try:
        default_key = st.secrets.get("ECOS_API_KEY", "")
    except Exception:
        pass
    ecos_key = st.text_input("ECOS API 키", value=default_key, type="password",
                              help="비워두면 Yahoo→Naver→Stooq→FRED 순으로만 조회합니다.")
    ecos_table = st.text_input("통계표코드", value=ECOS_DEFAULT_TABLE)
    ecos_keyword = st.text_input("항목명 검색 키워드", value=ECOS_DEFAULT_KEYWORD)

    if st.button("♻️ ECOS 항목코드 캐시 초기화"):
        st.session_state.pop("_ecos_item_code", None)
        st.success("초기화 완료")

st.button("🔄 실시간 지표 새로고침")

data = load_market_data(ecos_key, ecos_table, ecos_keyword)


def show_metric(col, label, v, fmt, unit=""):
    with col:
        val_str = f"{v['value']:{fmt}}{unit}" if v.get("value") is not None else "N/A"
        delta_str = f"{v['change']:+{fmt}}{unit}" if v.get("change") is not None else None
        tag = f" [{v['source']}]" if v.get("source") else ""
        display_label = f"{label}{tag} ⚠️({v['cached_at']})" if v.get("stale") else f"{label}{tag}"
        st.metric(label=display_label, value=val_str, delta=delta_str)


st.write("---")
st.subheader("🏦 한·미 핵심 국채 금리 현황")
b1, b2, b3, b4 = st.columns(4)
show_metric(b1, "🇰🇷 한국 국채 3년물", data["한국 국채 3년"], ",.3f", " %")
show_metric(b2, "🇰🇷 한국 국채 10년물", data["한국 국채 10년"], ",.3f", " %")
show_metric(b3, "🇺🇸 미국 국채 2년물", data["미국 국채 2년"], ",.4f", " %")
show_metric(b4, "🇺🇸 미국 국채 10년물", data["미국 국채 10년"], ",.4f", " %")

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

st.write("---")
with st.expander("🔧 진단 정보 (N/A 원인 확인용)"):
    st.caption("각 지표별로 어떤 소스를 어떤 순서로 시도했고 왜 실패했는지 보여줍니다.")
    st.json(st.session_state.get("_debug", {}))