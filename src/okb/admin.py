"""Admin web UI: pipeline status, batch actions, token management.

Security model (the panel is exposed to the internet via Caddy):
- own password auth, independent of any proxy-level protection;
- sessions are HMAC-signed cookies, login is rate-limited per IP;
- every mutation is a POST with a CSRF token;
- ops buttons map to a fixed whitelist of argv vectors — no user input
  ever reaches a shell; long work runs as transient systemd units so an
  admin restart cannot kill a running batch.
"""

import base64
import hashlib
import hmac
import html
import json
import secrets
import subprocess
import time
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import db
from .config import settings

SESSION_COOKIE = "okbadm"
SESSION_TTL = 7 * 24 * 3600  # any-device convenience; revoke by rotating secret
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 600

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "admin_templates"),
    autoescape=select_autoescape(["html"]),
)

_login_failures: dict[str, list[float]] = {}  # ip -> [monotonic timestamps]


# --- password / session -----------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1)
        return hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    except Exception:
        return False


def _sign(payload: bytes) -> str:
    sig = hmac.new(settings.admin_secret.encode(), payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode().rstrip("=")
            + "." + base64.urlsafe_b64encode(sig).decode().rstrip("="))


def _unsign(value: str) -> dict | None:
    try:
        payload_b64, sig_b64 = value.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        payload = base64.urlsafe_b64decode(pad(payload_b64))
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        want = hmac.new(settings.admin_secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, want):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def make_session() -> str:
    return _sign(json.dumps(
        {"exp": time.time() + SESSION_TTL, "csrf": secrets.token_urlsafe(16)}
    ).encode())


