"""Groq local-tool client. Credentials never enter messages or logs."""
import json
import re
import urllib.error
import urllib.request
from .core import GBMError

ENDPOINT = 'https://api.groq.com/openai/v1/chat/completions'
DEFAULT_MODEL = 'llama-3.3-70b-versatile'


def error_detail(raw, secret):
    """Expose bounded API diagnostics, never an arbitrary HTML response body."""
    try:
        data = json.loads(raw)
        error = data.get('error', {})
        if not isinstance(error, dict):
            return ''
        parts = [error.get(field) for field in ('message', 'type', 'code')]
        detail = ' | '.join(value for value in parts if isinstance(value, str))
    except (ValueError, AttributeError, UnicodeError):
        # Some edge gateways return plain-text/HTML error codes instead of JSON.
        text = raw.decode('utf-8', errors='replace')
        match = re.search(r'error\s+code\s*:\s*(\d{3,5})', text, re.IGNORECASE)
        return 'Gateway error code ' + match.group(1) if match else ''
    if secret:
        detail = detail.replace(secret, '[REDACTED]')
    detail = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', detail)
    detail = re.sub(r'\bgsk_[A-Za-z0-9_-]+', '[REDACTED]', detail)
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in detail).split())[:1000]


class GroqClient:
    def __init__(self, key, model=DEFAULT_MODEL):
        if not key or '\n' in key or '\r' in key:
            raise GBMError('Set GROQ_API_KEY locally before using gbm agent. Do not put it in project files.')
        self.key, self.model = key, model

    def complete(self, messages, tools):
        payload = {'model': self.model, 'messages': messages, 'tools': tools,
                   'tool_choice': 'auto', 'parallel_tool_calls': False,
                   'temperature': 0.2, 'max_tokens': 3000}
        if len(json.dumps(messages)) > 60000:
            raise GBMError('Agent context budget reached. Use a smaller, more specific task.')
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), method='POST',
                                        headers={'Authorization': 'Bearer ' + self.key,
                                                 'Content-Type': 'application/json',
                                                 'Accept': 'application/json',
                                                 'User-Agent': 'GBM/1.9.1 (+https://github.com/SumanyuDeshpande/GBM)'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(1024*1024+1)
        except urllib.error.HTTPError as error:
            try:
                detail = error_detail(error.read(16384), self.key)
            except (OSError, ValueError):
                detail = ''
            finally:
                error.close()
            hints = {401: 'Groq rejected the API key.', 403: 'Groq denied access.',
                     429: 'Groq rate limit reached. Wait before trying again; free-tier limits vary.',
                     400: 'Groq rejected the request/model/tool format. Check GROQ_MODEL and task size.'}
            hint = hints.get(error.code, 'Groq request failed. No automatic retry was made.')
            raise GBMError(f'Groq HTTP {error.code}: {hint}' + (f' Details: {detail}' if detail else '')) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GBMError('Could not reach Groq or the request timed out. No automatic retry was made.')
        if len(raw) > 1024*1024:
            raise GBMError('Groq response exceeded the agent response limit.')
        try:
            data = json.loads(raw)
            message = data['choices'][0]['message']
            if not isinstance(message, dict):
                raise ValueError()
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ValueError()
            return {'role': 'assistant', 'content': message.get('content') or '',
                    **({'tool_calls': calls} if calls else {})}
        except (ValueError, KeyError, IndexError, TypeError):
            raise GBMError('Groq returned an invalid agent response.')
