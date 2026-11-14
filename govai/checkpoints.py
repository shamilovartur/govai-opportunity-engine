"""Portable, strictly compatible per-chunk responses; no credentials or SDK state.

Restored responses always pass through the original grounding/merge pipeline.
An unsigned checkpoint is user-supplied data, not proof of an authentic LLM call.
"""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Literal

from pydantic import ValidationError

from govai.processes import (
    Candidates, ChunkSettings, ProcessError, ProcessProvider, StrictModel,
    build_chunks, extract_processes,
)

MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024


class AnalysisIdentity(StrictModel):
    document_sha256: str
    text_sha256: str
    structure_sha256: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    output_schema_sha256: str
    chunk_settings: dict[str, int]


class Checkpoint(StrictModel):
    kind: Literal['govai_process_checkpoint']
    schema_version: Literal['1.0']
    analysis_id: str
    identity: AnalysisIdentity
    chunk_inputs: dict[str, str]
    results: dict[str, Candidates]
    updated_at: str


class AnalysisInterrupted(ProcessError):
    def __init__(self, message: str, checkpoint: dict):
        self.checkpoint = checkpoint
        self.completed_blocks = len(checkpoint['results'])
        self.total_blocks = len(checkpoint['chunk_inputs'])
        super().__init__(f'{message} Успешно обработано блоков: '
                         f'{self.completed_blocks}/{self.total_blocks}. '
                         'Сохранённые ответы доступны в checkpoint; итоговый анализ не завершён.')


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def new_checkpoint(document: dict, provider: ProcessProvider,
                   settings: ChunkSettings = ChunkSettings()) -> dict:
    chunks = build_chunks(document, settings)
    if not document['text'].strip():
        raise ProcessError('В документе нет текста для анализа.')
    if not all(isinstance(getattr(provider, field, None), str) and getattr(provider, field).strip()
               for field in ('name', 'model', 'prompt_version', 'prompt_sha256')):
        raise ProcessError('Для checkpoint нужны провайдер, модель, версия и хеш промпта.')
    metadata = {k: v['value'] if v else None for k, v in document['metadata'].items()}
    identity = AnalysisIdentity(
        document_sha256=document['source']['sha256'],
        text_sha256=sha256(document['text'].encode()).hexdigest(),
        structure_sha256=_digest({k: v for k, v in document.items() if k != 'source'}),
        provider=provider.name, model=provider.model,
        prompt_version=provider.prompt_version, prompt_sha256=provider.prompt_sha256,
        output_schema_sha256=_digest(Candidates.model_json_schema()), chunk_settings=asdict(settings),
    )
    inputs = {c.id: _digest(c.payload(metadata)) for c in chunks}
    return Checkpoint(kind='govai_process_checkpoint', schema_version='1.0',
                      analysis_id=_digest({'identity': identity.model_dump(), 'chunk_inputs': inputs}),
                      identity=identity, chunk_inputs=inputs, results={}, updated_at=_now()).model_dump()


def _validate(value) -> Checkpoint:
    try:
        checkpoint = Checkpoint.model_validate(value)
        if not checkpoint.chunk_inputs or not checkpoint.results.keys() <= checkpoint.chunk_inputs.keys():
            raise ValueError('unknown block')
        if any(not r.procedures and not r.explanation.strip() for r in checkpoint.results.values()):
            raise ValueError('unexplained empty response')
        expected_id = _digest({'identity': checkpoint.identity.model_dump(),
                               'chunk_inputs': checkpoint.chunk_inputs})
        if checkpoint.analysis_id != expected_id:
            raise ValueError('invalid identity digest')
        return checkpoint
    except (ValidationError, TypeError, ValueError, RecursionError) as exc:
        # Do not display validation details: they may contain arbitrary uploaded data.
        raise ProcessError('Checkpoint повреждён или имеет неподдерживаемый формат.') from exc


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def load_checkpoint(data: bytes) -> dict:
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise ProcessError('Checkpoint превышает ограничение 64 МБ.')
    try:
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_keys)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProcessError('Не удалось прочитать checkpoint: нужен корректный UTF-8 JSON.') from exc
    return _validate(value).model_dump()


def dump_checkpoint(checkpoint: dict) -> bytes:
    data = _validate(checkpoint).model_dump_json(indent=2).encode('utf-8')
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise ProcessError('Checkpoint превышает ограничение 64 МБ.')
    return data


def compatible_checkpoint(checkpoint: dict, expected: dict) -> dict:
    saved = _validate(checkpoint)
    if (saved.analysis_id != expected['analysis_id']
            or saved.identity.model_dump() != expected['identity']
            or saved.chunk_inputs != expected['chunk_inputs']):
        raise ProcessError('Checkpoint несовместим: отличаются документ, модель, промпт, '
                           'структура или параметры блоков. Сохранённые ответы не использованы.')
    return saved.model_dump()


def combine_checkpoints(current: dict, imported: dict) -> dict:
    """Importing an older compatible file must not discard newer successful blocks."""
    restored = compatible_checkpoint(imported, current)
    saved = _validate(current).model_dump()
    for cid, response in restored['results'].items():
        if cid in saved['results'] and saved['results'][cid] != response:
            raise ProcessError('Checkpoint содержит другой ответ для уже сохранённого блока. '
                               'Импорт отменён; текущие ответы сохранены.')
        saved['results'][cid] = response
    saved['updated_at'] = _now()
    return saved


def extract_with_checkpoint(document: dict, provider: ProcessProvider, *,
                            settings: ChunkSettings = ChunkSettings(),
                            checkpoint: dict | None = None,
                            on_checkpoint: Callable[[dict], None] | None = None,
                            progress: Callable[[int, int], None] | None = None) -> dict:
    expected = new_checkpoint(document, provider, settings)
    state = _validate(expected if checkpoint is None else compatible_checkpoint(checkpoint, expected))
    reused = len(state.results)

    def save():
        state.updated_at = _now()
        if on_checkpoint:
            try:
                on_checkpoint(state.model_dump())  # independent snapshot after every successful response
            except OSError as exc:
                raise ProcessError('Не удалось сохранить checkpoint. Проверьте место сохранения.') from exc
        if progress:
            progress(len(state.results), len(state.chunk_inputs))

    class CachedProvider:
        name, model = provider.name, provider.model
        prompt_version = provider.prompt_version
        prompt_sha256 = provider.prompt_sha256

        def extract(self, payload):
            cid = payload['chunk_id']
            if _digest(payload) != state.chunk_inputs.get(cid):
                raise ProcessError('Вход блока изменился; повторное использование checkpoint запрещено.')
            if cid not in state.results:
                response = Candidates.model_validate(provider.extract(payload))
                if not response.procedures and not response.explanation.strip():
                    raise ProcessError('LLM вернула пустой результат без объяснения.')
                state.results[cid] = response
                save()
            # Round-trip also avoids sharing mutable model instances with callers.
            return Candidates.model_validate(state.results[cid].model_dump())

    try:
        save()
        result = extract_processes(document, CachedProvider(), settings=settings)
    except ProcessError as exc:
        raise AnalysisInterrupted(str(exc), state.model_dump()) from exc
    result['execution'] = {'analysis_id': state.analysis_id, 'blocks_reused': reused,
                           'blocks_requested': len(state.results) - reused}
    return result
