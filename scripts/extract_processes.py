"""Live-only CLI: python -m scripts.extract_processes INPUT --output work/processes.json.

No mock fallback. Requires OPENAI_API_KEY/OPENAI_MODEL and network access to OpenAI.
"""

import argparse
import json
from pathlib import Path

from govai.extraction import ExtractionError, extract_document
from govai.llm import OpenAIProvider, configuration
from govai.processes import ChunkSettings, ProcessError, extract_processes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--core-chars', type=int, default=6000)
    parser.add_argument('--check-261', action='store_true',
                        help='Report observed anchors 3.2–3.8; does not alter extraction or prove action recall.')
    args = parser.parse_args()
    provider = None
    try:
        key, model = configuration()
        document = extract_document(args.input.name, args.input.read_bytes())
        provider = OpenAIProvider(key, model)
        result = extract_processes(document, provider, settings=ChunkSettings(core_chars=args.core_chars),
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
