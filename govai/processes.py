"""Provider-independent candidate extraction, source validation and conservative merge.

Quote matching proves location, not semantic entailment. Every action needs review.
The source document and its fragment IDs are never modified by this module.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError


class ProcessError(Exception):
    """Safe, user-facing analysis failure (never includes raw provider responses)."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(StrictModel):
    fragment_id: str
    quote: str


class GroundedValue(StrictModel):
    text: str
    evidence: list[Evidence]


class Action(StrictModel):
    description: str
    actor: GroundedValue | None
    inputs: list[GroundedValue]
    outputs: list[GroundedValue]
    conditions: list[GroundedValue]
    dependencies: list[GroundedValue]
    evidence: list[Evidence]


class Procedure(StrictModel):
    title: str
    anchor: Evidence
    actions: list[Action]


class Candidates(StrictModel):
    procedures: list[Procedure]
    explanation: str


class ProcessProvider(Protocol):
    name: str
    model: str

    def extract(self, payload: dict) -> Candidates: ...


@dataclass(frozen=True)
class ChunkSettings:
    core_chars: int = 6000
    overlap_chars: int = 1200
    context_chars: int = 6000
    max_fragments: int = 100
    max_chunks: int = 200


@dataclass
class Chunk:
    id: str
    core: list[dict]
    context: list[dict]
    context_limited: bool = False

    def payload(self, metadata: dict) -> dict:
        return {"chunk_id": self.id, "metadata": metadata,
                "core": self.core, "context": self.context,
                "context_limited": self.context_limited}


def _slice(fragment, start, end, clauses):
    clause = clauses.get(fragment["clause_id"], {})
    return {"fragment_id": fragment["id"], "text": fragment["text"][start:end],
            "start": fragment["start"] + start, "end": fragment["start"] + end,
            "clause_id": fragment["clause_id"], "clause_number": clause.get("number"),
            "parent_clause_id": clause.get("parent_id"),
            "section_id": fragment["section_id"], "heading_id": fragment["heading_id"],
            "page_number": fragment["page_number"], "kind": fragment["kind"]}


def _split_fragment(fragment, limit, clauses):
    text, start = fragment["text"], 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            # Prefer sentence/word boundaries; never discard even a space.
            boundaries = list(re.finditer(r"[.!?;]\s+|\s+", text[start:end]))
            if boundaries and boundaries[-1].end() >= limit // 2:
                end = start + boundaries[-1].end()
        yield _slice(fragment, start, end, clauses)
        start = end