def session_of(request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    return _unsign(raw) if raw else None


def _client_ip(request) -> str:
    # Caddy terminates TLS and proxies over WireGuard; it sets X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")


def _locked_out(ip: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_LOCKOUT_S]
    _login_failures[ip] = fails
    return len(fails) >= LOGIN_MAX_FAILURES


# --- rendering --------------------------------------------------------------

def render(_template: str, request, **ctx) -> HTMLResponse:
    sess = session_of(request)
    ctx.setdefault("csrf", sess["csrf"] if sess else "")
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return HTMLResponse(_env.get_template(_template).render(**ctx))


def back(request, msg: str = "", err: str = "", to: str = "/") -> RedirectResponse:
    q = []
    if msg:
        q.append("msg=" + html.escape(msg))
    if err:
        q.append("err=" + html.escape(err))
    return RedirectResponse(to + ("?" + "&".join(q) if q else ""), status_code=303)


PUBLIC_PATHS = {"/login", "/healthz", "/connect", "/connect.md"}


class AuthRequired(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        sess = session_of(request)
        if sess is None:
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST":
            form = await request.form()
            if not hmac.compare_digest(str(form.get("csrf", "")), sess["csrf"]):
                return HTMLResponse("CSRF check failed", status_code=403)
            request.state.form = form
        return await call_next(request)


# --- auth routes ------------------------------------------------------------

async def login(request):
    if request.method == "GET":
        return render("login.html", request)
    ip = _client_ip(request)
    if _locked_out(ip):
        return render("login.html", request,
                      err="Too many attempts, try again in 10 minutes")
    form = await request.form()
    if settings.admin_password_hash and verify_password(
            str(form.get("password", "")), settings.admin_password_hash):
        _login_failures.pop(ip, None)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL,
                        httponly=True, secure=True, samesite="lax")
        return resp
    _login_failures.setdefault(ip, []).append(time.monotonic())
    time.sleep(0.5)
    return render("login.html", request, err="Wrong password")


async def logout(request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


async def healthz(request):
    return JSONResponse({"ok": True})


# --- public onboarding guide ------------------------------------------------

CONNECT_MD = """\
# OpenKnowledge — подключение

OpenKnowledge — база знаний команды: документы из рабочей папки Nextcloud,
доступные ИИ-агентам через MCP (поиск по смыслу, SQL по метаданным,
оригиналы файлов).

Если ты ИИ-агент: выполни шаги ниже сам, токен спроси у пользователя.

## Что нужно

1. Токен доступа — его выдаёт Алихан (вида `kb_live_...`). Больше ничего:
   ни VPN, ни аккаунтов.

## Подключение к Claude Code

```bash
claude mcp add --transport http openknowledge https://ok.kzt.asia/mcp \\
  --header "Authorization: Bearer ВАШ_ТОКЕН"
```

## Любой другой MCP-клиент (JSON-конфиг)

```json
{
  "mcpServers": {
    "openknowledge": {
      "type": "http",
      "url": "https://ok.kzt.asia/mcp",
      "headers": { "Authorization": "Bearer ВАШ_ТОКЕН" }
    }
  }
}
```

## Проверка

Попроси агента: «поищи в openknowledge договор с КазСтандарт».
Должны вернуться результаты с цитатами из документов.

## Какие инструменты есть

| Инструмент | Для чего |
|---|---|
| `search` | поиск по смыслу и по точной фразе (вопросы «где обсуждали X», «что мы знаем про Y») |
| `query_evidence` | SQL SELECT по метаданным — перечисления и подсчёты («сколько договоров за 2025») |
| `get_raw` | дословный оригинал документа (конвертированный markdown для бинарных форматов) |
| `recent` | что проиндексировано за последние дни |
| `add_note` | сохранить заметку в базу (человеко-одобряемый канал записи) |

Правило большого пальца: смысловые вопросы — `search`, точные списки и
цифры — `query_evidence`. Параметр `project` в search сужает поиск до
папки (например `2025/Договор услуг!!!!/ISO`). Заметки коллег ищутся
через `sources=["note"]`, документы — `sources=["archive"]`.

## Агенту: складывай знания в базу

Когда вместе с пользователем ты завершил значимую задачу — принято
решение, что-то развёрнуто или настроено, найдены неочевидные грабли —
предложи пользователю сохранить это в базу через `add_note`:

- **что делали и что решили** (1-3 абзаца, без воды);
- **где это стоит** (хосты, адреса, пути), **какие системы затронуты**;
- **кто участвовал** — имена, чтобы работал поиск по людям.

Правила:
1. Сохраняй только с явного согласия пользователя — покажи текст заметки
   перед отправкой.
2. НИКОГДА не включай в заметку пароли, токены, ключи и прочие секреты.
3. Пиши выжимку, а не пересказ диалога: заметку будут читать через год.

Авторство проставляется автоматически по токену. Заметки индексируются
как `source=note` и находятся тем же поиском, что и документы.

Вопросы и токены: Алихан Амирханов.
"""


async def connect_md(request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse(CONNECT_MD, media_type="text/markdown; charset=utf-8")


async def connect_page(request):
    return render("connect.html", request)


# --- status data ------------------------------------------------------------

def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as e:
        return f"error: {e}"


def _active_units(pattern: str) -> list[str]:
    out = _systemctl("list-units", "--plain", "--no-legend", pattern)
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def _endpoint_alive(url: str) -> bool:
    try:
        return httpx.get(url + "/models", timeout=3).status_code == 200
    except Exception:
        return False


MIRROR_DIR = "mirrors/pcf-cd-24-26"


def _mirror_remaining() -> dict:
    root = settings.data_dir.expanduser() / MIRROR_DIR
    try:
        total = sum(1 for p in root.rglob("*")
                    if p.is_file() and not p.name.startswith("."))
    except OSError:
        total = 0
    with db.connect() as conn:
        seen = conn.execute(
            "SELECT count(*) n FROM source_objects WHERE path LIKE %s",
            (str(root) + "/%",)).fetchone()["n"]
    return {"mirror_total": total, "mirror_remaining": max(0, total - seen)}


def gather_status() -> dict:
    with db.connect() as conn:
        jobs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) n FROM ingest_jobs GROUP BY status")}
        ev = conn.execute(
            "SELECT count(*) chunks, count(DISTINCT source_id) docs FROM evidence"
        ).fetchone()
        passthrough = conn.execute(
            "SELECT count(*) n FROM evidence WHERE meta->>'distill'='failed_passthrough'"
        ).fetchone()["n"]
        no_passport = conn.execute(
            """SELECT count(DISTINCT source_id) n FROM evidence e
               WHERE NOT EXISTS (SELECT 1 FROM evidence x
                   WHERE x.source_id=e.source_id AND x.meta ? 'basis')"""
        ).fetchone()["n"]
        doc_types = conn.execute(
            """SELECT coalesce(meta->>'doc_type','—') dt, count(DISTINCT source_id) n
               FROM evidence GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        # by-design skips (junk kinds) are counted, not listed — only real
        # failures and operator-parked jobs deserve the errors panel
        errors = conn.execute(
            """SELECT id, status, left(error, 200) error, updated_at
               FROM ingest_jobs
               WHERE error IS NOT NULL
                 AND NOT (status = 'skipped' AND error LIKE 'kind=%')
               ORDER BY updated_at DESC LIMIT 20"""
        ).fetchall()
        junk_skipped = conn.execute(
            """SELECT count(*) n FROM ingest_jobs
               WHERE status = 'skipped' AND error LIKE 'kind=%'"""
        ).fetchone()["n"]
        feed = conn.execute(
            """SELECT date_trunc('day', indexed_at)::date AS d,
                      count(DISTINCT source_id) AS n
               FROM evidence GROUP BY 1 ORDER BY 1 DESC LIMIT 7"""
        ).fetchall()
    return {
        **_mirror_remaining(),
        "feed": [{"d": str(r["d"]), "n": r["n"]} for r in feed],
        "feed_max": max((r["n"] for r in feed), default=1),
        "junk_skipped": junk_skipped,
        "jobs": jobs,
        "chunks": ev["chunks"], "docs": ev["docs"],
        "passthrough": passthrough,
        "passthrough_pct": round(100 * passthrough / ev["chunks"], 1) if ev["chunks"] else 0,
        "no_passport": no_passport,
        "doc_types": [dict(r) for r in doc_types],
        "errors": [dict(r, updated_at=str(r["updated_at"])[:19]) for r in errors],
        "workers": (_active_units("okb-worker*") + _active_units("okb-ingest-*")
                    + _active_units("okb-refresh-*")),
        "sync_state": _systemctl("is-active", "okb-nextcloud-sync.service"),
        "gen_alive": _endpoint_alive(settings.gen_url),
        "emb_alive": _endpoint_alive(settings.emb_url),
        "gen_model": settings.gen_model,
    }


async def dashboard(request):
    return render("dashboard.html", request, s=gather_status())


async def api_status(request):
    return JSONResponse(gather_status())


# --- ops actions (fixed whitelist, transient systemd units) -----------------

def _spawn_unit(unit: str, *argv: str) -> tuple[bool, str]:
    """Run a whitelisted command as a transient user unit (survives admin restart)."""
    cmd = ["systemd-run", "--user", "--collect", "--unit", unit,
           "--working-directory", str(Path.home() / "openknowledge"), *argv]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        ok = p.returncode == 0
        return ok, (p.stderr or p.stdout).strip()
    except Exception as e:
        return False, str(e)


UV = str(Path.home() / ".local/bin/uv")


async def action_refresh(request):
    """The one-button pipeline: sync -> inventory -> workers, as a single
    transient unit (okb refresh). Refuses to stack on a running pipeline."""
    busy = (_active_units("okb-refresh-*") + _active_units("okb-worker*")
            + _active_units("okb-ingest-*"))
    if busy:
        return back(request, err="Обновление уже идёт — смотри «в очереди» и «в обработке»")
    unit = f"okb-refresh-{time.strftime('%Y%m%d-%H%M%S')}"
    ok, detail = _spawn_unit(unit, UV, "run", "okb", "refresh")
    if ok:
        return back(request, msg="Обновление запущено: забираю файлы из Nextcloud и обрабатываю новое. "
                                 "Страницу можно закрыть — процесс идёт на сервере.")
    return back(request, err="Не удалось запустить: " + detail)


async def action_sync(request):
    out = subprocess.run(
        ["systemctl", "--user", "start", "--no-block", "okb-nextcloud-sync.service"],
        capture_output=True, text=True, timeout=10)
    if out.returncode == 0:
        return back(request, msg="Sync started")
    return back(request, err="Sync failed to start: " + out.stderr.strip())


async def action_ingest(request):
    try:
        limit = int(request.state.form.get("limit", "100"))
    except ValueError:
        return back(request, err="Batch size must be a number")
    if not 1 <= limit <= 1000:
        return back(request, err="Batch size must be 1..1000")
    mirror = str(settings.data_dir.expanduser() / "mirrors/pcf-cd-24-26")
    unit = f"okb-ingest-{time.strftime('%Y%m%d-%H%M%S')}"
    ok, detail = _spawn_unit(unit, UV, "run", "okb", "ingest", mirror,
                             "--source", "archive", "--limit", str(limit))
    if ok:
        return back(request, msg=f"Ingest of up to {limit} new files started ({unit})")
    return back(request, err="Ingest failed to start: " + detail)


async def action_worker(request):
    if len(_active_units("okb-worker*")) >= 3:
        return back(request, err="3 workers already running")
    unit = f"okb-worker-once-{time.strftime('%Y%m%d-%H%M%S')}"
    ok, detail = _spawn_unit(unit, UV, "run", "okb", "worker", "--once")
    if ok:
        return back(request, msg=f"Worker started ({unit}), exits when queue is empty")
    return back(request, err="Worker failed to start: " + detail)


# --- jobs -------------------------------------------------------------------

async def jobs_page(request):
    status = request.query_params.get("status", "")
    allowed = {"pending", "processing", "done", "skipped", "failed"}
    with db.connect() as conn:
        if status in allowed:
            rows = conn.execute(
                """SELECT j.id, j.status, j.attempts, left(j.error,150) error,
                          j.updated_at, r.kind, r.bytes, s.path
                   FROM ingest_jobs j
                   JOIN raw_objects r USING (content_hash)
                   LEFT JOIN LATERAL (SELECT path FROM source_objects s
                       WHERE s.content_hash=j.content_hash LIMIT 1) s ON true
                   WHERE j.status=%s ORDER BY j.updated_at DESC LIMIT 200""",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                """SELECT j.id, j.status, j.attempts, left(j.error,150) error,
                          j.updated_at, r.kind, r.bytes, s.path
                   FROM ingest_jobs j
                   JOIN raw_objects r USING (content_hash)
                   LEFT JOIN LATERAL (SELECT path FROM source_objects s
                       WHERE s.content_hash=j.content_hash LIMIT 1) s ON true
                   ORDER BY j.updated_at DESC LIMIT 200""").fetchall()
    jobs = [dict(r, updated_at=str(r["updated_at"])[:19],
                 name=(r["path"] or "?").rsplit("/", 1)[-1],
                 mb=round((r["bytes"] or 0) / 1e6, 1)) for r in rows]
    return render("jobs.html", request, jobs=jobs, status=status)


def _job_id(form) -> int | None:
    try:
        return int(form.get("id", ""))
    except ValueError:
        return None


async def job_requeue(request):
    jid = _job_id(request.state.form)
    if jid is None:
        return back(request, err="Bad job id", to="/jobs")
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE ingest_jobs SET status='pending', error=NULL, attempts=0,
               next_attempt_at=now(), lease_until=NULL
               WHERE id=%s AND status IN ('skipped','failed','processing')""", (jid,))
    if cur.rowcount:
        return back(request, msg=f"Job {jid} requeued", to="/jobs")
    return back(request, err=f"Job {jid} not in a requeueable state", to="/jobs")


async def job_park(request):
    jid = _job_id(request.state.form)
    if jid is None:
        return back(request, err="Bad job id", to="/jobs")
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE ingest_jobs SET status='skipped',
               error='parked via admin ' || to_char(now(), 'YYYY-MM-DD')
               WHERE id=%s AND status IN ('pending','processing')""", (jid,))
    if cur.rowcount:
        return back(request, msg=f"Job {jid} parked", to="/jobs")
    return back(request, err=f"Job {jid} not in a parkable state", to="/jobs")


# --- documents browser ------------------------------------------------------

def _folder_cards(conn) -> list[dict]:
    """Group documents into topic cards: distinct projects share a common
    prefix («2025/Договор услуг!!!!/…»); the first segment after it is the
    human-meaningful topic folder."""
    rows = conn.execute(
        """SELECT coalesce(meta->>'project', '') AS proj,
                  coalesce(meta->>'doc_type', '—') AS dt,
                  count(DISTINCT source_id) AS docs,
                  max(indexed_at) AS last
           FROM evidence GROUP BY 1, 2"""
    ).fetchall()
    projects = sorted({r["proj"] for r in rows if r["proj"]})
    prefix_parts: list[str] = []
    if projects:
        split = [p.split("/") for p in projects]
        for seg in split[0]:
            if all(len(s) > len(prefix_parts) and s[len(prefix_parts)] == seg
                   for s in split):
                prefix_parts.append(seg)
            else:
                break
    prefix = "/".join(prefix_parts)
    cards: dict[str, dict] = {}
    for r in rows:
        if r["proj"]:
            rest = r["proj"][len(prefix):].lstrip("/") if prefix else r["proj"]
            topic = rest.split("/")[0] if rest else "(корень)"
            scope = f"{prefix}/{topic}" if prefix and rest else (prefix or r["proj"])
        else:
            topic, scope = "без папки", ""
        c = cards.setdefault(topic, {"topic": topic, "scope": scope, "docs": 0,
                                     "last": r["last"], "types": {}})
        c["docs"] += r["docs"]
        c["types"][r["dt"]] = c["types"].get(r["dt"], 0) + r["docs"]
        c["last"] = max(c["last"], r["last"])
    out = sorted(cards.values(), key=lambda c: -c["docs"])
    for c in out:
        c["last"] = str(c["last"])[:10]
        c["types"] = sorted(c["types"].items(), key=lambda t: -t[1])[:4]
    return out


async def docs_page(request):
    q = request.query_params.get("q", "").strip()[:100]
    scope = request.query_params.get("project", "").strip()[:200]
    with db.connect() as conn:
        if not q and not scope:
            return render("docs.html", request, cards=_folder_cards(conn),
                          docs=None, q=q, scope=scope)
        where, params = [], []
        if q:
            where.append("""(max(meta->>'title') ILIKE %s
                          OR max(meta->'paths'->>0) ILIKE %s)""")
            params += [f"%{q}%", f"%{q}%"]
        if scope:
            where.append("max(meta->>'project') LIKE %s")
            params.append(scope + "%")
        rows = conn.execute(f"""
            SELECT source_id,
                   coalesce(max(meta->>'title'), '—') title,
                   coalesce(max(meta->>'doc_type'), '—') dt,
                   max(meta->>'year') AS yr,
                   max(meta->'paths'->>0) path,
                   count(*) chunks,
                   max(indexed_at) indexed_at,
                   bool_or(meta->>'distill'='failed_passthrough') has_passthrough
            FROM evidence
            GROUP BY source_id
            HAVING {' AND '.join(where)}
            ORDER BY max(indexed_at) DESC LIMIT 300""", params).fetchall()
    docs = [dict(r, indexed_at=str(r["indexed_at"])[:16],
                 name=(r["path"] or "?").rsplit("/", 1)[-1]) for r in rows]
    return render("docs.html", request, cards=None, docs=docs, q=q, scope=scope)


async def doc_page(request):
    sid = request.query_params.get("sid", "")
    if not (len(sid) == 64 and all(c in "0123456789abcdef" for c in sid)):
        return back(request, err="Bad document id", to="/docs")
    with db.connect() as conn:
        chunks = conn.execute(
            """SELECT unit_ord, content, meta->>'resolution_status' rs,
                      meta->>'distill' distill
               FROM evidence WHERE source_id=%s ORDER BY unit_ord""", (sid,)).fetchall()
        if not chunks:
            return back(request, err="Document not found", to="/docs")
        meta = conn.execute(
            "SELECT meta FROM evidence WHERE source_id=%s LIMIT 1", (sid,)
        ).fetchone()["meta"]
    passport = {k: meta.get(k) for k in
                ("title", "doc_type", "year", "authors", "basis", "mime")}
    path = (meta.get("paths") or ["?"])[0]
    return render("doc.html", request, chunks=[dict(c) for c in chunks],
                  passport=passport, path=path, name=path.rsplit("/", 1)[-1])


# --- tokens -----------------------------------------------------------------

async def tokens_page(request):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT t.name, t.created_at, t.revoked_at,
                      (SELECT max(ts) FROM query_log q WHERE q.token_name=t.name) last_used
               FROM api_tokens t ORDER BY t.created_at DESC""").fetchall()
    tokens = [dict(r, created_at=str(r["created_at"])[:19],
                   revoked_at=str(r["revoked_at"])[:19] if r["revoked_at"] else None,
                   last_used=str(r["last_used"])[:19] if r["last_used"] else "never")
              for r in rows]
    return render("tokens.html", request, tokens=tokens,
                  new_token=request.query_params.get("new_token", ""),
                  new_name=request.query_params.get("new_name", ""))


async def token_create(request):
    name = str(request.state.form.get("name", "")).strip()
    if not name or len(name) > 64 or not name.replace("-", "").replace("_", "").isalnum():
        return back(request, err="Name: letters, digits, - and _ only", to="/tokens")
    token = "kb_live_" + secrets.token_urlsafe(24)
    with db.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM api_tokens WHERE name=%s AND revoked_at IS NULL", (name,)
        ).fetchone()
        if exists:
            return back(request, err=f"Active token '{name}' already exists", to="/tokens")
        conn.execute("INSERT INTO api_tokens (token_hash, name) VALUES (%s,%s)",
                     (hashlib.sha256(token.encode()).hexdigest(), name))
    # token appears once in the redirect URL and is never retrievable again
    return back(request, to=f"/tokens?new_token={token}&new_name={html.escape(name)}")


