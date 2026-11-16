"""Bounded, public-HTTPS-only document retrieval and HTML main-content extraction.

DNS results are checked and the connection (including proxy CONNECT) uses the
validated IP, with TLS SNI/certificate validation for the original hostname.
No cookies, netrc credentials, scripts or subresource requests are used.
"""

import ipaddress
import os
import re
import ssl
import time
import zlib
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import dns.exception
import dns.resolver
import trafilatura
import urllib3
from lxml import etree, html

from govai.extraction import MAX_FILE_SIZE, ExtractionError, extract_document
from govai.structure import structure_document

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 5
TOTAL_TIMEOUT = 30
MAX_REDIRECTS = 3
MAX_URL_LENGTH = 4096
REDIRECTS = {301, 302, 303, 307, 308}


class URLInputError(ExtractionError):
    """An actionable error; failed or incomplete inputs are never returned."""


def _public_ip(value):
    ip = ipaddress.ip_address(value)
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Disallow mapped/transition addresses, including embedded private IPv4.
        return (ip in ipaddress.ip_network('2000::/3')
                and not ip.ipv4_mapped and not ip.sixtofour and not ip.teredo)
    return True


def _validated_url(value):
    value = value.strip()
    if not value or len(value) > MAX_URL_LENGTH or re.search(r'[\x00-\x20\x7f\\]', value):
        raise URLInputError('Некорректная HTTPS-ссылка.')
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != 'https' or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError
        hostname = parsed.hostname.encode('idna').decode('ascii').lower()
        if '%' in hostname or hostname.endswith('.'):
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if ('.' not in hostname or hostname.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.test', '.invalid'))
                    or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', hostname)
                    or re.fullmatch(r'[0-9xa-f.]+', hostname)):
                raise ValueError
        else:
            if not _public_ip(hostname):
                raise ValueError
    except (ValueError, UnicodeError):
        raise URLInputError(
            'Разрешены только публичные HTTPS-ссылки на порт 443, без логина и пароля. '
            'Локальные и внутренние адреса запрещены.'
        ) from None
    host = f'[{hostname}]' if ':' in hostname else hostname
    path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    return urlunsplit(('https', host, path, query, '')), hostname


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise URLInputError('Превышено время загрузки. Попробуйте позже или загрузите файл.')
    return remaining


def _resolve_public_ips(hostname, deadline):
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = []
        resolver = dns.resolver.Resolver()
        for family in ('A', 'AAAA'):
            try:
                answer = resolver.resolve(hostname + '.', family, search=False,
                                          lifetime=min(CONNECT_TIMEOUT, _remaining(deadline)))
                addresses.extend(str(record) for record in answer)
            except dns.resolver.NoAnswer:
                continue
            except dns.exception.DNSException:
                # A timeout is not proof that the unobserved family is public.
                raise URLInputError('Не удалось безопасно проверить адрес сайта (DNS). Загрузите файл вручную.') from None
    else:
        addresses = [str(literal)]
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise URLInputError('Ссылка ведёт на локальный, внутренний или иной непубличный адрес. Загрузка запрещена.')
    return addresses


def _open_response(url, hostname, address, deadline):
    """Pin the destination IP; retain the hostname for Host, TLS SNI and CA checks."""
    timeout = urllib3.Timeout(total=_remaining(deadline),
                              connect=min(CONNECT_TIMEOUT, _remaining(deadline)),
                              read=min(READ_TIMEOUT, _remaining(deadline)))
    ca_file = (os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('SSL_CERT_FILE')
               or ssl.get_default_verify_paths().cafile)
    settings = dict(server_hostname=hostname, assert_hostname=hostname,
                    cert_reqs='CERT_REQUIRED', ca_certs=ca_file, timeout=timeout)
    proxy = None if proxy_bypass(hostname) else getproxies().get('https') or getproxies().get('all')
    if proxy:
        # The proxy is trusted deployment configuration, never supplied by a URL.
        # CONNECT uses the validated IP too; proxies forbidding this fail closed.
        proxy_auth = urllib3.util.parse_url(proxy).auth
        proxy_headers = urllib3.make_headers(proxy_basic_auth=unquote(proxy_auth)) if proxy_auth else None
        manager = urllib3.ProxyManager(proxy, proxy_headers=proxy_headers, **settings)
        pool = manager.connection_from_host(address, port=443, scheme='https')
    else:
        manager = pool = urllib3.HTTPSConnectionPool(address, port=443, **settings)
    parsed = urlsplit(url)
    target = parsed.path + ('?' + parsed.query if parsed.query else '')
    headers = {'Host': f'[{hostname}]' if ':' in hostname else hostname,
               'User-Agent': 'GovAI-Opportunity-Engine/1.0',
               'Accept': 'application/pdf,text/html,application/xhtml+xml',
               'Accept-Encoding': 'identity'}
    try:
        response = pool.urlopen('GET', target, headers=headers, redirect=False,
                                retries=False, preload_content=False, decode_content=False)
        return response, manager
    except Exception:
        manager.clear() if proxy else manager.close()
        raise


