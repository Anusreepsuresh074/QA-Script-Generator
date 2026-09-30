"""
Map acceptance criteria to API endpoints.

Strategies are controlled by ``Settings.mapping_type``:

* **keyword** – simple token-overlap scoring (no ML model required).
* **embedding** – cosine similarity via ``sentence-transformers``.
* **ollama** / **groq** – LLM-based semantic matching (Ollama first, Groq fallback).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from app.ai.llm_client import LLMClient, LLMTransientError
from app.config.settings import MappingType, Settings, get_settings
from app.models.schemas import APISchema, ApiMatchScore

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did "
    "will would shall should may might can could of in to for on with at by "
    "from as into through during before after above below between out off "
    "and or not no nor so yet but if then else when while where which what "
    "that this these those it its".split()
)


@dataclass
class MappingResult:
    """Matched endpoints plus full criterion×endpoint score matrix."""

    matched: List[APISchema] = field(default_factory=list)
    scores: List[ApiMatchScore] = field(default_factory=list)


class APIMapper:
    """Select the best-matching API(s) for each acceptance criterion."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._model = None  # lazy-loaded embedding model

    def map(
        self,
        criteria: Sequence[str],
        endpoints: Sequence[APISchema],
        top_p: Optional[float] = None,
        summary: Optional[str] = None,
    ) -> List[APISchema]:
        """Match each criterion individually to endpoints and return the
        best-matching endpoint(s) per criterion, filtered by *top_p*.
        """
        return self.map_with_scores(criteria, endpoints, top_p, summary=summary).matched

    def map_with_scores(
        self,
        criteria: Sequence[str],
        endpoints: Sequence[APISchema],
        top_p: Optional[float] = None,
        summary: Optional[str] = None,
    ) -> MappingResult:
        """Return matched endpoints and all criterion×endpoint match scores."""
        if not endpoints:
            return MappingResult()

        mapping_criteria = _criteria_with_summary(criteria, summary)
        if not mapping_criteria:
            return MappingResult()

        if top_p is None:
            top_p = self._settings.top_p

        strategy = self._settings.mapping_type
        logger.info("Mapping strategy: %s (top_p=%.2f)", strategy.value, top_p)
        if summary:
            logger.info("Ticket summary included in mapping: \"%s\"", summary)

        if strategy in (MappingType.OLLAMA, MappingType.GROQ):
            result = self._llm_match(mapping_criteria, endpoints, top_p, summary=summary)
        elif strategy == MappingType.EMBEDDING:
            result = self._embedding_match(mapping_criteria, endpoints, top_p)
        else:
            result = self._keyword_match(mapping_criteria, endpoints, top_p)

        result = _finalize_mapping_result(result, endpoints, top_p, summary)
        _log_score_summary(result.scores, top_p)
        return result

    # -- keyword strategy ---------------------------------------------------

    @staticmethod
    def _keyword_match(
        criteria: Sequence[str],
        endpoints: Sequence[APISchema],
        top_p: float,
    ) -> MappingResult:
        ep_token_map: List[Tuple[APISchema, set[str]]] = []
        for ep in endpoints:
            tokens = _tokenize(
                f"{ep.endpoint} {ep.method} {ep.summary or ''} "
                + " ".join(ep.required_fields)
            )
            ep_token_map.append((ep, tokens))

        best_scores: dict[str, Tuple[float, APISchema]] = {}
        all_scores: List[ApiMatchScore] = []

        for criterion in criteria:
            c_tokens = _tokenize(criterion)
            if not c_tokens:
                continue

            logger.info("  Criterion: \"%s\"", criterion)
            best_score = 0.0
            best_ep: Optional[APISchema] = None

            for ep, ep_tokens in ep_token_map:
                overlap = len(c_tokens & ep_tokens)
                score = overlap / len(c_tokens)
                pct = round(score * 100, 1)
                logger.info("    %s %s → %.1f%%", ep.method, ep.endpoint, pct)
                all_scores.append(
                    ApiMatchScore(
                        criterion=criterion,
                        method=ep.method.upper(),
                        endpoint=ep.endpoint,
                        score=pct,
                        selected=False,
                    )
                )
                if score > best_score:
                    best_score = score
                    best_ep = ep

            if best_ep and best_score >= top_p:
                key = f"{best_ep.method} {best_ep.endpoint}"
                if key not in best_scores or best_score > best_scores[key][0]:
                    best_scores[key] = (best_score, best_ep)
                for row in all_scores:
                    if (
                        row.criterion == criterion
                        and row.method == best_ep.method.upper()
                        and row.endpoint == best_ep.endpoint
                    ):
                        row.selected = True
                logger.info(
                    "    ✓ Best: %s %s (%.1f%%)",
                    best_ep.method, best_ep.endpoint, best_score * 100,
                )

        matched = [
            ep.model_copy(update={"match_score": round(score * 100, 1), "matched": True})
            for score, ep in best_scores.values()
        ]
        logger.info("Keyword matching: %d endpoint(s) selected", len(matched))
        return MappingResult(matched=matched, scores=all_scores)

    # -- embedding strategy -------------------------------------------------

    def _embedding_match(
        self,
        criteria: Sequence[str],
        endpoints: Sequence[APISchema],
        top_p: float,
    ) -> MappingResult:
        model = self._get_embedding_model()

        ep_texts = [
            f"{ep.method} {ep.endpoint} {ep.summary or ''} "
            + " ".join(ep.required_fields)
            for ep in endpoints
        ]

        all_texts = list(criteria) + ep_texts
        embeddings = model.encode(all_texts, convert_to_tensor=True)

        from sentence_transformers.util import cos_sim  # type: ignore[import-untyped]

        n_criteria = len(criteria)
        criteria_emb = embeddings[:n_criteria]
        endpoint_emb = embeddings[n_criteria:]

        best_scores: dict[str, Tuple[float, APISchema]] = {}
        all_scores: List[ApiMatchScore] = []

        for i, criterion in enumerate(criteria):
            sims = cos_sim(criteria_emb[i], endpoint_emb).squeeze(0).tolist()

            logger.info("  Criterion: \"%s\"", criterion)
            best_score = 0.0
            best_ep: Optional[APISchema] = None

            for sim, ep in zip(sims, endpoints):
                pct = round(sim * 100, 1)
                logger.info("    %s %s → %.1f%%", ep.method, ep.endpoint, pct)
                all_scores.append(
                    ApiMatchScore(
                        criterion=criterion,
                        method=ep.method.upper(),
                        endpoint=ep.endpoint,
                        score=pct,
                        selected=False,
                    )
                )
                if sim > best_score:
                    best_score = sim
                    best_ep = ep

            if best_ep and best_score >= top_p:
                key = f"{best_ep.method} {best_ep.endpoint}"
                if key not in best_scores or best_score > best_scores[key][0]:
                    best_scores[key] = (best_score, best_ep)
                for row in all_scores:
                    if (
                        row.criterion == criterion
                        and row.method == best_ep.method.upper()
                        and row.endpoint == best_ep.endpoint
                    ):
                        row.selected = True
                logger.info(
                    "    ✓ Best: %s %s (%.1f%%)",
                    best_ep.method, best_ep.endpoint, best_score * 100,
                )

        matched = [
            ep.model_copy(update={"match_score": round(score * 100, 1), "matched": True})
            for score, ep in best_scores.values()
        ]
        logger.info("Embedding matching: %d endpoint(s) selected", len(matched))
        return MappingResult(matched=matched, scores=all_scores)

    def _get_embedding_model(self):
        if self._model is None:
            logger.info(
                "Loading sentence-transformer model: %s",
                self._settings.embedding_model,
            )
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

            self._model = SentenceTransformer(self._settings.embedding_model)
        return self._model

    # -- LLM strategy (Ollama → Groq fallback) ------------------------------

    _LLM_MAP_SYSTEM = """You are an API analyst. Score how well each API endpoint matches each Jira acceptance criterion.

Return ONLY valid JSON with this shape:
{
  "scores": [
    {
      "method": "POST",
      "endpoint": "/path",
      "score": 85,
      "criterion": "original criterion text"
    }
  ]
}

Rules:
- score is an integer 0-100 for relevance (probability-style match strength)
- Include one entry for EVERY combination of criterion and endpoint from the user message
- method and endpoint must exactly match entries from the provided endpoint list
- Do not invent endpoints"""

    def _llm_match(
        self,
        criteria: Sequence[str],
        endpoints: Sequence[APISchema],
        top_p: float,
        summary: Optional[str] = None,
    ) -> MappingResult:
        min_score = round(top_p * 100)
        ep_index = {
            f"{ep.method.upper()} {ep.endpoint}": ep
            for ep in endpoints
        }
        ep_list = [
            {
                "method": ep.method.upper(),
                "endpoint": ep.endpoint,
                "summary": ep.summary,
                "required_fields": ep.required_fields,
                "response_codes": ep.response_codes,
            }
            for ep in endpoints
        ]

        user_prompt = json.dumps(
            {
                "ticket_summary": summary or "",
                "minimum_score_for_selection": min_score,
                "acceptance_criteria": list(criteria),
                "endpoints": ep_list,
                "instruction": (
                    "The ticket_summary names the primary API under test — "
                    "endpoints mentioned there (e.g. POST /users) should score highest."
                ),
            },
            indent=2,
        )

        llm = LLMClient(self._settings)
        try:
            raw = llm.chat_completion(
                system=self._LLM_MAP_SYSTEM,
                user=user_prompt,
                temperature=0.1,
                max_tokens=8192,
                response_format={"type": "json_object"},
            )
        except LLMTransientError as exc:
            logger.error("LLM API mapping failed: %s", exc)
            raise

        parsed = _parse_llm_scores(raw)
        all_scores: List[ApiMatchScore] = []
        best_scores: dict[str, Tuple[float, APISchema]] = {}

        for item in parsed:
            criterion = str(item.get("criterion", ""))
            method = str(item.get("method", "")).upper()
            endpoint = str(item.get("endpoint", ""))
            score_raw = item.get("score", 0)
            try:
                score_pct = _normalize_score_pct(score_raw)
            except (TypeError, ValueError):
                continue

            logger.info(
                "  Criterion: \"%s\" → %s %s (%.1f%%)",
                criterion,
                method,
                endpoint,
                score_pct,
            )

            key = f"{method} {endpoint}"
            ep = ep_index.get(key)
            if not ep:
                logger.warning("LLM returned unknown endpoint: %s", key)
                continue

            all_scores.append(
                ApiMatchScore(
                    criterion=criterion,
                    method=method,
                    endpoint=endpoint,
                    score=round(score_pct, 1),
                    selected=False,
                )
            )

        for criterion in criteria:
            criterion_rows = [row for row in all_scores if row.criterion == criterion]
            if not criterion_rows:
                continue
            best_row = max(criterion_rows, key=lambda row: row.score)
            if best_row.score < min_score:
                continue
            best_row.selected = True
            ep = ep_index[f"{best_row.method} {best_row.endpoint}"]
            map_key = f"{best_row.method} {best_row.endpoint}"
            score_norm = best_row.score / 100.0
            if map_key not in best_scores or score_norm > best_scores[map_key][0]:
                best_scores[map_key] = (score_norm, ep)
            logger.info(
                "    ✓ Best: %s %s (%.1f%%)",
                best_row.method,
                best_row.endpoint,
                best_row.score,
            )

        matched = [
            ep.model_copy(update={"match_score": round(score * 100, 1), "matched": True})
            for score, ep in best_scores.values()
        ]
        logger.info("LLM matching (%s): %d endpoint(s) selected", llm.last_provider, len(matched))

        expected = len(criteria) * len(endpoints)
        max_score = max((row.score for row in all_scores), default=0.0)
        if not all_scores or max_score <= 0 or len(all_scores) < expected // 2:
            logger.warning(
                "LLM scores unusable (max=%.1f%%, got %d/%d pairs) — "
                "falling back to embedding for match probabilities",
                max_score,
                len(all_scores),
                expected,
            )
            return self._embedding_match(criteria, endpoints, top_p)

        return MappingResult(matched=matched, scores=all_scores)


