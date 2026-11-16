"""Validate completed extraction files against the document, without modifying them."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import ConfigDict, ValidationError

from govai.processes import ProcessError, StrictModel

MAX_JSON_BYTES = 64 * 1024 * 1024


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_json(data: bytes):
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError('duplicate key')
            obj[k] = v
        return obj
    if len(data) > MAX_JSON_BYTES:
        raise ProcessError('JSON превышает ограничение 64 МБ.')
    try:
        return json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite value')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProcessError('Нужен корректный UTF-8 JSON без повторяющихся ключей.') from exc


class Span(StrictModel):
    start: int
    end: int


class SourceEvidence(StrictModel):
    fragment_id: str
    quote: str
    clause_id: str | None
    clause_number: str | None
    section_id: str | None
    page_number: int | None
    attribution: str
    matches: list[Span]


class SourceValue(StrictModel):
    text: str
    evidence: list[SourceEvidence]


class SourceAction(StrictModel):
    id: str
    description: str
    actor: SourceValue | None
    inputs: list[SourceValue]
    outputs: list[SourceValue]
    conditions: list[SourceValue]
    dependencies: list[SourceValue]
    evidence: list[SourceEvidence]
    requires_review: bool
    validation: str
    review_reasons: list[str]


class SourceProcedure(StrictModel):
    id: str
    title: str
    title_variants: list[str]
    anchor_evidence: list[SourceEvidence]
    actions: list[SourceAction]


class SourceResult(StrictModel):
    # Preserve optional historical diagnostics/metrics; never trust them as proof.
    model_config = ConfigDict(extra='allow', strict=True)
    schema_version: Literal['1.0']
    status: Literal['completed', 'completed_with_rejections']
    provider: str
    model: str
    prompt_version: str
    generated_at: str
    source: dict
    metadata: dict
    text_sha256: str
    procedures: list[SourceProcedure]
    explanation: str
    rejected_candidates: list[dict]
    coverage: dict
    limitations: list[str]


@dataclass
class VerifiedAnalysis:
    raw: dict
    original_bytes: bytes
    result_sha256: str
    document_sha256: str
    structure_sha256: str
    actions: dict
    evidence: dict
    compact: dict


def verify_analysis(data: bytes, document: dict) -> VerifiedAnalysis:
    raw = read_json(data)
    if isinstance(raw, dict) and raw.get('kind') == 'govai_process_checkpoint':
        raise ProcessError('Это checkpoint.json, а не завершённый processes.json. Восстановите его в технических данных.')
    try:
        parsed = SourceResult.model_validate(raw)
    except ValidationError as exc:
        raise ProcessError('Формат processes.json не соответствует схеме завершённого извлечения.') from exc
    text_hash = sha256(document['text'].encode()).hexdigest()
    if (parsed.source.get('sha256') != document['source']['sha256'] or parsed.text_sha256 != text_hash
            or parsed.source.get('text_sha256') != text_hash
            or parsed.source.get('format') != document['source']['format']
            or parsed.metadata != document['metadata']):
        raise ProcessError('processes.json не соответствует загруженному НПА: отличаются хеши или реквизиты.')
    coverage = parsed.coverage
    if (type(coverage.get('chunks_total')) is not int or coverage['chunks_total'] < 1
            or coverage.get('chunks_completed') != coverage['chunks_total']
            or coverage.get('source_characters') != len(document['text'])
            or coverage.get('core_characters') != len(document['text'])):
        raise ProcessError('Исходное извлечение не завершено либо не соответствует длине документа.')
    fragments = {f['id']: f for f in document['fragments']}
    clauses = {c['id']: c for c in document['clauses']}
    registry, refs = {}, {}

    def check(e):
        value = e.model_dump()
        f = fragments.get(e.fragment_id)
        c = clauses.get(f['clause_id'], {}) if f else {}
        if (not f or not e.quote.strip() or not e.matches
                or any(value[k] != f[k] for k in ('clause_id', 'section_id', 'page_number', 'attribution'))
                or e.clause_number != c.get('number')):
            raise ProcessError('Некорректное доказательство в processes.json: ID или принадлежность фрагмента.')
        for span in e.matches:
            if (span.start < f['start'] or span.end > f['end'] or span.end <= span.start
                    or document['text'][span.start:span.end] != e.quote
                    or f['text'][span.start-f['start']:span.end-f['start']] != e.quote):
                raise ProcessError('Цитата или её координаты в processes.json не совпадают с НПА.')
        canonical = {**value, 'matches': sorted([s.model_dump() for s in e.matches], key=lambda s: s['start'])}
        key = digest(canonical)
        if key not in refs:
            eid = f'e{len(refs)+1}'
            refs[key] = eid
            registry[eid] = canonical
        return refs[key]

    def value(v):
        if not v.text.strip() or not v.evidence:
            raise ProcessError('В processes.json есть поле без подтверждающих цитат.')
        return {'text': v.text, 'refs': list(dict.fromkeys(check(e) for e in v.evidence))}

    actions, groups, group_ids = {}, [], set()
    for p in parsed.procedures:
        if not p.id or p.id in group_ids or not p.anchor_evidence:
            raise ProcessError('Пустой/повторяющийся ID группы либо отсутствует её доказательство.')
        group_ids.add(p.id)
        anchor_refs = list(dict.fromkeys(check(e) for e in p.anchor_evidence))
        groups.append({'id': p.id, 'title': p.title, 'anchor_refs': anchor_refs})
        for a in p.actions:
            if not a.id or a.id in actions or not a.description.strip() or not a.evidence:
                raise ProcessError('Пустой/повторяющийся ID действия либо отсутствует его доказательство.')
            entry = {'id': a.id, 'group': p.id, 'description': a.description,
                     'refs': list(dict.fromkeys(check(e) for e in a.evidence)),
                     'actor': value(a.actor) if a.actor else None}
            for field in ('inputs', 'outputs', 'conditions', 'dependencies'):
                entry[field] = [value(v) for v in getattr(a, field)]
            actions[a.id] = entry
    if group_ids & actions.keys():
        raise ProcessError('ID группы совпадает с ID исходного действия.')
    # Evidence text is sent once per source fragment; refs point to exact spans in it.
    used_fragments = {e['fragment_id'] for e in registry.values()}
    compact = {
        'metadata': {k: v['value'] if v else None for k, v in document['metadata'].items()},
        'groups': groups, 'actions': list(actions.values()),
        'evidence': {eid: [e['fragment_id'], [[s['start']-fragments[e['fragment_id']]['start'],
                                            s['end']-fragments[e['fragment_id']]['start']] for s in e['matches']]]
                     for eid, e in registry.items()},
        'fragments': {fid: {'text': fragments[fid]['text'],
                            'clause': clauses.get(fragments[fid]['clause_id'], {}).get('number')}
                      for fid in sorted(used_fragments, key=lambda fid: fragments[fid]['start'])},
    }
    return VerifiedAnalysis(raw, data, digest(raw), text_hash,
                            digest({k: v for k, v in document.items() if k != 'source'}),
                            actions, registry, compact)
