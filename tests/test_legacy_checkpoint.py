"""A tiny mock checkpoint saved by the previous PR iteration, before normalization."""
import json
from pathlib import Path
from govai.checkpoints import load_checkpoint, extract_with_checkpoint
from govai.extraction import extract_document
from govai.processes import Candidates


def test_checkpoint_from_previous_iteration_is_compatible_without_reextracting_first_block():
    root = Path(__file__).parent / 'fixtures'
    source = json.loads((root / 'legacy-checkpoint-source.json').read_text())
    document = extract_document(source['filename'], source['text'].encode())
    checkpoint = load_checkpoint((root / 'legacy-checkpoint-v1.json').read_bytes())
    class Provider:
        name, model = 'mock-browser', 'browser-mock'
        prompt_version, prompt_sha256 = '1.0', 'browser-fixture-prompt'
        def __init__(self): self.calls = []
        def extract(self, payload):
            self.calls.append(payload['chunk_id'])
            return Candidates(procedures=[], explanation='Legacy checkpoint compatibility mock.')
    provider = Provider()
    result = extract_with_checkpoint(document, provider, checkpoint=checkpoint)
    assert provider.calls == ['chunk-2', 'chunk-3']
    assert result['execution']['blocks_reused'] == 1
    assert result['coverage']['chunks_completed'] == 3
