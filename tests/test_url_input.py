import gzip
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import dns.resolver
import pytest
import urllib3

from govai import url_input as web

HTML = (Path(__file__).parent / 'fixtures/npa-page.html').read_bytes()
PUBLIC_IP = '93.184.216.34'
URL = 'https://public.example.org/document'


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.status, self.body = status, body
        self.headers = headers or {}
        self.closed = False
        self.position = 0

    def read1(self, amt, decode_content):
        assert not decode_content
        chunk = self.body[self.position:self.position + amt]
        self.position += len(chunk)
        return chunk

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    resolver = Mock()
    resolver.resolve.side_effect = lambda host, family, **kw: [PUBLIC_IP] if family == 'A' else []
    monkeypatch.setattr(web.dns.resolver, 'Resolver', lambda: resolver)
    manager = Mock()
    opened = Mock()
    opened.return_value = (Response(HTML, headers={'Content-Type': 'text/html; charset=utf-8'}), manager)
    monkeypatch.setattr(web, '_open_response', opened)
    return opened, manager, resolver


def test_pdf_url_runs_existing_extractor_and_records_source(transport, pdf_bytes):
    opened, _, _ = transport
    response = Response(pdf_bytes, headers={'Content-Type': 'application/pdf', 'Content-Length': str(len(pdf_bytes))})
    opened.return_value = response, Mock()
    result = web.extract_url_document(URL + '.pdf')
    assert 'First page: procurement' in result['text']
    assert [p['number'] for p in result['pages']] == [1, 2]
    assert result['source']['type'] == 'url_pdf'
    assert result['source']['original_url'] == result['source']['final_url'] == URL + '.pdf'
    assert datetime.fromisoformat(result['source']['retrieved_at']).utcoffset().total_seconds() == 0
    assert result['source']['sha256'] == sha256(pdf_bytes).hexdigest()
    assert response.closed


def test_html_main_text_headings_points_and_unknown_pages(transport):
    result = web.extract_url_document(URL)
    assert result['source']['type'] == 'url_html'
    assert result['source']['format'] == 'html'
    assert result['source']['completeness'] == 'unverified'
    assert result['metadata']['number']['value'] == '999'
    assert result['metadata']['revision_date']['value'] == '2023-05-30'
    assert 'Главная' not in result['text']
    assert 'Реклама' not in result['text']
    assert [c['marker'] for c in result['clauses']] == ['1.', '1.1.', '2.', 'а)', 'б)']
    assert result['clauses'][1]['parent_id'] == result['clauses'][0]['id']
    assert result['text'].index('I. Общие положения') < result['text'].index('II. Сроки рассмотрения')
    assert all(f['page_number'] is None for f in result['fragments'])
    assert ''.join(f['text'] for f in result['fragments']) == result['text']
    assert result['source']['text_sha256'] == sha256(result['text'].encode()).hexdigest()
    assert 'html_completeness_unverified' in result['warnings']
    json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_http_errors_never_return_document(transport, status):
    opened, manager, _ = transport
    response = Response(b'Forbidden', status=status)
    opened.return_value = response, manager
    with pytest.raises(web.URLInputError, match=f'HTTP {status}'):
        web.extract_url_document(URL)
    assert response.closed
    manager.clear.assert_called_once()


@pytest.mark.parametrize('response', [
    Response(HTML, status=206),
    Response(HTML, headers={'Content-Range': 'bytes 0-10/100'}),
    Response(HTML, headers={'Content-Type': 'text/html', 'Content-Length': str(len(HTML) + 100)}),
    Response('<html><body>Краткий фрагмент</body></html>'.encode(), headers={'Content-Type': 'text/html; charset=utf-8'}),
])
def test_incomplete_responses_are_rejected(transport, response):
    transport[0].return_value = response, transport[1]
    with pytest.raises(web.URLInputError):
        web.extract_url_document(URL)


@pytest.mark.parametrize('suffix', [
    '<button>Открыть полный текст документа</button>',
    '<p>Полный текст доступен после авторизации</p>',
    '<a rel="next" href="/page2">Далее</a>',
    '<form><input type="password"></form>',
])
def test_paywall_login_or_pagination_never_silently_succeeds(transport, suffix):
    body = HTML.replace(b'</article>', suffix.encode() + b'</article>')
    transport[0].return_value = Response(body, headers={'Content-Type': 'text/html'}), transport[1]
    with pytest.raises(web.URLInputError):
        web.extract_url_document(URL)


