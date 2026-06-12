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
8) momentum_note: 반드시 "강함", "보통", "약함" 셋 중 하나만 출력 (설명 금지, 큰따옴표 사용 금지)
9) 결과는 반드시 {language_label}로, 금융 전문 용어를 사용하여 가장 자연스럽게 작성
10) winners/emerging 선정 근거: 뉴스의 Tickers 태그 + 반복 등장 횟수 + 뉴스에 드러난 가격 모멘텀을 종합해 판단하세요.
    근거 없이 유명 대형주를 임의로 채워 넣는 것을 금지합니다. (가격 정량 검증은 다음 단계 스캐너가 수행)
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