# -- text utilities ---------------------------------------------------------

_HTTP_HINT = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}.-]+)",
    re.IGNORECASE,
)


def _criteria_with_summary(criteria: Sequence[str], summary: Optional[str]) -> List[str]:
    """Prepend ticket summary so mapping reflects the stated API under test."""
    items = [c.strip() for c in criteria if c and c.strip()]
    if summary and summary.strip():
        return [f"[Ticket summary] {summary.strip()}", *items]
    return items


def _apply_summary_endpoint_boost(
    scores: List[ApiMatchScore],
    summary: Optional[str],
    endpoints: Sequence[APISchema],
) -> None:
    """Raise scores for METHOD /path pairs explicitly named in the ticket summary."""
    if not summary:
        return

    ep_index = {f"{ep.method.upper()} {ep.endpoint.rstrip('/')}": ep for ep in endpoints}
    summary_label = f"[Ticket summary] {summary.strip()}"

    for method, path in _HTTP_HINT.findall(summary):
        method = method.upper()
        path = path.rstrip("/")
        ep = ep_index.get(f"{method} {path}")
        if not ep:
            continue

        boosted = False
        for row in scores:
            if (
                row.method.upper() == method
                and row.endpoint.rstrip("/") == path
                and row.criterion == summary_label
            ):
                row.score = max(row.score, 92.0)
                row.selected = True
                boosted = True
        if not boosted:
            scores.append(
                ApiMatchScore(
                    criterion=summary_label,
                    method=method,
                    endpoint=ep.endpoint,
                    score=92.0,
                    selected=True,
                )
            )
        logger.info(
            "Summary boost: %s %s → 92.0%% (named in ticket summary)",
            method,
            ep.endpoint,
        )


