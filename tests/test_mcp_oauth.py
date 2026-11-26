import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.config import Settings
from serein.core import Store


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def _authorize(client, registration, *, state='exact-state', resource='https://memory.example/serein/mcp'):
    verifier = 'synthetic-verifier-' + 'x' * 43
    params = {
        'response_type':'code', 'client_id':registration['client_id'],
        'redirect_uri':registration['redirect_uris'][0], 'state':state,
        'code_challenge':_challenge(verifier), 'code_challenge_method':'S256',
        'resource':resource, 'scope':'serein:mcp',
    }
    page = client.get('/authorize', params=params)
    assert page.status_code == 200
    assert 'synthetic-gateway-key' not in page.text
    consent = client.post('/authorize', data={**params, 'gateway_key':'synthetic-gateway-key'},
                          follow_redirects=False)
    assert consent.status_code == 303
    callback = urlsplit(consent.headers['location'])
    values = parse_qs(callback.query)
    assert values['state'] == [state]
    return values['code'][0], verifier, params


def test_oauth_discovery_pkce_refresh_rotation_and_static_bearer(tmp_path):
    token_file = tmp_path/'gateway-key'
    token_file.write_text('synthetic-gateway-key', encoding='utf-8')
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database) as store:
        store.create('scene_oauth', 'scene', 'OAuth fixture', 'Protected body')
    app = create_app(settings, token='synthetic-gateway-key', token_file=token_file)
    with TestClient(app, base_url='https://memory.example') as client:
        protected = client.get('/.well-known/oauth-protected-resource').json()
        assert protected == {
            'resource':'https://memory.example/serein/mcp',
            'authorization_servers':['https://memory.example'],
            'scopes_supported':['serein:mcp'], 'bearer_methods_supported':['header'],
        }
        metadata = client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['authorization_endpoint'] == 'https://memory.example/authorize'
        assert metadata['token_endpoint'] == 'https://memory.example/token'
        assert metadata['registration_endpoint'] == 'https://memory.example/register'
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert metadata['token_endpoint_auth_methods_supported'] == ['none']

        registered = client.post('/register', json={
            'client_name':'ChatGPT',
            'redirect_uris':['https://chat.openai.com/aip/callback'],
            'grant_types':['authorization_code', 'refresh_token'],
            'response_types':['code'], 'token_endpoint_auth_method':'none',
        })
        assert registered.status_code == 201, registered.text
        registration = registered.json()

        code, verifier, params = _authorize(client, registration)
        token = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':params['redirect_uri'], 'code_verifier':verifier,
            'resource':params['resource'],
        })
        assert token.status_code == 200, token.text
        grant = token.json()
        assert grant['token_type'] == 'Bearer' and grant['expires_in'] == 3600
        assert grant['access_token'].startswith('sat_') and grant['refresh_token'].startswith('srt_')

        initialize = {'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{
            'protocolVersion':'2025-03-26', 'capabilities':{},
            'clientInfo':{'name':'oauth-test', 'version':'1'},
        }}
        oauth_headers = {'Authorization':'Bearer '+grant['access_token'],
                         'Accept':'application/json, text/event-stream'}
        assert client.post('/serein/mcp', headers=oauth_headers, json=initialize).status_code == 200
        # Access tokens are audience-bound to the exact MCP resource.
        denied_alias = client.post('/mcp', headers=oauth_headers, json=initialize)
        assert denied_alias.status_code == 401
        assert 'resource_metadata="https://memory.example/.well-known/oauth-protected-resource/mcp"' in denied_alias.headers['www-authenticate']
        # The original static Gateway Key remains compatible.
        static = client.post('/mcp', headers={'Authorization':'Bearer synthetic-gateway-key',
                                              'Accept':'application/json, text/event-stream'}, json=initialize)
        assert static.status_code == 200

        refreshed = client.post('/token', data={
            'grant_type':'refresh_token', 'client_id':registration['client_id'],
            'refresh_token':grant['refresh_token'],
        })
        assert refreshed.status_code == 200
        rotated = refreshed.json()
        assert rotated['refresh_token'] != grant['refresh_token']
        assert client.post('/serein/mcp', headers=oauth_headers, json=initialize).status_code == 401
        rotated_headers = {'Authorization':'Bearer '+rotated['access_token'],
                           'Accept':'application/json, text/event-stream'}
        assert client.post('/serein/mcp', headers=rotated_headers, json=initialize).status_code == 200
        # Replaying a rotated refresh token revokes the entire token family.
        replay = client.post('/token', data={
            'grant_type':'refresh_token', 'client_id':registration['client_id'],
            'refresh_token':grant['refresh_token'], 'resource':params['resource'],
        })
        assert replay.status_code == 400 and replay.json()['error'] == 'invalid_grant'
        assert client.post('/serein/mcp', headers=rotated_headers, json=initialize).status_code == 401

        code2, verifier2, params2 = _authorize(client, registration, state='key-rotation')
        surviving = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code2, 'redirect_uri':params2['redirect_uri'], 'code_verifier':verifier2,
        }).json()

        # Rotation takes effect in the running service before an old grant can be refreshed.
        token_file.write_text('replacement-gateway-key', encoding='utf-8')
        refresh = client.post('/token', data={'grant_type':'refresh_token', 'client_id':registration['client_id'],
                                             'refresh_token':surviving['refresh_token']})
        assert refresh.status_code == 400 and refresh.json()['error'] == 'invalid_grant'
        headers = {'Accept':'application/json, text/event-stream'}
        for key in (surviving['access_token'], 'synthetic-gateway-key'):
            assert client.post('/serein/mcp', headers={**headers,'Authorization':'Bearer '+key}, json=initialize).status_code == 401
        headers['Authorization'] = 'Bearer replacement-gateway-key'
        assert client.post('/serein/mcp', headers=headers, json=initialize).status_code == 200


