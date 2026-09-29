# IDAT's fork of Wren

This is IDAT's fork of [Canner/WrenAI](https://github.com/Canner/WrenAI), the
open-source semantic engine. Upstream's own documentation is in
[README.md](README.md) — this file covers only **what IDAT changed and why**.

We run Wren as a **long-lived MCP server**: one process, started at logon on an
internal server, answering queries from IDAT-GPT and other agents against the
CRM semantic model in
[IDAT-GPT/idat-crm](https://github.com/IDAT-GPT/idat-crm). Upstream Wren is
designed and tested mainly as a short-lived CLI, and three things broke under
that long-running, network-served usage. This fork fixes them.

> **This fork is for IDAT's own deployment. We do not open pull requests
> upstream from it.**
>
> Deployment specifics — hosts, ports, service names, paths — are deliberately
> **not** in this file. This repository is a public fork. They live in
> `idat-crm`, which is private.

---

## 1. Branches

| Branch | What it is |
|---|---|
| `idat-gpt` | **The branch we run.** Upstream `main` plus the three fixes below and this README. |
| `main` | Plain mirror of `Canner/WrenAI` `main`. Never commit here — it exists so we can rebase onto it. |
| everything else | Inherited from the upstream fork. Ignore. |

The service runs an **editable install** of `core/wren`, so whichever branch is
checked out in the working tree is what the live MCP server executes. Leave
`idat-gpt` checked out on the server.

---

## 2. What we changed

All three are in `core/wren`, each reproduced against a live server before the
fix was written.

### a) The HTTP server rejected every non-localhost client

`wren serve mcp --transport http --host 0.0.0.0` bound the socket to all
interfaces but answered `421 Invalid Host header` to every request that did not
arrive as `127.0.0.1` or `localhost` — so a wildcard bind was useless and no
other machine on the network could reach it.

`FastMCP.__init__` decides *at construction time* whether to enable
DNS-rebinding protection, and when `host` is loopback it builds a
localhost-only `allowed_hosts` list. `build_server` constructed
`FastMCP("wren")` with the default host and `run_server` assigned
`mcp.settings.host` afterwards — too late. The loopback-only list stayed in
place while the socket bound `0.0.0.0`.

**Fix:** `build_server(ctx, host=..., port=...)` passes both to `FastMCP(...)`
at construction. Defaults are unchanged, so a loopback bind still gets
DNS-rebinding protection.

### b) Every query failed after eight idle hours

MySQL closes any connection idle longer than `wait_timeout` (28800 s / 8 h by
default). The connector held one `MySQLdb` connection for the entire process
lifetime without checking it, so once the server dropped it **every** `run_sql`
returned `(2006, 'MySQL server has gone away')` until someone restarted the
service. Observed on a process that had been up for ten days.

**Fix:** `_ensure_connection()` pings before each `query` / `dry_run` and
rebuilds the connection when the socket is dead, re-running the session setup so
the new connection keeps `ANSI_QUOTES`. Two deliberate choices, both recorded in
the docstring:

- Ping *before* executing rather than retrying *after* a failure — a retry
  could re-send a statement that already reached the server, which is unsafe
  for writes.
- `ping(reconnect=True)` is **not** used: MySQL 8.0.33 deprecated
  `MYSQL_OPT_RECONNECT`, mysqlclient deprecated the parameter in 2.2.1, and a
  driver-level reconnect would skip the session setup, leaving the new
  connection without `ANSI_QUOTES`.

Verified by killing the connection server-side with `KILL <id>`; the next query
succeeded instead of returning 2006.

### c) Any query with its own LIMIT was a syntax error

`_apply_limit` appended `LIMIT n` unconditionally. `run_sql` always passes a row
cap, so SQL that already ended in its own `LIMIT` became two `LIMIT` clauses and
MySQL rejected the whole statement with error 1064 — and "write a query ending
in `LIMIT 20`" is exactly what an LLM does by default.

**Fix:** a trailing `LIMIT` is recognised in all three MySQL spellings
(`LIMIT n`, `LIMIT offset, count`, `LIMIT count OFFSET offset`) and reconciled
with the cap: the more restrictive of the two wins, any `OFFSET` is preserved.
The pattern is anchored to the end of the statement, so a `LIMIT` inside a
subquery or a string literal is not touched.

Verified against a live server: `SELECT ... LIMIT 20` under a cap of 21 now
returns 20 rows instead of failing.

**Known edge case:** SQL ending in a comment with no semicolon
(`SELECT ... LIMIT 20 -- note`) is still not matched, so the appended `LIMIT`
lands on a commented-out line. That was equally broken before this change and
has not been seen in practice.

---

## 3. Tests

| File | Covers |
|---|---|
| `core/wren/tests/unit/test_mcp_server.py` | the bind host reaches `FastMCP` at construction |
| `core/wren/tests/unit/test_mysql_reconnect.py` | ping, replace, re-init, never re-send a statement, closed connector stays closed, Doris skips `ANSI_QUOTES` |
| `core/wren/tests/unit/test_mysql_apply_limit.py` | reconciliation per `LIMIT` spelling |
| `core/wren/tests/unit/test_mysql_semicolon_unlimited.py` | fixture updated for the liveness check |

None of them need a database.

```bash
cd core/wren
uv run --no-sync pytest tests/unit/test_mysql_apply_limit.py \
    tests/unit/test_mysql_reconnect.py \
    tests/unit/test_mysql_semicolon_unlimited.py \
    tests/unit/test_mcp_server.py -q
```

Note that the rest of `tests/unit/` needs the optional extras from
`pyproject.toml`; a `--extra mysql --extra mcp` environment will show unrelated
failures in the memory and Postgres suites.

---

## 4. Keeping up with upstream

```bash
git fetch origin                 # origin = Canner/WrenAI
git switch main && git merge --ff-only origin/main
git switch idat-gpt && git rebase origin/main
```

Rebase, do not merge — the fork is three commits plus a README, and a linear
history makes it obvious what is ours. Expect conflicts in
`core/wren/src/wren/connector/mysql.py`; upstream is actively changing that
file, and all three of our fixes touch it or `mcp_server.py`.

After a rebase, re-run the four test files above **and restart the MCP
service**, because the service runs this working tree.

---

## 5. Related repositories

| Repository | What it is |
|---|---|
| [IDAT-GPT/idat-crm](https://github.com/IDAT-GPT/idat-crm) *(private)* | The semantic model this engine serves, and every deployment detail. |
| [IDAT-GPT/onyx](https://github.com/IDAT-GPT/onyx) | IDAT-GPT, the AI platform that queries this server. |
| [Canner/WrenAI](https://github.com/Canner/WrenAI) | Upstream. |