def _select_matched_endpoints(
    scores: Sequence[ApiMatchScore],
    endpoints: Sequence[APISchema],
    top_p: float,
) -> List[APISchema]:
    """Endpoints with at least one score meeting TOP_P."""
    min_score = round(top_p * 100, 1)
    ep_index = {f"{ep.method.upper()} {ep.endpoint}": ep for ep in endpoints}
    best: dict[str, Tuple[float, APISchema]] = {}

    for row in scores:
        if row.score < min_score:
            continue
        key = f"{row.method.upper()} {row.endpoint}"
        ep = ep_index.get(key)
        if not ep:
            continue
        if key not in best or row.score > best[key][0]:
            best[key] = (row.score, ep)

    return [
        ep.model_copy(update={"match_score": round(score, 1), "matched": True})
        for score, ep in best.values()
    ]


def _finalize_mapping_result(
    result: MappingResult,
    endpoints: Sequence[APISchema],
    top_p: float,
    summary: Optional[str],
) -> MappingResult:
    scores = list(result.scores)
    _apply_summary_endpoint_boost(scores, summary, endpoints)
    matched = _select_matched_endpoints(scores, endpoints, top_p)
    return MappingResult(matched=matched, scores=scores)


def _normalize_score_pct(score_raw: Any) -> float:
    """Convert LLM score to a 0–100 percentage."""
    score = float(score_raw)
    # Models sometimes return 0–1 probabilities instead of 0–100 percentages.
    if 0 < score <= 1:
        score *= 100
    return round(score, 1)


def _parse_llm_scores(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scores", "matches"):
            items = data.get(key)
            if isinstance(items, list):
                return items
    raise ValueError("LLM mapping response must contain a 'scores' array")


def _log_score_summary(scores: Sequence[ApiMatchScore], top_p: float) -> None:
    """Print best match probability per endpoint to the server log."""
    if not scores:
        return

    threshold_pct = round(top_p * 100, 1)
    by_endpoint: dict[str, float] = {}
    for row in scores:
        key = f"{row.method} {row.endpoint}"
        by_endpoint[key] = max(by_endpoint.get(key, 0.0), row.score)

    logger.info("=== API match probabilities (TOP_P=%.0f%%) ===", threshold_pct)
    for key in sorted(by_endpoint):
        pct = by_endpoint[key]
        status = "SELECTED" if pct >= threshold_pct else "below threshold"
        logger.info("  %s → %.1f%% (%s)", key, pct, status)

def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - _STOPWORDS


def _tokenize_many(texts: Sequence[str]) -> set[str]:
    combined: set[str] = set()
    for t in texts:
        combined |= _tokenize(t)
    return combined