def test_oauth_rejects_open_redirect_bad_key_and_replayed_code(tmp_path):
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='https://memory.example') as client:
        bad = client.post('/register', json={'redirect_uris':['http://foreign.example/callback']})
        assert bad.status_code == 400 and bad.json()['error'] == 'invalid_client_metadata'
        registration = client.post('/register', json={
            'client_name':'ChatGPT', 'redirect_uris':['https://chat.openai.com/aip/callback'],
        }).json()
        verifier = 'v' * 43
        params = {'response_type':'code', 'client_id':registration['client_id'],
            'redirect_uri':'https://evil.example/callback', 'state':'state',
            'code_challenge':_challenge(verifier), 'code_challenge_method':'S256',
            'resource':'https://memory.example/serein/mcp'}
        mismatch = client.get('/authorize', params=params)
        assert mismatch.status_code == 400 and 'redirect_uri' in mismatch.text
        bad_consent = client.post('/authorize', data={**params, 'gateway_key':'synthetic-gateway-key'},
                                  follow_redirects=False)
        assert bad_consent.status_code == 400 and 'location' not in bad_consent.headers

        params['redirect_uri'] = registration['redirect_uris'][0]
        wrong_key = client.post('/authorize', data={**params, 'gateway_key':'wrong'},
                                follow_redirects=False)
        assert wrong_key.status_code == 401 and 'synthetic-gateway-key' not in wrong_key.text

        code, correct_verifier, valid = _authorize(client, registration)
        wrong_pkce = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':valid['redirect_uri'], 'code_verifier':'z'*43,
            'resource':valid['resource'],
        })
        assert wrong_pkce.status_code == 400 and wrong_pkce.json()['error'] == 'invalid_grant'
        replay = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':valid['redirect_uri'], 'code_verifier':correct_verifier,
            'resource':valid['resource'],
        })
        assert replay.status_code == 400 and replay.json()['error'] == 'invalid_grant'