def test_table_of_contents_is_not_a_full_document(transport):
    body = '<html><body><main>' + ''.join(
        f'<a href="/section{i}">Раздел {i}. Подробное наименование раздела нормативного акта</a>' for i in range(12)
    ) + '</main></body></html>'
    transport[0].return_value = Response(body.encode(), headers={'Content-Type': 'text/html; charset=utf-8'}), transport[1]
    with pytest.raises(web.URLInputError, match='оглавление'):
        web.extract_url_document(URL)


def test_http_200_browser_challenge_is_not_a_document(transport):
    body = HTML.replace('Синтетический НПА для проверки HTML'.encode(), b'Just a moment...')
    transport[0].return_value = Response(body, headers={'Content-Type': 'text/html'}), transport[1]
    with pytest.raises(web.URLInputError, match='проверки браузера'):
        web.extract_url_document(URL)


@pytest.mark.parametrize('url', [
    'http://public.example.org/doc', 'file:///etc/passwd', 'https://localhost/a',
    'https://foo.local/doc', 'https://service.internal/doc', 'https://127.0.0.1/doc',
    'https://10.0.0.2/doc', 'https://172.16.0.1/doc', 'https://192.168.1.2/doc',
    'https://169.254.169.254/latest/meta-data/', 'https://100.64.0.1/doc',
    'https://[::1]/doc', 'https://[::ffff:127.0.0.1]/doc', 'https://[fc00::1]/doc',
    'https://[64:ff9b::7f00:1]/doc', 'https://224.0.0.1/doc',
    'https://2130706433/doc', 'https://0177.0.0.1/doc', 'https://0x7f000001/doc',
    'https://user:password@public.example.org/doc', 'https://public.example.org:8443/doc',
    'https://public.example.org\\@127.0.0.1/doc', 'https://public.example.org/\nfoo',
])
def test_unsafe_urls_are_rejected_before_request(transport, url):
    with pytest.raises(web.URLInputError):
        web.extract_url_document(url)
    transport[0].assert_not_called()


@pytest.mark.parametrize('records', [['127.0.0.1'], [PUBLIC_IP, '10.1.2.3'], ['::1']])
def test_dns_private_or_mixed_results_are_rejected(transport, records):
    transport[2].resolve.side_effect = None
    transport[2].resolve.return_value = records
    with pytest.raises(web.URLInputError, match='непубличный'):
        web.extract_url_document(URL)
    transport[0].assert_not_called()


def test_dns_timeout_fails_closed(transport):
    transport[2].resolve.side_effect = dns.resolver.LifetimeTimeout()
    with pytest.raises(web.URLInputError, match='DNS'):
        web.extract_url_document(URL)
    transport[0].assert_not_called()


def test_redirect_to_private_address_is_not_followed(transport):
    response = Response(status=302, headers={'Location': 'https://127.0.0.1/admin'})
    transport[0].return_value = response, transport[1]
    with pytest.raises(web.URLInputError):
        web.extract_url_document(URL)
    assert transport[0].call_count == 1
    assert response.closed


def test_redirect_target_dns_is_checked_again(transport):
    transport[2].resolve.side_effect = lambda host, family, **kw: (
        [PUBLIC_IP] if host == 'public.example.org.' else ['192.168.0.2'])
    transport[0].return_value = Response(status=302, headers={'Location': 'https://second.example.org/doc'}), transport[1]
    with pytest.raises(web.URLInputError, match='непубличный'):
        web.extract_url_document(URL)
    assert transport[0].call_count == 1


def test_public_relative_redirect_preserves_original_and_final_urls(transport):
    transport[0].side_effect = [
        (Response(status=302, headers={'Location': '/final'}), transport[1]),
        (Response(HTML, headers={'Content-Type': 'text/html'}), transport[1]),
    ]
    result = web.extract_url_document(URL)
    assert result['source']['original_url'] == URL
    assert result['source']['final_url'] == 'https://public.example.org/final'


def test_redirect_limit_and_cycle(transport):
    transport[0].return_value = Response(status=302, headers={'Location': URL}), transport[1]
    with pytest.raises(web.URLInputError, match='цикл'):
        web.extract_url_document(URL)
    transport[0].side_effect = [(Response(status=302, headers={'Location': f'/hop{i}'}), transport[1])
                                for i in range(4)]
    with pytest.raises(web.URLInputError, match='перенаправлений'):
        web.extract_url_document(URL)


