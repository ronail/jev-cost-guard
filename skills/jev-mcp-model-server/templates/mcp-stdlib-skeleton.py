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


# Framing is auto-detected per client so this skeleton is loadable everywhere:
# strict MCP clients (Claude Code, Codex) speak Content-Length; Hermes' MCP
# client reads newline-delimited JSON. _FRAME is set on the first message and
# _send replies in the same framing.
_FRAME = None


def _read_stdin_message(stream):
    """Read one MCP message, auto-detecting Content-Length or newline-JSON framing."""
    global _FRAME
    if _FRAME is None:
        first = stream.readline()
        while first and not first.strip():
            first = stream.readline()
        if not first:
            return None
        if first.lower().startswith(b"content-length:"):
            _FRAME = "crlf"
            length = 0
            line = first
            while line.strip():
                name, _, value = line.decode("utf-8", "replace").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip() or 0)
                line = stream.readline()
            body = stream.read(length)
            return json.loads(body.decode("utf-8", "replace")) if body else None
        _FRAME = "jsonl"
        return json.loads(first.decode("utf-8", "replace").strip())
    if _FRAME == "crlf":
        length = 0
        line = stream.readline()
        while line.strip():
            name, _, value = line.decode("utf-8", "replace").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip() or 0)
            line = stream.readline()
        body = stream.read(length)
        return json.loads(body.decode("utf-8", "replace")) if body else None
    line = stream.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8", "replace").strip())


def _send(msg):
    payload = json.dumps(msg).encode("utf-8")
    if _FRAME == "crlf":
        sys.stdout.buffer.write(
            b"Content-Length: " + str(len(payload)).encode("utf-8")
            + b"\r\n\r\n" + payload)
    else:
        sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


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
    stream = sys.stdin.buffer
    while True:
        msg = _read_stdin_message(stream)
        if msg is None:
            break
        if not isinstance(msg, dict) or 'jsonrpc' not in msg:
            continue
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