@pytest.mark.parametrize(('callback', 'callback_origin'), [
    ('https://chat.openai.com/aip/callback', 'https://chat.openai.com'),
    ('https://client.example:8443/oauth/callback?existing=1', 'https://client.example:8443'),
    ('http://127.0.0.1:9876/callback', 'http://127.0.0.1:9876'),
    ('http://[::1]:9876/callback', None),
])
def test_oauth_consent_csp_allows_only_selected_callback_origin(tmp_path, callback, callback_origin):
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='https://memory.example') as client:
        registration = client.post('/register', json={
            'redirect_uris': [callback, 'https://other.example/callback'],
        })
        assert registration.status_code == 201
        params = {'response_type':'code', 'client_id':registration.json()['client_id'],
            'redirect_uri':callback, 'state':'exact-state',
            'code_challenge':_challenge('v'*43), 'code_challenge_method':'S256',
            'resource':'https://memory.example/serein/mcp'}
        page = client.get('/authorize', params=params)
        assert page.status_code == 200
        directives = {part.strip().split()[0]: part.strip().split()[1:]
                      for part in page.headers['content-security-policy'].split(';')}
        form_sources = ["'self'"] + ([callback_origin] if callback_origin else [])
        assert directives == {'default-src':["'none'"], 'form-action':form_sources,
                              'base-uri':["'none'"], 'frame-ancestors':["'none'"]}
        assert '<form method="post" action="/authorize">' in page.text
        assert page.headers['referrer-policy'] == 'no-referrer'
        assert page.headers['cache-control'] == 'no-store'
        consent = client.post('/authorize', data={**params, 'gateway_key':'synthetic-gateway-key'},
                              follow_redirects=False)
        if callback_origin is None:
            # CSP host-source syntax cannot represent IPv6 literals; the
            # success document navigates independently of the submitted form.
            from html.parser import HTMLParser

            class CompletionPage(HTMLParser):
                refresh = None
                link = None

                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == 'meta' and attrs.get('http-equiv') == 'refresh':
                        self.refresh = attrs['content']
                    if tag == 'a':
                        self.link = attrs['href']

            assert consent.status_code == 200
            completion = CompletionPage()
            completion.feed(consent.text)
            assert completion.refresh == '0;url='+completion.link
            assert consent.headers['referrer-policy'] == 'no-referrer'
            assert "form-action 'none'" in consent.headers['content-security-policy']
            assert 'synthetic-gateway-key' not in consent.text
            destination = completion.link
        else:
            assert consent.status_code == 303
            destination = consent.headers['location']
        location = urlsplit(destination)
        assert (location.scheme, location.netloc, location.path) == (
            urlsplit(callback).scheme, urlsplit(callback).netloc, urlsplit(callback).path)
        query = parse_qs(location.query)
        assert query['state'] == ['exact-state'] and query['code'][0]
        assert all(query[name] == values for name, values in parse_qs(urlsplit(callback).query).items())
        assert 'gateway_key' not in query and 'synthetic-gateway-key' not in destination
        assert 'other.example' not in page.headers['content-security-policy']


@pytest.mark.parametrize('callback', [
    'https://*.example/callback',
    'https://client.example;form-action/callback',
    'https://client.example https:/callback',
    'https://client.example,https:/callback',
    'https://client.example:99999/callback',
    'https://client.example\\bad.example/callback',
    'https://client.example%20/callback',
])
def test_oauth_rejects_unsafe_callback_csp_source(tmp_path, callback):
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='https://memory.example') as client:
        result = client.post('/register', json={'redirect_uris':[callback]})
        assert result.status_code == 400
        assert result.json()['error'] == 'invalid_client_metadata'


def test_oauth_registration_cap_evicts_unused_clients_and_public_http_is_refused(tmp_path, monkeypatch):
    import serein.api.oauth as oauth
    monkeypatch.setattr(oauth, 'CLIENT_LIMIT', 2)
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    app = create_app(settings, token='synthetic-gateway-key')
    with TestClient(app, base_url='https://memory.example') as client:
        registrations = [client.post('/register', json={
            'client_name':f'client-{index}',
            'redirect_uris':[f'https://client.example/callback/{index}'],
        }) for index in range(3)]
        assert [response.status_code for response in registrations] == [201, 201, 201]
        first = registrations[0].json()
        params = {'response_type':'code', 'client_id':first['client_id'],
            'redirect_uri':first['redirect_uris'][0], 'state':'state',
            'code_challenge':_challenge('v'*43), 'code_challenge_method':'S256',
            'resource':'https://memory.example/serein/mcp'}
        assert client.get('/authorize', params=params).status_code == 400
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='http://public.example') as client:
        response = client.post('/register', json={
            'redirect_uris':['https://client.example/callback'],
        })
        assert response.status_code == 400
        assert 'requires HTTPS' in response.text


