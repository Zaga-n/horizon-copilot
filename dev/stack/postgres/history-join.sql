-- Supply a conversation UUID with psql -v conversation_id=... .
-- Feedback remains in app; no rating is copied into telemetry.
SELECT r.conversation_id, r.turn_id, r.user_message_id, r.id AS run_id,
       r.assistant_message_id, r.attempt_number, r.status, r.trace_id,
       f.rating, f.updated_at AS feedback_updated_at
FROM app.agent_runs r
LEFT JOIN app.message_feedback f ON f.assistant_message_id = r.assistant_message_id
WHERE r.conversation_id = :'conversation_id'::uuid
ORDER BY r.started_at, r.attempt_number;
