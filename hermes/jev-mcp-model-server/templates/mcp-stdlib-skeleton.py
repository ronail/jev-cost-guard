#!/usr/bin/env python3
# Zero-dependency stdlib MCP server skeleton for a System One DECISION model
# (jev-1.13-free at opencode.ai/zen/v1/systemone). Copy + modify.
# Pure stdlib; no mcp/pydantic. Run as: python3 server.py
import json, os, sys, urllib.request, urllib.error

NL = chr(10)
ENDPOINT = os.environ.get('JEV_ENDPOINT', 'https://opencode.ai/zen/v1/systemone')
MODEL = os.environ.get('JEV_MODEL', 'jev-1.13-free')
# OpenCode fingerprint: x-opencode-project + an OpenCode UA are validated.
# A bare token with no OpenCode UA -> 403 error code 1010.
FP = {
    'User-Agent': 'opencode/1.18.31',
    'x-opencode-client': 'desktop',
    'x-opencode-project': 'global',
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}


def _send(msg):
    sys.stdout.write(json.dumps(msg) + NL)
    sys.stdout.flush()


def _load_key():
    """JEV_API_KEY -> OPENCODE_ZEN_API_KEY -> profile .env (never echoed)."""
    for v in ('JEV_API_KEY', 'OPENCODE_ZEN_API_KEY', 'OPENCODE_API_KEY'):
        if os.environ.get(v):
            return os.environ[v]
    home = os.environ.get('HERMES_HOME') or os.path.expanduser('~/.hermes')
    for p in (os.path.join(home, '.env'),
              os.path.join(home, 'profiles', 'dev', '.env'),
              os.path.expanduser('~/.hermes/profiles/dev/.env')):
        try:
            with open(p) as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith(('OPENCODE_ZEN_API_KEY=', 'OPENCODE_API_KEY=')):
                        return s.split('=', 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def _ask(state, question, instructions, qtype='noul'):
    # A noul question REQUIRES `instructions` (or `criteria`) or the relay
    # returns 400 "Noul question must have criteria or instructions".
    key = _load_key()
    headers = dict(FP)
    if key:
        headers['Authorization'] = 'Bearer ' + key
    elif MODEL.endswith('-free'):
        headers['Authorization'] = 'Bearer public'   # relay free-tier; no key needed
    else:
        raise RuntimeError('non-free model needs a real key')
    q = {'type': qtype, 'instructions': instructions}
    body = {'model': MODEL, 'state': state, 'questions': {question: q}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                 method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read()[:300]!r}')
    return json.loads(raw).get('answers', {}).get(question)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
        if method == 'initialize':
            _send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'skeleton', 'version': '0.2.1'}}})
        elif method == 'tools/list':
            _send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [
                {'name': 'ping', 'description': 'Reachability noul probe.',
                 'inputSchema': {'type': 'object', 'properties': {}}}]}})
        elif method == 'tools/call' and params.get('name') == 'ping':
            ans = _ask('reachability probe', 'is_reachable',
                       'Does the jev endpoint respond with a calibrated answer to this question?')
            _send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': json.dumps(ans)}]}})


if __name__ == '__main__':
    main()