def _close(response, manager):
    response.close()
    manager.clear() if hasattr(manager, 'clear') else manager.close()


def _read_body(response, deadline):
    declared = response.headers.get('Content-Length')
    try:
        length = int(declared) if declared is not None else None
    except ValueError:
        raise URLInputError('Сервер вернул некорректный размер ответа.') from None
    if length is not None and (length < 0 or length > MAX_FILE_SIZE):
        raise URLInputError('Ответ превышает допустимый размер 10 МБ.')
    encoding = response.headers.get('Content-Encoding', 'identity').lower().strip()
    if encoding not in ('identity', 'gzip'):
        raise URLInputError('Сервер прислал неподдерживаемое сжатие. Скачайте документ и загрузите файл.')
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
    chunks, wire_size, decoded_size = [], 0, 0
    while True:
        # read1 returns after one underlying read; do not wait to fill a 64-KB
        # buffer while a slow server keeps resetting the socket read timeout.
        chunk = response.read1(64 * 1024, decode_content=False)
        _remaining(deadline)
        if not chunk:
            break
        wire_size += len(chunk)
        if wire_size > MAX_FILE_SIZE:
            raise URLInputError('Ответ превышает допустимый размер 10 МБ.')
        if decompressor:
            chunk = decompressor.decompress(chunk, MAX_FILE_SIZE - decoded_size + 1)
        decoded_size += len(chunk)
        if decoded_size > MAX_FILE_SIZE:
            raise URLInputError('Распакованный ответ превышает допустимый размер 10 МБ.')
        chunks.append(chunk)
    _remaining(deadline)
    if length is not None and wire_size != length:
        raise URLInputError('Документ получен не полностью: размер ответа не совпадает с заявленным.')
    if decompressor and (not decompressor.eof or decompressor.unused_data):
        raise URLInputError('Сжатый документ получен не полностью или содержит неподдерживаемые дополнительные данные.')
    if not decoded_size:
        raise URLInputError('Сервер вернул пустой документ.')
    return b''.join(chunks)


def _download(url):
    deadline, visited = time.monotonic() + TOTAL_TIMEOUT, set()
    for hop in range(MAX_REDIRECTS + 1):
        url, hostname = _validated_url(url)
        if url in visited:
            raise URLInputError('Обнаружен цикл перенаправлений.')
        visited.add(url)
        addresses = _resolve_public_ips(hostname, deadline)
        response, manager = _open_response(url, hostname, addresses[0], deadline)
        try:
            if response.status in REDIRECTS:
                location = response.headers.get('Location')
                if not location or hop == MAX_REDIRECTS:
                    raise URLInputError('Слишком много перенаправлений или отсутствует адрес назначения.')
                url = urljoin(url, location)
                continue
            if response.status in (401, 403):
                raise URLInputError(f'HTTP {response.status}: сайт или сетевой шлюз отказал в доступе. '
                                    'Возможно, нужна авторизация. Загрузите доступный вам файл вручную.')
            if response.status == 206 or response.headers.get('Content-Range'):
                raise URLInputError('Сервер вернул только часть документа (частичный ответ).')
            if response.status != 200:
                raise URLInputError(f'Не удалось загрузить документ: HTTP {response.status}.')
            return _read_body(response, deadline), response.headers.get('Content-Type', ''), url
        finally:
            _close(response, manager)
    raise URLInputError('Превышен лимит перенаправлений.')


INCOMPLETE = re.compile(
    r'открыть полный текст документа|текст (?:документа )?(?:приведен|приведён) не полностью|'
    r'полный текст.{0,60}(?:после авторизации|по подписке|доступен после)|'
    r'для (?:чтения|просмотра).{0,60}(?:авторизуйтесь|войдите)|'
    r'только фрагмент документа|продолжение (?:документа )?на следующей странице|'
    r'sign in to (?:read|view)|subscribe to (?:read|continue)|read the full (?:document|text)', re.I)


