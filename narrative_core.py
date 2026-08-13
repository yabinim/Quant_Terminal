# narrative_core.py
# ─────────────────────────────────────────────────────────────────────────────
# 공유 뉴스 파이프라인 (SSOT). app.py 와 자동화(run_narrative.py)가 함께 import 한다.
# Streamlit 의존성 없음 · Gemini SDK 의존성 없음 (순수 수집/정렬/컨텍스트 빌드).
# FMP 키는 호출자가 인자로 전달한다 (app: st.secrets, 자동화: 환경변수).
#
# [중요] 이 파일이 내러티브 '뉴스 소스/랭킹/컨텍스트'의 단일 진실원천이다.
# 관련 로직 변경 시 여기만 고치면 app.py·자동화 양쪽에 동시 반영된다.
# ─────────────────────────────────────────────────────────────────────────────
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests
import feedparser
import pytz

_ET = pytz.timezone("America/New_York")
_FMP_BASE = "https://financialmodelingprep.com/stable"


def _clean_news_text(raw_text, max_len: int = 0) -> str:
    """HTML 태그 제거 + 공백 정규화. max_len>0이면 truncate."""
    text = str(raw_text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len > 0:
        text = text[:max_len]
    return text


def _fmp_news_fetch(endpoint: str, params: dict, fmp_key: str, timeout: int = 8) -> list:
    """FMP 뉴스 API 단일 호출 래퍼."""
    if not fmp_key:
        return []
    try:
        p = {**params, "apikey": fmp_key}
        r = requests.get(f"{_FMP_BASE}/{endpoint}", params=p, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fmp_news_to_unified(items: list, source_label: str, weight: float,
                         body_key: str = "text", max_body: int = 300) -> list:
    """FMP 뉴스 응답 → 통합 dict."""
    result = []
    for item in items:
        title = _clean_news_text(item.get("title", ""))
        body  = _clean_news_text(item.get(body_key, "") or item.get("text", "") or item.get("content", ""), max_len=max_body)
        pub_raw = str(item.get("publishedDate", "") or item.get("date", "") or "").strip()
        tickers = _clean_news_text(item.get("tickers", "") or item.get("symbol", ""))
        pub_dt = datetime.min.replace(tzinfo=timezone.utc)
        if pub_raw:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    pub_dt = datetime.strptime(pub_raw[:19], fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        if not title:
            continue
        result.append({
            "title": title, "summary": body, "tickers": tickers,
            "published": pub_raw if pub_raw else "N/A",
            "published_dt": pub_dt, "source": source_label, "weight": weight,
        })
    return result


def _rss_news_fallback() -> list:
    """FMP 키 없거나 전 레이어 0건일 때 RSS 폴백 (4개 소스)."""
    browser_headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")}
    rss_sources = {
        "Yahoo Finance":       {"url": "https://finance.yahoo.com/news/rssindex", "weight": 1.0},
        "CNBC":                {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000", "weight": 0.9},
        "Google News Finance": {"url": "https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en", "weight": 0.8},
        "MarketWatch":         {"url": "http://feeds.marketwatch.com/marketwatch/marketpulse/", "weight": 0.7},
    }
    out = []
    for source_name, cfg in rss_sources.items():
        try:
            resp = requests.get(cfg["url"], headers=browser_headers, timeout=8)
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
            for entry in getattr(feed, "entries", []):
                title   = _clean_news_text(getattr(entry, "title", ""))
                summary = _clean_news_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
                if not (title or summary):
                    continue
                published_raw = str(getattr(entry, "published", "") or "").strip()
                parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                try:
                    pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else datetime.min.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = datetime.min.replace(tzinfo=timezone.utc)
                out.append({"title": title, "summary": summary, "tickers": "",
                            "published": published_raw or "N/A", "published_dt": pub_dt,
                            "source": source_name, "weight": cfg["weight"]})
        except Exception:
            continue
    return out


def _dedup_and_rank(all_news: list, top_n: int = 60) -> list:
    """제목 유사도 디둡 → (weight DESC, published_dt DESC) 정렬 → top_n."""
    deduped = []
    for news in all_news:
        dup_idx = None
        for idx, kept in enumerate(deduped):
            if SequenceMatcher(None, news["title"].lower(), kept["title"].lower()).ratio() >= 0.7:
                dup_idx = idx
                break
        if dup_idx is None:
            deduped.append(news)
        else:
            kept = deduped[dup_idx]
            if news["weight"] > kept["weight"]:
                deduped[dup_idx] = news
            elif news["weight"] == kept["weight"] and news["published_dt"] > kept["published_dt"]:
                deduped[dup_idx] = news
    ranked = sorted(deduped,
        key=lambda x: (x.get("weight", 0.0), x.get("published_dt", datetime.min.replace(tzinfo=timezone.utc))),
        reverse=True)
    return ranked[:top_n]


def _build_news_context_text(top_news: list) -> str:
    """LLM 프롬프트용 섹션 구조화 컨텍스트."""
    section_defs = [
        ("FMP Press Release", "\u2501\u2501\u2501 [SECTION A] Press Releases \u2014 고임팩트 기업 공시 (최우선 참고) \u2501\u2501\u2501",
         "어닝 발표\u00b7M&A\u00b7가이던스 등 주가에 직접 영향을 주는 공식 기업 공시입니다.\n이 섹션의 Tickers 종목은 즉각적인 가격 반응이 확인된 최우선 winners 후보입니다."),
        ("FMP Stock News", "\u2501\u2501\u2501 [SECTION B] Stock News \u2014 종목 티커 태그 포함 뉴스 \u2501\u2501\u2501",
         "각 기사에 관련 종목 티커가 태그되어 있습니다.\nTickers 필드에 명시된 종목을 해당 테마의 직접 수혜주 후보로 간주하세요."),
        ("FMP Article", "\u2501\u2501\u2501 [SECTION C] FMP Articles \u2014 전문 애널리스트 분석 기사 \u2501\u2501\u2501",
         "FMP 에디터의 심층 분석 기사입니다. 테마의 구조적 배경과 expanding_to 단계 설정에 활용하세요."),
        ("FMP General News", "\u2501\u2501\u2501 [SECTION D] General / Macro News \u2014 거시경제 맥락 \u2501\u2501\u2501",
         "금리\u00b7환율\u00b7정책 등 매크로 뉴스입니다. regime(Risk On/Off, liquidity) 판단에 활용하세요."),
        ("RSS", "\u2501\u2501\u2501 [SECTION E] RSS Fallback News \u2501\u2501\u2501",
         "RSS 수집 뉴스입니다. FMP 데이터가 없을 때 사용됩니다."),
    ]
    buckets = {key: [] for key, _, _ in section_defs}
    buckets["__etc__"] = []
    for item in top_news:
        src = str(item.get("source", "") or "")
        matched = False
        for key, _, _ in section_defs:
            if key in src:
                buckets[key].append(item); matched = True; break
        if not matched:
            buckets["__etc__"].append(item)
    sections = []
    gidx = 1
    for key, header, desc in section_defs:
        items = buckets.get(key, [])
        if not items:
            continue
        chunks = []
        for item in items:
            lines = [f"[{gidx}] Published: {item.get('published', 'N/A')}"]
            tk = str(item.get("tickers", "") or "").strip()
            if tk:
                lines.append(f"  Tickers: {tk}")
            lines.append(f"  Title: {item.get('title', '')}")
            ct = str(item.get("summary", "") or "").strip()
            if ct:
                lines.append(f"  Content: {ct}")
            chunks.append("\n".join(lines)); gidx += 1
        sections.append(f"{header}\n{desc}\n\n" + "\n\n".join(chunks))
    if buckets["__etc__"]:
        chunks = []
        for item in buckets["__etc__"]:
            lines = [f"[{gidx}] Source: {item.get('source','Unknown')} | Published: {item.get('published','N/A')}"]
            tk = str(item.get("tickers", "") or "").strip()
            if tk:
                lines.append(f"  Tickers: {tk}")
            lines.append(f"  Title: {item.get('title', '')}")
            ct = str(item.get("summary", "") or "").strip()
            if ct:
                lines.append(f"  Content: {ct}")
            chunks.append("\n".join(lines)); gidx += 1
        sections.append("\u2501\u2501\u2501 [SECTION F] Other News \u2501\u2501\u2501\n\n" + "\n\n".join(chunks))
    return "\n\n\n".join(sections).strip()


def compute_news_freshness(top_news: list) -> dict:
    """top_news(아직 published_dt 보유)로 신선도 메타 계산."""
    now_utc = datetime.now(timezone.utc)
    valid = [n["published_dt"] for n in top_news
             if n.get("published_dt") and n["published_dt"].year > 2000]
    meta = {"newest": None, "oldest": None, "fresh_6h": 0,
            "total": len(top_news), "sources": sorted({n["source"] for n in top_news})}
    if valid:
        newest = max(valid); oldest = min(valid)
        meta["newest_min_ago"] = int((now_utc - newest).total_seconds() // 60)
        meta["oldest_hr_ago"]  = round((now_utc - oldest).total_seconds() / 3600, 1)
        meta["newest"] = newest.astimezone(_ET).strftime("%m/%d %H:%M ET")
        meta["oldest"] = oldest.astimezone(_ET).strftime("%m/%d %H:%M ET")
        meta["fresh_6h"] = sum(1 for d in valid if (now_utc - d).total_seconds() <= 6 * 3600)
    return meta


def fetch_global_market_news(fmp_api_key: str = "", top_n: int = 60):
    """FMP 4-레이어 뉴스 파이프라인 (RSS 폴백 포함) + 신선도 메타.
    Returns: (top_news, context_text, raw_count, news_meta)
      - top_news: published_dt 제거된 dict 리스트
    """
    all_news = []
    source_log = []
    if fmp_api_key:
        layer_a = _fmp_news_to_unified(_fmp_news_fetch("news/stock-latest", {"page": 0, "limit": 50}, fmp_api_key),
                                       "FMP Stock News", weight=1.2, body_key="text", max_body=300)
        layer_b = _fmp_news_to_unified(_fmp_news_fetch("press-releases-latest", {"page": 0, "limit": 30}, fmp_api_key),
                                       "FMP Press Release", weight=1.3, body_key="text", max_body=300)
        layer_c = _fmp_news_to_unified(_fmp_news_fetch("fmp-articles", {"page": 0, "limit": 20}, fmp_api_key),
                                       "FMP Article", weight=1.0, body_key="content", max_body=300)
        layer_d = _fmp_news_to_unified(_fmp_news_fetch("news/general-latest", {"page": 0, "limit": 20}, fmp_api_key),
                                       "FMP General News", weight=0.8, body_key="text", max_body=200)
        all_news = layer_a + layer_b + layer_c + layer_d
        source_log = [f"Stock {len(layer_a)}", f"PR {len(layer_b)}", f"Articles {len(layer_c)}", f"General {len(layer_d)}"]

    raw_count = len(all_news)
    if raw_count == 0:
        all_news = _rss_news_fallback()
        raw_count = len(all_news)
        source_log = [f"RSS Fallback {raw_count}"]

    top = _dedup_and_rank(all_news, top_n=top_n)
    news_meta = compute_news_freshness(top)
    context_text = _build_news_context_text(top)
    for item in top:
        item.pop("published_dt", None)
    news_meta["source_log"] = " | ".join(source_log)
    return top, context_text, raw_count, news_meta


# ─────────────────────────────────────────────────────────────────────────────
# 내러티브 생성 프롬프트/파서 (SSOT) — app.py 프롬프트를 기준으로 한다.
# Gemini '호출'은 각 측이 자기 클라이언트로 수행하고, '프롬프트와 파싱'만 공유한다.
# (app: 전역 model.generate_content · 자동화: genai.Client — 호출부는 각자 유지)
# quant_data(앱 정량) 와 fred_alert(자동화 경제지표) 는 둘 다 옵션 주입.
# ─────────────────────────────────────────────────────────────────────────────
import json


def build_narrative_prompt(news_text, target_language: str = "ko",
                           quant_data: dict = None, fred_alert: str = "") -> str:
    """시장 내러티브 생성 프롬프트 (app.py SSOT)."""
    language_label = "한국어" if target_language == "ko" else "English"

    quant_section = ""
    if quant_data and quant_data.get("summary_text"):
        quant_section = f"""
[정량 스크리닝 데이터 — 실제 가격/거래량 기반]
{quant_data['summary_text']}

중요: winners 선정 시 위 정량 데이터에서 RS Score가 양수(+)이고 200일선 위에 있는 종목을 우선적으로 포함하세요.
뉴스 테마와 정량 모멘텀이 일치하는 종목이 가장 신뢰도 높은 Winners입니다.
"""
    fred_section = fred_alert if fred_alert else ""

    prompt = f"""
당신은 월가 수석 퀀트 전략가입니다.
아래 뉴스를 종합 분석하여 지정된 JSON 스키마 그대로만 응답하세요.

핵심 원칙:
- 뉴스(정성) + 가격 모멘텀(정량)이 동시에 확인된 종목만 Winners로 선정
- 뉴스만 좋고 가격이 안 받쳐주는 종목은 emerging으로만 분류
- 각 theme의 winners는 반드시 3~6개 티커
- **winners / emerging / top_quant_picks / expanding_to의 expected_tickers는 모두 개별 종목(individual stocks)만 사용**.
  ETF·레버리지 ETF(예: TQQQ, UPRO, SOXL)·섹터 ETF(예: XLK, SMH, SOXX)·국가 ETF(예: EWY, EWT)·인덱스 ETF는 **절대 포함 금지**.
  (이 단계는 개별주 발굴이 목적이며, ETF/섹터 흐름은 별도 단계에서 다룬다.)

뉴스 소스 유형별 활용 지침 (매우 중요):
- [SECTION A] Press Releases: 기업이 직접 발표한 공식 공시입니다.
  해당 섹션의 "Tickers:" 필드에 태그된 종목은 실적·M&A·가이던스 등 고임팩트 이벤트가 확인된 종목이므로,
  이런 이벤트가 가격 상승으로 확인되는 종목을 winners의 최우선 후보로 선정하세요.
- [SECTION B] Stock News: 각 기사의 "Tickers:" 필드는 해당 뉴스와 직접 연관된 종목입니다.
  동일 티커가 여러 기사에 반복 등장할수록 모멘텀이 강한 종목으로 간주하세요.
- [SECTION C] FMP Articles: 테마의 구조적 맥락과 expanding_to 단계 설정에 활용하세요.
- [SECTION D] General/Macro News: regime(Risk On/Off, liquidity 방향) 판단에 주로 활용하세요.

중요 규칙:
1) 반드시 순수 JSON 텍스트만 출력 (```json 같은 마크다운 금지)
2) 모든 키를 빠짐없이 포함
3) themes는 최소 3개 이상 생성
4) winners/emerging은 티커를 쉼표로 구분한 문자열
5) 각 theme의 expanding_to는 반드시 객체 배열(list)이어야 함
6) expanding_to의 각 객체는 반드시 "stage", "expected_tickers", "linkage" 세 키를 포함
7) expanding_to 작성 규칙 (확산주 품질 — 매우 중요):
   - 각 stage는 테마가 번지는 '순서'를 따른다: stage 1 = 직접 인접 섹터, stage 2 = 한 다리 건너, stage 3 = 원거리. 병렬 테마 나열 금지.
   - expected_tickers는 각 stage마다 2~4개 티커를 쉼표 구분 문자열로 작성.
   - linkage는 "이 단계가 왜/어떻게 테마의 수혜를 받는가"의 인과 고리를 한 줄로 명시한다.
   - driver(이 테마의 실제 원동력)에서 출발해 전파를 추론하라. 일반 섹터 상식으로 채우지 말 것.
   - 인과 고리를 설명할 수 없는 종목은 포함하지 말 것.
   - 이미 크게 오른 유명 대형주보다, 아직 시장이 주목하지 않은 후발 수혜주를 우선하라.
   - **driver 기업 자체는 expanding_to 에 넣지 말 것.** 테마의 원동력에 해당하는 기업
     (예: AI 캐팩스 테마의 반도체 설계·클라우드 사업자)은 확산의 '출발점'이지 수혜처가
     아니다. 같은 theme 의 winners·emerging 에 이미 넣은 티커도 expanding_to 에서 제외한다.
   - stage 3(원거리)에서 인과 고리를 확신할 수 있는 티커가 없으면 **그 stage 를 통째로
     생략하라.** 규칙 11(티커 정확도) 때문에 확신 가능한 유명 대형주로 빈칸을 메우는 것을
     금지한다 — 억지로 채운 확산주는 다음 단계에서 걸러지지 않고 그대로 매수 후보가 된다.
8) momentum_note: 반드시 "강함", "보통", "약함" 셋 중 하나만 출력 (설명 금지, 큰따옴표 사용 금지)
9) 결과는 반드시 {language_label}로, 금융 전문 용어를 사용하여 가장 자연스럽게 작성
10) winners/emerging 선정 근거: 뉴스의 Tickers 태그 + 반복 등장 횟수 + 뉴스에 드러난 가격 모멘텀을 종합해 판단하세요.
    근거 없이 유명 대형주를 임의로 채워 넣는 것을 금지합니다. (가격 정량 검증은 다음 단계 스캐너가 수행)
11) 티커 정확도 (매우 중요): winners/emerging/expected_tickers/top_quant_picks 의 심볼은
    뉴스의 "Tickers:" 태그 또는 위 정량 데이터에 **그대로 등장하는 정확한 거래 심볼**을 사용하세요.
    특정 후보의 정확한 심볼이 불확실하면, 그 **후보 하나만** 제외하고 확신하는 다른 종목으로 채워
    각 theme의 winners 3~6개(규칙 6)를 **최대한 유지**하세요. 추측·축약·변형으로 티커를 지어내지 마세요
    (지어낸 심볼은 다음 단계 검증에서 제거되어 신호가 통째로 버려집니다).
    단, 해당 theme에 조건을 충족하는 개별 종목이 정말로 없을 때만 winners를 비우세요(억지로 채우지 말 것).
{quant_section}
{fred_section}
[뉴스 데이터 — 소스 유형별 섹션 구분]
{news_text}

[출력 JSON 스키마]
{{
  "regime": {{
    "risk": "Risk On 또는 Risk Off",
    "growth_value": "Growth 선호 또는 Value 선호",
    "liquidity": "Expanding 또는 Tightening"
  }},
  "themes": [
    {{
      "title": "테마명 (예: AI Capex Expansion)",
      "driver": "무엇이 이 테마를 촉발했는가? Press Release·Stock News 기반 구체적 근거 포함",
      "winners": "정성+정량 모두 확인된 개별 수혜주 (예: NVDA, MSFT, AVGO)",
      "emerging": "뉴스 Tickers 태그는 있으나 가격 확인 필요한 개별주 (예: ARM, MRVL)",
      "momentum_note": "강함/보통/약함 중 하나만 선택 (예: 강함)",
      "expanding_to": [
        {{"stage": "기업용 AI 솔루션 (2차)", "expected_tickers": "CRM, NOW, WDAY", "linkage": "AI 인프라 구축 → 기업 워크플로우 AI 적용 수요 연쇄"}},
        {{"stage": "AI 기반 사이버 보안 (3차)", "expected_tickers": "CRWD, PANW, FTNT", "linkage": "AI 도입 확산 → 공격면 증가 → 보안 수요 연쇄"}}
      ],
      "risk": "이 테마가 무너질 수 있는 위험 요인"
    }}
  ],
  "rotation": "과열 섹터 -> 수혜 섹터 플로우 요약 (예: Tech -> Industrials)",
  "top_quant_picks": "내러티브상 확신도가 가장 높은 개별 종목 3~5개 (ETF 금지, 쉼표 구분)",
  "summary": "월가 퀀트 리포트 스타일 전체 시장 핵심 요약. 반드시 뉴스에 등장한 구체적 기업명·사건(실적·계약·IPO·제품 출시 등)을 2~3개 직접 근거로 인용할 것. 테마 제목만 재진술하는 일반론은 금지. 기관 vs 개인 투자자 뷰 차이 포함."
}}
You MUST respond ONLY with a valid JSON object. No markdown tags, no greetings.
"""
    return prompt


def parse_narrative_json(raw_text):
    """Gemini 원문 → dict. 마크다운 펜스 제거 후 json.loads. 실패 시 None."""
    if not raw_text:
        return None
    try:
        return json.loads(raw_text)
    except Exception:
        pass
    try:
        c = re.sub(r"^```json", "", raw_text.strip(), flags=re.IGNORECASE)
        c = re.sub(r"^```", "", c.strip())
        c = re.sub(r"```$", "", c.strip())
        return json.loads(c.strip())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 티커 검증 게이트 (SSOT) — LLM 출력 종목의 "존재·거래가능성"을 하드 검증한다.
# app.py(parse_narrative_json 직후)·자동화(run_narrative.generate 직후)가 함께 호출.
# Streamlit 비의존(requests만 사용) → 자동화에서도 동일하게 동작.
#
# 설계:
#  - 제거 + 리포트: 무효(상장폐지/비상장/오타) 티커는 실투자 필드에서 제거하되,
#    제거 목록을 report로 반환해 화면·이메일에 투명 표기.
#  - fail-open: FMP 키 없음/배치콜 0건(API 장애)이면 아무것도 제거하지 않고
#    verified_ok=False 만 반환(일시 장애로 내러티브 전체가 비는 것 방지).
#  - 검증 대상 필드: winners, emerging, top_quant_picks(문자열),
#    각 theme.expanding_to[].expected_tickers(문자열).
#  - summary(자유서술)의 이름-티커 오매칭은 v1 범위 밖(별도 퍼지 검증 영역).
# ─────────────────────────────────────────────────────────────────────────────

# 티커 후보 토큰: 점/하이픈 포함 클래스주(BRK.B)와 LLM이 만들어낸 변종 가짜
# 티커(예: P-SPAC)까지 폭넓게 포착한다. 시작은 영문자, 허용 문자는 [A-Z0-9.-],
# 길이 1~12, 공백·한글·문장은 제외. 일반 약어/가짜 오탐은 FMP 실거래 조회로
# 자연 필터(존재하지 않으면 제거)되므로 추출은 관대하게, 검증은 엄격하게.
_TICKER_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def _split_ticker_field(value) -> list:
    """winners/emerging/expected_tickers 같은 쉼표구분 문자열 → 티커 후보 리스트(순서/원형 보존)."""
    out = []
    for raw in str(value or "").replace("、", ",").replace("，", ",").split(","):
        tok = raw.strip().strip("·•*`()[]{} ").upper()
        if tok and _TICKER_TOKEN_RE.match(tok):
            out.append(tok)
    return out


def _fmp_validate_symbols(symbols, fmp_key: str, timeout: int = 8) -> set:
    """단건 /quote 기반 존재·거래가능 검증으로 위임(앱과 동일 경로).
    batch-quote-short는 일부 FMP 플랜에서 Restricted → 단건 quote로 대체.
    키 없음·전건 실패 시 빈 set(검증 불가, fail-open)."""
    valid, got_any = _fmp_validate_symbols_ex(symbols, fmp_key, timeout)
    return valid if got_any else set()


def sanitize_narrative_tickers(analysis, fmp_key: str = "") -> tuple:
    """내러티브 dict의 실투자 티커 필드에서 무효(비거래) 티커 제거 + 리포트 반환.
    Returns: (analysis, report)
      report = {
        "verified_ok": bool,        # FMP 검증이 실제로 수행됐는지
        "checked": int,             # 검증한 고유 티커 수
        "removed": [str, ...],      # 제거된 무효 티커(중복 제거, 정렬)
        "removed_detail": {ticker: [위치라벨, ...]},  # 어디서 제거됐는지
      }
    fail-open: verified_ok=False면 원본을 그대로 반환(아무것도 제거 안 함)."""
    report = {"verified_ok": False, "checked": 0, "removed": [], "removed_detail": {}}
    if not isinstance(analysis, dict) or not analysis:
        return analysis, report

    themes = analysis.get("themes")
    themes = themes if isinstance(themes, list) else []

    # 1) 전체 티커 수집(1회 배치 검증용)
    candidates = set()
    for fld in ("top_quant_picks",):
        candidates.update(_split_ticker_field(analysis.get(fld, "")))
    for theme in themes:
        if not isinstance(theme, dict):
            continue
        candidates.update(_split_ticker_field(theme.get("winners", "")))
        candidates.update(_split_ticker_field(theme.get("emerging", "")))
        flows = theme.get("expanding_to")
        if isinstance(flows, list):
            for flow in flows:
                if isinstance(flow, dict):
                    candidates.update(_split_ticker_field(flow.get("expected_tickers", "")))

    if not candidates:
        return analysis, report

    valid = _fmp_validate_symbols(candidates, fmp_key)
    if not valid:
        # 검증 불가(키 없음/API 장애) → fail-open: 원본 유지
        return analysis, report

    report["verified_ok"] = True
    report["checked"] = len(candidates)
    removed_detail = {}

    def _clean_field(value, where_label):
        """문자열 필드에서 무효 티커만 제거하고, 유효 티커는 순서대로 재조합."""
        toks = _split_ticker_field(value)
        if not toks:
            return value  # 티커 형태가 아니면 원본 보존(예: 빈 값/설명문)
        kept = []
        for t in toks:
            if t in valid:
                kept.append(t)
            else:
                removed_detail.setdefault(t, []).append(where_label)
        return ", ".join(kept)

    # 2) 필드별 정제
    if analysis.get("top_quant_picks"):
        analysis["top_quant_picks"] = _clean_field(analysis.get("top_quant_picks", ""), "top_quant_picks")

    for ti, theme in enumerate(themes):
        if not isinstance(theme, dict):
            continue
        tlabel = str(theme.get("title", "") or f"theme[{ti}]")[:40]
        if theme.get("winners"):
            theme["winners"] = _clean_field(theme.get("winners", ""), f"{tlabel}/winners")
        if theme.get("emerging"):
            theme["emerging"] = _clean_field(theme.get("emerging", ""), f"{tlabel}/emerging")
        flows = theme.get("expanding_to")
        if isinstance(flows, list):
            for flow in flows:
                if isinstance(flow, dict) and flow.get("expected_tickers"):
                    stage = str(flow.get("stage", "") or "expanding")[:30]
                    flow["expected_tickers"] = _clean_field(
                        flow.get("expected_tickers", ""), f"{tlabel}/{stage}")

    report["removed"] = sorted(removed_detail.keys())
    report["removed_detail"] = removed_detail
    return analysis, report


def format_ticker_gate_note(report) -> str:
    """리포트 → 사람이 읽는 한 줄 요약(화면/이메일/로그 공용).
    신형(verify_narrative_with_quant: removed_fake/removed_etf) · 구형(sanitize: removed) 둘 다 호환."""
    if not isinstance(report, dict):
        return ""
    if not report.get("verified_ok"):
        return "⚠️ 티커 정량 검증을 수행하지 못했습니다 (FMP 키 없음 또는 일시적 API 장애)."
    # 신형: fake(무효·오타)와 ETF(개별주 아님·정상)를 분리 표기. 구형(removed)은 무효로 취급.
    removed_fake = sorted(set(report.get("removed_fake") or []) or set(report.get("removed") or []))
    removed_etf = sorted(set(report.get("removed_etf") or []))
    checked = report.get("checked", 0)
    if not removed_fake and not removed_etf:
        return f"✅ 티커 정량 검증 완료 — {checked}개 모두 유효."
    parts = []
    if removed_fake:
        parts.append(f"🛑 무효·오타 {len(removed_fake)}개 제거: {', '.join(removed_fake)}")
    if removed_etf:
        parts.append(f"ℹ️ ETF 제외(개별주 아님) {len(removed_etf)}개: {', '.join(removed_etf)}")
    return " · ".join(parts) + f" (검증 {checked}개 중)"


# ─────────────────────────────────────────────────────────────────────────────
# 정량 검증 + 티어 enrich (SSOT) — 추천 티커 전체를 실제 RS·200일선·모멘텀으로 검증.
#
# 핵심 설계(드리프트 0): 정량 '공식'은 새로 짜지 않는다. 호출자(app.py·자동화)가
# 자기 쪽 verify_emerging_with_quant 를 verify_fn 으로 주입한다(의존성 주입).
# 따라서 RS/200일선/모멘텀 공식은 각 측 기존 함수 1곳에만 존재(이미 lockstep 유지).
# 이 모듈은 '전 티커 추출 → verify_fn 호출 → fake/ETF 제거 → 지표 부착'만 담당(순수).
#
# - fake/데이터없음: verify_fn 결과에 없는 티커 = 배치 가격에 안 잡힌 종목 → 제거.
# - ETF: etf_symbols(호출자가 isEtf 로 판별해 전달)에 든 티커 → 제거(개별주 단계 규칙).
# - 정량/내러티브 불일치(예: winner인데 RS 음수·200일선 아래): 기본은 '제거 안 함',
#   지표만 부착해 렌더에서 ⚠️ 플래그(대장주가 눌림목에서 단기 RS 음수일 수 있으므로,
#   regime-adaptive 관점에서 무조건 제거는 위험). 정책 토글은 호출자 책임.
# ─────────────────────────────────────────────────────────────────────────────

def _collect_output_tickers(analysis) -> list:
    """winners+emerging+top_quant_picks+expanding_to 의 모든 티커(중복 제거, 순서 보존)."""
    out, seen = [], set()

    def _add(value):
        for t in _split_ticker_field(value):
            if t not in seen:
                seen.add(t); out.append(t)

    if isinstance(analysis, dict):
        _add(analysis.get("top_quant_picks", ""))
        for theme in (analysis.get("themes") or []):
            if not isinstance(theme, dict):
                continue
            _add(theme.get("winners", ""))
            _add(theme.get("emerging", ""))
            for flow in (theme.get("expanding_to") or []):
                if isinstance(flow, dict):
                    _add(flow.get("expected_tickers", ""))
    return out


def _fmp_validate_symbols_ex(symbols, fmp_key: str, timeout: int = 8):
    """존재검증 — 단일 /quote?symbol= 로 종목별 확인(앱 가격 폴백이 쓰는 검증된 단일심볼 방식).
    반환 (유효심볼 set, 검사성공 bool). 미검증 티커만 들어오므로 보통 소수.
    판정: 200+행 있고 price>0 → 존재 / 200+빈리스트 → 없음(fake) / 비200·예외 → 보류(None)."""
    syms = sorted({str(s).upper().strip() for s in symbols if str(s).strip()})
    if not fmp_key or not syms:
        return set(), False
    import concurrent.futures as _cf

    def _one(s):
        try:
            r = requests.get(f"{_FMP_BASE}/quote",
                             params={"symbol": s, "apikey": fmp_key}, timeout=timeout)
            if r.status_code != 200:
                return s, None  # 일시 실패 → 보류
            d = r.json()
            if isinstance(d, list):
                if not d:
                    return s, False  # 200+빈 = 존재하지 않음(fake)
                row = d[0]
            elif isinstance(d, dict):
                row = d
            else:
                return s, None
            if isinstance(row, dict):
                price = row.get("price")
                try:
                    return s, (price is not None and float(price) > 0)
                except (TypeError, ValueError):
                    return s, True  # 행이 있으면 존재로 간주
            return s, None
        except Exception:
            return s, None  # 타임아웃/커넥션 → 보류

    valid, got_any = set(), False
    try:
        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            for fut in _cf.as_completed([ex.submit(_one, s) for s in syms]):
                try:
                    s, res = fut.result()
                except Exception:
                    res = None
                    s = None
                if res is None:
                    continue           # 보류(판단 불가)
                got_any = True         # 확정 답을 1건이라도 받음
                if res:
                    valid.add(s)
    except Exception:
        return set(), False
    return valid, got_any


def verify_narrative_with_quant(analysis, verify_fn, fmp_key: str = "", etf_symbols=None) -> tuple:
    """추천 티커 검증 — 정량(historical)을 1차 주력으로 실행하고, 존재검증은 비차단 보조.
    - 1차 PRIMARY: verify_fn(robust historical)로 RS·200일선·verdict 확보 = ✅검증됨.
    - 2차 SECONDARY(비차단): 미검증분만 quote 존재검증 → quote있음=🆕신규(유지),
      quote없음(검사성공시)=⛔fake 제거, 검사실패=🔁보류(유지, 제거 안 함).
    - 어떤 단계가 실패해도 정량 결과는 그대로 살림(차단 게이트 제거). ETF는 항상 제거.
    Returns: (analysis, report). analysis["_quant"]=지표, analysis["_quant_status"]=무지표 사유."""
    report = {"verified_ok": False, "checked": 0, "removed_fake": [], "removed_etf": [],
              "kept": [], "no_quant": [], "new_ipo": [], "unchecked": [],
              "quant_failed": False, "metrics": {}}
    if not isinstance(analysis, dict) or not analysis:
        return analysis, report

    candidates = _collect_output_tickers(analysis)
    if not candidates:
        return analysis, report
    cand_set = set(candidates)

    # 1) PRIMARY: robust 정량 페치 (존재+지표 동시 확인)
    try:
        results = verify_fn(candidates) or []
    except Exception:
        results = []
    metrics = {}
    for row in results:
        if isinstance(row, dict) and row.get("ticker"):
            metrics[str(row["ticker"]).upper().strip()] = row
    verified = set(metrics.keys())

    # 2) SECONDARY(비차단): 미검증분만 quote 존재검증으로 fake/신규 구분
    etf_set = {str(t).upper().strip() for t in (etf_symbols or set())}
    unverified = [t for t in candidates if t not in verified and t not in etf_set]
    exist_ok, exist_checked = (_fmp_validate_symbols_ex(unverified, fmp_key)
                               if unverified else (set(), True))

    removed_fake, new_ipo, unchecked = [], [], []
    for t in unverified:
        if t in exist_ok:
            new_ipo.append(t)         # 🆕 quote 있음·히스토리 부족 = 신규
        elif exist_checked:
            removed_fake.append(t)    # ⛔ quote 없음(검사 성공) = fake/상폐
        else:
            unchecked.append(t)       # 🔁 존재검사 실패 = 판단 보류(제거 안 함)

    removed_etf = sorted(cand_set & etf_set)
    keep = (verified | set(new_ipo) | set(unchecked)) - etf_set
    quant_failed = (len(verified) == 0 and len(cand_set) > 0)

    report.update({
        "verified_ok": True, "checked": len(cand_set),
        "removed_fake": sorted(removed_fake), "removed_etf": removed_etf,
        "kept": sorted(keep), "no_quant": sorted(set(new_ipo) | set(unchecked)),
        "new_ipo": sorted(new_ipo), "unchecked": sorted(unchecked),
        "quant_failed": quant_failed,
        "metrics": {k: metrics[k] for k in keep if k in metrics},
    })

    def _clean(value):
        toks = _split_ticker_field(value)
        if not toks:
            return value
        return ", ".join(t for t in toks if t in keep)

    if analysis.get("top_quant_picks"):
        analysis["top_quant_picks"] = _clean(analysis.get("top_quant_picks", ""))
    for theme in (analysis.get("themes") or []):
        if not isinstance(theme, dict):
            continue
        if theme.get("winners"):
            theme["winners"] = _clean(theme.get("winners", ""))
        if theme.get("emerging"):
            theme["emerging"] = _clean(theme.get("emerging", ""))
        for flow in (theme.get("expanding_to") or []):
            if isinstance(flow, dict) and flow.get("expected_tickers"):
                flow["expected_tickers"] = _clean(flow.get("expected_tickers", ""))

    analysis["_quant"] = report["metrics"]
    status = {t: "new" for t in new_ipo}
    status.update({t: "unchecked" for t in unchecked})
    analysis["_quant_status"] = status
    return analysis, report


def format_quant_gate_note(report) -> str:
    """정량 검증 리포트 → 한 줄 요약(화면/이메일/로그 공용)."""
    if not isinstance(report, dict):
        return ""
    if not report.get("verified_ok"):
        return "⚠️ 추천 티커 정량 검증을 수행하지 못했습니다 (가격 데이터 배치 실패)."
    if report.get("quant_failed"):
        return ("🔁 정량 검증 일시 실패 — 가격 데이터를 받지 못했습니다(재시도 권장). "
                "fake 제거는 적용됨, 정량 지표는 다음 실행에서 채워집니다.")
    parts = []
    if report.get("removed_fake"):
        parts.append(f"🛑 fake/데이터없음 {len(report['removed_fake'])}개 제거: {', '.join(report['removed_fake'])}")
    if report.get("removed_etf"):
        parts.append(f"📦 ETF {len(report['removed_etf'])}개 제거: {', '.join(report['removed_etf'])}")
    if report.get("new_ipo"):
        parts.append(f"🆕 신규(데이터 축적 전) {len(report['new_ipo'])}개: {', '.join(report['new_ipo'])}")
    if report.get("unchecked"):
        parts.append(f"🔁 검증보류 {len(report['unchecked'])}개: {', '.join(report['unchecked'])}")
    if not parts:
        return f"✅ 추천 티커 정량 검증 완료 — {report.get('checked', 0)}개 모두 유효."
    return f"정량 검증 {report.get('checked', 0)}개 중 · " + " · ".join(parts)


# =============================================================================
# 주간 트렌드(1.5) SSOT — 2C 설계
#
# app.py 의 「📊 주간 트렌드 추출(최근 7일)」 버튼과 automation/run_weekly_report.py 가
# **이 섹션의 함수들만** 호출한다. 프롬프트가 한 글자라도 갈라지면 브리핑이 달라지고,
# 그러면 3버킷 스캐너 유니버스 자체가 달라지므로 여기 단일 정의를 유지해야 한다.
#
# 2C 핵심: 주간 레코드에 **7일 병합 themes(driver/stage/linkage 포함)** 를 담는다.
#   - Winners 열      = themes[].winners 합집합      → 주도주·대기주 라우팅 풀
#   - Emerging 열     = themes[].expanding_to 합집합 → 확산주 유니버스
#   - analysis.themes = 확산주 Structural(가중치 0.40) 채점 근거
# =============================================================================

# SSOT 버전 스탬프 — app.py 가 import 직후 대조한다.
# 배포 누락/재부팅 누락 시 AttributeError 대신 명확한 안내를 띄우기 위함.
SSOT_VERSION = "2026-08-12a"

NARRATIVE_SOURCE_WEEKLY_7D = "weekly_trend_7d"

# 7일치 themes 를 제목 기준으로 병합한 뒤 유지할 최대 테마 수 (설계 확정: 12)
WEEKLY_THEME_MERGE_LIMIT = 12

# 확산주 유니버스 최소 등장 '일수'(ET 달력 기준 고유 날짜).
# 주간 브리핑 프롬프트가 이미 "7일 빈도·일관성"을 기준으로 쓰는데 확산주 풀만
# 그 기준을 안 쓰고 있어서 하루 스쳐간 일회성 언급까지 전부 들어와 87종목까지 불었다.
import os as _os
WEEKLY_EXPANSION_MIN_DAYS = max(1, int(_os.environ.get("WEEKLY_EXPANSION_MIN_DAYS", "2") or 2))

_WEEKLY_ET_TZ = pytz.timezone("America/New_York")

# Google Sheets 셀 한도 50,000자에 대한 안전 예산
SHEET_CELL_BUDGET = 49000

_WEEKLY_TICKER_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9.\-]{0,9}\b")


def parse_theme_tickers(text) -> list:
    """테마 필드 텍스트에서 티커 후보를 순서 보존·중복 제거로 추출.

    ⚠️ 블랙리스트 최종 필터는 scanner_core.filter_scanner_ticker_list 가 담당한다
       (run_three_bucket_scan 진입부에서 일괄 적용 — 중복 정의 방지).
    """
    out, seen = [], set()
    for tk in _WEEKLY_TICKER_TOKEN_RE.findall(str(text or "").upper()):
        tk = tk.strip(".-")
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


def theme_expanding_tickers(theme) -> list:
    """theme.expanding_to[].expected_tickers 합집합."""
    theme = theme if isinstance(theme, dict) else {}
    flows = theme.get("expanding_to")
    if not isinstance(flows, list):
        return []
    out, seen = [], set()
    for flow in flows:
        flow = flow if isinstance(flow, dict) else {}
        for tk in parse_theme_tickers(flow.get("expected_tickers", "")):
            if tk not in seen:
                seen.add(tk)
                out.append(tk)
    return out


def _norm_theme_title(title) -> str:
    """테마 제목 정규화 — 대소문자·공백·구두점 차이를 흡수해 같은 테마로 묶는다."""
    s = str(title or "").strip().lower()
    s = re.sub(r"[^\w가-힣]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact_record_for_timeseries(rec):
    """`Narratives` 한 건을 시계열 LLM 입력용으로 축약.

    ⚠️ 2C 변경점: 기존 축약은 title/winners/risk 만 남기고 **driver 와 expanding_to 를
       통째로 버려서**, 주간 브리핑이 2·3차 공급망 분석을 한 번도 못 보고 있었다.
       확산주 유니버스가 이 구조에서 나오는 만큼 driver 와 확장 티커를 함께 넘긴다.
    """
    if not isinstance(rec, dict):
        return None
    a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    themes = a.get("themes") if isinstance(a.get("themes"), list) else []
    theme_rows = []
    for th in themes[:12]:
        th = th if isinstance(th, dict) else {}
        exp = theme_expanding_tickers(th)
        theme_rows.append({
            "title": str(th.get("title", "") or "")[:200],
            "driver": str(th.get("driver", "") or "")[:200],
            "winners": str(th.get("winners", "") or "")[:400],
            "expanding": ", ".join(exp)[:300],
            "risk": str(th.get("risk", "") or "")[:300],
        })
    regime = a.get("regime") if isinstance(a.get("regime"), dict) else {}
    return {
        "saved_at": rec.get("saved_at"),
        "session_label": rec.get("session_label"),
        "language": rec.get("language"),
        "regime": regime,
        "themes": theme_rows,
        "rotation": str(a.get("rotation") or "")[:800],
        "summary": str(a.get("summary") or "")[:2000],
    }


def _rec_et_date(rec):
    """레코드의 ET 달력 날짜. 하루 여러 스냅샷(8am/5pm)을 1일로 세기 위함."""
    s = str((rec or {}).get("saved_at") or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_WEEKLY_ET_TZ).date().isoformat()
    except Exception:
        return None


def count_expanding_frequency(week_recs) -> dict:
    """티커 → expanding_to 에 등장한 '고유 날짜 수'(ET 기준).

    확산주 풀 정제용. 추가 API 호출 없이 이미 가진 스냅샷만으로 계산한다.
    """
    seen = {}
    for rec in (week_recs or []):
        if not isinstance(rec, dict):
            continue
        a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        if str(a.get("source") or "") == NARRATIVE_SOURCE_WEEKLY_7D:
            continue
        day = _rec_et_date(rec) or f"_seq{len(seen)}"
        for th in (a.get("themes") or []):
            for tk in theme_expanding_tickers(th if isinstance(th, dict) else {}):
                seen.setdefault(tk, set()).add(day)
    return {tk: len(days) for tk, days in seen.items()}


def merge_weekly_themes(week_recs, limit: int = WEEKLY_THEME_MERGE_LIMIT) -> list:
    """7일치 스냅샷의 themes 를 제목 기준으로 병합한다 (2C 핵심).

    병합 규칙:
      - 같은(정규화) 제목의 테마는 하나로 묶고 winners·expanding_to 티커는 **합집합**
      - driver / risk / linkage 는 **가장 최근** 스냅샷의 값을 채택
      - 등장 빈도(occurrences) 내림차순 → 동률이면 최신순 → 상위 `limit` 개만 유지

    Returns:
        app.py themes 스키마와 호환되는 list[dict]
        (title, driver, winners, emerging, expanding_to[{stage,expected_tickers,linkage}],
         risk, occurrences)
    """
    buckets = {}
    order = []
    for seq, rec in enumerate(week_recs or []):
        if not isinstance(rec, dict):
            continue
        a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        if str(a.get("source") or "") == NARRATIVE_SOURCE_WEEKLY_7D:
            continue  # 주간 레코드끼리 재귀 병합 방지
        themes = a.get("themes") if isinstance(a.get("themes"), list) else []
        for th in themes:
            th = th if isinstance(th, dict) else {}
            key = _norm_theme_title(th.get("title"))
            if not key:
                continue
            b = buckets.get(key)
            if b is None:
                b = {
                    "title": str(th.get("title", "") or "").strip(),
                    "driver": "", "risk": "",
                    "winners": [], "emerging": [],
                    "stages": {},          # stage명 -> {"tickers": [...], "linkage": str}
                    "occurrences": 0, "last_seq": -1,
                }
                buckets[key] = b
                order.append(key)
            b["occurrences"] += 1
            # 최신 레코드일수록 seq 가 크다는 전제(호출부에서 시간 오름차순 정렬)
            if seq >= b["last_seq"]:
                b["last_seq"] = seq
                if str(th.get("driver", "") or "").strip():
                    b["driver"] = str(th.get("driver")).strip()[:400]
                if str(th.get("risk", "") or "").strip():
                    b["risk"] = str(th.get("risk")).strip()[:300]
                if str(th.get("title", "") or "").strip():
                    b["title"] = str(th.get("title")).strip()

            for tk in parse_theme_tickers(th.get("winners", "")):
                if tk not in b["winners"]:
                    b["winners"].append(tk)
            for tk in parse_theme_tickers(th.get("emerging", "")):
                if tk not in b["emerging"]:
                    b["emerging"].append(tk)

            flows = th.get("expanding_to")
            if isinstance(flows, list):
                for flow in flows:
                    flow = flow if isinstance(flow, dict) else {}
                    stage = str(flow.get("stage", "") or "").strip() or "확장"
                    slot = b["stages"].setdefault(stage, {"tickers": [], "linkage": ""})
                    for tk in parse_theme_tickers(flow.get("expected_tickers", "")):
                        if tk not in slot["tickers"]:
                            slot["tickers"].append(tk)
                    lk = str(flow.get("linkage", "") or "").strip()
                    if lk and seq >= b["last_seq"] - 1:
                        slot["linkage"] = lk[:300]

    ranked = sorted(
        (buckets[k] for k in order),
        key=lambda b: (b["occurrences"], b["last_seq"]),
        reverse=True,
    )[: max(1, int(limit))]

    out = []
    for b in ranked:
        out.append({
            "title": b["title"],
            "driver": b["driver"],
            "winners": ", ".join(b["winners"]),
            "emerging": ", ".join(b["emerging"]),
            "expanding_to": [
                {"stage": st, "expected_tickers": ", ".join(v["tickers"]), "linkage": v["linkage"]}
                for st, v in b["stages"].items() if v["tickers"]
            ],
            "risk": b["risk"],
            "occurrences": b["occurrences"],
        })
    return out


def expansion_ticker_context(analysis) -> dict:
    """확산주 티커 → {"theme","stage","linkage"} 맥락 맵.

    "어느 테마의 몇 차 확산인가"를 표·이메일에 표시해 사용자가 인과 고리의 타당성을
    눈으로 검증할 수 있게 한다. 억지로 끼워진 확산주가 바로 드러난다.
    """
    a = analysis if isinstance(analysis, dict) else {}
    out = {}
    for th in (a.get("themes") or []):
        th = th if isinstance(th, dict) else {}
        title = str(th.get("title", "") or "").strip()
        for flow in (th.get("expanding_to") or []):
            flow = flow if isinstance(flow, dict) else {}
            stage = str(flow.get("stage", "") or "").strip()
            linkage = str(flow.get("linkage", "") or "").strip()
            for tk in parse_theme_tickers(flow.get("expected_tickers", "")):
                if tk not in out:   # 먼저 등장한(= 빈도 높은) 테마를 대표로
                    out[tk] = {"theme": title, "stage": stage, "linkage": linkage}
    return out


def weekly_scan_pools(analysis, min_days: int = None, with_stats: bool = False) -> dict:
    """주간 레코드 analysis 에서 3버킷 입력 풀을 뽑는다 (2C).

    확산주 풀에 두 가지 정제를 적용한다:
      ① **winners ∩ expanding 교집합 제거** — 확산주는 정의상 "아직 주도주·대기주가
         아닌" 후발주다. MSFT 가 대기주(39.54)와 확산주(83.90)에 동시 등장한 것은
         설계 위반이었다. driver 종목은 인과 고리가 없어 근거도 일반론으로 흐른다.
      ② **등장 일수 하한** — 7일 중 `min_days` 일 이상 expanding_to 에 나온 티커만.
         하루 스쳐간 일회성 언급을 걸러낸다. 추가 API 호출 없음.

    Args:
        min_days: 기본 WEEKLY_EXPANSION_MIN_DAYS. 0/None-safe.
        with_stats: True 면 정제 내역(`stats`)을 함께 반환.

    Returns:
        {"winners": [...], "expanding": [...]}  (+ with_stats 면 "stats")
    """
    a = analysis if isinstance(analysis, dict) else {}
    themes = a.get("themes") if isinstance(a.get("themes"), list) else []
    thr = WEEKLY_EXPANSION_MIN_DAYS if min_days is None else max(1, int(min_days))
    freq = a.get("expanding_freq") if isinstance(a.get("expanding_freq"), dict) else {}

    winners, seen_w = [], set()
    expanding, seen_x = [], set()
    for th in themes:
        th = th if isinstance(th, dict) else {}
        for tk in parse_theme_tickers(th.get("winners", "")):
            if tk not in seen_w:
                seen_w.add(tk)
                winners.append(tk)
        for tk in parse_theme_tickers(th.get("emerging", "")):
            if tk not in seen_w:
                seen_w.add(tk)
                winners.append(tk)
        for tk in theme_expanding_tickers(th):
            if tk not in seen_x:
                seen_x.add(tk)
                expanding.append(tk)

    raw_expanding = list(expanding)
    dropped_overlap = [t for t in raw_expanding if t in seen_w]
    kept = [t for t in raw_expanding if t not in seen_w]
    # 빈도 정보가 없는 옛 레코드는 필터를 적용하지 않는다(하위 호환).
    if freq:
        dropped_rare = [t for t in kept if int(freq.get(t, 0)) < thr]
        kept = [t for t in kept if int(freq.get(t, 0)) >= thr]
    else:
        dropped_rare = []
    expanding = kept
    _stats = {
        "raw": len(raw_expanding),
        "dropped_overlap": dropped_overlap,
        "dropped_rare": dropped_rare,
        "min_days": thr,
        "freq_available": bool(freq),
        "kept": len(expanding),
        "freq_hist": _freq_histogram(freq, raw_expanding) if freq else {},
    }

    # themes 가 비어 있는 옛 주간 레코드 폴백 — precomputed_universe 를 winners 로 사용
    if not winners and not expanding:
        pre = a.get("precomputed_universe")
        if isinstance(pre, list):
            for tk in pre:
                tk = str(tk).strip().upper()
                if tk and tk not in seen_w:
                    seen_w.add(tk)
                    winners.append(tk)
    out = {"winners": winners, "expanding": expanding}
    if with_stats:
        out["stats"] = _stats
    return out


def _freq_histogram(freq: dict, tickers) -> dict:
    """등장 일수별 티커 수 — 다음 주 임계값을 실측으로 정하기 위한 분포 로그."""
    hist = {}
    for t in (tickers or []):
        d = int((freq or {}).get(t, 0))
        hist[d] = hist.get(d, 0) + 1
    return dict(sorted(hist.items()))


def build_weekly_trend_record(briefing_markdown: str, language: str, week_recs: list,
                              theme_limit: int = WEEKLY_THEME_MERGE_LIMIT) -> dict:
    """주간 트렌드 레코드(analysis 포함)를 만든다. 앱 버튼·자동화가 공유한다."""
    md = str(briefing_markdown or "").strip()
    merged = merge_weekly_themes(week_recs, limit=theme_limit)

    ordered, seen = [], set()
    for th in merged:
        for tk in parse_theme_tickers(th.get("winners", "")) + \
                  parse_theme_tickers(th.get("emerging", "")) + \
                  theme_expanding_tickers(th):
            if tk not in seen:
                seen.add(tk)
                ordered.append(tk)
    for tk in parse_theme_tickers(md):
        if tk not in seen:
            seen.add(tk)
            ordered.append(tk)

    analysis = {
        "source": NARRATIVE_SOURCE_WEEKLY_7D,
        "themes": merged,
        # 확산주 풀 정제용 티커별 등장 일수 (weekly_scan_pools 가 필터에 사용)
        "expanding_freq": count_expanding_frequency(week_recs),
        "regime": {},
        "rotation": (f"최근 7일 롤링 윈도우 · 스냅샷 {len(week_recs or [])}건 기반 주간 트렌드 브리핑 "
                     f"(테마 {len(merged)}개 병합)"),
        "summary": md,
        "weekly_briefing_markdown": md,
        "precomputed_universe": ordered,
    }
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session_label": "📊 주간 트렌드 (최근 7일 교집합)",
        "language": str(language or "ko"),
        "analysis": analysis,
    }


def trim_weekly_analysis_for_cell(analysis, budget: int = SHEET_CELL_BUDGET) -> dict:
    """시트 셀 한도에 맞게 analysis 를 단계적으로 줄인다.

    ⚠️ 하드컷(`content[:49000]`)은 JSON 을 깨뜨려 레코드를 통째로 소실시킨다
       (`_sheet_row_to_narrative_record` 가 파싱 실패 시 None 반환).
       7일 병합 themes 는 무압축 시 10만 자를 넘길 수 있으므로 **하드컷 전에**
       여기서 반드시 줄여야 한다.

    축소 순서(가치가 낮은 것부터):
       1) weekly_briefing_markdown 20,000자
       2) 각 테마의 linkage/driver/risk 절삭
       3) 테마 수 절반씩 감축 (최소 3개)
       4) summary 5,000자
    """
    a = dict(analysis if isinstance(analysis, dict) else {})

    def size(d):
        try:
            return len(json.dumps(d, ensure_ascii=False))
        except Exception:
            return budget + 1

    if size(a) <= budget:
        return a

    md = str(a.get("weekly_briefing_markdown") or "")
    if len(md) > 20000:
        a["weekly_briefing_markdown"] = md[:20000] + "\n\n…(truncated)"
    if size(a) <= budget:
        return a

    themes = list(a.get("themes") or [])
    for th in themes:
        if not isinstance(th, dict):
            continue
        th["driver"] = str(th.get("driver") or "")[:150]
        th["risk"] = str(th.get("risk") or "")[:120]
        for flow in (th.get("expanding_to") or []):
            if isinstance(flow, dict):
                flow["linkage"] = str(flow.get("linkage") or "")[:120]
    a["themes"] = themes
    if size(a) <= budget:
        return a

    while len(a.get("themes") or []) > 3:
        a["themes"] = list(a["themes"])[: max(3, len(a["themes"]) // 2)]
        if size(a) <= budget:
            return a

    a["summary"] = str(a.get("summary") or "")[:5000]
    return a


def build_timeseries_prompt(kind: str, records_payload, target_language: str = "ko") -> str:
    """시계열 브리핑 프롬프트 SSOT (daily / weekly / wow).

    app.py `run_narrative_timeseries_gemini` 와 automation/run_weekly_report.py 가
    **이 함수만** 호출한다. weekly 분기의 `## 🏆 Weekly Winners` /
    `## 🚀 Weekly Expanding To` 고정 헤더 규칙이 여기 단일 정의로 존재한다.

    Args:
        kind: 'daily' | 'weekly' | 'wow'
        records_payload: compact_record_for_timeseries 로 축약된 스냅샷 묶음(dict)
        target_language: 'ko' | 'en'

    Returns:
        완성된 프롬프트 문자열.
    """
    language_label = "한국어" if target_language == "ko" else "English"
    payload_json = json.dumps(records_payload, ensure_ascii=False)
    if kind == "daily":
        task = """
당신은 월가 매크로 스트래티지스트입니다.
입력은 **미국 동부(ET) 달력 기준 오늘 하루**에 저장된 시장 내러티브 스냅샷들입니다(시간순).

다음을 **마크다운**으로 간결하지만 밀도 있게 작성하세요:
1) 오늘 하루 내러티브가 어떻게 **흘러갔는지**(시간대/세션 라벨이 있으면 활용)
2) 반복 등장한 **공통 승자(티커)·테마**
3) 레짐(regime) 변화가 있었다면 한 줄로
4) 투자자가 오늘 밤 준비할 **한 가지 체크포인트**

데이터에 없는 내용은 추측하지 마세요. 출력은 반드시 {lang}입니다.
""".format(
            lang=language_label
        )
    elif kind == "weekly":
        task = """
당신은 월가 매크로 스트래티지스트이자 액티브 트레이딩 데스크의 운영 파트너입니다.
입력은 **최근 7일(롤링)** 동안 저장된 시장 내러티브 스냅샷입니다(시간순).

[Part 1] 시장 요약 본문 (서술부)
다음을 **마크다운**으로 작성하세요:
1) 일주일 동안 **가장 강하게 유지된 메가 트렌드** 2~4개
2) **지속적으로 언급된 티커**(빈도·일관성 관점에서 상위)
3) rotation / 테마 확장 흐름에서 보이는 **자본 이동 가설**(보수적으로)
4) 다음 주 초반 **모니터링 우선순위** 3개

근거 없는 확정은 피하고, 데이터에 기반해 서술하세요. 본문은 반드시 {lang}로 작성합니다.

[Part 2] 실전 매매 연동 섹션 — 매우 중요 (1.6 Opportunity Scanner 자동 연동용)
전체적인 시장 요약 외에, 리포트 **하단에 반드시 다음 두 가지 섹션을 별도로 분리하여 작성**하라.
이 두 섹션은 1.6 스캐너가 정규식으로 자동 파싱하여 유니버스로 사용하므로,
아래 포맷을 **단 한 글자도** 어기지 마라.

A) 두 섹션의 **헤더 문자열은 고정**이며 그대로 사용한다(번역 금지, 이모지 포함, ## 레벨):
   ## 🏆 Weekly Winners (주간 대장주)
   ## 🚀 Weekly Expanding To (주간 후발/확장 수혜주)

B) 각 섹션은 아래 구성을 따른다:
   - 첫 줄: `테마: ...` — 해당 섹션의 핵심 테마/카테고리 1~3개를 쉼표로 구분
   - 그 다음 줄부터 **불릿 리스트**. 각 라인은 **정확히 티커 1개**만 담는다.
     라인 포맷(엄격):
       - **TICKER** — 한 줄 이유(약 20자, {lang})
     예: `- **NVDA** — AI 가속기 수요 가속`

C) 🏆 Weekly Winners (주간 대장주):
   - 일주일 내내 강한 모멘텀을 유지한 **핵심 주도 테마와 그 대표 티커**.
   - 5~10개 종목, **강한 순서**로 정렬.

D) 🚀 Weekly Expanding To (주간 후발/확장 수혜주):
   - 위 1차 대장주에서 **자금이 이동(Sector Rotation)** 중이거나, 공급망/인프라/2차 파생 수혜로
     다음 주 초반에 부상할 가능성이 높은 **순환매 후보 티커**.
   - 5~10개 종목, **기대도 높은 순**으로 정렬.
   - 🏆 Weekly Winners와의 **중복은 최소화**(가급적 0~1개).

[티커 표기 규칙 — 절대 준수]
- 본문 서술부와 두 섹션 모두에서, **모든 티커는 반드시 영문 대문자(UPPERCASE)** 로 표기한다.
  허용 문자: `[A-Z0-9.-]`, 길이 1~10자. 예) NVDA, MSFT, AVGO, BRK.B, MOG-A
- 두 섹션의 불릿 라인에는 **티커 1개만** 둔다.
  · 잘못된 예: `- **NVDA, AMD** — AI 칩 수혜`
  · 올바른 예: `- **NVDA** — AI 가속기 1위`  /  `- **AMD** — MI300 점유율 확대`
- 회사명·괄호·여러 티커 나열·소문자·풀네임은 금지(서술부에서도 동일).
- 일반 영어 약어(AI, ETF, US, FED, GDP, CEO 등 — 실제 상장 티커가 아닌 단어)는
  티커로 오인되지 않도록 **가급적 한국어로 풀어 쓰거나 소문자**로 적어라.
  (예: `AI` → `인공지능`, `FED` → `연준`)

[환각 방지]
- 입력 7일 데이터에 한 번도 등장하지 않은 **새 티커를 도입하지 말 것**.
- 근거가 약하면 해당 섹션을 비우지 말고, 가장 보수적인 후보 1~2개라도 제시하되
  이유 칸에 `데이터 부족 — 모니터링 후보` 와 같이 명시한다.
""".format(
            lang=language_label
        )
    else:
        task = """
당신은 월가 매크로 스트래티지스트입니다.
입력에는 **이번 주(최근 7일)** 스냅샷과 **저번 주(그 이전 7일)** 스냅샷이 구분되어 있습니다.

다음을 **마크다운**으로 작성하세요 (WoW = week-over-week):
1) **교집합**: 두 주 모두에서 살아남은 테마·티커
2) **차집합 / Narrative Drift**: 저번 주에는 A였는데 이번 주에는 B로 **돈·관심이 이동**한 흔적
3) **Fading**(사그라지는 내러티브)과 **Emerging**(부상하는 내러티브)을 명시적으로 구분
4) 한 문단 **Executive Summary** (투자 회의 브리핑 톤)

데이터 밖 환각 금지. 출력은 반드시 {lang}입니다.

[필수 — 자동 파싱 섹션: 아래 4개 헤더는 번역·수정 금지, 이모지 포함, ## 레벨 그대로 유지]
리포트 **하단**에 반드시 다음 4개 섹션을 추가하라. 시스템이 정규식으로 파싱하므로 헤더 문자열을 단 한 글자도 바꾸지 마라.

## 🏆 Weekly Winners (주간 대장주)
- 이번 주와 저번 주 **모두에서 강했던** 핵심 티커 (교집합 + 지속 모멘텀)
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 5~10개)

## 🚀 Weekly Expanding To (주간 후발/확장 수혜주)
- 이번 주 새로 부상하거나 다음 주 초반 순환매 수혜 예상 티커
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 5~10개)

## 🌱 Emerging (이번 주 새로 부상)
- 저번 주에는 없었고 이번 주에 새로 등장한 티커·테마
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 3~7개)

## 🥀 Fading (저번 주 대비 약화)
- 저번 주에는 강했지만 이번 주에 사그라든 티커·테마
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 3~7개)

[티커 표기 규칙 — 절대 준수]
- 모든 티커는 반드시 영문 대문자(UPPERCASE). 예) NVDA, MSFT, AVGO
- 각 불릿 라인에 티커 1개만. 잘못된 예: `- **NVDA, AMD** — ...`
- 일반 약어(AI, ETF, FED, GDP 등)는 티커로 표기 금지.
""".format(
            lang=language_label
        )
    return f"""
{task}

[입력 JSON]
{payload_json}
"""
