import json
from unittest.mock import patch

import pytest

from govai.checkpoints import load_checkpoint
from scripts.extract_processes import main, save_checkpoint_file
from test_checkpoints import Provider, SETTINGS, new_checkpoint, source


def test_cli_saves_recovers_without_duplicate_requests(tmp_path, monkeypatch):
    document = tmp_path / 'npa.txt'
    output = tmp_path / 'result.json'
    checkpoint = tmp_path / 'result.checkpoint.json'
    document.write_text(source()['text'])
    monkeypatch.setattr('sys.argv', ['extract_processes', str(document), '--output', str(output),
                                   '--core-chars', '250'])
    failed = Provider(fail_at='chunk-3')
    failed.close = lambda: None
    with patch('scripts.extract_processes.configuration', return_value=('unused-test-key', failed.model)), \
         patch('scripts.extract_processes.OpenAIProvider', return_value=failed):
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    assert not output.exists()
    saved = load_checkpoint(checkpoint.read_bytes())
    assert len(saved['results']) == 2
    fresh = Provider()
    fresh.close = lambda: None
    with patch('scripts.extract_processes.configuration', return_value=('unused-test-key', fresh.model)), \
         patch('scripts.extract_processes.OpenAIProvider', return_value=fresh):
        main()
    assert fresh.calls == ['chunk-3', 'chunk-4']
    assert len(load_checkpoint(checkpoint.read_bytes())['results']) == 4
    assert json.loads(output.read_text())['execution']['blocks_reused'] == 2


def test_atomic_write_failure_preserves_previous_file(tmp_path):
    path = tmp_path / 'checkpoint.json'
    snapshot = new_checkpoint(source(), Provider(), SETTINGS)
    save_checkpoint_file(path, snapshot)
    before = path.read_bytes()
    snapshot['updated_at'] = 'different time'
    with patch('scripts.extract_processes.os.replace', side_effect=OSError('disk error')):
        with pytest.raises(OSError):
            save_checkpoint_file(path, snapshot)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('collision', ['input_output', 'input_checkpoint', 'output_checkpoint'])
def test_path_collisions_rejected_before_api_and_do_not_overwrite_source(tmp_path, monkeypatch, collision):
    source_path = tmp_path / 'source.txt'
    source_path.write_text('Исходный текст')
    output = source_path if collision == 'input_output' else tmp_path / 'result.json'
    checkpoint = source_path if collision == 'input_checkpoint' else output if collision == 'output_checkpoint' else tmp_path / 'checkpoint.json'
    monkeypatch.setattr('sys.argv', ['extract_processes', str(source_path), '--output', str(output),
                                   '--checkpoint', str(checkpoint)])
    with patch('scripts.extract_processes.configuration') as configure:
        with pytest.raises(SystemExit) as caught:
            main()
    assert caught.value.code == 2
    configure.assert_not_called()
    assert source_path.read_text() == 'Исходный текст'
