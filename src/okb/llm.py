import asyncio
import math
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .config import settings

_gen = AsyncOpenAI(base_url=settings.gen_url, api_key="local", timeout=600)
_emb = AsyncOpenAI(base_url=settings.emb_url, api_key="local", timeout=120)


class Artifact(BaseModel):
    """Distillation output contract; guided decoding enforces the JSON shape."""

    summary: str = Field(description="2-4 sentence summary in the source language")
    question: str | None = Field(default=None, description="Question this text answers, if any")
    resolution: str | None = Field(default=None, description="Decision/answer/action item, with owner if named")
    resolution_status: Literal["decided", "discussed", "open"] | None = Field(
        default=None,
        description="decided = explicitly committed; discussed = tentative leaning, "
        "no commitment; open = question raised but unresolved",
    )
    people: list[str] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list, description="Systems, tools, projects mentioned")


# Closed vocabulary. Extending it is a deliberate one-line code change reviewed
# by a human — constrained decoding makes drift ("papers", "Paper") impossible.
DocType = Literal[
    "paper", "report", "contract", "proposal", "invoice", "order", "certificate",
    "presentation", "instruction", "letter", "note", "other",
]


class DocPassport(BaseModel):
    """Document-level attributes, extracted once per document from its head."""

    doc_type: DocType | None = None
    title: str | None = Field(default=None, description="Document title as written in the source")
    year: int | None = Field(default=None, description="Year from the CONTENT (header, DOI, signing date), never from file mtime")
    authors: list[str] = Field(default_factory=list, max_length=10)
    basis: str | None = Field(default=None, description="Where doc_type/year were taken from, e.g. 'DOI', 'title page', 'signing date', 'filename'")


PASSPORT_SYSTEM = (
    "You classify work documents for a team knowledge base. Given a filename and "
    "the beginning of a document, extract its passport.\n"
    "doc_type vocabulary: paper = scientific article/publication (incl. journal "
    "and conference papers); report = report of any kind; contract = "
    "contract/agreement; proposal = application/grant proposal/tender bid; "
    "invoice = invoice/payment document; order = order/decree/directive; "
    "certificate = certificate/diploma; presentation = slides/pitch; "
    "instruction = manual/how-to; letter = letter/official correspondence; "
    "note = working note; other = ONLY when nothing above fits.\n"
    "title: as written in the document. year: strictly from the content itself "
    "(paper header, DOI, signing date), NOT from file dates. authors: the "
    "document's own authors only (max 10), not people merely mentioned. "
    "basis: one short sentence on where doc_type/year came from. "
    "Use null for anything the text does not establish."
)


async def doc_passport(filename: str, head: str) -> DocPassport:
    async with _sem:
        resp = await _gen.chat.completions.create(
            model=settings.gen_model,
            messages=[
                {"role": "system", "content": PASSPORT_SYSTEM},
                {"role": "user", "content": f"Filename: {filename}\n\n{head}"},
            ],
            temperature=0.0,
            max_tokens=1000,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "doc_passport", "schema": DocPassport.model_json_schema()},
            },
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    return DocPassport.model_validate_json(resp.choices[0].message.content)


DISTILL_SYSTEM = (
    "You distill work documents into retrieval artifacts for a team knowledge base. "
    "The input may start with a 'Document:' line describing the parent document "
    "(title, type, year) — context only, not part of the unit; the text unit to "
    "distill follows it. Extract: a concise summary, the question it answers "
    "(if any), the resolution/decision (if any), people and systems mentioned. "
    "question/resolution/resolution_status capture a live question or decision "
    "RECORDED in this text: set them only when the text itself raises a question "
    "or fixes a decision, as meeting minutes, working notes and correspondence "
    "do. The standard content of formal documents (contract clauses, invoice "
    "lines, certificate statements and the like) is not a decision — leave all "
    "three null there. Never present a tentative discussion as a firm decision: "
    "resolution_status=decided only when the text explicitly commits; a leaning "
    "without commitment is 'discussed' (phrase it tentatively); an unresolved "
    "question is 'open'. "
    "Write summary/question/resolution in the same language as the source text. "
    "Be factual, keep names, numbers, codes and dates exactly as written. "
    "Hard limits: summary <= 4 sentences, resolution <= 3 sentences, "
    "<= 10 people, <= 10 systems. Never repeat yourself."
)

