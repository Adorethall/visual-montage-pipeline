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

CREATE TABLE IF NOT EXISTS video_analysis_cache (
    cache_key TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    video_path TEXT NOT NULL,
    category TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    model_id TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_video ON video_analysis_cache(video_id);
CREATE INDEX IF NOT EXISTS idx_analysis_category ON video_analysis_cache(category);

CREATE TABLE IF NOT EXISTS bgm_assets (
    bgm_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source_video_id TEXT NOT NULL,
    source_video_path TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    audio_fingerprint TEXT NOT NULL,
    audio_type TEXT NOT NULL,
    selected_audio_path TEXT NOT NULL,
    use_source TEXT NOT NULL,
    best_start REAL NOT NULL,
    best_end REAL NOT NULL,
    bpm REAL NOT NULL,
    music_score REAL NOT NULL,
    speech_risk REAL NOT NULL,
    singing_probability REAL NOT NULL,
    separation_quality REAL NOT NULL,
    eligible INTEGER NOT NULL,
    status TEXT NOT NULL,
    selected_count INTEGER NOT NULL DEFAULT 0,
    exported_count INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    last_creative_id TEXT,
    last_used_at TEXT,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_video_id, source_fingerprint, configuration_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_bgm_category ON bgm_assets(category);
CREATE INDEX IF NOT EXISTS idx_bgm_fingerprint ON bgm_assets(audio_fingerprint);

CREATE TABLE IF NOT EXISTS bgm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bgm_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(bgm_id, run_id, creative_id)
);
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

    def finalize_creative(self, run_id: str, creative_id: str, state: str) -> int:
        if state not in {"committed", "released"}:
            raise ValueError("state must be committed or released")
        now = utc_now()
        rows = self.connection.execute(
            """
            SELECT candidate_id FROM candidate_usage
            WHERE run_id=? AND creative_id=? AND state='reserved'
            """,
            (run_id, creative_id),
        ).fetchall()
        candidate_ids = [row["candidate_id"] for row in rows]
        with self.connection:
            self.connection.execute(
                """
                UPDATE candidate_usage SET state=?, updated_at=?
                WHERE run_id=? AND creative_id=? AND state='reserved'
                """,
                (state, now, run_id, creative_id),
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

    def get_analysis_cache(self, cache_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload_json FROM video_analysis_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        self.connection.execute(
            """
            UPDATE video_analysis_cache SET last_accessed_at=?
            WHERE cache_key=?
            """,
            (utc_now(), cache_key),
        )
        self.connection.commit()
        return json.loads(row["payload_json"])

    def put_analysis_cache(
        self,
        *,
        cache_key: str,
        video_id: str,
        video_path: str,
        category: str,
        source_fingerprint: str,
        configuration_fingerprint: str,
        model_id: str,
        payload: dict,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO video_analysis_cache (
                cache_key, video_id, video_path, category, source_fingerprint,
                configuration_fingerprint, model_id, candidate_count,
                payload_json, analyzed_at, last_accessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                video_path=excluded.video_path,
                candidate_count=excluded.candidate_count,
                payload_json=excluded.payload_json,
                analyzed_at=excluded.analyzed_at,
                last_accessed_at=excluded.last_accessed_at
            """,
            (
                cache_key,
                video_id,
                video_path,
                category,
                source_fingerprint,
                configuration_fingerprint,
                model_id,
                len(payload.get("candidates") or []),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        self.connection.commit()

    def get_bgm_for_source(
        self,
        source_video_id: str,
        source_fingerprint: str,
        configuration_fingerprint: str,
    ) -> dict | None:
        row = self.connection.execute(
            """
            SELECT analysis_json FROM bgm_assets
            WHERE source_video_id=? AND source_fingerprint=?
              AND configuration_fingerprint=?
            """,
            (source_video_id, source_fingerprint, configuration_fingerprint),
        ).fetchone()
        return json.loads(row["analysis_json"]) if row else None

    def upsert_bgm(self, payload: dict) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO bgm_assets (
                bgm_id, category, source_video_id, source_video_path,
                source_fingerprint, configuration_fingerprint, audio_fingerprint,
                audio_type, selected_audio_path, use_source, best_start, best_end,
                bpm, music_score, speech_risk, singing_probability,
                separation_quality, eligible, status, analysis_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bgm_id) DO UPDATE SET
                selected_audio_path=excluded.selected_audio_path,
                audio_type=excluded.audio_type,
                music_score=excluded.music_score,
                speech_risk=excluded.speech_risk,
                singing_probability=excluded.singing_probability,
                separation_quality=excluded.separation_quality,
                eligible=excluded.eligible,
                status=excluded.status,
                analysis_json=excluded.analysis_json,
                updated_at=excluded.updated_at
            """,
            (
                payload["bgm_id"],
                payload["category"],
                payload["source_video_id"],
                payload["source_video_path"],
                payload["source_fingerprint"],
                payload["configuration_fingerprint"],
                payload["audio_fingerprint"],
                payload["audio_type"],
                payload.get("selected_audio_path", ""),
                payload.get("use_source", "none"),
                float(payload.get("best_window", {}).get("start", 0)),
                float(payload.get("best_window", {}).get("end", 0)),
                float(payload.get("bpm", 0)),
                float(payload.get("music_score", 0)),
                float(payload.get("speech_risk", 1)),
                float(payload.get("singing_probability", 0)),
                float(payload.get("separation_quality", 0)),
                int(bool(payload.get("eligible_as_bgm"))),
                str(payload.get("status", "analyzed")),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        self.connection.commit()

    def select_bgms(
        self,
        category: str,
        count: int,
        *,
        minimum_score: float,
        maximum_speech_risk: float,
        same_bgm_max_per_batch: int,
        target_bpm: float,
        minimum_duration_seconds: float = 5.0,
    ) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT *, (
                music_score - exported_count * 0.15
            ) AS adjusted_score
            FROM bgm_assets
            WHERE category=? AND eligible=1
              AND music_score>=? AND speech_risk<=?
            ORDER BY adjusted_score DESC, updated_at DESC
            """,
            (category, minimum_score, maximum_speech_risk),
        ).fetchall()
        def bpm_distance(row) -> float:
            bpm = float(row["bpm"])
            return min(
                abs(bpm - target_bpm),
                abs(bpm * 2 - target_bpm),
                abs(bpm / 2 - target_bpm),
            )

        ranked_rows = sorted(
            rows,
            key=lambda row: (
                -(
                    float(row["adjusted_score"])
                    - min(0.3, bpm_distance(row) / 100)
                ),
                -float(row["music_score"]),
            ),
        )
        unique = []
        seen_fingerprints = set()
        for row in ranked_rows:
            analysis = json.loads(row["analysis_json"])
            duration = float(
                analysis.get("duration_seconds")
                or (
                    float((analysis.get("best_window") or {}).get("end", 0))
                    - float((analysis.get("best_window") or {}).get("start", 0))
                )
            )
            if duration < minimum_duration_seconds:
                continue
            if row["audio_fingerprint"] in seen_fingerprints:
                continue
            seen_fingerprints.add(row["audio_fingerprint"])
            unique.append(analysis)
        if not unique:
            return []
        output = []
        index = 0
        while len(output) < count:
            bgm = unique[index % len(unique)]
            uses = sum(item["bgm_id"] == bgm["bgm_id"] for item in output)
            if uses < same_bgm_max_per_batch:
                output.append(bgm)
            index += 1
            if index > count * max(2, len(unique)) * 2:
                break
        return output

    def reserve_bgm(self, bgm_id: str, run_id: str, creative_id: str) -> None:
        now = utc_now()
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO bgm_usage (
                    bgm_id, run_id, creative_id, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?, ?)
                """,
                (bgm_id, run_id, creative_id, now, now),
            )
            if inserted.rowcount:
                self.connection.execute(
                    """
                    UPDATE bgm_assets SET selected_count=selected_count+1,
                        last_run_id=?, last_creative_id=?, updated_at=?
                    WHERE bgm_id=?
                    """,
                    (run_id, creative_id, now, bgm_id),
                )

    def finalize_bgm(
        self, bgm_id: str, run_id: str, creative_id: str, state: str
    ) -> None:
        if state not in {"committed", "released"}:
            raise ValueError("invalid BGM usage state")
        now = utc_now()
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE bgm_usage SET state=?, updated_at=?
                WHERE bgm_id=? AND run_id=? AND creative_id=? AND state='reserved'
                """,
                (state, now, bgm_id, run_id, creative_id),
            )
            if state == "committed" and changed.rowcount:
                self.connection.execute(
                    """
                    UPDATE bgm_assets SET exported_count=exported_count+1,
                        last_used_at=?, updated_at=? WHERE bgm_id=?
                    """,
                    (now, now, bgm_id),
                )
