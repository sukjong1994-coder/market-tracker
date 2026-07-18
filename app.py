import streamlit as st
import datetime as dt
import requests
import re
import concurrent.futures
import pandas as pd
import altair as alt

# --- 🛠️ 기본 설정 ---
KST = dt.timezone(dt.timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

FAST_TIMEOUT = 4.0
HISTORY_DAYS = 30


# =========================================================
# 🌐 단일 소스 fetch 함수 (날짜 + 값 페어로 history 구성)
# =========================================================
def fetch_yahoo(ticker, debug=None, scale=1.0):
    try:
        r = SESSION.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "3mo"},
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

        # ✅ 날짜 + 종가 페어로 히스토리 구성 (툴팁용 날짜 포함)
        history = []
        try:
            timestamps = result[0].get("timestamp", [])
            closes = result[0]["indicators"]["quote"][0].get("close", [])
            paired = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
            paired = paired[-HISTORY_DAYS:]
            history = [
                {
                    "date": dt.datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d"),
                    "value": c * scale,
                }
                for ts, c in paired
            ]
        except Exception:
            history = []

        return {"value": price, "change": change, "history": history}
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
        date_idx = header.index("Date") if "Date" in header else 0
        close_idx = header.index("Close") if "Close" in header else 4

        rows_parsed = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) > close_idx:
                try:
                    date_str = parts[date_idx]
                    close_val = float(parts[close_idx])
                    rows_parsed.append((date_str, close_val))
                except ValueError:
                    continue

        if len(rows_parsed) < 2:
            if debug is not None:
                debug["error"] = f"유효 종가 부족 (n={len(rows_parsed)})"
            return None

        history_pairs = rows_parsed[-HISTORY_DAYS:]
        history = [{"date": d, "value": c} for d, c in history_pairs]
        last_val = rows_parsed[-1][1]
        prev_val = rows_parsed[-2][1]
        return {"value": last_val, "change": last_val - prev_val, "history": history}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def fetch_korea_bond(marketindex_cd, debug=None):
    try:
        pairs = []  # (date_str, value), 최신순
        page = 1
        max_page = 6
        while len(pairs) < HISTORY_DAYS + 1 and page <= max_page:
            r = SESSION.get(
                "https://finance.naver.com/marketindex/interestDailyQuote.naver",
                params={"marketindexCd": marketindex_cd, "page": page},
                headers={**HEADERS, "Referer": "https://finance.naver.com/marketindex/"},
                timeout=FAST_TIMEOUT,
            )
            if debug is not None:
                debug[f"status_code_p{page}"] = r.status_code

            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
            page_pairs = []
            for row in rows:
                date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", row)
                nums = re.findall(r'<td[^>]*class="num"[^>]*>\s*([\d,.\-]+)\s*</td>', row)
                if date_match and nums:
                    try:
                        val = float(nums[0].replace(",", ""))
                        page_pairs.append((date_match.group(1), val))
                    except ValueError:
                        continue

            if not page_pairs:
                break
            pairs.extend(page_pairs)
            page += 1

        if len(pairs) < 2:
            if debug is not None:
                debug["error"] = f"파싱된 값 부족 (values={len(pairs)})"
            return None

        values_only = [p[1] for p in pairs]
        change = values_only[0] - values_only[1]
        latest_value = values_only[0]

        # 과거 → 최근 순으로 정렬, 날짜 포맷 통일(YYYY-MM-DD)
        history_pairs = list(reversed(pairs[:HISTORY_DAYS]))
        history = [{"date": d.replace(".", "-"), "value": v} for d, v in history_pairs]

        return {"value": latest_value, "change": change, "history": history}
    except Exception as e:
        if debug is not None:
            debug["error"] = f"{type(e).__name__}: {e}"
        return None


def try_chain(debug_bucket, attempts):
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
        ("Yahoo", lambda d: fetch_yahoo("2YY=F", debug=d)),
    ])


def fetch_us10y(debug_bucket):
    return try_chain(debug_bucket, [
        ("Yahoo", lambda d: fetch_yahoo("^TNX", debug=d, scale=1.0)),
        ("Stooq", lambda d: fetch_stooq("10usy.b", debug=d)),
    ])


# =========================================================
# 🔄 캐시된 병렬 로더
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
        done, not_done = concurrent.futures.wait(future_to_key.keys(), timeout=12)
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
            debug_refs.setdefault(key, {})["error"] = "12초 타임아웃"

    fetched_at = dt.datetime.now(KST).strftime("%H:%M:%S")
    for key in results:
        if results[key] and results[key].get("value") is not None:
            results[key] = {
                "history": [],
                **results[key],
                "stale": False,
                "cached_at": fetched_at,
            }
        else:
            results[key] = {"value": None, "change": None, "history": [], "stale": True, "cached_at": "N/A"}

    return results, debug_refs