@pytest.mark.parametrize(('callback_host', 'scheme'), [('127.0.0.1', 'http'), ('::1', 'http'), ('localhost', 'https')])
def test_oauth_cross_origin_redirect_in_chromium(tmp_path, callback_host, scheme):
    """Opt-in real-browser check; both endpoints are synthetic loopback servers."""
    import os
    import shutil
    import socket
    import subprocess
    import threading
    import time
    from urllib.parse import urlencode

    executable = os.environ.get('SEREIN_TEST_CHROMIUM')
    if not executable:
        pytest.skip('Set SEREIN_TEST_CHROMIUM to a Chromium executable and install playwright')
    playwright = pytest.importorskip('playwright.sync_api')
    import httpx
    import uvicorn

    if callback_host == '::1' and not socket.has_ipv6:
        pytest.skip('IPv6 unavailable')
    tls = {}
    if scheme == 'https':
        openssl = shutil.which('openssl')
        if not openssl:
            pytest.skip('openssl required to generate a synthetic HTTPS certificate')
        cert, key = tmp_path/'certificate.pem', tmp_path/'key.pem'
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                       check=True, capture_output=True)
        tls = {'ssl_certfile':str(cert), 'ssl_keyfile':str(key)}

    settings = Settings(tmp_path/'browser-memory.db')
    with Store(settings.database):
        pass
    app = create_app(settings, token='synthetic-gateway-key')

    @app.get('/test-callback')
    def callback(code: str, state: str):
        return {'code':code, 'state':state}

    with socket.socket() as server_socket, socket.socket(socket.AF_INET6 if callback_host == '::1' else socket.AF_INET) as callback_socket:
        server_socket.bind(('127.0.0.1', 0))
        try:
            callback_socket.bind(('127.0.0.1' if callback_host == 'localhost' else callback_host, 0))
        except OSError as exc:
            if callback_host == '::1':
                pytest.skip(f'IPv6 loopback unavailable: {exc}')
            raise
        origin = f'{scheme}://127.0.0.1:{server_socket.getsockname()[1]}'
        host = '[::1]' if callback_host == '::1' else callback_host
        redirect_uri = f'{scheme}://{host}:{callback_socket.getsockname()[1]}/test-callback'
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='error', **tls))
        thread = threading.Thread(target=server.run, kwargs={'sockets':[server_socket, callback_socket]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started
            verifier = 'v'*43
            registration = httpx.post(origin+'/register', trust_env=False, verify=False, json={'redirect_uris':[redirect_uri]})
            assert registration.status_code == 201
            params = {'response_type':'code', 'client_id':registration.json()['client_id'],
                      'redirect_uri':redirect_uri, 'state':'browser-state',
                      'code_challenge':_challenge(verifier), 'code_challenge_method':'S256',
                      'resource':origin+'/serein/mcp'}
            with playwright.sync_playwright() as p:
                browser = p.chromium.launch(executable_path=executable, args=['--disable-gpu', '--no-proxy-server'])
                try:
                    # Ignore only the synthetic loopback server's test certificate; CSP stays enabled.
                    page = browser.new_page(ignore_https_errors=scheme == 'https')
                    errors = []
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    page.goto(origin+'/authorize?'+urlencode(params))
                    page.locator('input[name=gateway_key]').fill('synthetic-gateway-key')
                    try:
                        with page.expect_request(lambda request: request.url.startswith(redirect_uri), timeout=5000) as navigation:
                            page.get_by_role('button', name='确认授权').click()
                    except playwright.TimeoutError:
                        pytest.fail(f'Callback navigation blocked; browser console: {errors}')
                    request = navigation.value
                    assert request.method == 'GET' and request.post_data is None
                    page.wait_for_url(redirect_uri+'?*', timeout=5000)
                    query = parse_qs(urlsplit(page.url).query)
                    assert query['state'] == ['browser-state'] and query['code'][0]
                    assert 'synthetic-gateway-key' not in request.url and 'gateway_key' not in query
                    assert not any('form-action' in error for error in errors)
                    token = httpx.post(origin+'/token', trust_env=False, verify=False, data={
                        'grant_type':'authorization_code', 'client_id':params['client_id'],
                        'code':query['code'][0], 'redirect_uri':redirect_uri, 'code_verifier':verifier,
                        'resource':params['resource'],
                    })
                    assert token.status_code == 200
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