_sem = asyncio.Semaphore(settings.concurrency)


async def distill(unit_text: str, doc_context: str = "") -> Artifact:
    async with _sem:
        resp = await _gen.chat.completions.create(
            model=settings.gen_model,
            messages=[
                {"role": "system", "content": DISTILL_SYSTEM},
                {"role": "user",
                 "content": f"{doc_context}\n\n{unit_text}" if doc_context else unit_text},
            ],
            temperature=0.1,
            max_tokens=2000,  # constrained decoding can't recover from truncation
            # NB: legacy vLLM `guided_json` is silently ignored by the deployed
            # version — the OpenAI-standard response_format does enforce the schema
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "artifact", "schema": Artifact.model_json_schema()},
            },
            extra_body={
                # Qwen3.6 is a reasoning model; thinking would eat the whole token
                # budget before content — distillation doesn't need it
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
    return Artifact.model_validate_json(resp.choices[0].message.content)


async def _distill_safe(unit_text: str, doc_context: str = "") -> Artifact | None:
    """Chunk-level error isolation: one poison chunk must not fail the document.

    Returns None after retries; caller falls back to raw-text passthrough.
    A degenerate over-long summary counts as a failure too (repetition loops).
    """
    for _ in range(2):
        try:
            art = await distill(unit_text, doc_context)
            if len(art.summary) <= 1500:
                return art
        except Exception:
            pass
    return None


async def distill_batch(units: list[str], doc_context: str = "") -> list[Artifact | None]:
    return list(await asyncio.gather(*[_distill_safe(u, doc_context) for u in units]))


OCR_SYSTEM = (
    "You transcribe scanned document pages. Output the page content as clean "
    "markdown, preserving structure: headings, lists, tables. Transcribe exactly "
    "what is written — do not summarize, translate or invent anything. "
    "If the page is blank or contains no text, output exactly: (empty page)"
)

# a plausible OCR page is under ~15k chars; longer output means degeneration
OCR_MAX_PLAUSIBLE = 15_000


async def ocr_page(png_b64: str) -> str:
    """Transcribe one scanned page image via the (vision-capable) generation model."""
    async with _sem:
        resp = await _gen.chat.completions.create(
            model=settings.gen_model,
            max_tokens=4000,
            temperature=0.0,
            messages=[
                {"role": "system", "content": OCR_SYSTEM},
                {"role": "user", "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                }]},
            ],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    text = resp.choices[0].message.content or ""
    if not text.strip() or len(text) > OCR_MAX_PLAUSIBLE:
        raise ValueError(f"implausible OCR output ({len(text)} chars)")
    return text


def _truncate_normalize(vec: list[float], dim: int) -> list[float]:
    # Qwen3-Embedding is matryoshka-trained: truncation + re-normalization is the
    # supported way to get a smaller dim (pgvector HNSW caps at 2000).
    v = vec[:dim]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Documents are embedded WITHOUT the instruct prefix (offline path)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        batch = texts[i : i + 64]
        resp = await _emb.embeddings.create(model=settings.emb_model, input=batch)
        out += [_truncate_normalize(d.embedding, settings.emb_dim) for d in resp.data]
    return out


QUERY_TASK = (
    "Дан вопрос о рабочих документах, митингах и проектах команды; "
    "найди фрагменты, отвечающие на него"
)


async def embed_query(q: str) -> list[float]:
    """Queries ARE wrapped in the task instruction (online path) — asymmetric by design."""
    resp = await _emb.embeddings.create(
        model=settings.emb_model, input=[f"Instruct: {QUERY_TASK}\nQuery: {q}"]
    )
    return _truncate_normalize(resp.data[0].embedding, settings.emb_dim)