def build_chunks(document: dict, settings: ChunkSettings = ChunkSettings()) -> list[Chunk]:
    if (settings.core_chars < 200 or settings.overlap_chars < 0
            or settings.context_chars < 0 or settings.max_fragments < 1
            or settings.max_chunks < 1):
        raise ValueError("Invalid chunk settings")
    fragments = document["fragments"]
    if (not fragments or len({f['id'] for f in fragments}) != len(fragments)
            or ''.join(f['text'] for f in fragments) != document['text']
            or any(document['text'][f['start']:f['end']] != f['text'] for f in fragments)
            or any(a['end'] != b['start'] for a, b in zip(fragments, fragments[1:]))
            or fragments[0]['start'] != 0 or fragments[-1]['end'] != len(document['text'])):
        raise ProcessError("Нарушена целостность структурированного документа. Загрузите его заново.")
    clauses = {c["id"]: c for c in document["clauses"]}
    by_id = {f['id']: f for f in fragments}
    groups, group, previous = [], [], None
    for f in fragments:
        key = (f['section_id'], f['clause_id'] or f['heading_id'])
        if group and key != previous:
            groups.append(group)
            group = []
        group.extend(_split_fragment(f, settings.core_chars, clauses))
        previous = key
    if group:
        groups.append(group)
    cores, core, size = [], [], 0
    for group in groups:
        # Keep a whole clause/heading together when it fits. A section starts a new chunk.
        n = sum(len(p['text']) for p in group)
        section_changed = core and core[-1]['section_id'] != group[0]['section_id']
        fits_alone = n <= settings.core_chars and len(group) <= settings.max_fragments
        if core and (section_changed or (fits_alone and (
                size + n > settings.core_chars or len(core) + len(group) > settings.max_fragments))):
            cores.append(core)
            core, size = [], 0
        for piece in group:
            if core and (size + len(piece['text']) > settings.core_chars
                         or len(core) >= settings.max_fragments):
                cores.append(core)
                core, size = [], 0
            core.append(piece)
            size += len(piece['text'])
    if core:
        cores.append(core)
    if len(cores) > settings.max_chunks:
        raise ProcessError(f"Документ требует {len(cores)} блоков; лимит — {settings.max_chunks}. "
                           "Разделите документ на части. Запросы API не выполнялись.")

    sections = {s['id']: s for s in document['sections']}
    chunks = []
    for i, core in enumerate(cores):
        context, used, limited = [], 0, False
        seen = {(p['fragment_id'], p['start'], p['end']) for p in core}

        def add(piece, budget):
            nonlocal used, limited
            key = (piece['fragment_id'], piece['start'], piece['end'])
            if key in seen:
                return
            if used + len(piece['text']) > budget or len(context) >= settings.max_fragments:
                limited = True
                return
            context.append(piece)
            seen.add(key)
            used += len(piece['text'])

        # Neighboring text is independent of structural recognition, in both directions.
        for neighbors in (list(reversed(cores[i-1])) if i else [],
                          cores[i+1] if i+1 < len(cores) else []):
            remaining = settings.overlap_chars
            for p in neighbors:
                if remaining <= 0:
                    break
                q = dict(p)
                if len(q['text']) > remaining:
                    if neighbors and neighbors[0]['end'] <= core[0]['start']:
                        q['text'] = q['text'][-remaining:]
                        q['start'] = q['end'] - remaining
                    else:
                        q['text'] = q['text'][:remaining]
                        q['end'] = q['start'] + remaining
                add(q, 2 * settings.overlap_chars)
                remaining -= len(q['text'])
        # Include observed parent clause openings and complete heading fragments.
        wanted = set()
        for p in core:
            cid, visited = p['clause_id'], set()
            while cid in clauses and cid not in visited:
                visited.add(cid)
                c = clauses[cid]
                wanted.update([fid for fid in c['fragment_ids'] if by_id[fid]['text'].strip()][:2])
                cid = c['parent_id']
            for sid in (p['section_id'], p['heading_id']):
                s = sections.get(sid)
                if s:
                    wanted.update(f['id'] for f in fragments
                                  if f['start'] >= s['start'] and f['end'] <= s['end'])
        for fid in sorted(wanted, key=lambda fid: by_id[fid]['start']):
            f = by_id[fid]
            # Long ancestor bodies are covered in their own core chunks.
            for p in _split_fragment(f, min(settings.core_chars, 1200), clauses):
                add(p, 2 * settings.overlap_chars + settings.context_chars)
                if p['end'] < f['end']:
                    limited = True
                    break
        context.sort(key=lambda p: (p['start'], p['end']))
        chunks.append(Chunk(f"chunk-{i+1}", core, context, limited))
    return chunks


def _normal(text):
    return ' '.join(text.casefold().split()).strip(' .;')


def _unique(items):
    return list({json.dumps(i, ensure_ascii=False, sort_keys=True): i for i in items}.values())


def _overlapping_evidence(left, right):
    return any(a['fragment_id'] == b['fragment_id']
               and any(x['start'] < y['end'] and y['start'] < x['end']
                       for x in a['matches'] for y in b['matches'])
               for a in left for b in right)


