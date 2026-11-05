"""Groq local-tool client. Credentials never enter messages or logs."""
import json
import urllib.error
import urllib.request
from .core import GBMError

ENDPOINT = 'https://api.groq.com/openai/v1/chat/completions'
DEFAULT_MODEL = 'llama-3.3-70b-versatile'


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
                                                 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(1024*1024+1)
        except urllib.error.HTTPError as error:
            error.close()
            hints = {401: 'Groq rejected the API key.', 403: 'Groq denied access.',
                     429: 'Groq rate limit reached. Wait before trying again; free-tier limits vary.',
                     400: 'Groq rejected the request/model/tool format. Check GROQ_MODEL and task size.'}
            raise GBMError(hints.get(error.code, f'Groq HTTP error {error.code}. No automatic retry was made.'))
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
