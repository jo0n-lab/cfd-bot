"""Minimal Telegram Bot API client with token-safe errors and bounded uploads."""
from . import diagnostics as _diagnostics
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .artifacts import MAX_DOCUMENT, MAX_PHOTO
from .ui import load_ui


class TelegramError(RuntimeError):
    @_diagnostics.trace
    def __init__(self, message, retry_after=0, code=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.code = code


@_diagnostics.trace
def chunks(text, units=3500):
    """Stay below Telegram's UTF-16 text length even for emoji-heavy messages."""
    output, used = [], 0
    for char in text:
        n = 2 if ord(char) > 0xFFFF else 1
        if used + n > units:
            if _diagnostics.enabled: _diagnostics.step('telegram.chunks:L25:then')
            yield ''.join(output)
            output, used = [], 0
        output.append(char)
        used += n
    if output:
        if _diagnostics.enabled: _diagnostics.step('telegram.chunks:L30:then')
        yield ''.join(output)


class Telegram:
    @_diagnostics.trace
    def __init__(self, token):
        self._base = 'https://api.telegram.org/bot' + token + '/'
        self._token = token
        _diagnostics.remember_secret(token)

    @_diagnostics.trace
    def call(self, method, payload, attachment=None, timeout=30):
        headers = {}
        if attachment:
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L41:then')
            field, path = attachment
            path = Path(path)
            limit = MAX_PHOTO if field == 'photo' else MAX_DOCUMENT
            with path.open('rb') as f:
                data = f.read(limit + 1)
            if len(data) > limit:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L47:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.transport.file_too_large',
                                                filename=path.name))
            boundary = uuid.uuid4().hex
            body = bytearray()
            for key, value in payload.items():
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L52:loop', key=key, value=value)
                if isinstance(value, (dict, list, bool)):
                    if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L53:then')
                    value = json.dumps(value)
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            filename = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
            mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            body.extend(data)
            body.extend(f'\r\n--{boundary}--\r\n'.encode())
            headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
            encoded = bytes(body)
        else:
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L41:else')
            encoded = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self._base + method, data=encoded, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L70:except')
            try:
                result = json.loads(exc.read())
            except ValueError:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L73:except')
                raise TelegramError(f'Telegram HTTP {exc.code}') from None
        except (OSError, ValueError) as exc:
            # urllib exception strings can contain the token-bearing URL.
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L75:except')
            raise TelegramError(load_ui().text('scenarios.diagnostics.transport.connection_failed',
                                               error=type(exc).__name__)) from None
        if not result.get('ok'):
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.call:L79:then')
            description = str(result.get('description', 'unknown error')).replace(self._token, '[redacted]')
            raise TelegramError(description, result.get('parameters', {}).get('retry_after', 0), result.get('error_code'))
        return result.get('result')

    @_diagnostics.trace
    def send(self, chat, text, keyboard=None):
        parts = list(chunks(text))
        sent = []
        for i, part in enumerate(parts):
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.send:L87:loop', i=i, part=part)
            payload = dict(chat_id=chat, text=part)
            if keyboard and i == len(parts) - 1:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.send:L89:then')
                payload['reply_markup'] = keyboard
            sent.append(self.call('sendMessage', payload))
        return sent

    @_diagnostics.trace
    def file(self, chat, item):
        path = Path(item['path'])
        kind = item.get('kind', 'document')
        if kind == 'photo' and path.stat().st_size > MAX_PHOTO:
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.file:L97:then')
            kind = 'document'
        method = 'sendPhoto' if kind == 'photo' else 'sendDocument'
        payload = dict(chat_id=chat, caption=item.get('caption', '')[:450])
        try:
            return self.call(method, payload, attachment=(kind, path), timeout=90)
        except TelegramError as exc:
            # Oversized dimensions / invalid photo encoding can still be sent as a file.
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.file:L103:except')
            if kind == 'photo' and exc.code == 400:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.file:L105:then')
                return self.call('sendDocument', payload, attachment=('document', path), timeout=90)
            raise

    @_diagnostics.trace
    def updates(self, offset):
        return self.call('getUpdates', dict(offset=offset, timeout=10,
                                           allowed_updates=['message', 'callback_query']), timeout=20)

    @_diagnostics.trace
    def delete_messages(self, chat, message_ids):
        """Delete tracked messages with deleteMessages, never one request per ID."""
        message_ids = sorted(set(message_ids))
        deleted = []
        for start in range(0, len(message_ids), 100):
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram.delete_messages:L117:loop', start=start)
            batch = message_ids[start:start + 100]
            deleted.extend(self._delete_batch(chat, batch))
        return deleted

    @_diagnostics.trace
    def _delete_batch(self, chat, batch):
        if not batch:
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram._delete_batch:L123:then')
            return []
        try:
            self.call('deleteMessages', dict(chat_id=chat, message_ids=batch))
            return list(batch)
        except TelegramError as exc:
            # A stale/undeletable ID can make Telegram reject the whole batch.
            # Isolate it with smaller deleteMessages calls, without reverting to
            # one deleteMessage API request for every conversation message.
            if _diagnostics.enabled: _diagnostics.step('telegram.Telegram._delete_batch:L128:except')
            if exc.code != 400:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram._delete_batch:L132:then')
                raise
            if len(batch) == 1:
                if _diagnostics.enabled: _diagnostics.step('telegram.Telegram._delete_batch:L134:then')
                return []
            middle = len(batch) // 2
            return (self._delete_batch(chat, batch[:middle])
                    + self._delete_batch(chat, batch[middle:]))
