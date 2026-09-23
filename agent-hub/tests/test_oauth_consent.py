"""Consent adapter tests use local HTTP and the real transactional memory core."""
from contextlib import contextmanager
from html.parser import HTMLParser
import http.client
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from agent_hub.dashboard import CONSENT_COOKIE, MAX_CONSENT_INPUT, Monitor, make_server
from agent_hub.oauth_service import OAuthClient, OAuthConfig, OAuthService, PinnedIdentity
from agent_hub.oauth_store import MemoryOAuthStore


ISSUER = 'https://cloud.google.com/iap'
ORIGIN = 'https://dashboard.example'
CALLBACK = 'https://chatgpt.com/connector_platform/oauth_redirect'


class VerifiedFixture:
    def identity(self, assertion):
        if assertion not in ('assertion-one', 'assertion-two', 'assertion-third'):
            return None
        return {'iss': ISSUER, 'sub': assertion.removeprefix('assertion-'),
                'email': 'same@example.com', 'iat': 1, 'exp': 2}

    def __call__(self, assertion):
        return self.identity(assertion) is not None


class Inputs(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.values = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == 'input':
            attrs = dict(attrs)
            self.values[attrs['name']] = attrs['value']


@contextmanager
def consent_server(service, authorize=None, origin=ORIGIN):
    with patch.dict(os.environ, {'HUB_OAUTH_AUTHORIZATION_ORIGIN': origin,
                                 'HUB_OWNER_EMAILS_JSON': '["unrelated@example.com"]'}):
        server = make_server('127.0.0.1', 0, Monitor(lambda: {
            'schema_version': 1, 'hub': {'deployment': 'cloud', 'status': 'ok'}}),
            authorize=authorize or VerifiedFixture(), oauth_service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class OAuthConsentTests(unittest.TestCase):
    def setUp(self):
        self.config = OAuthConfig('https://gateway.example', 'https://gateway.example/mcp',
                                  (OAuthClient('chatgpt-fixture', (CALLBACK,)),),
                                  (PinnedIdentity(ISSUER, 'one'), PinnedIdentity(ISSUER, 'two')))
        self.service = OAuthService(self.config, MemoryOAuthStore())
        self.params = {'response_type': 'code', 'client_id': 'chatgpt-fixture', 'redirect_uri': CALLBACK,
                       'scope': 'hub:manage', 'state': 'state-fixture', 'code_challenge': 'a' * 43,
                       'code_challenge_method': 'S256', 'resource': self.config.resource}

    def call(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def begin(self, port, assertion='assertion-one', params=None):
        response = self.call(port, 'GET', '/oauth/authorize?' + urlencode(params or self.params),
                             headers={'X-Goog-IAP-JWT-Assertion': assertion})
        self.assertEqual(response[0], 200, response[2])
        return response, Inputs(response[2]).values

    def approve(self, port, form, cookie, assertion='assertion-one', origin=ORIGIN, **changes):
        headers = {'X-Goog-IAP-JWT-Assertion': assertion,
                   'Content-Type': 'application/x-www-form-urlencoded', 'Origin': origin}
        if cookie is not None:
            headers['Cookie'] = cookie
        headers.update(changes)
        return self.call(port, 'POST', '/oauth/approve', urlencode(form), headers)

    def test_success_requires_browser_consent_and_redirects_to_exact_core_callback(self):
        with consent_server(self.service) as port:
            response, form = self.begin(port)
            cookie = response[1]['Set-Cookie']
            self.assertTrue(cookie.startswith(CONSENT_COOKIE + '=' + form['csrf'] + ';'))
            for flag in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Lax'):
                self.assertIn(flag, cookie)
            self.assertNotIn('Domain=', cookie)
            self.assertIn("form-action 'self' " + CALLBACK, response[1]['Content-Security-Policy'])
            self.assertIn("default-src 'none'", response[1]['Content-Security-Policy'])
            self.assertEqual(response[1]['Cache-Control'], 'no-store')
            self.assertEqual(response[1]['Referrer-Policy'], 'same-origin')
            self.assertNotIn('assertion-one', response[2])
            result = self.approve(port, form, cookie.split(';', 1)[0])
            self.assertEqual(result[0], 303, result[2])
            location = urlsplit(result[1]['Location'])
            self.assertEqual(location.scheme + '://' + location.netloc + location.path, CALLBACK)
            query = parse_qs(location.query)
            self.assertEqual(query['state'], ['state-fixture'])
            self.assertEqual(query['iss'], ['https://gateway.example'])
            self.assertEqual(len(query['code']), 1)
            self.assertIn('Max-Age=0', result[1]['Set-Cookie'])
            self.assertEqual(result[1]['Referrer-Policy'], 'no-referrer')
            self.assertEqual(result[2], '')
            self.assertEqual(self.approve(port, form, cookie.split(';', 1)[0])[0], 403)

    def test_missing_mismatched_and_duplicate_cookie_never_call_core_approve(self):
        with consent_server(self.service) as port:
            _, form = self.begin(port)
            good = CONSENT_COOKIE + '=' + form['csrf']
            cookies = [None, CONSENT_COOKIE + '=' + 'b' * 43, good + '; ' + good]
            with patch.object(self.service, 'approve', wraps=self.service.approve) as approve:
                for cookie in cookies:
                    self.assertEqual(self.approve(port, form, cookie)[0], 403)
                approve.assert_not_called()

    def test_chatgpt_locale_hint_reaches_consent_and_exact_callback_without_reflection(self):
        with consent_server(self.service) as port:
            response, form = self.begin(port, params={**self.params, 'ui_locales': 'en-US'})
            self.assertNotIn('ui_locales', response[2])
            self.assertNotIn('en-US', response[2])
            result = self.approve(port, form, response[1]['Set-Cookie'].split(';', 1)[0])
            self.assertEqual(result[0], 303, result[2])
            location = urlsplit(result[1]['Location'])
            self.assertEqual(location.scheme + '://' + location.netloc + location.path, CALLBACK)
            self.assertEqual(set(parse_qs(location.query)), {'code', 'state', 'iss'})
            self.assertNotIn('ui_locales', repr(self.service.store.snapshot()))

    def test_wrong_verified_subject_denied_even_with_same_email(self):
        with consent_server(self.service) as port:
            response, form = self.begin(port)
            cookie = response[1]['Set-Cookie'].split(';', 1)[0]
            self.assertEqual(self.approve(port, form, cookie, assertion='assertion-two')[0], 403)
            self.assertEqual(self.approve(port, form, cookie, assertion='assertion-third')[0], 403)
            self.assertEqual(self.approve(port, form, cookie)[0], 303)

    def test_post_origin_must_exactly_match_configured_authorization_origin(self):
        with consent_server(self.service) as port:
            response, form = self.begin(port)
            cookie = response[1]['Set-Cookie'].split(';', 1)[0]
            with patch.object(self.service, 'approve', wraps=self.service.approve) as approve:
                for origin in ('', 'null', 'https://attacker.example', ORIGIN + '/', 'http://dashboard.example'):
                    self.assertEqual(self.approve(port, form, cookie, origin=origin)[0], 403)
                approve.assert_not_called()

    def test_strict_bounded_forms_reject_duplicates_extra_fields_encoding_and_oversize(self):
        with consent_server(self.service) as port:
            response, form = self.begin(port)
            headers = {'X-Goog-IAP-JWT-Assertion': 'assertion-one', 'Origin': ORIGIN,
                       'Content-Type': 'application/x-www-form-urlencoded',
                       'Cookie': response[1]['Set-Cookie'].split(';', 1)[0]}
            bodies = [(urlencode(form) + '&id=' + form['id'], 400),
                      (urlencode(form) + '&unexpected=1', 400), ('id=%GG&csrf=x', 400),
                      ('id=%FF&csrf=x', 400), ('x' * (MAX_CONSENT_INPUT + 1), 413)]
            with patch.object(self.service, 'approve', wraps=self.service.approve) as approve:
                for body, status in bodies:
                    self.assertEqual(self.call(port, 'POST', '/oauth/approve', body, headers)[0], status)
                headers['Content-Type'] = 'application/json'
                self.assertEqual(self.call(port, 'POST', '/oauth/approve', '{}', headers)[0], 415)
                approve.assert_not_called()

    def test_duplicate_and_oversize_authorization_query_do_not_create_consent(self):
        with consent_server(self.service) as port, patch.object(
                self.service, 'begin_authorization', wraps=self.service.begin_authorization) as begin:
            for query in (urlencode(self.params) + '&client_id=duplicate', 'state=' + 'x' * MAX_CONSENT_INPUT,
                          urlencode({**self.params, 'ui_locales': 'en-US'}) + '&ui_locales=fr'):
                result = self.call(port, 'GET', '/oauth/authorize?' + query,
                                   headers={'X-Goog-IAP-JWT-Assertion': 'assertion-one'})
                self.assertEqual(result[0], 400)
            begin.assert_not_called()

    def test_unverified_and_unpinned_identities_cannot_begin(self):
        with consent_server(self.service) as port:
            path = '/oauth/authorize?' + urlencode(self.params)
            for assertion, status in (('forged', 401), ('assertion-third', 403)):
                self.assertEqual(self.call(port, 'GET', path,
                                           headers={'X-Goog-IAP-JWT-Assertion': assertion})[0], status)
        with consent_server(self.service, authorize=lambda token: True) as port:
            self.assertEqual(self.call(port, 'GET', path,
                                       headers={'X-Goog-IAP-JWT-Assertion': 'assertion-one'})[0], 401)

    def test_consent_page_escapes_registered_client_and_scope(self):
        client_id = '<script>alert(1)</script>'
        self.config = OAuthConfig('https://gateway.example', 'https://gateway.example/mcp',
                                  (OAuthClient(client_id, (CALLBACK,)),), self.config.identities)
        self.service = OAuthService(self.config, MemoryOAuthStore())
        self.params['client_id'] = client_id
        original = self.service.begin_authorization
        def altered(*args):
            consent = original(*args)
            consent.update(scope='<img/src=x/onerror=alert(1)>')
            return consent
        with patch.object(self.service, 'begin_authorization', side_effect=altered), \
                consent_server(self.service) as port:
            response, _ = self.begin(port)
            self.assertNotIn('<script>', response[2])
            self.assertNotIn('<img/', response[2])
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', response[2])
            self.assertIn('&lt;img/src=x/onerror=alert(1)&gt;', response[2])
            self.assertNotIn(client_id, response[1]['Content-Security-Policy'])

    def test_callback_csp_uses_only_validated_registration_and_cannot_inject_sources(self):
        original = self.service.begin_authorization
        for callback in ('https://attacker.example/callback', CALLBACK + '; script-src *',
                         CALLBACK + '\r\nX-Injected: yes', 'https://*.chatgpt.com/callback',
                         'https://chatgpt.com:*/callback', "https://chatgpt.com/cb' 'unsafe-inline'"):
            def altered(*args):
                consent = original(*args)
                consent['redirect_uri'] = callback
                return consent
            with self.subTest(callback=callback), patch.object(
                    self.service, 'begin_authorization', side_effect=altered), consent_server(self.service) as port:
                response = self.call(port, 'GET', '/oauth/authorize?' + urlencode(self.params),
                                     headers={'X-Goog-IAP-JWT-Assertion': 'assertion-one'})
                self.assertEqual(response[0], 503)
                self.assertEqual(response[1]['Referrer-Policy'], 'no-referrer')
                self.assertNotIn(callback, response[1]['Content-Security-Policy'])
                self.assertNotIn('Set-Cookie', response[1])

    def test_callback_registration_does_not_make_unsafe_csp_syntax_acceptable(self):
        from agent_hub.dashboard import _consent_page_csp
        for callback in ('https://chatgpt.com/cb;script-src*', 'https://*.chatgpt.com/cb',
                         'https://chatgpt.com:*/cb', "https://chatgpt.com/cb'", 'https://chatgpt.com/cb\\x'):
            with self.subTest(callback=callback):
                # This fixture isolates header serialization from core URL
                # validation; even a configured malformed source must fail.
                from types import SimpleNamespace
                service = SimpleNamespace(config=SimpleNamespace(clients=(
                    SimpleNamespace(client_id='known', redirect_uris=(callback,)),)))
                with self.assertRaises(ValueError):
                    _consent_page_csp({'client_id': 'known', 'redirect_uri': callback}, service)

    def test_only_successful_consent_page_relaxes_referrer_policy(self):
        with consent_server(self.service) as port:
            headers = {'X-Goog-IAP-JWT-Assertion': 'assertion-one'}
            for path in ('/api/status', '/identity', '/oauth/authorize?bad=1'):
                response = self.call(port, 'GET', path, headers=headers)
                self.assertEqual(response[1]['Referrer-Policy'], 'no-referrer')
                self.assertNotIn(CALLBACK, response[1]['Content-Security-Policy'])

    def test_absent_service_preserves_status_and_has_no_consent_routes(self):
        with consent_server(None, origin='') as port:
            headers = {'X-Goog-IAP-JWT-Assertion': 'assertion-one'}
            self.assertEqual(self.call(port, 'GET', '/oauth/authorize', headers=headers)[0], 404)
            self.assertEqual(self.call(port, 'POST', '/oauth/approve', '', headers)[0], 405)
            self.assertEqual(self.call(port, 'GET', '/api/status', headers=headers)[0], 200)

    def test_head_and_unrelated_mutations_do_not_create_or_approve_consent(self):
        with consent_server(self.service) as port, patch.object(self.service, 'begin_authorization') as begin, \
                patch.object(self.service, 'approve') as approve:
            for method, path in (('HEAD', '/oauth/authorize?' + urlencode(self.params)),
                                 ('POST', '/api/status'), ('PUT', '/oauth/approve')):
                self.assertEqual(self.call(port, method, path)[0], 405)
            begin.assert_not_called()
            approve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