@pytest.mark.parametrize('headers', [{}, {'Content-Length': '5000'}])
def test_size_limit_with_or_without_content_length(transport, monkeypatch, headers):
    monkeypatch.setattr(web, 'MAX_FILE_SIZE', 100)
    transport[0].return_value = Response(b'x' * 101, headers=headers), transport[1]
    with pytest.raises(web.URLInputError, match='размер'):
        web.extract_url_document(URL)


def test_gzip_response_and_expansion_limit(transport, monkeypatch):
    body = gzip.compress(HTML)
    transport[0].return_value = Response(body, headers={'Content-Type': 'text/html', 'Content-Encoding': 'gzip'}), transport[1]
    assert web.extract_url_document(URL)['source']['type'] == 'url_html'
    monkeypatch.setattr(web, 'MAX_FILE_SIZE', 1000)
    transport[0].return_value = Response(gzip.compress(b'x' * 5000), headers={'Content-Encoding': 'gzip'}), transport[1]
    with pytest.raises(web.URLInputError, match='Распакованный'):
        web.extract_url_document(URL)


def test_truncated_pdf_is_not_repaired_silently(transport, pdf_bytes):
    transport[0].return_value = Response(pdf_bytes[:-15], headers={'Content-Type': 'application/pdf'}), transport[1]
    with pytest.raises(web.URLInputError, match='завершающий маркер'):
        web.extract_url_document(URL)


def test_network_timeout_is_actionable(transport):
    transport[0].side_effect = urllib3.exceptions.ReadTimeoutError(None, URL, 'timed out')
    with pytest.raises(web.URLInputError, match='время'):
        web.extract_url_document(URL)


def test_total_deadline_during_streaming_closes_response(transport, monkeypatch):
    response = Response(HTML, headers={'Content-Type': 'text/html'})
    transport[0].return_value = response, transport[1]
    monkeypatch.setattr(web.time, 'monotonic', Mock(side_effect=[0, 0, 0, 31]))
    with pytest.raises(web.URLInputError, match='время'):
        web.extract_url_document(URL)
    assert response.closed


def test_http_downgrade_redirect_is_rejected(transport):
    transport[0].return_value = Response(status=302, headers={'Location': 'http://public.example.org/doc'}), transport[1]
    with pytest.raises(web.URLInputError):
        web.extract_url_document(URL)
    assert transport[0].call_count == 1


def test_binary_response_is_not_interpreted_as_html(transport):
    transport[0].return_value = Response(b'PK\x03\x04data', headers={'Content-Type': 'application/octet-stream'}), transport[1]
    with pytest.raises(web.URLInputError, match='не получен PDF или HTML'):
        web.extract_url_document(URL)


@pytest.mark.parametrize('proxy', [None, 'http://proxy.example:8080'])
def test_transport_pins_ip_retains_tls_hostname_and_never_auto_redirects(monkeypatch, proxy):
    monkeypatch.setattr(web, 'getproxies', lambda: {'https': proxy} if proxy else {})
    monkeypatch.setattr(web, 'proxy_bypass', lambda host: False)
    pool, manager = Mock(), Mock()
    manager.connection_from_host.return_value = pool
    direct, proxied = Mock(return_value=pool), Mock(return_value=manager)
    monkeypatch.setattr(web.urllib3, 'HTTPSConnectionPool', direct)
    monkeypatch.setattr(web.urllib3, 'ProxyManager', proxied)
    web._open_response(URL, 'public.example.org', PUBLIC_IP, web.time.monotonic() + 30)
    call = proxied.call_args if proxy else direct.call_args
    assert call.kwargs['server_hostname'] == call.kwargs['assert_hostname'] == 'public.example.org'
    assert call.kwargs['cert_reqs'] == 'CERT_REQUIRED'
    if proxy:
        manager.connection_from_host.assert_called_once_with(PUBLIC_IP, port=443, scheme='https')
    else:
        assert call.args == (PUBLIC_IP,)
    args = pool.urlopen.call_args
    assert args.kwargs['redirect'] is False and args.kwargs['retries'] is False
    assert args.kwargs['preload_content'] is False
    assert args.kwargs['headers']['Host'] == 'public.example.org'
    assert 'Authorization' not in args.kwargs['headers']
