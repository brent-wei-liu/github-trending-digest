"""github-trending-digest — FastAPI server for the web UI.

Run: python3 api.py
URL: http://127.0.0.1:8082 (local), http://<mac-ip>:8082 (LAN/phone)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from db import get_db, init_db

PORT = 8082
HOST = "0.0.0.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="github-trending-digest")


def _conn():
    conn = get_db()
    init_db(conn)
    return conn


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(str(index))


@app.get("/api/stats")
def api_stats():
    conn = _conn()
    try:
        return {
            "repos_total": conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
            "repos_starred": conn.execute("SELECT COUNT(*) FROM repos WHERE starred = 1").fetchone()[0],
            "entries_total": conn.execute("SELECT COUNT(*) FROM trending_entries").fetchone()[0],
            "summaries_total": conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0],
            "languages_top": [
                dict(r) for r in conn.execute(
                    """SELECT r.language, COUNT(DISTINCT t.repo) AS n
                       FROM trending_entries t JOIN repos r ON t.repo = r.full_name
                       WHERE r.language IS NOT NULL AND r.language != ''
                       GROUP BY r.language ORDER BY n DESC LIMIT 6"""
                ).fetchall()
            ],
            "latest_fetch": conn.execute("SELECT MAX(fetched_at) FROM trending_entries").fetchone()[0],
        }
    finally:
        conn.close()


@app.get("/api/repos")
def api_repos(
    period: str = Query("daily", regex="^(daily|weekly|monthly)$"),
    q: str | None = None,
    starred: bool = False,
    days: int = Query(7, ge=1, le=90),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """List repos seen on trending in the last N days, joined with the
    most recent trending_entry for the given period."""
    conn = _conn()
    try:
        where = ["t.period = ?", "t.fetched_at >= datetime('now', ?)"]
        params: list = [period, f"-{days} days"]
        if q:
            where.append("(r.full_name LIKE ? OR r.description LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if starred:
            where.append("r.starred = 1")
        where_sql = " WHERE " + " AND ".join(where)

        total = conn.execute(
            f"""SELECT COUNT(DISTINCT r.id)
                FROM repos r JOIN trending_entries t ON t.repo = r.full_name
                {where_sql}""",
            params,
        ).fetchone()[0]

        offset = max(0, (page - 1) * page_size)
        rows = conn.execute(
            f"""SELECT r.id, r.full_name, r.description, r.language, r.url,
                       r.first_seen, r.starred, r.starred_at,
                       MIN(t.rank)        AS best_rank,
                       MAX(t.stars)       AS max_stars,
                       MAX(t.stars_delta) AS max_delta,
                       MAX(t.forks)       AS max_forks,
                       MAX(t.fetched_at)  AS last_seen
                FROM repos r JOIN trending_entries t ON t.repo = r.full_name
                {where_sql}
                GROUP BY r.id
                ORDER BY MAX(t.fetched_at) DESC, best_rank ASC
                LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()
        return {
            "repos": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
            "period": period,
        }
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/star")
def api_star(repo_id: int):
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE repos SET starred = 1, starred_at = ? WHERE id = ?",
            (now, repo_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "repo not found")
        return {"ok": True, "id": repo_id, "starred": True}
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/unstar")
def api_unstar(repo_id: int):
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE repos SET starred = 0, starred_at = NULL WHERE id = ?",
            (repo_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "repo not found")
        return {"ok": True, "id": repo_id, "starred": False}
    finally:
        conn.close()


@app.get("/api/digests")
def api_digests():
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, date, period, focus, created_at,
                      LENGTH(content) AS content_length
               FROM summaries ORDER BY date DESC, id DESC"""
        ).fetchall()
        return {"digests": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/digests/{digest_id}")
def api_digest(digest_id: int):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM summaries WHERE id = ?", (digest_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "digest not found")
        return dict(row)
    finally:
        conn.close()


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    print(f"github-trending-digest UI on http://127.0.0.1:{PORT}  (LAN: http://<mac-ip>:{PORT})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
