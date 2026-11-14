"""Live-only CLI: python -m scripts.extract_processes INPUT --output work/processes.json.

No mock fallback. Requires OPENAI_API_KEY/OPENAI_MODEL and network access to OpenAI.
"""

import argparse
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from govai.checkpoints import MAX_CHECKPOINT_BYTES, dump_checkpoint, extract_with_checkpoint, load_checkpoint
from govai.extraction import ExtractionError, extract_document
from govai.llm import OpenAIProvider, configuration
from govai.processes import ChunkSettings, ProcessError


def save_checkpoint_file(path: Path, checkpoint: dict):
    """A failed write never replaces the previous complete JSON snapshot."""
    data = dump_checkpoint(checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path,
                        help='Resume and save after each block. Default: OUTPUT stem + .checkpoint.json')
    parser.add_argument('--core-chars', type=int, default=6000)
    parser.add_argument('--check-261', action='store_true',
                        help='Report observed anchors 3.2–3.8; does not alter extraction or prove action recall.')
    args = parser.parse_args()
    provider = None
    try:
        checkpoint_path = args.checkpoint or args.output.with_suffix('.checkpoint.json')
        if len({checkpoint_path.resolve(), args.input.resolve(), args.output.resolve()}) != 3:
            raise ProcessError('Исходный документ, checkpoint и итоговый JSON должны иметь разные пути.')
        checkpoint = None
        if checkpoint_path.exists():
            if checkpoint_path.stat().st_size > MAX_CHECKPOINT_BYTES:
                raise ProcessError('Checkpoint превышает ограничение 64 МБ.')
            checkpoint = load_checkpoint(checkpoint_path.read_bytes())
        key, model = configuration()
        document = extract_document(args.input.name, args.input.read_bytes())
        provider = OpenAIProvider(key, model)
        result = extract_with_checkpoint(document, provider, settings=ChunkSettings(core_chars=args.core_chars),
            checkpoint=checkpoint, on_checkpoint=lambda snapshot: save_checkpoint_file(checkpoint_path, snapshot),
            progress=lambda done, total: print(f'Блоки: {done}/{total}', flush=True))
        if args.check_261:
            expected = {f'3.{n}' for n in range(2, 9)}
            found = {e['clause_number'] for p in result['procedures'] for e in p['anchor_evidence']}
            result['benchmark_261'] = {'matched_anchor_clauses': sorted(expected & found),
                'missing_anchor_clauses': sorted(expected - found),
                'note': 'Проверка совпадения якорей; смысл и полнота конкретных действий требуют ручной оценки.'}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Сохранено: {args.output}')
    except (ExtractionError, ProcessError, OSError, ValueError) as exc:
        # No result file is written on failure. Never relabel partial output as success.
        parser.exit(2, f'Анализ не завершён: {exc}\n')
    finally:
        if provider:
            provider.close()


if __name__ == '__main__':
    main()