def _checked_evidence(evidence, chunk, fragments, clauses):
    checked = []
    for e in evidence:
        f = fragments.get(e.fragment_id)
        if not f or not e.quote.strip():
            raise ValueError("unknown_fragment_or_empty_quote")
        # A model may only cite text it actually received, even if it guesses another valid ID.
        ranges = sorted((p['start'], p['end']) for p in chunk.core + chunk.context
                        if p['fragment_id'] == e.fragment_id)
        sent = []
        for start, end in ranges:
            if sent and start <= sent[-1][1]:
                sent[-1][1] = max(sent[-1][1], end)
            else:
                sent.append([start, end])
        matches = []
        pos = f['text'].find(e.quote)
        while pos >= 0:
            start, end = f['start'] + pos, f['start'] + pos + len(e.quote)
            if any(a <= start and end <= b for a, b in sent):
                matches.append({'start': start, 'end': end})
            pos = f['text'].find(e.quote, pos + 1)
        if not matches:
            raise ValueError("quote_not_in_sent_fragment")
        c = clauses.get(f['clause_id'], {})
        checked.append({"fragment_id": f['id'], "quote": e.quote,
                        "clause_id": f['clause_id'], "clause_number": c.get('number'),
                        "section_id": f['section_id'], "page_number": f['page_number'],
                        "attribution": f['attribution'], "matches": _unique(matches)})
    if not checked:
        raise ValueError("missing_evidence")
    return _unique(checked)