async def token_revoke(request):
    name = str(request.state.form.get("name", "")).strip()
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE api_tokens SET revoked_at=now() WHERE name=%s AND revoked_at IS NULL",
            (name,))
    if cur.rowcount:
        return back(request, msg=f"Token '{name}' revoked", to="/tokens")
    return back(request, err=f"No active token named '{name}'", to="/tokens")


# --- app --------------------------------------------------------------------

routes = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/healthz", healthz),
    Route("/connect", connect_page),
    Route("/connect.md", connect_md),
    Route("/", dashboard),
    Route("/api/status", api_status),
    Route("/actions/refresh", action_refresh, methods=["POST"]),
    Route("/actions/sync", action_sync, methods=["POST"]),
    Route("/actions/ingest", action_ingest, methods=["POST"]),
    Route("/actions/worker", action_worker, methods=["POST"]),
    Route("/docs", docs_page),
    Route("/doc", doc_page),
    Route("/jobs", jobs_page),
    Route("/jobs/requeue", job_requeue, methods=["POST"]),
    Route("/jobs/park", job_park, methods=["POST"]),
    Route("/tokens", tokens_page),
    Route("/tokens/create", token_create, methods=["POST"]),
    Route("/tokens/revoke", token_revoke, methods=["POST"]),
]

app = Starlette(routes=routes)
app.add_middleware(AuthRequired)


def run() -> None:
    import uvicorn

    if not settings.admin_password_hash or not settings.admin_secret:
        raise SystemExit(
            "KB_ADMIN_PASSWORD_HASH and KB_ADMIN_SECRET must be set "
            "(generate with: okb admin-password)")
    uvicorn.run(app, host=settings.admin_host, port=settings.admin_port,
                proxy_headers=True)
