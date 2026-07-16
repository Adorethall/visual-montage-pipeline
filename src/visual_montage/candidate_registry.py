from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .models import VisualCandidate


SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    source_candidate_id TEXT NOT NULL,
    category TEXT NOT NULL,
    video_id TEXT NOT NULL,
    video_path TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    peak_time REAL,
    event TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'eligible',
    analysis_count INTEGER NOT NULL DEFAULT 1,
    selected_count INTEGER NOT NULL DEFAULT 0,
    exported_count INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    last_creative_id TEXT,
    last_used_at TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_category ON candidates(category);
CREATE INDEX IF NOT EXISTS idx_candidates_video ON candidates(video_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

CREATE TABLE IF NOT EXISTS candidate_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(candidate_id, run_id, creative_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_run ON candidate_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_usage_state ON candidate_usage(state);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_candidate_id(candidate: VisualCandidate, category: str) -> str:
    # Quarter-second buckets absorb small timestamp drift across repeated model runs.
    start_bucket = round(candidate.source_window.start * 4) / 4
    end_bucket = round(candidate.source_window.end * 4) / 4
    identity = (
        f"{category}|{candidate.video_id}|{start_bucket:.2f}|{end_bucket:.2f}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return (
        f"{category}_{candidate.video_id[-12:]}_"
        f"{round(start_bucket * 1000):07d}_{round(end_bucket * 1000):07d}_{digest}"
    )


class CandidateRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CandidateRegistry":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def register(
        self,
        candidates: Iterable[VisualCandidate],
        category: str,
        *,
        increment_analysis: bool = True,
    ) -> list[VisualCandidate]:
        now = utc_now()
        registered: list[VisualCandidate] = []
        for candidate in candidates:
            stable_id = stable_candidate_id(candidate, category)
            payload = candidate.model_copy(update={"candidate_id": stable_id})
            self.connection.execute(
                """
                INSERT INTO candidates (
                    candidate_id, source_candidate_id, category, video_id, video_path,
                    start, end, peak_time, event, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    source_candidate_id=excluded.source_candidate_id,
                    video_path=excluded.video_path,
                    peak_time=excluded.peak_time,
                    event=excluded.event,
                    analysis_count=candidates.analysis_count + ?,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    stable_id,
                    candidate.candidate_id,
                    category,
                    candidate.video_id,
                    candidate.video_path,
                    candidate.source_window.start,
                    candidate.source_window.end,
                    candidate.peak_time,
                    candidate.event,
                    payload.model_dump_json(),
                    now,
                    1 if increment_analysis else 0,
                ),
            )
            registered.append(payload)
        self.connection.commit()
        return registered

    def history(self, category: str) -> dict[str, dict]:
        rows = self.connection.execute(
            """
            SELECT c.candidate_id, c.selected_count, c.exported_count,
                   c.last_run_id, c.last_creative_id,
                   EXISTS(
                       SELECT 1 FROM candidate_usage u
                       WHERE u.candidate_id=c.candidate_id AND u.state='reserved'
                   ) AS reserved
            FROM candidates c
            WHERE c.category=? AND c.status IN ('eligible', 'used')
            """,
            (category,),
        ).fetchall()
        return {row["candidate_id"]: dict(row) for row in rows}

    def reserve(self, run_id: str, assignments: dict[str, list[str]]) -> None:
        now = utc_now()
        with self.connection:
            for creative_id, candidate_ids in assignments.items():
                for candidate_id in candidate_ids:
                    inserted = self.connection.execute(
                        """
                        INSERT OR IGNORE INTO candidate_usage (
                            candidate_id, run_id, creative_id, state, created_at, updated_at
                        ) VALUES (?, ?, ?, 'reserved', ?, ?)
                        """,
                        (candidate_id, run_id, creative_id, now, now),
                    )
                    if not inserted.rowcount:
                        continue
                    self.connection.execute(
                        """
                        UPDATE candidates
                        SET selected_count=selected_count+1,
                            last_run_id=?, last_creative_id=?, updated_at=?
                        WHERE candidate_id=?
                        """,
                        (run_id, creative_id, now, candidate_id),
                    )

    def finalize_run(self, run_id: str, state: str) -> int:
        if state not in {"committed", "released"}:
            raise ValueError("state must be committed or released")
        now = utc_now()
        rows = self.connection.execute(
            "SELECT candidate_id FROM candidate_usage WHERE run_id=? AND state='reserved'",
            (run_id,),
        ).fetchall()
        candidate_ids = [row["candidate_id"] for row in rows]
        with self.connection:
            self.connection.execute(
                """
                UPDATE candidate_usage SET state=?, updated_at=?
                WHERE run_id=? AND state='reserved'
                """,
                (state, now, run_id),
            )
            if state == "committed":
                for candidate_id in candidate_ids:
                    self.connection.execute(
                        """
                        UPDATE candidates
                        SET status='used', exported_count=exported_count+1,
                            last_used_at=?, updated_at=?
                        WHERE candidate_id=?
                        """,
                        (now, now, candidate_id),
                    )
        return len(candidate_ids)

    def usage_for_run(self, run_id: str) -> dict[str, list[str]]:
        rows = self.connection.execute(
            """
            SELECT creative_id, candidate_id FROM candidate_usage
            WHERE run_id=? AND state IN ('reserved', 'committed')
            ORDER BY creative_id, candidate_id
            """,
            (run_id,),
        ).fetchall()
        output: dict[str, list[str]] = {}
        for row in rows:
            output.setdefault(row["creative_id"], []).append(row["candidate_id"])
        return output
