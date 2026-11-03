import http.client
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch

from cfd_bot.telegram import Telegram, TelegramError
from cfd_bot.telegram_transport import _Connection, _tls_context


class TransportPhaseTests(unittest.TestCase):
    def test_connect_upload_response_have_separate_timeouts_and_safe_metrics(self):
        connection = _Connection('api.telegram.org', api_method='sendPhoto', timeout=90)
        sock = Mock()

        def connect(current):
            self.assertEqual(current.timeout, 10)
            current.sock = sock

        with patch.object(http.client.HTTPSConnection, 'connect', connect), \
                patch.object(http.client.HTTPSConnection, 'send') as send, \
                patch.object(http.client.HTTPSConnection, 'getresponse', return_value='response'), \
                patch('cfd_bot.telegram_transport.diagnostics.event') as event:
            connection.send(b'private file contents')
            self.assertEqual(connection.getresponse(), 'response')
        send.assert_called_once_with(b'private file contents')
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [90, 20])
        phases = [c.kwargs for c in event.call_args_list]
        self.assertEqual([p['stage'] for p in phases], ['connect', 'request_send', 'response_headers'])
        self.assertNotIn('private file contents', json.dumps(phases))
        self.assertTrue(all(p['duration_ms'] >= 0 for p in phases))
        self.assertEqual(_tls_context().verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(_tls_context().check_hostname)

    def test_real_urllib_handler_records_request_and_body_without_token(self):
        body = b'{"ok":true,"result":{"id":7}}'
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
            + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)

        def connect(current):
            current.sock = sock

        with patch.object(http.client.HTTPSConnection, 'connect', connect), \
                patch('cfd_bot.telegram_transport.diagnostics.event') as event, \
                patch('urllib.request.getproxies', return_value={}):
            self.assertEqual(Telegram('test-private-token').call('getMe', {}), {'id': 7})
        phases = [c.kwargs for c in event.call_args_list]
        stages = {p['stage'] for p in phases}
        self.assertTrue({'prepare', 'connect', 'request_send', 'response_headers', 'response_body'} <= stages)
        self.assertNotIn('test-private-token', json.dumps(phases))
        self.assertNotIn('https://', json.dumps(phases))

    def test_failed_connection_records_stage_without_raw_exception(self):
        connection = _Connection('api.telegram.org', api_method='sendPhoto', timeout=90)
        with patch.object(http.client.HTTPSConnection, 'connect', side_effect=TimeoutError('secret URL')), \
                patch('cfd_bot.telegram_transport.diagnostics.event') as event:
            with self.assertRaises(TimeoutError):
                connection.connect()
        self.assertEqual(connection.timeout, 90)
        self.assertEqual(event.call_args.kwargs['stage'], 'connect')
        self.assertEqual(event.call_args.kwargs['error_type'], 'TimeoutError')
        self.assertNotIn('secret URL', str(event.call_args))

    def test_ambiguous_transport_timeout_does_not_retry_or_expose_url(self):
        with patch('cfd_bot.telegram.open_request', side_effect=TimeoutError('private-token')) as request:
            with self.assertRaises(TelegramError) as raised:
                Telegram('private-token').call('sendMessage', {'text': 'private'})
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('private-token', str(raised.exception))
