"""gemini_core.py — Gemini 호출 SSOT (탄력적 생성 헬퍼).

자동화가 하루 한 번꼴로 죽던 두 가지 원인을 한 곳에서 막는다:
  1) 503 high-demand 스파이크 (8am ET·5pm 피크) — 재시도 횟수↑ + 지터 + 모델 폴백
  2) thinking 토큰이 max_output_tokens 예산을 깎아 본문/JSON이 잘림 — thinking_budget=0 기본
     + finish_reason==MAX_TOKENS(잘림) 감지 → 503과 구분 로그, 재시도 대상 처리

run_narrative.py · run_drg_predict.py · run_drg_verify.py 가 공유한다.
순수 google-genai 의존(외부 추가 의존 없음). automation 은 repo root 를 sys.path 에
추가하므로 `import gemini_core` 로 임포트한다.

정책(논의 확정):
  - 폴백 체인은 같은 2.5 패밀리(2.5-flash → 2.5-flash-lite)로 한정.
    같은 패밀리라 thinking 설정·파싱 동작이 동일 → 코드 단순. (2.0-flash 제외)
  - 한 잡(GitHub Actions, 기본 6h 한도) 안에서 끝나도록 백오프 상한 120초.
"""

import time
import random

from google.genai import types as genai_types

# ── 모델 폴백 체인 (같은 2.5 패밀리) ──────────────────────────────────────────
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-flash-lite"

# ── 기본 백오프(초). 지터는 ±20% 적용. 후반 시도는 마지막 값(120)으로 수렴. ──
DEFAULT_BACKOFF = [15, 30, 60, 90, 120]

# 재시도 대상으로 간주할 오류 키워드(소문자 비교)
_RETRYABLE_KEYS = (
    "503", "unavailable", "high demand", "overload",
    "429", "resource_exhausted", "rate limit", "rate_limit", "quota",
    "500", "internal", "deadline", "timeout", "temporar",
    "max_tokens", "잘림", "빈 응답", "empty response", "검증 실패",
)


def _is_retryable(exc) -> bool:
    s = str(exc).lower()
    return any(k in s for k in _RETRYABLE_KEYS)


def _jittered(seconds, frac: float = 0.20) -> float:
    delta = seconds * frac
    return max(0.5, seconds + random.uniform(-delta, delta))


def _finish_reason(resp) -> str:
    """응답의 finish_reason 이름 추출. 실패 시 ''."""
    try:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        fr = getattr(cand, "finish_reason", None)
        return getattr(fr, "name", None) or str(fr or "")
    except Exception:
        return ""


def generate_text(client, prompt, *,
                  temperature: float = 0.3,
                  max_output_tokens: int = 8192,
                  top_p=None,
                  thinking_budget=0,
                  validate=None,
                  primary_attempts: int = 5,
                  fallback_attempts: int = 5,
                  backoff=None,
                  log=print,
                  label: str = "Gemini") -> str:
    """탄력적 Gemini 텍스트 생성.

    Args:
        client: genai.Client 인스턴스
        prompt: 프롬프트 문자열
        thinking_budget: 0=사고 끔(기본, 잘림 방지). None 이면 thinking_config 미설정(사고 기본 ON).
        validate: 선택. callable(text)->bool. False/예외면 그 시도를 실패(재시도 대상)로 처리.
                  예) 내러티브: lambda t: narrative_core.parse_narrative_json(t) is not None
        primary_attempts / fallback_attempts: 모델별 최대 시도 횟수.
        backoff: 백오프 초 리스트(없으면 DEFAULT_BACKOFF).
        label: 로그 접두 라벨.

    Returns:
        생성된 텍스트(공백 제거).

    Raises:
        RuntimeError: PRIMARY·FALLBACK 모두 소진 실패 시.
    """
    backoff = backoff or DEFAULT_BACKOFF
    models = [(PRIMARY_MODEL, primary_attempts), (FALLBACK_MODEL, fallback_attempts)]

    cfg_kwargs = dict(temperature=temperature, max_output_tokens=max_output_tokens)
    if top_p is not None:
        cfg_kwargs["top_p"] = top_p
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=thinking_budget)
    cfg = genai_types.GenerateContentConfig(**cfg_kwargs)

    total = sum(n for _, n in models)
    done = 0
    last_err = None

    for model, n_attempts in models:
        for i in range(n_attempts):
            done += 1
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
                fr = _finish_reason(resp)
                if fr and "MAX_TOKENS" in fr.upper():
                    # 잘림: 503 과 구분. (thinking_budget=0 이면 거의 안 생기지만 안전망)
                    raise RuntimeError("응답 잘림(finish_reason=MAX_TOKENS) — 출력 예산 부족")
                text = ""
                try:
                    text = str(getattr(resp, "text", "") or "").strip()
                except Exception:
                    text = ""
                if not text:
                    raise RuntimeError(f"빈 응답 (finish_reason={fr or 'unknown'})")
                if validate is not None and not validate(text):
                    raise RuntimeError("출력 검증 실패(파싱 등)")
                if model != PRIMARY_MODEL:
                    log(f"[INFO] {label}: 폴백 모델({model})로 생성 성공")
                return text
            except Exception as e:
                last_err = e
                retryable = _is_retryable(e)
                has_more = done < total
                if retryable and has_more:
                    wait = _jittered(backoff[min(done - 1, len(backoff) - 1)])
                    log(f"[WARN] {label} {model} 시도 {i+1}/{n_attempts} 실패(재시도): {e} → {wait:.0f}초 대기")
                    time.sleep(wait)
                else:
                    # 비재시도성(인증/프롬프트 등) → 같은 모델 반복 무의미: 다음 모델로 즉시 넘어감
                    log(f"[WARN] {label} {model} 시도 {i+1}/{n_attempts} 실패"
                        f"({'재시도불가→모델전환' if not retryable else '소진'}): {e}")
                    if not retryable:
                        break  # 다음 모델로
    raise RuntimeError(f"{label} 생성 최종 실패(모든 모델·시도 소진): {last_err}")