# =========================================================
# 🎨 UI 스타일 (카드형 디자인)
# =========================================================
st.set_page_config(page_title="글로벌 시장 대시보드", page_icon="📈", layout="wide")

st.markdown("""
<style>
    .metric-card {
        border: 1px solid #E5E7EB;
        border-radius: 12px 12px 0 0;
        padding: 16px 18px 10px 18px;
        background-color: #FFFFFF;
        border-bottom: none;
    }
    .metric-label {
        font-size: 13px;
        color: #6B7280;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 4px;
    }
    .metric-badge {
        font-size: 10px;
        background-color: #F3F4F6;
        color: #9CA3AF;
        padding: 1px 6px;
        border-radius: 6px;
        font-weight: 500;
    }
    .metric-value {
        font-size: 26px;
        font-weight: 700;
        color: #111827;
        line-height: 1.3;
    }
    .metric-delta-up { font-size: 14px; font-weight: 600; color: #DC2626; }
    .metric-delta-down { font-size: 14px; font-weight: 600; color: #2563EB; }
    .metric-delta-flat { font-size: 14px; font-weight: 600; color: #9CA3AF; }
    .stale-tag { font-size: 11px; color: #F59E0B; font-weight: 600; }
    .section-title { font-size: 18px; font-weight: 700; margin-top: 4px; margin-bottom: 12px; }
    .sparkline-wrap {
        border: 1px solid #E5E7EB;
        border-top: none;
        border-radius: 0 0 12px 12px;
        padding: 0 10px 4px 10px;
        margin-bottom: 10px;
        background-color: #FFFFFF;
    }
    .sparkline-caption {
        font-size: 10px;
        color: #D1D5DB;
        margin: 2px 0 0 4px;
    }
    div[data-testid="stVegaLiteChart"] { margin-top: -10px; }
</style>
""", unsafe_allow_html=True)

st.title("📈 글로벌 시장 지표 대시보드")

top_l, top_r = st.columns([3, 1])
with top_l:
    st.caption(
        f"조회 시각 (KST): {dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}  ·  "
        "🔴 상승 / 🔵 하락  ·  📉 최근 30거래일 추이 (그래프에 마우스를 올리면 날짜·값 확인 가능)"
    )
with top_r:
    if st.button("🔄 강제 새로고침", use_container_width=True):
        load_market_data.clear()

with st.spinner("⚡ 지표 동기화 중..."):
    data, debug_log = load_market_data()


# =========================================================
# 🧩 스파크라인 생성 함수 (등락 강조 + 툴팁)
# =========================================================
def build_sparkline(history, value_fmt=",.2f", unit=""):
    if not history or len(history) < 2:
        return None

    values = [h["value"] for h in history]
    dates = [h["date"] for h in history]

    if values[-1] > values[0]:
        color = "#DC2626"
    elif values[-1] < values[0]:
        color = "#2563EB"
    else:
        color = "#9CA3AF"

    y_min, y_max = min(values), max(values)
    rng = y_max - y_min
    # ✅ 여백을 좁혀서(10%) 실제 등락이 최대한 뚜렷하게 보이도록 스케일링
    pad = rng * 0.10 if rng > 0 else (abs(y_max) * 0.005 or 0.01)

    df = pd.DataFrame({
        "idx": range(len(values)),
        "date": dates,
        "value": values,
        "label": [f"{v:{value_fmt}}{unit}" for v in values],
    })

    chart = (
        alt.Chart(df)
        .mark_line(
            strokeWidth=2.0,
            point=alt.OverlayMarkDef(filled=True, size=18, opacity=0),  # 투명 포인트로 호버 감지
        )
        .encode(
            x=alt.X("idx:Q", axis=None),
            y=alt.Y("value:Q", axis=None, scale=alt.Scale(domain=[y_min - pad, y_max + pad])),
            color=alt.value(color),
            tooltip=[
                alt.Tooltip("date:N", title="날짜"),
                alt.Tooltip("label:N", title="값"),
            ],
        )
        .properties(height=60)
        .configure_view(strokeWidth=0)
    )
    return chart


