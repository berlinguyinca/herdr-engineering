"""Deployment-evidence redaction rules, vendored from the InferWeave emitter.

Copied (not imported: a separate repository) from InferWeave/inferweave
``deploy/pipelines/deploy_event.py`` (``_redact_window`` / ``redact`` and their
patterns), so herdr's second layer covers at least every class the host's first
layer does: PEM private keys, auth/cookie/api-key headers, Bearer, URL userinfo,
``export ..._KEY=``, argv secret flags, curl/wget/http/openssl/ssh-keygen/docker/
sshpass/redis-cli/mysql passwords, ``--password-stdin`` feeds, aws/vault/htpasswd,
signed-URL query secrets and known token formats. Keep in sync with the emitter.
"""
import re

R = "[REDACTED]"

MAX_RAW = 65536
LEAD_IN = 1024
_KW = r"(?:secret|token|password|passwd|apikey|api_key|private_key|credential|kek)"
_NAME = r"[a-z0-9_.-]{0,64}"
_VAL = (r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\"[^\r\n]*|'[^\r\n]*"
        r"|[^\s\"',;]+)")
_SEP = r"[\"']?[ \t]{0,16}[=:][ \t]{0,16}"

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]{0,40}-----|\Z)", re.S)
_HEADER = re.compile(
    r"(?i)((?:[a-z0-9_-]{0,64}-api-key|[a-z0-9_-]{0,64}authorization|set-cookie|cookie"
    r"|x-auth-token)[\"']?[ \t]{0,16}:[ \t]{0,16})[^\r\n]*")
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_URLAUTH = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)([^/\s@]{1,1024})@")
_FLAG = re.compile(
    r"(?i)(--[a-z0-9_-]{0,32}?(?:secret|token|password|passwd|key|credential|kek)[a-z0-9_-]{0,64})"
    r"(?:=(\"[^\"]*\"|'[^']*'|\S*)|([ \t]+)(?!-)(\"[^\"]*\"|'[^']*'|\S+))")
_PAIR = re.compile(r"(?i)(" + _KW + _NAME + _SEP + r")" + _VAL)
_PAIR_KEY = re.compile(r"(?i)([_-](?:key|pwd|pass)" + _SEP + r")" + _VAL)
_PAIR_BARE = re.compile(r"(\bKEY" + _SEP + r")" + _VAL)
_MYSQL = re.compile(
    r"(?i)(\bmysql(?:dump|admin)?\b[^\n]{0,200}?\s-p)([ \t]*)(?!-)\S+")
_AUTHENV = re.compile(r"(?i)(authorization[\"']?[ \t]{0,16}=[ \t]{0,16})[^\r\n]*")
_EXPORT = re.compile(
    r"(?i)(\bexport[ \t]+[A-Za-z0-9_]{0,64}(?:secret|token|password|passwd|apikey|api_key"
    r"|credential|kek|_key|_pwd)[A-Za-z0-9_]{0,32}[ \t]+)(?!-)\S+")


_PASSBARE = re.compile(r"(?i)(\bpass[\"']?[ \t]{0,16}=[ \t]{0,16})" + _VAL)
_QUERY = re.compile(
    r"(?i)([?&;](?:sig|signature|awsaccesskeyid|googleaccessid)=|"
    r"\bx-amz-(?:signature|credential|security-token)=)[^&\s\"'#]+")
_AWSCFG = re.compile(
    r"(?i)(\baws\b[^\n]{0,100}?\sconfigure\s+set\s+[a-z0-9_.-]{0,64}"
    r"(?:secret|key|token)[a-z0-9_.-]{0,32}[ \t]+)(?!-)\S+")
_VAULT = re.compile(r"(?i)(\bvault[ \t]+login[ \t]+(?:-[a-z-]{1,32}(?:=\S*)?[ \t]+){0,5})(?!-)\S+")
_HTPASSWD = re.compile(
    r"(?i)(\bhtpasswd\b[^\n]{0,100}?\s-[a-z]{0,6}b[a-z]{0,6}[ \t]+\S+[ \t]+\S+[ \t]+)(?!-)\S+")
_STDIN_PIPE = re.compile(
    r"(\b(?:echo|printf)[ \t]+(?:-[a-zA-Z]+[ \t]+)*(?:(?:'%s'|\"%s\"|%s)[ \t]+)?)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s|]+)([ \t]*\|[^\n]{0,200}--password-stdin)")
