A2 API audit summary — 8 findings
P1: 3
P2: 4
P3: 1
P4: 0

## Route inventory (88 routes)

| Method | Path | Auth | Inputs | Supabase RPCs | Stripe | Touches credits/profiles/audit_log | Idempotency | Error handling |
|---|---|---|---|---|---|---|---|---|
| GET | `/preview/{task_id}` | public | task_id:str | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/preview/{task_id}/{file_path:path}` | public | task_id:str, file_path:str | none | N | C:N/P:N/A:N | read-only | try |
| GET | `/api/preview/{task_id}` | JWT-required | task_id:str, noqa:B008 ) -> dict[str | none | Y | C:Y/P:Y/A:N | read-only | try/Exception |
| GET | `/api/health` | public | HealthResponse:"""Liveness probe — always returns 200 if the process is running.""" return HealthResponse(status | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/config` | JWT-required | ConfigResponse:"""Return sanitised runtime configuration (API keys are never exposed). VULN-C2-07 fix: Requires authentication to prevent information disclosure of internal paths and deployment details. """ cfg | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/orchestrator-models` | public | none | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/investigations/classify` | JWT-required | body:ClassifyRequest, ClassifyResponse:""" Classify a research topic into a tier (instant / standard / deep) and return estimated duration | none | N | C:Y/P:Y/A:N | none | none |
| POST | `/api/chat/respond` | JWT-required | body:ChatRequest, ChatResponse:""" Primary chat endpoint. The LLM decides how to handle the message: - **Conversation** (greetings | none | N | C:Y/P:N/A:Y | none | try/Exception |
| POST | `/api/conversations` | JWT-required | body:CreateConversationRequest, CreateConversationResponse:cfg | none | N | C:N/P:N/A:N | none | none |
| GET | `/api/conversations` | JWT-required | ConversationListResponse:cfg, 200:logger.error("list_conversations_failed" | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/conversations/{conversation_id}` | JWT-required | conversation_id:str, ConversationDetailResponse:# BUG-P2-04: Validate conversation_id is a valid UUID to avoid 500 from Supabase try: uuid.UUID(conversation_id) except (ValueError | none | N | C:N/P:N/A:N | read-only | try |
| PATCH | `/api/conversations/{conversation_id}` | JWT-required | conversation_id:str, body:UpdateConversationRequest, dict:try: uuid.UUID(conversation_id) except (ValueError | none | N | C:N/P:N/A:N | none | try/pass |
| DELETE | `/api/conversations/{conversation_id}` | JWT-required | conversation_id:str, dict:try: uuid.UUID(conversation_id) except (ValueError | none | N | C:N/P:N/A:N | none | try/pass |
| POST | `/api/conversations/messages` | JWT-required | body:SaveMessageRequest, SaveMessageResponse:"""Persist a single message to a conversation. The frontend calls this to save user, 028:Validate conversation_id as a UUID to avoid confusing # PostgREST 400/500s when a client sends a malformed id. try: uuid.UUID(body.conversation_id) except (ValueError, exc:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try |
| POST | `/api/investigations` | JWT-required | body:StartInvestigationRequest, StartInvestigationResponse:""" Submit a new investigation. Requires a valid Supabase JWT in the Authorization header. If budget_usd or duration_hours are omitted the endpoint classifies the topic automatically and fills in AI-determined values. Writes a ``.task.json`` file to the daemon inbox directory so the background orchestrator picks it up asynchronously. Returns the generated ``task_id`` immediately with a 202 Accepted response. """ cfg, fix:Validate quality_tier before any processing ───────── # Previously an invalid value like "ultra" was silently accepted and # written to .task.json, _VALID_QUALITY_TIERS:frozenset[str], _VALID_QUALITY_TIERS:raise HTTPException( status_code, of:{sorted(_VALID_QUALITY_TIERS)}" ), _MODEL_TO_TIER:dict[str, _MODEL_TO_TIER:body.quality_tier | deduct_credits, add_credits (refund), get_user_tokens | N | C:Y/P:Y/A:N | none | try/Exception/pass |
| GET | `/api/investigations` | JWT-required | page:int, page_size:int, status:str | None, PaginatedTasksResponse:"""List investigations owned by the authenticated user. BUG-S2-11 fix: Previously unauthenticated and returned ALL investigations. Now requires auth and filters by user_id from the JWT. Admin users see all investigations via /api/admin/investigations. """ db, 024:Validate the status filter against known values so callers # get a helpful 400 instead of a silent 0-result response. if status: normalized_status, _VALID_TASK_STATUSES:raise HTTPException( status_code, of:" f"{sorted(_VALID_TASK_STATUSES)}" ), status:total: int, else:total | deduct_credits, add_credits (refund), get_user_tokens | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/investigations/{task_id}` | JWT-required | task_id:str, TaskSummary:"""Retrieve full detail for a single investigation by its task_id. BUG-S2-12 fix: Added auth — only the investigation owner or admin can view. """ # ADV-FIX: Validate UUID format before DB query. try: uuid.UUID(task_id) except (ValueError | none | N | C:N/P:N/A:N | read-only | try |
| POST | `/api/investigations/{task_id}/kill` | JWT-required | task_id:str, KillTaskResponse:""" Request a running investigation to halt. Sets the task status to HALTED and publishes a ``kill:<task_id>`` message on Redis so the orchestrator daemon can detect the signal on its next loop iteration. """ db, 001:reject non-UUID before DB # BUG-S3-01 fix: Verify ownership before allowing kill. row, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try/Exception |
| POST | `/api/investigations/{task_id}/stop` | JWT-required | task_id:str, KillTaskResponse:""" Manually stop a running investigation that is in continuous mode. Sets a Redis key ``stop:{task_id}`` (24 h TTL) that the event loop reads before each continuous-mode restart. Also marks the task status as HALTED so it does not restart on a daemon reload. Unlike ``/kill``, 001:reject non-UUID before DB # Verify ownership row, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try/Exception |
| DELETE | `/api/investigations/{task_id}` | JWT-required | task_id:str, dict:""" Permanently delete an investigation and all associated intelligence data. Only the owner can delete their investigations. Running investigations are killed first before deletion. """ pool, 001:reject non-UUID before DB _log, 1:Use metadata->>'user_id' (canonical owner source) instead of # the top-level user_id column, row:raise HTTPException(status_code | none | N | C:N/P:N/A:Y | none | try/Exception |
| GET | `/api/investigations/{task_id}/branches` | JWT-required | task_id:str | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/investigations/{task_id}/findings` | JWT-required | task_id:str, limit:int, evidence_type:str | None, evidence_type:rows, else:rows | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/investigations/{task_id}/cost` | JWT-required | task_id:str, CostBreakdown:"""Return a detailed cost breakdown for an investigation.""" db, 018:The ``_require_investigation_owner`` dependency has already # confirmed the row exists and is owned by the caller. We fetch only the # columns we need here (budget + total spent) and do not repeat the 404 # guard — doing so produced a race window where the dep passed 200 but # this endpoint returned 404 if the task vanished in between. task_row, None:# Extremely narrow race: row deleted between dep and this query. # Return zeroes rather than 404 so clients don't see inconsistent # status codes from the same request. return CostBreakdown( task_id, 044:model_used may be NULL — use "unknown" as key to avoid JSON serialization error per_model | none | N | C:N/P:N/A:N | read-only | try |
| GET | `/api/investigations/{task_id}/graph` | JWT-required | task_id:str, GraphData:"""Return all graph nodes and edges recorded for a given investigation. Returns 404 if the task does not exist and 403 if the caller is not the task owner. Both checks are handled by the ``_require_investigation_owner`` dependency. """ db | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/investigations/{task_id}/graph` | JWT-required | task_id:str, body:GraphData, GraphData:"""Upsert a batch of nodes and edges into the investigation graph. Both nodes and edges use ``ON CONFLICT (id) DO UPDATE`` semantics so callers can safely re-send the same payload without creating duplicates. The D3-style ``source`` / ``target`` fields on edges are mapped to the ``source_node`` / ``target_node`` DB columns transparently. Returns the full graph state after the upsert (all nodes + edges for the task, 042:cap batch sizes so a malicious or buggy client cannot # flood the graph tables in one request. 500 is generous for a UI batch # but well under Postgres statement / payload limits. _MAX_GRAPH_BATCH, _MAX_GRAPH_BATCH:raise HTTPException( status_code, large:max {_MAX_GRAPH_BATCH} nodes " f"and {_MAX_GRAPH_BATCH} edges per request " f"(got {len(body.nodes)} nodes, 034:before ON CONFLICT overwrites, conn:async with conn.transaction( | none | N | C:N/P:N/A:N | event-id table | none |
| POST | `/api/investigations/{task_id}/stream-token` | JWT-required | task_id:str | none | N | C:N/P:N/A:N | none | none |
| GET | `/api/investigations/{task_id}/logs` | JWT or stream-token | task_id:str, format:str | None, EventSourceResponse:""" Subscribe to live log events for a running investigation. Uses Redis pub/sub on the channel ``logs:<task_id>``. The orchestrator publishes structured JSON log lines there; this endpoint re-broadcasts them as SSE events. Falls back to polling the DB ``task_logs`` table if Redis is unavailable. """ use_legacy, 008:throttle DB status polls in the SSE pub/sub loop so that # each idle subscriber no longer produces ~1 query/second against the # pool. We still send heartbeats every ``timeout`` seconds. last_db_check, None:# ── Redis pub/sub path ────────────────────────────────────── pubsub, logs:{task_id}") try: # BUG-D6-01: Replay fast-path answer if the task completed before # the SSE subscription was established. Quick-tier fast-path events # are emitted via pub/sub (transient) and may be lost if the frontend # connects after the orchestrator finishes. The answer is persisted # in task.metadata["fast_path_answer"] by the orchestrator. _initial_replay_done, None:try: _replay_row | none | N | C:Y/P:N/A:N | read-only | try/Exception/pass |
| GET | `/api/investigations/{task_id}/report` | JWT-required | task_id:str, FileResponse:""" Stream the generated PDF report for a completed investigation. Returns 404 if the investigation does not exist or no PDF has been generated yet. """ db, None:raise HTTPException(status_code, pdf_path:str | None, pdf_path:raise HTTPException( status_code, traversal:ensure the resolved path is under DATA_ROOT. # BUG-API-022: explicitly refuse to follow symlinks on the candidate # path. ``resolve()`` transparently follows symlinks | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/investigations/{task_id}/report/docx` | JWT-required | task_id:str, FileResponse:""" Stream the generated DOCX report for a completed investigation. The DOCX export is not yet implemented in the report generator; this endpoint is reserved for future use and currently returns 404 unless a DOCX path has been set on the task. """ db, None:raise HTTPException(status_code, docx_path:str | None, docx_path:raise HTTPException( status_code, traversal:ensure the resolved path is under DATA_ROOT. # BUG-API-022: explicit symlink rejection for defense in depth — see # the same comment in ``download_report_pdf`` above. cfg | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/investigations/{task_id}/files` | JWT-required | task_id:str, 001:reject non-UUID before DB/Path # Verify user owns the investigation row, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | read-only | try |
| GET | `/api/investigations/{task_id}/files/{filename:path}` | JWT-required | task_id:str, filename:str, FileResponse:"""Download a specific file from an investigation's artifacts.""" cfg, 001:reject non-UUID before DB/Path # Verify user owns the investigation row, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | read-only | try |
| POST | `/api/investigations/{task_id}/upload` | JWT-required | task_id:str, files:Annotated[list[UploadFile], UploadResponse:"""Upload files to an existing investigation. Max 10MB per file, types:.pdf, 001:reject non-UUID before DB/Path # Verify user owns the investigation row, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try |
| POST | `/api/upload` | JWT-required | files:Annotated[list[UploadFile], session_uuid:str | None, UploadResponse:"""Upload files before an investigation exists (pre-submission). Files are saved to a temporary pending directory keyed by a session UUID. When the investigation is created, _UPLOAD_MAX_FILES_PER_INVESTIGATION:raise HTTPException( status_code, 040:Serialize the owner-binding check and file # writes inside a single lock so two concurrent requests cannot both pass # the ".exists()" check and stomp each other's ownership claim. We also # use os.open(O_CREAT|O_EXCL) so the ownership file creation is atomic # even across processes sharing the filesystem. owner_meta, 003:Serialize owner-binding | none | N | C:N/P:N/A:N | none | try |
| GET | `/api/connectors` | JWT-required | fix:Requires authentication. ``available`` is True when the corresponding API key is non-empty. Full health checks (live HTTP probes) are not performed here to keep the endpoint fast; they run asynchronously in the orchestrator. """ cfg, 006:``available`` for Redis only reflects whether the client # object was constructed at startup. If Redis becomes unreachable at # runtime, _redis_reachable:try: await _redis.ping() # type: ignore[union-attr] except Exception as exc: # noqa: BLE001 logger.warning( "redis_ping_failed_but_client_present", connectors:list[ConnectorStatus], topic:str, tier:str) -> ResearchArchitecturePlan: """Build a lightweight research architecture preview for the plan card. This is a deterministic, hypotheses:list[ArchitectureHypothesis], analysis:verifying the core assertions about '{topic[:80]}'", hypothesis:exploring alternative explanations and opposing evidence" | none | N | C:Y/P:Y/A:N | read-only | try/Exception |
| GET | `/api/plans` | public | none | none | N | C:N/P:Y/A:N | read-only | none |
| GET | `/api/billing/usage` | JWT-required | 5:single endpoint the frontend polls to show a usage meter and gate agent-task creation when the user is out of credits. Credits returned as the raw integer balance from Supabase; plan details come from the static ``_PLANS`` list so we don't need a round trip to Stripe. """ cfg, balance:int | None | get_user_tokens | N | C:Y/P:Y/A:N | read-only | try/Exception |
| POST | `/api/billing/create-checkout` | JWT-required | body:CreateCheckoutRequest, CreateCheckoutResponse:""" Create a Stripe Checkout session for the given plan. Looks up the plan by ID, STRIPE_SECRET_KEY:raise HTTPException(status_code, fix:Validate redirect URLs to prevent open-redirect phishing. _ALLOWED_REDIRECT_HOSTS, try:from urllib.parse import urlparse parsed, _ALLOWED_REDIRECT_HOSTS:raise HTTPException( status_code, HTTPException:raise except Exception: raise HTTPException(status_code, None:raise HTTPException(status_code, metadata:dict[str, try:session, exc:# M-02 fix: log the raw Stripe error server-side but return a # generic message to the client so we don't leak internal # configuration / price IDs / Stripe diagnostics. logger.error( "stripe_checkout_failed", 004:Stripe can return null session.url in edge cases if not session.url: raise HTTPException(status_code | none | Y | C:N/P:Y/A:N | none | try/Exception |
| POST | `/api/billing/webhook` | public | STRIPE_SECRET_KEY:raise HTTPException(status_code, fix:Reject webhooks entirely when STRIPE_WEBHOOK_SECRET is # not configured, STRIPE_WEBHOOK_SECRET:logger.error("stripe_webhook_secret_not_configured") raise HTTPException(status_code, try:event, exc:logger.warning("stripe_webhook_signature_invalid", exc:# noqa: BLE001 logger.error("stripe_webhook_parse_failed", event_id:str | None, event_type:str | None, 029:use .get() to avoid KeyError on malformed webhooks if not event_type: raise HTTPException(status_code, event_id:raise HTTPException(status_code, fix:If idempotency check fails due to DB error, try:recorded, exc:# noqa: BLE001 log.error("stripe_idempotency_check_failed", recorded:log.info("stripe_webhook_replay_ignored") return JSONResponse(content, try:if event_type, else:log.info("stripe_webhook_unhandled_event") except HTTPException: # BUG-C1-09 fix: Let 503 from _supabase_add_credits propagate as # 500 so Stripe retries when the credit RPC is down. log.error("stripe_webhook_handler_failed_retriable") return JSONResponse( status_code, exc:# noqa: BLE001 log.error("stripe_webhook_handler_failed", fix:Return 500 so Stripe retries. Idempotency guard # (_record_webhook_event_once) prevents double-processing on retry. # Returning 200 on handler errors silently lost credits. return JSONResponse( status_code | update_profile_by_id, update_profile_by_stripe_customer, grant_credits | Y | C:Y/P:Y/A:N | none | try/Exception |
| GET | `/api/billing/portal` | JWT-required | BillingPortalResponse:""" Generate a Stripe Customer Portal URL for the authenticated user. Requires the user's Stripe customer ID to be stored in the Supabase profile. Fetches it via the Supabase REST API. """ cfg, STRIPE_SECRET_KEY:raise HTTPException(status_code, stripe_customer_id:raise HTTPException( status_code, try:portal_session, exc:# M-02 fix: generic client-facing detail, 004:Stripe can return null portal_session.url if not portal_session.url: raise HTTPException(status_code, session_obj:dict[str, cfg:AppConfig, event_id:str, None:"""Process checkout.session.completed. For subscription mode: link Stripe customer / subscription / plan, mode:skip — handled by payment_intent.succeeded so refunds and metadata are consistent. """ # BUG-API-043: Stripe may return metadata: null; guard with `or {}` _meta, user_id:str | None, plan_id:str | None, kind:str, stripe_customer_id:str | None, subscription_id:str | None, user_id:logger.warning("checkout_completed_no_user_id", period_end:str | None, subscription_id:try: sub, period_end_ts:period_end, exc:# noqa: BLE001 logger.warning("subscription_retrieve_failed", update_payload:dict[str, stripe_customer_id:update_payload["stripe_customer_id"], subscription_id:update_payload["stripe_subscription_id"], plan_id:update_payload["subscription_plan"], period_end:update_payload["subscription_current_period_end"] | get_stripe_customer_id | Y | C:Y/P:Y/A:N | read-only | try/Exception |
| GET | `/api/admin/users` | admin-only | SUPABASE_URL:raise HTTPException(status_code, 048:normalize the header so we never forward # whitespace-only or malformed values. auth_header, client:resp, 200:logger.error("admin_list_users_failed", rows:list[dict[str | admin_list_profiles | Y | C:Y/P:Y/A:N | read-only | none |
| GET | `/api/admin/investigations` | admin-only | page:int, page_size:int, status:str | None, PaginatedTasksResponse:"""List every investigation in the system regardless of owner. Admin only.""" db, 024:validate status filter against known values. if status: normalized_status, _VALID_TASK_STATUSES:raise HTTPException( status_code, of:" f"{sorted(_VALID_TASK_STATUSES)}" ), status:total: int, else:total | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/admin/users/{user_id}/credits` | admin-only | user_id:str, body:AdminSetCreditsRequest, JSONResponse:""" Set the absolute credit balance for a user, SUPABASE_URL:raise HTTPException(status_code, 048:normalize auth header before forwarding. auth_header, client:resp, 200:detail, credits:{detail}") new_balance | admin_set_credits | N | C:Y/P:N/A:N | none | none |
| GET | `/api/admin/stats` | admin-only | AdminStatsResponse:"""Return aggregated system stats for the admin overview.""" db, 025:track availability so the response can signal when # total_users is unreliable rather than defaulting to 0. total_users, SUPABASE_URL:try: # BUG-API-030 / BUG-API-048: normalize auth header before forwarding. auth_header, client:resp, 200:total_users, else:total_users_available, e:total_users_available, else:total_users_available, total_investigations:int, running:int, completed:int, failed:int, total_spent:float, consumed:apply 20% platform markup before converting to credits. # Formula: credits, active_users_30d:int, cfg:AppConfig) -> dict[str, fn:str, payload:dict[str, timeout:float, Any:"""POST to a Supabase RPC function, SUPABASE_URL:raise HTTPException(status_code, client:resp, 200:body, failed:{body}") try: return resp.json() except ValueError: return None async def _admin_rest_request( request: Request, method:str, path:str, params:dict[str, json_body:Any, prefer:str | None, timeout:float, Response:"""Proxy a PostgREST request with caller JWT. RLS ensures admin-only tables refuse non-admin callers. """ cfg, SUPABASE_URL:raise HTTPException(status_code, prefer:headers["Prefer"], client:resp | admin_count_profiles | N | C:Y/P:Y/A:N | read-only | try/Exception |
| GET | `/api/admin/overview` | admin-only | JSONResponse:data | admin_overview_stats | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/admin/audit-log` | admin-only | limit:int, offset:int, action:str | None, JSONResponse:data | admin_audit_list | N | C:N/P:N/A:Y | read-only | none |
| POST | `/api/admin/users/{user_id}/role` | admin-only | user_id:str, body:AdminSetRoleRequest, JSONResponse:await _admin_rpc_call( request | admin_set_role | N | C:N/P:N/A:N | none | none |
| POST | `/api/admin/users/{user_id}/suspend` | admin-only | user_id:str, body:AdminSuspendRequest, JSONResponse:await _admin_rpc_call( request | admin_suspend | N | C:N/P:N/A:N | none | none |
| POST | `/api/admin/users/{user_id}/credits-v2` | admin-only | user_id:str, body:AdminCreditsV2Request, JSONResponse:if body.mode, 0:raise HTTPException(status_code | admin_adjust_credits | N | C:N/P:N/A:N | none | none |
| POST | `/api/admin/system/freeze` | admin-only | body:AdminSystemFreezeRequest, JSONResponse:await _admin_rpc_call( request | admin_system_freeze | N | C:N/P:N/A:N | none | none |
| GET | `/api/admin/tasks` | admin-only | status:str | None, user_id:str | None, limit:int, offset:int, JSONResponse:data | admin_list_tasks | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/admin/admin-tasks` | admin-only | status:str | None, category:str | None, priority:str | None, limit:int, offset:int, JSONResponse:params: dict[str, status:params["status"], category:params["category"], priority:params["priority"], 200:raise HTTPException(status_code, failed:{resp.text[:200]}") return JSONResponse(content | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/admin/admin-tasks` | admin-only | body:AdminAdminTaskUpsert, JSONResponse:payload | none | N | C:N/P:N/A:N | none | none |
| PATCH | `/api/admin/admin-tasks/{task_id}` | admin-only | task_id:str, body:AdminAdminTaskPatch, JSONResponse:try: uuid.UUID(task_id) except ValueError as exc: raise HTTPException(status_code, payload:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try |
| DELETE | `/api/admin/admin-tasks/{task_id}` | admin-only | task_id:str, JSONResponse:try: uuid.UUID(task_id) except ValueError as exc: raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | try |
| GET | `/api/admin/feature-flags` | admin-only | JSONResponse:resp, 200:raise HTTPException(status_code, failed:{resp.text[:200]}") return JSONResponse(content | admin_audit_insert | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/admin/feature-flags` | admin-only | body:AdminFeatureFlagUpsert, JSONResponse:payload | admin_audit_insert | N | C:N/P:N/A:Y | upsert | try/pass |
| DELETE | `/api/admin/feature-flags/{key}` | admin-only | key:str, JSONResponse:resp | none | N | C:N/P:N/A:Y | none | try/pass |
| GET | `/api/admin/usage` | admin-only | days:int, limit:int, JSONResponse:from datetime import timedelta since, 200:# Table may be empty or missing — return empty list rather than 502 logger.warning("admin_usage_rollup_non_200" | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/admin/health-probe` | admin-only | JSONResponse:"""Probe DB, component:{ok: bool, detail:str, latency_ms:float}}. Each probe runs with a short timeout and is isolated so one failure does not mask others. Never raises — always returns 200 with per-component status. """ cfg, results:dict[str, name:str, None:t0, try:detail, exc:# noqa: BLE001 results[name], str:db, str:if _redis is None: raise RuntimeError("redis not initialized") pong, str:if not cfg.SUPABASE_URL: raise RuntimeError("SUPABASE_URL not configured") async with httpx.AsyncClient(timeout, client:r, str:url, http://mariana-browser:8000" ).rstrip("/") async with httpx.AsyncClient(timeout, client:r, str:url, http://mariana-sandbox:8000" ).rstrip("/") async with httpx.AsyncClient(timeout, client:r, str:base, base:raise RuntimeError("LLM_GATEWAY_BASE_URL not configured") async with httpx.AsyncClient(timeout, client:r | none | N | C:N/P:N/A:N | read-only | try/Exception |
| POST | `/api/admin/danger/flush-redis` | admin-only | body:AdminDangerConfirm, JSONResponse:if body.confirm !, None:raise HTTPException(status_code, try:await _redis.flushdb() except Exception as exc: # noqa: BLE001 raise HTTPException(status_code, failed:{exc}") from exc logger.warning("admin_danger_flush_redis", try:await _admin_rpc_call( request, HTTPException:pass return JSONResponse(content | admin_audit_insert | N | C:N/P:N/A:Y | none | try/Exception/pass |
| POST | `/api/admin/danger/halt-running` | admin-only | body:AdminDangerConfirm, JSONResponse:if body.confirm !, try:halted, Exception:pass logger.warning("admin_danger_halt_running", try:await _admin_rpc_call( request, HTTPException:pass return JSONResponse(content | admin_audit_insert | N | C:N/P:N/A:Y | none | try/Exception/pass |
| POST | `/api/shutdown` | admin-key | x_admin_key:Header, ShutdownResponse:""" Initiate a graceful server shutdown. Marks all RUNNING tasks as HALTED in the DB and schedules process termination via ``asyncio``. Requires X-Admin-Key header matching ADMIN_SECRET_KEY config value. Use with care in production. """ # BUG-009 + BUG-S2-03: Require admin key to prevent unauthenticated shutdown. # When ADMIN_SECRET_KEY is not configured, admin_key:raise HTTPException(status_code, fix:constant-time comparison to prevent timing-attack recovery of # the admin key byte-by-byte. if not hmac.compare_digest((x_admin_key or "").encode("utf-8") | none | N | C:N/P:N/A:N | none | try/Exception |
| GET | `/api/memory` | JWT-required | MemoryResponse:"""Retrieve the current user's persistent memory.""" from pathlib import Path as _MemPath # noqa: PLC0415 from mariana.tools.memory import UserMemory # noqa: PLC0415 cfg | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/memory/facts` | JWT-required | body:MemoryFactRequest, noqa:PLC0415 from mariana.tools.memory import UserMemory # noqa: PLC0415 cfg | none | N | C:N/P:N/A:N | none | none |
| POST | `/api/memory/preferences` | JWT-required | body:MemoryPreferenceRequest, noqa:PLC0415 from mariana.tools.memory import UserMemory # noqa: PLC0415 cfg | none | N | C:N/P:N/A:N | none | none |
| DELETE | `/api/memory/facts` | JWT-required | body:DeleteFactRequest, noqa:PLC0415 from mariana.tools.memory import UserMemory # noqa: PLC0415 cfg, found:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | none |
| DELETE | `/api/memory/preferences` | JWT-required | body:DeletePreferenceRequest, noqa:PLC0415 from mariana.tools.memory import UserMemory # noqa: PLC0415 cfg, found:raise HTTPException(status_code | none | N | C:N/P:N/A:N | none | none |
| GET | `/api/skills` | JWT-required | noqa:PLC0415 from mariana.tools.skills import SkillManager # noqa: PLC0415 cfg | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/skills` | JWT-required | body:CreateSkillRequest, SkillResponse:"""Create a custom skill.""" from pathlib import Path as _SkPath # noqa: PLC0415 from mariana.tools.skills import SkillManager # noqa: PLC0415 cfg | none | N | C:N/P:N/A:N | none | none |
| DELETE | `/api/skills/{skill_id}` | JWT-required | skill_id:str, noqa:PLC0415 from mariana.tools.skills import SkillManager # noqa: PLC0415 cfg, ownership:only custom skills owned by the user can be deleted skill, None:raise HTTPException(status_code, 6:Require explicit ownership match; don't allow deletion of # orphaned skills (owner_id | none | N | C:N/P:N/A:N | none | none |
| POST | `/api/feedback` | JWT-required | body:FeedbackRequest, FeedbackResponse:"""Submit feedback for an investigation (rating, noqa:PLC0415 if body.event_type not in ("rating" | none | N | C:N/P:N/A:N | none | try |
| GET | `/api/feedback/{task_id}` | JWT-required | task_id:str, JSONResponse:"""Fetch all feedback events for a specific investigation.""" db, noqa:PLC0415 events | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/learning/insights` | JWT-required | InsightsResponse:"""Fetch all learning insights extracted from the user's investigations.""" db, noqa:PLC0415 insights | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/learning/context` | JWT-required | LearningContextResponse:"""Get the formatted learning context string used for prompt injection.""" db, noqa:PLC0415 context | none | N | C:N/P:N/A:N | read-only | none |
| POST | `/api/learning/extract` | JWT-required | JSONResponse:"""Trigger full pattern extraction across all user's investigations.""" db, noqa:PLC0415 count | none | N | C:N/P:N/A:N | none | none |
| GET | `/api/learning/outcome/{task_id}` | JWT-required | task_id:str, OutcomeResponse:"""Get the automated outcome record for an investigation.""" db, None:raise HTTPException(status_code | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/claims` | JWT-required | task_id:str, JSONResponse:"""Fetch all atomic claims extracted from research findings.""" db, noqa:PLC0415 claims | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/claims/summary` | JWT-required | task_id:str, JSONResponse:"""Fetch summary statistics for the evidence ledger.""" db, noqa:PLC0415 summary | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/source-scores` | JWT-required | task_id:str, JSONResponse:"""Fetch credibility scores for all sources in an investigation.""" db, noqa:PLC0415 scores | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/contradictions` | JWT-required | task_id:str, JSONResponse:"""Fetch detected contradictions between claims.""" db, noqa:PLC0415 matrix | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/hypotheses/rankings` | JWT-required | task_id:str, JSONResponse:"""Fetch Bayesian posterior rankings for all hypotheses.""" db, noqa:PLC0415 rankings | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/gaps` | JWT-required | task_id:str, JSONResponse:"""Fetch the latest gap analysis (missing evidence, noqa:PLC0415 gap, None:return JSONResponse(content | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/temporal` | JWT-required | task_id:str, JSONResponse:"""Fetch temporal coverage and timeline of claims.""" db, noqa:PLC0415 coverage, noqa:PLC0415 timeline, timeline_rows:d | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/perspectives` | JWT-required | task_id:str, JSONResponse:"""Fetch multi-perspective synthesis (bull/bear/skeptic/expert views).""" db, noqa:PLC0415 perspectives, perspectives:if p.get("created_at" | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/audit` | JWT-required | task_id:str, JSONResponse:"""Fetch the latest reasoning chain audit results.""" db, noqa:PLC0415 audit, None:return JSONResponse(content | none | N | C:N/P:N/A:Y | read-only | none |
| GET | `/api/intelligence/{task_id}/executive-summary` | JWT-required | task_id:str, JSONResponse:"""Fetch executive summaries at all compression levels.""" db, noqa:PLC0415 summary, None:return JSONResponse(content | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/diversity` | JWT-required | task_id:str, JSONResponse:"""Fetch source diversity assessment for an investigation.""" db, noqa:PLC0415 result | none | N | C:N/P:N/A:N | read-only | none |
| GET | `/api/intelligence/{task_id}/overview` | JWT-required | task_id:str, JSONResponse:"""Comprehensive intelligence overview: claims, overview:dict, try:from mariana.orchestrator.intelligence.evidence_ledger import get_ledger_summary # noqa: PLC0415 overview["claims"], Exception:overview["claims"], try:from mariana.orchestrator.intelligence.credibility import get_average_credibility # noqa: PLC0415 overview["average_credibility"], Exception:overview["average_credibility"], try:cnt, Exception:overview["contradictions_count"], try:from mariana.orchestrator.intelligence.hypothesis_engine import get_winning_hypothesis # noqa: PLC0415 overview["bayesian_winner"], Exception:overview["bayesian_winner"], try:from mariana.orchestrator.intelligence.gap_detector import get_latest_gap_analysis # noqa: PLC0415 gap, Exception:overview["completeness_score"], try:from mariana.orchestrator.intelligence.auditor import get_latest_audit # noqa: PLC0415 audit, Exception:overview["audit_passed"], try:from mariana.orchestrator.intelligence.executive_summary import get_executive_summary as _get_es # noqa: PLC0415 es, Exception:overview["one_liner"], 037:Register on RequestValidationError (not integer 422) and return # structured field-level errors via exc.errors() instead of str(exc). @app.exception_handler(json.JSONDecodeError) async def json_decode_error_handler( request: Request, exc:json.JSONDecodeError ) -> JSONResponse: """Handle malformed JSON (e.g. Infinity, exc:RequestValidationError ) -> JSONResponse: """Return a structured JSON body for Pydantic validation errors. ADV-FIX: exc.errors() can contain bytes or other non-serializable types (e.g. when body has null bytes or invalid encoding). We sanitize the errors list so JSONResponse never crashes with TypeError. """ import json as _json def _sanitize(obj: object) -> object: if isinstance(obj | none | N | C:N/P:N/A:Y | read-only | try/Exception |

## Focus-area notes

### Auth bypass
- No unsigned-JWT shortcut remains in `api.py`; `_authenticate_supabase_token()` calls Supabase Auth.
- Two admin weaknesses remain: `/api/shutdown` is guarded only by a shared header secret, and `_is_admin_user()` caches positive admin decisions for 30 seconds.

### Input validation
- Most user-facing JSON bodies use Pydantic constraints.
- Notable gaps: `AdminCreditsV2Request.amount` is unconstrained, `CreateCheckoutRequest` uses plain strings instead of URL types, and `AdminAdminTaskPatch` drops all field-level limits.

### Stripe webhook handling
- Live ledger idempotency (`uq_credit_tx_idem`) is present, and webhook credit grants use `grant_credits`.
- The app-level `stripe_webhook_events` table is still written before successful processing, so a transient handler failure is converted into a permanent skip on the next retry.
- Refund / dispute reversal events are still not handled.

### Credit math
- Investigation reservation/refund still uses `deduct_credits` / `add_credits`, both of which mutate `profiles.tokens` directly and bypass `credit_buckets` / `credit_transactions`.
- `spend_credits` exists live but `api.py` never calls it.

### Error swallowing
- `admin_feature_flags_upsert`, `admin_feature_flags_delete`, `admin_danger_flush_redis`, and `admin_danger_halt_running` all ignore audit-log failures after completing the state change.

### PII / secret leak
- I did not find a log statement that emits full Stripe keys, full JWTs, or raw Authorization headers.
- Some logs still emit user IDs, session IDs, and raw Stripe error strings.

### Rate limiting
- All routes get the in-process limiter; I did not find an unauthenticated route that directly calls the paid LLM gateway.
- The limiter is per-process memory only, so multi-worker deployments can exceed configured ceilings.

### CORS / origin
- Main CORS middleware uses an explicit allowlist with `allow_credentials=True`; no wildcard origin is configured there.
- `/preview/*` deliberately overrides response headers with `Access-Control-Allow-Origin: *` for embeddable previews.

### Concurrency on shared state
- `_rate_limit_store` and `_ADMIN_ROLE_CACHE` are mutable module-level dicts with per-process semantics.
- `_upload_locks` is weakref-backed but appears adequate for in-process serialization while a request is alive.

### BUG_AUDIT.md cross-check (BUG-01..16)
- `BUG_AUDIT.md` is mostly about `event_loop.py`, `db.py`, `router.py`, and `report/generator.py`, not the FastAPI surface.
- None of BUG-01..16 appear to remain as `api.py` route bugs; BUG-04 and BUG-11 are explicitly retracted in the source document.

### Credit-RPC trace against live signatures
- `/api/investigations` → `_supabase_deduct_credits(target_user_id, amount)` and `_supabase_add_credits(p_user_id, p_credits)` both match live signatures, but both write only `profiles.tokens`.
- `/api/admin/users/{user_id}/credits` → forwards `target_user_id`, `new_credits`, `is_delta`, which matches live `admin_set_credits(...)`; still non-ledger.
- `/api/admin/users/{user_id}/credits-v2` → forwards `p_caller`, `p_target`, `p_mode`, `p_amount`, `p_reason`, which matches live `admin_adjust_credits(...)`; still non-ledger.
- Stripe webhook grants use `mariana.billing.ledger.grant_credits(...)`, which matches live `grant_credits(...)` and benefits from live `uq_credit_tx_idem` coverage.
- Live `spend_credits(...)` exists but is unused in `api.py`.

## YAML findings
- id: A2-01
  severity: P1
  category: money
  surface: api
  title: Stripe webhook idempotency marks events processed before the business logic succeeds
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 5262-5273, 5301-5317, 6001-6011
      excerpt: |
        recorded = await _record_webhook_event_once(event_id, event_type)
        ...
        if not recorded:
            log.info("stripe_webhook_replay_ignored")
            return JSONResponse(content={"status": "duplicate", "event_id": event_id})
        ...
        except Exception as exc:
            log.error("stripe_webhook_handler_failed", error=str(exc), exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"status": "handler_error", "error": str(exc)},
            )
        ...
        INSERT INTO stripe_webhook_events (event_id, event_type, processed_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (event_id) DO NOTHING
    - reproduction: |
        1. Trigger `/api/billing/webhook` with a valid Stripe event whose handler fails after `_record_webhook_event_once()` succeeds, for example by making `_supabase_patch_profile()` or `_grant_credits_for_event()` raise.
        2. The first request returns 500, but the event row is already inserted into `stripe_webhook_events`.
        3. Replay the same Stripe event ID.
        4. The second request returns `{"status":"duplicate"}` and skips the handler entirely, so the original credit/profile mutation is lost permanently.
  blast_radius: Any transient failure after event insert but before the handler completes turns a retryable Stripe event into a permanently skipped event. That can drop subscription grants, top-up grants, or subscription status sync for paying users under ordinary network or Supabase hiccups.
  proposed_fix: |
    Treat webhook dedupe as a post-success commit, not a pre-handler write. Either (a) store webhook rows with a `processing` / `processed` state and only flip to processed after the handler succeeds, or (b) move dedupe fully into the ledger/profile write path and make the outer table advisory only. Add a regression test that forces a post-insert handler failure and verifies the retry re-runs the business logic.
  fix_type: api_patch
  test_to_add: |
    test_stripe_webhook_retry_after_handler_failure_reprocesses_event — fail after `_record_webhook_event_once()` and verify the next delivery re-enters the handler instead of returning `duplicate`.
  blocking: [none]
  confidence: high

- id: A2-02
  severity: P1
  category: money
  surface: api
  title: Stripe refund and dispute events never reverse previously granted credits
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 5277-5299
      excerpt: |
        if event_type == "checkout.session.completed":
            ...
        elif event_type == "invoice.paid":
            ...
        elif event_type == "payment_intent.succeeded":
            ...
        elif event_type == "customer.subscription.updated":
            ...
        elif event_type == "customer.subscription.deleted":
            ...
        else:
            log.info("stripe_webhook_unhandled_event")
    - file: /home/user/workspace/mariana/mariana/billing/ledger.py
      lines: 152-173
      excerpt: |
        async def refund_credits(
            ...
            return await _rpc(
                supabase_url,
                service_key,
                "refund_credits",
                {
                    "p_user_id": user_id,
                    "p_credits": credits,
                    "p_ref_type": ref_type,
                    "p_ref_id": ref_id,
                },
            )
    - reproduction: |
        1. Buy a top-up or subscription renewal so `grant_credits` runs successfully.
        2. Send a later Stripe refund/dispute event such as `charge.refunded` or `charge.dispute.created` for that same payment.
        3. The webhook falls into `stripe_webhook_unhandled_event` and returns 200.
        4. No call is made to `refund_credits`, `admin_adjust_credits`, or any equivalent reversal path, so credits remain spendable after the payment was reversed.
  blast_radius: Refunded or disputed purchases can leave credits in user accounts indefinitely, creating direct money leakage. The issue affects both one-time top-ups and any subscription-related credit grants that later need clawback.
  proposed_fix: |
    Add explicit refund/dispute handlers that map Stripe payment objects back to the original `ref_id`, then call a ledger reversal primitive (`refund_credits` or a dedicated clawback RPC) idempotently. The reversal path must use the same reference IDs so repeated refund webhooks collapse safely.
  fix_type: api_patch
  test_to_add: |
    test_stripe_refund_event_reverses_prior_grant — grant credits, deliver a refund/dispute event, and assert balance plus ledger rows are reversed exactly once.
  blocking: [A2-01]
  confidence: high

- id: A2-03
  severity: P1
  category: integrity
  surface: api
  title: Investigation reservation and refund paths still mutate profiles.tokens directly instead of using the credit ledger
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 2677-2699, 2875-2912
      excerpt: |
        reserved = await _supabase_deduct_credits(current_user["user_id"], estimated_credits_needed, cfg)
        ...
        if reserved_credits > 0:
            try:
                await _supabase_add_credits(current_user["user_id"], reserved_credits, cfg)
            except Exception as refund_err:
                logger.error(
                    "refund_after_http_exception_failed",
                    user_id=current_user["user_id"],
                    amount=reserved_credits,
                    error=str(refund_err),
                )
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 5816-5867, 5906-5987
      excerpt: |
        rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/add_credits"
        ...
        json={"p_user_id": user_id, "p_credits": credits}
        ...
        rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/deduct_credits"
        ...
        json={"target_user_id": user_id, "amount": amount}
    - file: pg_catalog
      lines: select proname, pg_get_functiondef(...) from public.add_credits/public.deduct_credits
      excerpt: |
        CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
        ...
          UPDATE profiles
          SET tokens = tokens + p_credits,
              updated_at = now()
        ...
        CREATE OR REPLACE FUNCTION public.deduct_credits(target_user_id uuid, amount integer)
        ...
          UPDATE profiles
          SET tokens = new_balance, updated_at = now()
    - reproduction: |
        1. Start an investigation so `/api/investigations` reserves credits.
        2. Force a later failure in task creation, inbox write, or conversation linking.
        3. The reserve/refund path changes `profiles.tokens`, but no `credit_transactions` or `credit_buckets` entry is created because `spend_credits` and `refund_credits` are never called.
        4. Repeat under concurrency and the displayed token balance can diverge from the live ledger-backed balance used elsewhere.
  blast_radius: This is the open R3/R6 drift problem in the API surface. Every investigation submission and refund path can move the legacy `profiles.tokens` counter without producing corresponding ledger rows, making balances unreconcilable and undermining expiry, FIFO spending, and financial auditability.
  proposed_fix: |
    Replace investigation reservation/refund with ledger-native operations: `spend_credits` for reservation, `refund_credits` for rollback, and a single derived balance source for all UI reads. After the migration, treat `profiles.tokens` as deprecated or derived-only and add reconciliation tests that fail if a reservation changes one store without the other.
  fix_type: api_patch
  test_to_add: |
    test_start_investigation_reservation_writes_ledger_not_profiles_only — submit, fail, and assert `credit_transactions`/`credit_buckets` reflect the reserve/refund sequence without orphan `profiles.tokens` drift.
  blocking: [none]
  confidence: high

- id: A2-04
  severity: P2
  category: integrity
  surface: api
  title: Legacy admin credits endpoint keeps a direct-token mutation path alive alongside the newer v2 endpoint
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 6184-6238, 6487-6514
      excerpt: |
        async def admin_set_credits(...):
            url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_set_credits"
            payload = {
                "target_user_id": user_id,
                "new_credits": body.credits,
                "is_delta": body.delta,
            }
        ...
        async def admin_user_credits_v2(...):
            new_balance = await _admin_rpc_call(
                request,
                "admin_adjust_credits",
                {
                    "p_caller": caller["user_id"],
                    "p_target": user_id,
                    "p_mode": body.mode,
                    "p_amount": body.amount,
                    "p_reason": body.reason,
                },
            )
    - file: pg_catalog
      lines: select proname, pg_get_functiondef(...) from public.admin_set_credits/public.admin_adjust_credits
      excerpt: |
        CREATE OR REPLACE FUNCTION public.admin_set_credits(target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false)
        ...
          UPDATE public.profiles
             SET tokens = v_final,
                 updated_at = now()
        ...
        CREATE OR REPLACE FUNCTION public.admin_adjust_credits(p_caller uuid, p_target uuid, p_mode text, p_amount integer, p_reason text DEFAULT NULL::text)
        ...
          UPDATE public.profiles SET tokens = v_new, updated_at = NOW() WHERE id = p_target;
    - reproduction: |
        1. Call either admin credit endpoint to add, subtract, or set balance.
        2. The request matches the live RPC signatures correctly and succeeds.
        3. Only `profiles.tokens` is changed; no `credit_buckets` or `credit_transactions` write is produced.
        4. Ledger-derived balance and admin-adjusted balance can diverge until a later reconciliation job repairs it.
  blast_radius: Admin support tooling can still create immediate ledger drift even after the app added a newer `/credits-v2` route. Because admins are the break-glass path for fixing balances, keeping both routes non-ledger-backed makes manual corrections harder to trust and harder to audit financially.
  proposed_fix: |
    Retire `/api/admin/users/{user_id}/credits` entirely or make it a compatibility wrapper around a ledger-native admin adjustment path. Both admin credit routes should produce explicit ledger events (`admin_grant`, `refund`, or a dedicated adjustment type) and only then refresh any cached balance column.
  fix_type: api_patch
  test_to_add: |
    test_admin_credit_adjustment_writes_ledger_rows — call both admin credit endpoints and assert the resulting state includes durable ledger transactions, not just a `profiles.tokens` update.
  blocking: [A2-03]
  confidence: high

- id: A2-05
  severity: P2
  category: integrity
  surface: api
  title: Multiple admin mutation routes ignore audit-log failures after the state change already succeeded
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 6702-6720, 6739-6756, 6912-6929, 6952-6969
      excerpt: |
        try:
            await _admin_rpc_call(... "admin_audit_insert", ...)
        except HTTPException:
            pass
        ...
        try:
            await _admin_rpc_call(... "admin_audit_insert", ...)
        except HTTPException:
            pass
    - reproduction: |
        1. Make the underlying feature-flag or danger-zone request succeed.
        2. Force `admin_audit_insert` to fail, for example by revoking the RPC grant or returning a 5xx from PostgREST.
        3. The endpoint still returns success because the `except HTTPException: pass` block suppresses the audit failure.
        4. The state change lands with no audit-log row.
  blast_radius: Feature-flag changes and dangerous admin operations can become unaudited during exactly the kinds of partial outages where operators most need a reliable audit trail. The state mutation succeeds, but the forensic evidence disappears.
  proposed_fix: |
    Treat audit insertion as part of the same success contract for admin mutations. Either fail the whole request when audit logging fails, or move the mutation plus audit write into one database RPC/transaction so they cannot diverge.
  fix_type: api_patch
  test_to_add: |
    test_admin_mutation_fails_when_audit_insert_fails — simulate `admin_audit_insert` failure and assert the endpoint does not return 200 with the change applied.
  blocking: [none]
  confidence: high

- id: A2-06
  severity: P2
  category: availability
  surface: api
  title: Shutdown route bypasses normal JWT admin checks and is controlled only by a shared header secret
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 6978-7019
      excerpt: |
        @app.post("/api/shutdown", ...)
        async def graceful_shutdown(
            x_admin_key: str | None = Header(None),
        ) -> ShutdownResponse:
            ...
            if not admin_key:
                raise HTTPException(status_code=403, detail="Shutdown endpoint disabled ...")
            if not hmac.compare_digest((x_admin_key or "").encode("utf-8"), admin_key.encode("utf-8")):
                raise HTTPException(status_code=401, detail="Unauthorized")
            ...
            asyncio.get_running_loop().call_later(3.0, _exit_process)
    - reproduction: |
        1. Send `POST /api/shutdown` with no JWT at all.
        2. Include a valid `X-Admin-Key` header.
        3. The request is accepted and schedules process termination.
        4. No user identity or RBAC role is required, and no audit-log row is written.
  blast_radius: Anyone who learns the shared shutdown key can halt the service from outside the normal auth model. Because the route is not tied to a user identity, it also weakens attribution and operational auditability for one of the highest-impact admin actions.
  proposed_fix: |
    Move shutdown behind the standard admin JWT path, or restrict it to an internal-only network surface. If a secret header is kept as a second factor, require both admin JWT and the secret, and write an audit-log row before scheduling process exit.
  fix_type: api_patch
  test_to_add: |
    test_shutdown_requires_authenticated_admin_identity — verify that a correct header without an admin JWT is rejected and that successful shutdown requests emit an audit record.
  blocking: [none]
  confidence: high

- id: A2-07
  severity: P2
  category: security
  surface: api
  title: Admin authorization cache keeps positive admin decisions alive for up to 30 seconds after role revocation
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 143-194, 1425-1431
      excerpt: |
        _ADMIN_ROLE_CACHE: dict[str, tuple[float, bool]] = {}
        _ADMIN_ROLE_CACHE_TTL = 30.0
        ...
        cached = _ADMIN_ROLE_CACHE.get(user_id)
        if cached and now - cached[0] < _ADMIN_ROLE_CACHE_TTL:
            return cached[1]
        ...
        _ADMIN_ROLE_CACHE[user_id] = (now, is_admin)
        ...
        async def _require_admin(...):
            if not _is_admin_user(current_user["user_id"]):
                raise HTTPException(status_code=403, detail="Admin access required")
    - reproduction: |
        1. Authenticate as an admin and hit any admin endpoint to populate `_ADMIN_ROLE_CACHE` with `True`.
        2. Revoke that user's admin role in `profiles`.
        3. Reuse the same token within 30 seconds.
        4. `_require_admin()` still accepts the request because it reuses the cached positive result.
  blast_radius: This is a short but real auth-bypass window after demotion, affecting every route that relies on `_require_admin()` or admin ownership overrides. During incident response or privilege revocation, the stale cache delays enforcement exactly when immediate lockout matters most.
  proposed_fix: |
    Avoid caching positive admin decisions for security-sensitive paths, or add an explicit invalidation path when roles change. At minimum, reduce the TTL sharply and restrict caching to low-risk read endpoints rather than destructive admin mutations.
  fix_type: api_patch
  test_to_add: |
    test_admin_revocation_takes_effect_immediately — populate the cache, revoke admin role, and assert the next admin request is denied without waiting for TTL expiry.
  blocking: [none]
  confidence: high

- id: A2-08
  severity: P3
  category: correctness
  surface: api
  title: Billing usage endpoint always falls back to free-plan metadata because auth context never includes subscription fields
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: 1190-1196, 5075-5077
      excerpt: |
        user_id: str | None = payload.get("id") or payload.get("sub")
        ...
        role: str = payload.get("role") or app_metadata.get("role") or "authenticated"
        return {"user_id": user_id, "role": role}
        ...
        plan_slug = (current_user.get("subscription_plan") or "free").lower()
        plan_status = current_user.get("subscription_status") or "none"
        matched = next((p for p in _PLANS if p["id"] == plan_slug), None)
    - reproduction: |
        1. Authenticate as a paid user with `profiles.subscription_plan` and `profiles.subscription_status` populated.
        2. Call `GET /api/billing/usage`.
        3. Because `_authenticate_supabase_token()` returns only `user_id` and `role`, `current_user` has no subscription fields.
        4. The endpoint reports `free` / `none` plan metadata even when the token owner is paid.
  blast_radius: Paid users can see incorrect plan information, incorrect credit meter baselines, and misleading upgrade prompts. The balance lookup itself may work, but the surrounding plan/status metadata is systematically wrong.
  proposed_fix: |
    Either enrich `_authenticate_supabase_token()` with the plan/status claims the endpoint expects, or have `/api/billing/usage` read those fields from `profiles` directly instead of assuming they are present in the auth context.
  fix_type: api_patch
  test_to_add: |
    test_billing_usage_uses_actual_profile_plan_status — seed a paid profile, call the endpoint, and assert the returned `plan.id` and `subscription_status` match the profile rather than falling back to `free`.
  blocking: [none]
  confidence: high