def _html_document(data, url, content_type):
    # Honor an explicit HTTP charset; otherwise lxml respects a meta charset/BOM.
    charset = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
    try:
        content = data.decode(charset[1]) if charset else data
        tree = html.fromstring(content, parser=html.HTMLParser(no_network=True))
    except (LookupError, UnicodeError, etree.ParserError, ValueError):
        raise URLInputError('Не удалось прочитать HTML или его кодировку.') from None
    if tree.xpath('//input[translate(@type,"PASSWORD","password")="password"]'):
        raise URLInputError('Страница требует авторизации; текст НПА не получен.')
    title = ' '.join(tree.xpath('//title/text()')).strip()
    if re.match(r'^(?:авторизация|вход в систему|доступ запрещ[её]н|access denied|forbidden|just a moment|captcha|проверка браузера)\b', title, re.I):
        raise URLInputError('Вместо документа получена страница входа, отказа в доступе или проверки браузера.')
    visible = ' '.join(' '.join(tree.xpath('//body//text()[not(ancestor::script or ancestor::style)]')).split())
    if INCOMPLETE.search(visible) or tree.xpath('//*[@rel="next"]'):
        raise URLInputError('Страница содержит признаки неполного документа, подписки или продолжения. '
                            'Загрузите полный текст файлом или укажите прямую ссылку на PDF.')
    region = (tree.xpath('//article') or tree.xpath('//main') or tree.xpath('//body') or [tree])[0]
    links = region.xpath('.//a')
    region_length = len(''.join(region.itertext()).strip())
    link_length = sum(len(''.join(link.itertext()).strip()) for link in links)
    if len(links) >= 4 and link_length > region_length * 0.6:
        raise URLInputError('Страница похожа на оглавление или список ссылок, а не на полный текст документа.')
    extracted = trafilatura.bare_extraction(
        tree, url=url, output_format='xml', include_comments=False, include_tables=True,
        include_links=False, with_metadata=False, deduplicate=False,
    )
    if extracted is None or extracted.body is None:
        raise URLInputError('Основной текст не найден. Возможно, это оглавление, страница входа или текст загружается JavaScript.')
    parts, headings, offset = [], [], 0

    def blocks(element):
        if element.tag == 'row':
            yield '\t'.join(''.join(c.itertext()) for c in element), False
        elif element.tag in ('head', 'p', 'item', 'quote') and not any(
                child.tag in ('p', 'item', 'list') for child in element):
            yield ''.join(element.itertext()), element.tag == 'head'
        else:
            if element.text and element.text.strip():
                yield element.text, False
            for child in element:
                yield from blocks(child)
                if child.tail and child.tail.strip():
                    yield child.tail, False

    for block, is_heading in blocks(extracted.body):
        if not block.strip():
            continue
        if is_heading:
            headings.append((offset, offset + len(block)))
        parts.append(block)
        offset += len(block) + 2
    text = '\n\n'.join(parts)
    if len(text.strip()) < 200:
        raise URLInputError('Получено слишком мало основного текста: документ может быть неполным или недоступным.')
    if INCOMPLETE.search(' '.join(text.split())):
        raise URLInputError('Извлечённый текст содержит признаки неполного документа.')
    return structure_document('web-document.html', sha256(data).hexdigest(), 'html', [text], headings,
                              ['html_completeness_unverified', 'html_main_content_transformed',
                               'html_automatic_list_numbering_not_resolved'])


def extract_url_document(url: str) -> dict:
    """Fetch one public document; never fall back to another source or cached text."""
    original_url = url.strip()
    try:
        data, content_type, final_url = _download(original_url)
    except URLInputError:
        raise
    except urllib3.exceptions.TimeoutError:
        raise URLInputError('Истекло время ожидания ответа сайта. Попробуйте позже или загрузите файл.') from None
    except (urllib3.exceptions.ProtocolError, zlib.error):
        raise URLInputError('Ответ оборван или повреждён: документ мог быть получен не полностью.') from None
    except (urllib3.exceptions.HTTPError, OSError, ValueError):
        raise URLInputError('Не удалось установить безопасное HTTPS-соединение. '
                            'Проверьте доступность сайта и настройки сети; можно загрузить файл вручную.') from None
    media_type = content_type.split(';', 1)[0].strip().lower()
    if data.startswith(b'%PDF-'):
        if not data.rstrip().endswith(b'%%EOF'):
            raise URLInputError('PDF не содержит завершающий маркер: документ может быть получен не полностью.')
        name = unquote(urlsplit(final_url).path.rsplit('/', 1)[-1])
        name = re.sub(r'[^\w.-]', '_', name)[:160] or 'web-document.pdf'
        if not name.lower().endswith('.pdf'):
            name += '.pdf'
        document = extract_document(name, data)
        source_type = 'url_pdf'
    elif media_type in ('text/html', 'application/xhtml+xml'):
        try:
            document = _html_document(data, final_url, content_type)
        except URLInputError:
            raise
        except Exception:
            raise URLInputError('Не удалось извлечь основной текст HTML. Загрузите документ файлом.') from None
        source_type = 'url_html'
    else:
        raise URLInputError('По ссылке не получен PDF или HTML-документ. Проверьте прямой адрес документа.')
    document['source'].update({
        'type': source_type, 'original_url': original_url, 'final_url': final_url,
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'completeness': 'unverified',
    })
    return document