def extract_processes(document: dict, provider: ProcessProvider, *,
                      settings: ChunkSettings = ChunkSettings(),
                      progress: Callable[[int, int], None] | None = None) -> dict:
    chunks = build_chunks(document, settings)
    if not document['text'].strip():
        raise ProcessError("В документе нет текста для анализа.")
    fragments = {f['id']: f for f in document['fragments']}
    clauses = {c['id']: c for c in document['clauses']}
    metadata = {k: v['value'] if v else None for k, v in document['metadata'].items()}
    procedures, rejected, reports = {}, [], []
    if progress:
        progress(0, len(chunks))
    for index, chunk in enumerate(chunks):
        try:
            response = provider.extract(chunk.payload(metadata))
            batch = Candidates.model_validate(response)
            if not batch.procedures and not batch.explanation.strip():
                raise ProcessError("LLM вернула пустой результат без объяснения.")
        except (ValidationError, ValueError) as exc:
            raise ProcessError(f"Блок {index+1}/{len(chunks)}: неверная структура ответа LLM. "
                               "Завершённый результат не сформирован.") from exc
        except ProcessError as exc:
            raise ProcessError(f"Блок {index+1}/{len(chunks)}: {exc} "
                               "Завершённый результат не сформирован.") from exc
        reports.append({"chunk_id": chunk.id, "core_ranges": [
            {k: p[k] for k in ('fragment_id', 'start', 'end')} for p in chunk.core],
            "context_limited": chunk.context_limited, "explanation": batch.explanation,
            "candidate_procedures": len(batch.procedures)})
        for proc in batch.procedures:
            try:
                anchor = _checked_evidence([proc.anchor], chunk, fragments, clauses)
                if not proc.title.strip() or not proc.actions:
                    raise ValueError('empty_procedure')
            except ValueError as exc:
                rejected.append({"chunk_id": chunk.id, "scope": "procedure", "reason": str(exc)})
                continue
            # Stable explicit source anchor, not an LLM-invented identifier or clause number.
            f = fragments[proc.anchor.fragment_id]
            key = f['clause_id'] or f['heading_id'] or f['id']
            target = procedures.setdefault(key, {"id": f"procedure-{key}", "title": proc.title,
                "title_variants": [], "anchor_evidence": [], "actions": []})
            target['anchor_evidence'] = _unique(target['anchor_evidence'] + anchor)
            if proc.title != target['title'] and proc.title not in target['title_variants']:
                target['title_variants'].append(proc.title)
            for action in proc.actions:
                try:
                    evidence = _checked_evidence(action.evidence, chunk, fragments, clauses)
                    if not action.description.strip() or not any(len(e['quote'].strip()) >= 12 for e in evidence):
                        raise ValueError('insufficient_action_evidence')
                except ValueError as exc:
                    rejected.append({"chunk_id": chunk.id, "scope": "action", "reason": str(exc)})
                    continue
                item = {"description": action.description, "evidence": evidence,
                        "actor": None, "requires_review": True,
                        "validation": "exact_quote_match_only", "review_reasons": []}
                for field in ('actor', 'inputs', 'outputs', 'conditions', 'dependencies'):
                    values = ([action.actor] if action.actor else []) if field == 'actor' else getattr(action, field)
                    valid = []
                    for value in values:
                        try:
                            ev = _checked_evidence(value.evidence, chunk, fragments, clauses)
                            if not value.text.strip():
                                raise ValueError('empty_value')
                            valid.append({"text": value.text, "evidence": ev})
                        except ValueError:
                            item['review_reasons'].append(f"unsupported_{field}_removed")
                            rejected.append({"chunk_id": chunk.id, "scope": field,
                                             "reason": "unsupported_value_removed"})
                    item[field] = (valid[0] if valid else None) if field == 'actor' else valid
                # Only exact normalized semantic fields are merged; distinct actions survive.
                signature = json.dumps([_normal(item['description']),
                    _normal(item['actor']['text']) if item['actor'] else None,
                    *[sorted(_normal(v['text']) for v in item[k])
                      for k in ('inputs', 'outputs', 'conditions', 'dependencies')]], ensure_ascii=False)
                existing = next((a for a in target['actions'] if a['_signature'] == signature
                                 and _overlapping_evidence(evidence, a['evidence'])), None)
                if existing:
                    existing['evidence'] = _unique(existing['evidence'] + item['evidence'])
                    existing['review_reasons'] = sorted(set(existing['review_reasons'] + item['review_reasons']))
                    if existing['actor']:
                        existing['actor']['evidence'] = _unique(existing['actor']['evidence'] + item['actor']['evidence'])
                    for field in ('inputs', 'outputs', 'conditions', 'dependencies'):
                        existing[field] = _unique(existing[field] + item[field])
                else:
                    item['_signature'] = signature
                    first_position = min(m['start'] for e in evidence for m in e['matches'])
                    item['id'] = 'action-' + sha256(
                        (key + signature + str(first_position)).encode()).hexdigest()[:16]
                    target['actions'].append(item)
        if progress:
            progress(index+1, len(chunks))
    final = [p for p in procedures.values() if p['actions']]
    def position(evidence):
        return min(m['start'] for e in evidence for m in e['matches'])
    final.sort(key=lambda p: position(p['anchor_evidence']))
    for p in final:
        p['actions'].sort(key=lambda a: position(a['evidence']))
        for a in p['actions']:
            del a['_signature']
    if final:
        explanation = "Извлечены кандидаты процессов. Проверены цитаты и их расположение; смысл требует проверки."
    elif rejected:
        explanation = "Кандидаты LLM не прошли проверку доказательств. Это не означает отсутствия процессов."
    else:
        explanation = "LLM не обнаружила конкретных административных действий во всех обработанных блоках."
    return {"schema_version": "1.0", "status": "completed_with_rejections" if rejected else "completed",
            "provider": provider.name, "model": provider.model, "prompt_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": dict(document['source']), "metadata": document['metadata'],
            "text_sha256": sha256(document['text'].encode()).hexdigest(),
            "procedures": final, "explanation": explanation, "rejected_candidates": rejected,
            "coverage": {"chunks_total": len(chunks), "chunks_completed": len(reports),
                         "core_characters": sum(len(p['text']) for c in chunks for p in c.core),
                         "source_characters": len(document['text']), "chunks": reports},
            "limitations": ["quote_match_does_not_prove_entailment", "semantic_recall_not_guaranteed",
                            "conservative_deduplication", "source_completeness_unverified"]}
