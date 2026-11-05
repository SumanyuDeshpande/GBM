"""Interactive user-wide Groq configuration, separate from project history."""
import getpass
import json
import os
from pathlib import Path
import tempfile
import warnings
from .core import GBMError
from .credentials import CredentialStore
from .groq_client import GroqClient, DEFAULT_MODEL


def settings_path():
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA', Path.home()/'AppData'/'Roaming'))
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))
    if not base.is_absolute(): raise GBMError('User configuration directory must be absolute.')
    return base/'GBM'/'settings.json'


def load():
    path = settings_path()
    if not path.exists(): return {}
    if path.stat().st_size>16384: raise GBMError('GBM user settings are too large. Run gbm setup after fixing settings.json.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('provider')!='groq' or not isinstance(data.get('model'),str):
            raise ValueError()
        return {'provider':'groq','model':data['model']}
    except (ValueError,UnicodeError): raise GBMError('Invalid GBM user settings. Fix or remove settings.json, then run gbm setup.') from None


def save(model):
    path = settings_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent,prefix='.settings-',suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump({'provider':'groq','model':model},stream,indent=2)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def resolve(model=None):
    # Existing environment-only users never need to open the native key store.
    env_key = os.environ.get('GROQ_API_KEY')
    env_model = os.environ.get('GROQ_MODEL')
    saved = load() if not (model or env_model) else {}
    key = env_key or CredentialStore().get()
    if not key: raise GBMError('Run gbm setup once, or set GROQ_API_KEY locally before using gbm agent.')
    return key, model or env_model or saved.get('model') or DEFAULT_MODEL


def hidden_key():
    # getpass normally falls back to visible input when there is no secure terminal.
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        try: return getpass.getpass('Groq API key (hidden; Enter keeps saved key): ').strip()
        except getpass.GetPassWarning:
            raise GBMError('Hidden input is unavailable. Run gbm setup in an interactive terminal.') from None


def verify(client):
    tool = {'type':'function','function':{'name':'gbm_setup_check',
            'description':'Confirm tool calling works. No code is executed.',
            'parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},
                          'required':['ok'],'additionalProperties':False}}}
    result = client.complete([{'role':'user','content':'Call gbm_setup_check with ok=true.'}],
                             [tool],tool_choice={'type':'function','function':{'name':'gbm_setup_check'}})
    calls = result.get('tool_calls') or []
    try:
        if len(calls)!=1 or calls[0]['function']['name']!='gbm_setup_check' or json.loads(calls[0]['function']['arguments'])!={'ok':True}:
            raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise GBMError('Model did not return the expected tool call. Nothing saved; choose another model.') from None


def run(ask=input, emit=print):
    store = CredentialStore()
    previous = store.get()
    emit('[GBM] Provider: Groq. Your key stays in the native credential store; settings apply to all projects.')
    key = hidden_key() or previous
    if not key: raise GBMError('Enter your own Groq API key. Nothing saved.')
    client = GroqClient(key)
    emit('[GBM] Checking connection and fetching models from Groq…')
    models = client.list_models()
    if not models: raise GBMError('Groq returned no active models. Nothing saved.')
    for index, model in enumerate(models,1): emit(f'  {index}. {model}')
    choice = ask('Choose model number or exact model ID: ').strip()
    if choice.isdigit():
        number = int(choice)
        if not 1<=number<=len(models): raise GBMError('Invalid model number. Nothing saved.')
        model = models[number-1]
    else: model = choice
    if model not in models: raise GBMError('Choose a model from the returned list. Nothing saved.')
    client.model = model
    emit('[GBM] Testing model access and tool calling (a small API request uses your account quota)…')
    verify(client)
    emit('[GBM] Model access and tool calling succeeded.')
    if ask('Save configuration? [Y/n]: ').strip().lower() not in ('','y','yes'):
        emit('[GBM] Cancelled. Existing configuration unchanged.'); return
    store.set(key)
    try: save(model)
    except BaseException:
        if previous is None: store.delete()
        else: store.set(previous)
        raise
    emit('[GBM] Setup complete. Run gbm watch in a project, then gbm agent --manual --test "Your task".')
    if os.environ.get('GROQ_API_KEY') or os.environ.get('GROQ_MODEL'):
        emit('[GBM] Existing GROQ_API_KEY/GROQ_MODEL environment variables override saved settings. --model overrides the saved model too.')
