"""Project activity from durable records; reading never changes work state."""

from __future__ import annotations

from .workbench_store import WorkbenchError


def project_activity(store, project_id: str, limit: int = 100) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WorkbenchError("invalid_field", "日志条数需要在 1 到 100 之间。")
    with store._connection() as db:
        store._required(db, "projects", project_id)
        rows = db.execute(
            """
            WITH activity AS (
              SELECT e.id, e.created_at, 'task_' || e.type AS type,
                     t.title, CASE
                       WHEN e.type='done' THEN 'human'
                       WHEN e.type='resources_pinned' THEN 'system'
                       WHEN e.type='queued' THEN COALESCE(
                         'employee:' || (SELECT sender_id FROM messages WHERE id=t.request_message_id), 'human')
                       ELSE 'employee:' || t.assignee_id END AS actor,
                     e.message AS summary, t.id AS task_id, NULL AS message_id
              FROM events e JOIN tasks t ON t.id=e.task_id
              WHERE t.project_id=? AND e.type NOT IN ('event','output')
              UNION ALL
              SELECT id,created_at,'message',title,
                     COALESCE('employee:' || sender_id,'human'),substr(body,1,500),
                     task_id,id
              FROM messages WHERE project_id=?
              UNION ALL
              SELECT id,created_at,'note',title,source,substr(body,1,500),NULL,NULL
              FROM memories WHERE project_id=?
              UNION ALL
              SELECT id,created_at,type,
                     COALESCE((SELECT name FROM resources WHERE id=json_extract(governance_events.payload,'$.resource_id')),type),
                     actor,COALESCE(NULLIF(reason,''),json_extract(payload,'$.summary'),''),task_id,NULL
              FROM governance_events WHERE project_id=? AND type!='task_dispatched'
            )
            SELECT * FROM activity ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            (project_id, project_id, project_id, project_id, limit + 1),
        ).fetchall()
        events = [dict(row) for row in rows[:limit]]
        # Actor ids establish attribution; expose labels without inventing
        # verified internal authorship or leaking membership credentials.
        for event in events:
            if event["actor"].startswith("employee:"):
                person = db.execute(
                    "SELECT name FROM employees WHERE id=?", (event["actor"][9:],)
                ).fetchone()
                event["actor_name"] = person[0] if person else event["actor"]
            else:
                event["actor_name"] = event["actor"]
        return store._scrub(db, {"events": events, "limit": limit, "has_older": len(rows) > limit})