_STDIN_HERE1 = re.compile(r"(--password-stdin[^\n]{0,200}?<<<[ \t]*)(\"[^\"\n]*\"|'[^'\n]*'|\S+)")
_STDIN_HERE2 = re.compile(r"(<<<[ \t]*)(\"[^\"\n]*\"|'[^'\n]*'|\S+)(?=[^\n]{0,200}--password-stdin)")


def _cmd(cmd, flags, sep=r"(?:=|[ \t]*)"):
    return re.compile(r"(?i)(\b(?:" + cmd + r")\b[^\n]{0,300}?\s(?:" + flags
                      + r")" + sep + r")(?!-)\S+")


_CMDS = (
    _cmd("curl|wget", r"--proxy-user|--http-password|--password|--user|-u"),
    _cmd("http|https|xh", r"--auth|-a", r"(?:=|[ \t]+)"),
    _cmd("openssl", r"-k", r"[ \t]+"),
    _cmd("openssl", r"-pass(?:in|out)?[ \t]+pass:", r""),
    _cmd("ssh-keygen", r"-N|-P", r"[ \t]+"),
    _cmd(r"(?:docker|podman)\b[^\n]{0,200}?\slogin", r"--password|-p"),
    _cmd("sshpass", r"-p"),
    _cmd("redis-cli", r"--pass|-a"),
)
_KNOWN = re.compile(
    r"gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}")


def _flag_sub(m):
    if m.group(1).lower().endswith("-stdin"):
        return m.group(0)
    if m.group(3) is not None:
        return m.group(1) + m.group(3) + R
    return m.group(1) + "=" + R


def _url_sub(m):
    user = m.group(2).split(":", 1)[0] + ":" if ":" in m.group(2) else ""
    return m.group(1) + user + R + "@"


def _drop_partial_start(text):
    nl = text.find("\n")
    if nl >= 0:
        return text[nl + 1:]
    # no line boundary: drop the (possibly partial) first token and the next one
    ws = re.match(r"\s*\S*\s+\S{0,4096}\s*", text)
    return text[ws.end():] if ws else ""


def _redact_window(text):
    text = _PEM.sub(R, text)
    text = _HEADER.sub(lambda m: m.group(1) + R, text)
    text = _BEARER.sub(lambda m: m.group(1) + R, text)
    text = _URLAUTH.sub(_url_sub, text)
    text = _AUTHENV.sub(lambda m: m.group(1) + R, text)
    text = _EXPORT.sub(lambda m: m.group(1) + R, text)
    text = _STDIN_PIPE.sub(lambda m: m.group(1) + R + m.group(3), text)
    text = _STDIN_HERE1.sub(lambda m: m.group(1) + R, text)
    text = _STDIN_HERE2.sub(lambda m: m.group(1) + R, text)
    for rx in _CMDS + (_AWSCFG, _VAULT, _HTPASSWD):
        text = rx.sub(lambda m: m.group(1) + R, text)
    text = _MYSQL.sub(lambda m: m.group(1) + m.group(2) + R, text)
    text = _FLAG.sub(_flag_sub, text)
    for rx in (_PAIR, _PAIR_KEY, _PAIR_BARE, _PASSBARE):
        text = rx.sub(lambda m: m.group(1) + R, text)
    text = _QUERY.sub(lambda m: m.group(1) + R, text)
    text = _KNOWN.sub(R, text)
    return text


def redact(text):
    """Redact a window of the last MAX_RAW + LEAD_IN chars, THEN cut to MAX_RAW,
    so a secret whose key sits just before the cut is redacted before it is split."""
    if len(text) <= MAX_RAW:
        return _redact_window(text)
    window = LEAD_IN + MAX_RAW
    prefix, win = text[:-window], text[-window:]
    if prefix:
        # a private-key block whose BEGIN line was cut away: drop its orphan body
        begin = prefix.rfind("-----BEGIN ")
        if begin >= 0 and "-----END " not in prefix[begin:]:
            end = win.find("-----END ")
            close = win.find("-----", end + 9) if end >= 0 else -1
            win = win[close + 5:] if close >= 0 else ""
    red = _redact_window(win)
    if prefix or len(red) > MAX_RAW:
        red = red[-MAX_RAW:]
        red = _drop_partial_start(red)
    return "[earlier output dropped]\n" + red