# =========================================================
# 🧩 카드 렌더링 함수
# =========================================================
def render_card(col, label, v, fmt, unit="", icon="📌"):
    with col:
        has_value = v.get("value") is not None
        val_str = f"{v['value']:{fmt}}{unit}" if has_value else "N/A"

        if v.get("change") is not None:
            chg = v["change"]
            arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "―")
            css_class = "metric-delta-up" if chg > 0 else ("metric-delta-down" if chg < 0 else "metric-delta-flat")
            delta_html = f'<span class="{css_class}">{arrow} {chg:+{fmt}}{unit}</span>'
        else:
            delta_html = '<span class="metric-delta-flat">―</span>'

        badge = f'<span class="metric-badge">{v["source"]}</span>' if v.get("source") else ""
        stale = '<span class="stale-tag"> ⚠ 이전값</span>' if v.get("stale") else ""

        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">{icon} {label} {badge}{stale}</div>
            <div class="metric-value">{val_str}</div>
            {delta_html}
        </div>
        """, unsafe_allow_html=True)

        history = v.get("history")
        chart = build_sparkline(history, value_fmt=fmt, unit=unit)
        st.markdown('<div class="sparkline-wrap">', unsafe_allow_html=True)
        if chart is not None:
            st.altair_chart(chart, use_container_width=True)
            st.markdown(f'<div class="sparkline-caption">최근 {len(history)}거래일</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="sparkline-caption">추이 데이터 없음</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)


def render_jpy_card(col, v):
    with col:
        has_value = v.get("value") is not None
        val = v["value"] * 100 if has_value else None
        chg = v["change"] * 100 if v.get("change") is not None else None
        val_str = f"{val:,.2f} 원" if val is not None else "N/A"

        if chg is not None:
            arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "―")
            css_class = "metric-delta-up" if chg > 0 else ("metric-delta-down" if chg < 0 else "metric-delta-flat")
            delta_html = f'<span class="{css_class}">{arrow} {chg:+,.2f} 원</span>'
        else:
            delta_html = '<span class="metric-delta-flat">―</span>'

        stale = '<span class="stale-tag"> ⚠ 이전값</span>' if v.get("stale") else ""

        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">💴 원/100엔 환율{stale}</div>
            <div class="metric-value">{val_str}</div>
            {delta_html}
        </div>
        """, unsafe_allow_html=True)

        raw_history = v.get("history") or []
        history_scaled = [{"date": h["date"], "value": h["value"] * 100} for h in raw_history]
        chart = build_sparkline(history_scaled, value_fmt=",.2f", unit=" 원")
        st.markdown('<div class="sparkline-wrap">', unsafe_allow_html=True)
        if chart is not None:
            st.altair_chart(chart, use_container_width=True)
            st.markdown(f'<div class="sparkline-caption">최근 {len(history_scaled)}거래일</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="sparkline-caption">추이 데이터 없음</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)


# =========================================================
# 📄 한 페이지에 모든 섹션 표시
# =========================================================

# --- 국채금리 ---
st.markdown('<div class="section-title">🏦 한·미 핵심 국채 금리 현황</div>', unsafe_allow_html=True)
b1, b2, b3 = st.columns(3)
render_card(b1, "한국 국채 3년물", data["한국 국채 3년"], ",.3f", " %", "🇰🇷")
render_card(b2, "미국 국채 2년물", data["미국 국채 2년"], ",.3f", " %", "🇺🇸")
render_card(b3, "미국 국채 10년물", data["미국 국채 10년"], ",.3f", " %", "🇺🇸")

st.write("---")

# --- 주가지수 ---
st.markdown('<div class="section-title">📊 국내 및 해외 주요 주가지수</div>', unsafe_allow_html=True)
c1, c2, c3, c4, c5 = st.columns(5)
render_card(c1, "코스피", data["KOSPI"], ",.2f", "", "🇰🇷")
render_card(c2, "코스닥", data["KOSDAQ"], ",.2f", "", "🇰🇷")
render_card(c3, "S&P 500", data["S&P 500"], ",.2f", "", "🇺🇸")
render_card(c4, "나스닥", data["NASDAQ"], ",.2f", "", "🇺🇸")
render_card(c5, "필라델피아 반도체", data["필라델피아 반도체"], ",.2f", "", "💻")

st.write("---")

# --- 환율 ---
st.markdown('<div class="section-title">💵 주요 환율 및 달러 인덱스</div>', unsafe_allow_html=True)
h1, h2, h3, h4, h5 = st.columns(5)
render_card(h1, "원/달러 환율", data["원달러 환율"], ",.2f", " 원", "💵")
render_jpy_card(h2, data["엔화 환율"])
render_card(h3, "원/유로 환율", data["유로 환율"], ",.2f", " 원", "💶")
render_card(h4, "원/위안화 환율", data["위안화 환율"], ",.2f", " 원", "💴")
render_card(h5, "달러 인덱스", data["달러 인덱스"], ",.2f", "", "📈")

st.write("---")

# --- 원자재/공포지수 ---
st.markdown('<div class="section-title">🔥 주요 원자재 및 공포지수</div>', unsafe_allow_html=True)
e1, e2, e3 = st.columns(3)
render_card(e1, "WTI 국제유가", data["유가 (WTI)"], ",.2f", " $", "🛢️")
render_card(e2, "국제 금 시세", data["국제 금"], ",.2f", " $", "🥇")
render_card(e3, "VIX 공포지수", data["VIX 공포지수"], ",.2f", "", "😨")

st.write("---")
with st.expander("🔧 진단 정보 (N/A 원인 확인용)"):
    st.caption("15초 캐시가 적용되어 있어, 강제 새로고침 직후에도 방금 값이 그대로 보일 수 있습니다(정상).")
    st.json(debug_log)