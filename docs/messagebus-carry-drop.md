# The quarantined broker suites, assertion by assertion (Q-0023 -> D-0028)

Issue `#19` deliverable. Five test files were quarantined by the v1 purge
(`PORTING_LEDGER.md`, purge rows 408-413): their assertions are kept verbatim
but never run, because the module they drive -- `broker/server.py` -- was
deleted. The ledger classified them at file granularity and named individual
carve-outs in prose; per the operator direction of 2026-08-21 on Q-0023 (Issue
`#19`) and D-0028, this table takes that to assertion granularity: every test
function in the five files, with its disposition under the new S7/S8 delivery
model. There is no bulk retarget: what carries is landed as a specification
against the new contract (passing where the contract satisfies it, failing --
`xfail(strict=True)` -- where it does not yet), and what drops is recorded
with the mechanism it drops with.

The quarantined files themselves are left untouched, still importorskip-
guarded at their ledgered paths: this table supersedes their *disposition*,
not their text, and D-0028 records the decision that none of them may be
revived by resurrecting `broker/server.py` to drive them.

## Legend

| Disposition | Meaning |
|---|---|
| `carried-by-s7` | The invariant is already pinned by the S7 outbox suite; the named `tests/control_plane/test_outbox.py` test is its successor. |
| `carried-by-s8` | The invariant is re-pinned by the S8 MessageBus suite; the named `tests/messagebus/` test is its successor. |
| `failing-spec` | The invariant is delivery semantics the new contract should satisfy but does not yet; landed failing (`xfail(strict=True)`) at the named test, per Q-0015/D-0028. |
| `carried-deferred` | The invariant carries per the ledger, but its successor surface is not the MessageBus and is not built yet; it stays quarantined, and the named surface is where it re-lands. |
| `superseded` | S7 took a deliberate contrary design decision; the named S7 evidence records it. |
| `dropped-with-pane` | Pins pane/tmux/adapter/spawn/screen behaviour; discarded with the pane transport (D-0009, D-0014). |
| `dropped-with-http-transport` | Pins the v1 HTTP daemon / delivery-credential / generation / instance / observer-lease / adopt machinery; discarded with that transport. Write exclusivity is the lease epoch now, and there is no competing-sidecar concept to arbitrate. |

## Totals

| Disposition | Count |
|---|---|
| `carried-by-s7` | 2 |
| `carried-by-s8` | 11 |
| `failing-spec` | 1 |
| `carried-deferred` | 11 |
| `superseded` | 3 |
| `dropped-with-pane` | 35 |
| `dropped-with-http-transport` | 138 |
| **Total** | **201** |

## `tests/broker/test_delivery.py`

Ledger class: **carry (invariant) / rewrite (mechanism)** (PORTING_LEDGER.md:219). 118 test functions: 76 dropped-with-http-transport, 31 dropped-with-pane, 6 carried-by-s8, 3 superseded, 2 carried-by-s7.

| Test | Disposition | Successor / goes with | Rationale |
|---|---|---|---|
| `test_claim_then_confirm_lifecycle` (:88) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_pull_then_ack_walks_the_claim_then_confirm_states | pending->CLAIMED->confirmed becomes pending->delivered->acked, driven by the recipient; the double-confirm idempotency half is also pinned by S7 (test_a_duplicate_ack_changes_nothing). |
| `test_check_messages_respects_live_claim` (:108) | `dropped-with-http-transport` | CLAIMED-state push/pull dual-path (poll_claims lease vs check_messages fallback) | Pins the v1 dual-path push-claim-vs-pull-fallback race, which has no analogue once MessageBus is the single pull contract. |
| `test_check_messages_drains_unclaimed` (:120) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_settled_message_is_never_presented_again | Mirrors test_store.py's fixed-point test_drain_is_at_most_once=carried-s8: at-most-once pull-drain is exactly the S8 poll/ack contract. |
| `test_lease_reap_recovers_dead_sidecar` (:131) | `carried-by-s7` | test_rows_orphaned_by_a_dead_epoch_are_adopted_by_recovery | Transport-neutral invariant (dead claimant does not lose the message, it is recovered) is exactly outbox.recover()'s adoption of orphaned rows. |
| `test_confirm_after_lease_expiry_rejected` (:148) | `superseded` | test_an_ack_is_recorded_even_after_the_writers_lease_moved_on | Rejecting a late confirm on lease expiry is the fenced-confirm design S7 deliberately replaced with an unfenced record_ack. |
| `test_mode_epoch_fencing_rejects_stale_confirm` (:161) | `superseded` | test_an_ack_is_recorded_even_after_the_writers_lease_moved_on | Mode-epoch fencing of confirms is precisely the fenced-ack design S7's deliberately-unfenced record_ack supersedes. |
| `test_stale_confirm_does_not_strip_newer_claim` (:176) | `superseded` | test_an_ack_is_recorded_even_after_the_writers_lease_moved_on | Depends on the same epoch-fenced-confirm state machine S7 replaced; there is no CLAIMED row to strip in the unfenced-ack design. |
| `test_pull_mode_disables_claim_issuance` (:200) | `dropped-with-http-transport` | PUSH/PULL flip_mode toggle | PUSH-vs-PULL delivery mode is v1 broker/server.py machinery with no counterpart in the single pull-contract MessageBus. |
| `test_poll_claims_gated_on_registered_owner` (:213) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_message_is_sent_only_to_a_registered_recipient | Mirrors test_store.py's fixed-point test_enqueue_only_to_registered=carried-s8: messages to an unregistered recipient are neither lost nor misdelivered. |
| `test_poll_claims_only_returns_owner_rows` (:238) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_poll_returns_only_the_polling_recipients_rows | Per-recipient isolation on poll is a core S8 pull-contract invariant, not HTTP-credential machinery. |
| `test_confirm_not_owner_rejected` (:250) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_an_ack_from_the_wrong_recipient_is_refused | Ack authorization scoped to the recipient identity is a transport-neutral part of the poll/ack lifecycle. |
| `test_revoked_delivery_cred_cannot_claim_or_confirm` (:266) | `dropped-with-http-transport` | delivery-scoped bearer credential revocation | Pins the HTTP delivery-credential/revocation mechanism; MessageBus's recipient set has no bearer-credential concept. |
| `test_flip_mode_invalid` (:287) | `dropped-with-http-transport` | flip_mode PUSH/PULL validation | Validates an API for the discarded PUSH/PULL mode toggle, which does not exist in the new contract. |
| `test_register_bumps_generation_monotonically` (:294) | `dropped-with-http-transport` | delivery-instance generation counter | Generation counters exist to arbitrate competing sidecars over a push transport; S8 has no competing-sidecar concept. |
| `test_register_requires_delivery_scope` (:307) | `dropped-with-http-transport` | delivery-cred scope enforcement on register | Bearer-token scope gating on the register RPC is HTTP delivery-credential machinery. |
| `test_claim_owner_rejects_full_token_over_http` (:325) | `dropped-with-http-transport` | /claim-owner scope enforcement | Exercises the discarded HTTP /claim-owner endpoint's token-scope check. |
| `test_old_generation_poll_rejected` (:336) | `dropped-with-http-transport` | generation-fenced poll rejection | Fork/replay generation fencing for competing push sidecars has no S8 analogue. |
| `test_old_generation_confirm_rejected` (:355) | `dropped-with-http-transport` | generation-fenced confirm rejection | Same generation-war fencing mechanism as the poll case, tied to the discarded push transport. |
| `test_stale_instance_cannot_replay_current_generation` (:382) | `dropped-with-http-transport` | instance_id replay fencing | Guards against a stale sidecar replaying a leaked generation number, a push-transport-specific attack surface. |
| `test_register_requeues_old_generation_claim` (:410) | `dropped-with-http-transport` | immediate requeue of old-generation CLAIMED rows on register | The requeue-on-register trigger is part of the generation-war handover mechanism, not a transport-neutral recovery invariant. |
| `test_duplicate_sidecar_detected_journaled` (:425) | `dropped-with-http-transport` | duplicate_sidecar_detected journal event | Detects two competing push sidecars polling the same owner, a condition that cannot occur in the pull-only MessageBus. |
| `test_single_sidecar_never_flags_duplicate` (:446) | `dropped-with-http-transport` | duplicate-sidecar false-positive guard | Negative case for the same duplicate-sidecar detection machinery. |
| `test_duplicate_detection_cooldown_reemit_and_distinct_pairs` (:460) | `dropped-with-http-transport` | duplicate-sidecar cooldown/re-emit logic | Anti-spam cadence logic for the duplicate-sidecar journal event, tied to the discarded push transport. |
| `test_delivery_dump_exposes_generation_and_instance` (:489) | `dropped-with-http-transport` | delivery_dump generation/instance fields | Admin-diagnostic surface over the generation/instance bookkeeping that does not exist in S8. |
| `test_reset_delivery_state_clears_fencing` (:500) | `dropped-with-http-transport` | reset_delivery_state generation/instance clearing | Clears bookkeeping for the generation-fencing mechanism itself, which has no S8 counterpart. |
| `test_double_sidecar_over_http_only_current_claims` (:517) | `dropped-with-http-transport` | HTTP /claim-owner + /poll-claims double-sidecar scenario | End-to-end HTTP reproduction of the generation-war scenario. |
| `test_delivery_endpoints_require_delivery_scope` (:575) | `dropped-with-http-transport` | /poll-claims and /confirm-delivered scope enforcement | Bearer-scope gate on the discarded HTTP delivery endpoints. |
| `test_delivery_cred_cannot_use_mcp_surface` (:591) | `dropped-with-http-transport` | delivery-cred exclusion from /mcp | Structural separation between delivery-scoped and full MCP HTTP surfaces, both discarded. |
| `test_delivery_endpoint_roundtrip_over_http` (:610) | `carried-by-s8` | tests/messagebus/test_endpoint.py::test_the_acceptance_sequence_end_to_end_over_stdio | The wire round trip, re-run over the worker-outbound stdio MCP endpoint instead of the HTTP daemon. |
| `test_confirm_invalid_id_400` (:635) | `dropped-with-http-transport` | HTTP 400 invalid_id validation on /confirm-delivered | Pins an HTTP status-code contract of the discarded endpoint, not a delivery-semantics invariant. |
| `test_spawn_claude_injects_broker_state_dir_env` (:643) | `dropped-with-pane` | spawn_claude_pane env injection | Pane-spawn environment-variable injection has no meaning without a pane transport. |
| `test_spawn_generic_injects_broker_state_dir_env` (:661) | `dropped-with-pane` | spawn_pane env injection | Same pane-spawn env-injection mechanism for the generic spawn tool. |
| `test_spawn_injects_broker_state_dir_on_space_layout_branch` (:672) | `dropped-with-pane` | space-layout backend spawn branch | Regression for a renga/Herdr space-layout spawn code path. |
| `test_broker_stores_root_cwd` (:725) | `dropped-with-pane` | Broker(root_cwd=...) venv-discovery basis | root_cwd exists solely to support the pane-spawn venv-activation fallback. |
| `test_adapter_spawn_activates_pane_cwd_venv` (:731) | `dropped-with-pane` | pane cwd .venv activation | Pane-process venv activation on spawn is terminal-adapter machinery. |
| `test_adapter_spawn_falls_back_to_root_cwd_venv` (:745) | `dropped-with-pane` | root_cwd .venv fallback on spawn | Same venv-activation fallback mechanism, still pane-spawn specific. |
| `test_adapter_spawn_noop_without_venv` (:757) | `dropped-with-pane` | no-venv spawn no-op | Negative case for the pane venv-activation mechanism. |
| `test_spawn_claude_pane_activates_venv_end_to_end` (:768) | `dropped-with-pane` | spawn_claude_pane venv activation end-to-end | Full tool-level pane venv activation, same mechanism as above. |
| `test_spawn_claude_injects_channel_sidecar_and_dev_channel` (:787) | `dropped-with-pane` | channel sidecar mcp-config injection on spawn | Wires the pane child process to the discarded channel_sidecar push transport. |
| `test_delivery_cred_not_in_list_peers` (:816) | `dropped-with-pane` | delivery cred exclusion from list_peers | Exercises the pane-spawn-issued delivery cred and the pane-oriented list_peers tool. |
| `test_close_pane_revokes_delivery_cred_and_resets_mode` (:831) | `dropped-with-pane` | close_pane teardown of delivery cred + mode | close_pane's cred-revoke/mode-reset teardown is a pane-lifecycle hook with no MessageBus counterpart. |
| `test_close_pane_purges_undelivered_rows` (:851) | `dropped-with-pane` | close_pane purge of UNDELIVERED rows on teardown | Session teardown tied to pane close purging rows so a same-name respawn cannot misread them; MessageBus has no pane-scoped teardown to hook. |
| `test_spawn_failure_revokes_delivery_cred` (:869) | `dropped-with-pane` | spawn-failure rollback of delivery cred | Rollback of spawn side effects on adapter failure, pane-spawn specific. |
| `test_spawn_rejects_collision_with_bind_only_agent` (:887) | `dropped-with-pane` | name-collision spawn rejection | Guards the pane-spawn name-reservation path against agent_id collision. |
| `test_observer_lease_gates_generation_bump` (:914) | `dropped-with-http-transport` | observer-lease gating of register generation bump | Observer-secret handoff exists to fence fork/replay of a push sidecar; no such competing-consumer concept exists in S8. |
| `test_observer_fork_cannot_take_over_delivery` (:941) | `dropped-with-http-transport` | observer-lease fork takeover prevention | Same observer-lease anti-takeover mechanism as above. |
| `test_no_observer_lease_keeps_last_register_wins` (:960) | `dropped-with-http-transport` | unobserved owner last-register-wins default | Regression on the default (non-observed) generation-bump behaviour of the discarded push registration RPC. |
| `test_observer_lease_armed_survives_slow_startup` (:975) | `dropped-with-http-transport` | armed observer-lease TTL grace before first register | Startup-latency handling for the observer-lease arming state machine. |
| `test_observer_lease_stays_fenced_after_the_heartbeat_stops` (:994) | `dropped-with-http-transport` | observer-lease fenced-not-expired on heartbeat stop | Deliberately encodes that TTL expiry must not reopen the fence, a heartbeat-liveness-signal design for the push sidecar. |
| `test_reset_delivery_state_clears_observer_lease` (:1042) | `dropped-with-http-transport` | reset_delivery_state observer-lease clearing | Clears observer-lease bookkeeping specific to the discarded fencing mechanism. |
| `test_assert_observer_rotates_secret` (:1052) | `dropped-with-http-transport` | assert_observer secret rotation | Secret rotation exists to supersede prior observer sessions in the fork-fencing scheme. |
| `test_spawn_claude_asserts_observer_lease_and_hands_secret_via_pane_env` (:1070) | `dropped-with-pane` | observer secret handoff via pane process env | Hands the observer secret to the spawned pane process specifically to distinguish it from mcp-config replay. |
| `test_spawn_claude_lease_fences_fork_but_not_the_spawned_session` (:1100) | `dropped-with-pane` | spawn-path observer-lease fork fencing | End-to-end spawn-path acceptance test for the observer-lease anti-fork mechanism. |
| `test_spawn_claude_lease_armed_survives_a_session_slower_than_the_ttl` (:1128) | `dropped-with-pane` | spawn-path armed lease survives slow session startup | Spawn-path variant of the armed-lease TTL grace acceptance test. |
| `test_spawn_failure_clears_observer_lease` (:1155) | `dropped-with-pane` | spawn-failure rollback of observer lease | Rolls back observer-lease side effects of a failed pane spawn. |
| `test_name_collision_spawn_cannot_rotate_a_live_agents_lease` (:1172) | `dropped-with-pane` | name-collision spawn cannot rotate victim's observer lease | Guards ordering between issue_token(unique=True) and assert_observer on the pane-spawn path. |
| `test_spawn_failure_rollback_does_not_clear_someone_elses_lease` (:1196) | `dropped-with-http-transport` | compare-and-delete rollback of observer lease | Direct assert_observer/clear_observer API test of the compare-and-delete rollback guard, no pane/spawn involved. |
| `test_spawn_failure_error_and_journal_do_not_leak_the_observer_secret` (:1212) | `dropped-with-pane` | observer-secret scrubbing from spawn-failure exception text | Scrubs a secret leaked via a pane adapter's spawn-failure exception message. |
| `test_scrub_secrets_redacts_live_secret_without_the_env_prefix` (:1240) | `dropped-with-http-transport` | scrub_secrets bare-value redaction | Redacts a live observer-lease secret from arbitrary diagnostic text; the secret-handoff mechanism itself is discarded. |
| `test_spawn_codex_and_generic_do_not_assert_a_lease` (:1250) | `dropped-with-pane` | non-channel spawn paths skip observer-lease assertion | Distinguishes spawn tool variants by whether they carry a channel sidecar. |
| `test_bg_hosted_marker_suppresses_register` (:1264) | `dropped-with-http-transport` | bg_hosted marker suppression of register | bg_hosted is a push-sidecar opt-out marker with no MessageBus analogue. |
| `test_bg_hosted_suppress_does_not_regress_normal_register` (:1278) | `dropped-with-http-transport` | default (non-bg_hosted) register regression guard | Negative case for the same bg_hosted suppression mechanism. |
| `test_admin_mint_observer_optin_asserts_lease_and_returns_secret` (:1288) | `dropped-with-http-transport` | admin_mint_token observer opt-in | Wires the discarded admin-mint RPC to the observer-lease mechanism. |
| `test_admin_mint_channel_without_observer_does_not_bind` (:1301) | `dropped-with-http-transport` | admin_mint_token channel-without-observer default | Negative case for the same admin-mint/observer-lease wiring. |
| `test_admin_mint_observer_requires_channel` (:1317) | `dropped-with-http-transport` | admin_mint_token observer requires channel validation | Input validation for the discarded admin-mint RPC's observer/channel combination. |
| `test_admin_mint_channel_not_requested_has_no_observer_secret` (:1325) | `dropped-with-http-transport` | admin_mint_token no-channel default | Further negative case on the same admin-mint RPC surface. |
| `test_superseded_instance_cannot_win_the_claim_back_by_retrying` (:1342) | `dropped-with-http-transport` | latched unobserved rejection persistence | Confirms the observer-lease latch mechanism resists retry, part of the discarded fork-fencing design. |
| `test_pending_instance_recovers_when_the_pane_actually_dies` (:1363) | `dropped-with-http-transport` | observer standdown recovery on reset_delivery_state | Ties recovery of a stood-down sidecar to an external pane-death declaration, a push-transport liveness concept. |
| `test_stale_lease_is_released_when_the_pane_died_out_of_band` (:1390) | `dropped-with-pane` | Q-0023 transport-neutral successor: lease expiry + outbox.recover(); stale-readout path deliberately excluded from delivery decisions | Given verbatim in task context: the one test in the file calling kill_pane, making lease release depend on adapter pane-liveness observation. |
| `test_spawn_lease_expires_if_it_is_never_activated` (:1438) | `dropped-with-pane` | observer_arming_seconds safety-valve on spawn path | Safety valve for a secret that never reaches the spawned pane process. |
| `test_secretary_path_lease_never_expires_while_armed` (:1463) | `dropped-with-http-transport` | unbounded arming for non-spawn (secretary) observer path | Contrasts arming-TTL behaviour between spawn and admin-mint observer paths, both discarded. |
| `test_poll_does_not_activate_an_armed_lease` (:1476) | `dropped-with-http-transport` | poll must not activate an armed observer lease | Restricts lease activation to observed register only, part of the observer-lease state machine. |
| `test_fenced_instance_poll_does_not_renew_the_incumbent_lease` (:1493) | `dropped-with-http-transport` | only current-generation instance polls renew the lease | Renewal semantics tied to the generation/instance fencing model. |
| `test_delivery_dump_exposes_standdowns_and_clears_them_on_register` (:1521) | `dropped-with-http-transport` | delivery_dump standdown records | Admin-diagnostic exposure of the stood-down-sidecar bookkeeping, which has no S8 counterpart. |
| `test_standdown_records_survive_two_claimants_without_overwriting` (:1552) | `dropped-with-http-transport` | per-instance standdown record isolation | Bookkeeping detail of the same standdown-tracking mechanism. |
| `test_standdown_records_are_bounded_and_keep_the_latched_ones` (:1572) | `dropped-with-http-transport` | bounded standdown record eviction policy | Memory-bound eviction policy for standdown records, same discarded mechanism. |
| `test_fenced_poll_is_recorded_as_a_standdown` (:1587) | `dropped-with-http-transport` | fenced poll recorded as standdown | Links generation-fenced poll rejection to the standdown journal, both discarded. |
| `test_repeated_pending_refusals_do_not_grow_the_journal` (:1601) | `dropped-with-http-transport` | standdown journal de-duplication cadence | Anti-spam journaling cadence for the same discarded standdown mechanism. |
| `test_reset_delivery_state_clears_standdowns` (:1615) | `dropped-with-http-transport` | reset_delivery_state standdown clearing | Clears standdown bookkeeping tied to the discarded mechanism. |
| `test_adopt_fences_the_incumbent_sidecar_immediately` (:1628) | `dropped-with-http-transport` | adopt_delivery immediate incumbent fencing | adopt/handover is explicit push-sidecar takeover machinery; the S8 contract has no adopt flow at the transport level (only outbox.recover()). |
| `test_adopt_clears_the_registered_instance_so_nobody_can_deliver` (:1648) | `dropped-with-http-transport` | adopt_delivery clears current instance | Same adopt-handover mechanism, closing the replay-attack window it creates. |
| `test_message_enqueued_during_the_adopt_window_is_held_not_lost` (:1665) | `carried-by-s7` | test_a_reconstructed_process_sees_every_unfinished_row | The transport-neutral fact -- a message enqueued during any takeover window is held, not lost -- is exactly what S7 guarantees by making delivery decisions SQLite-only and visible to any reconstructed process. |
| `test_adopting_register_with_the_new_secret_completes_the_adoption` (:1685) | `dropped-with-http-transport` | adopt completion condition (adopting instance registered, not RPC success) | Defines completion semantics for the discarded adopt RPC. |
| `test_pre_adopt_secret_cannot_register_after_the_adopt` (:1710) | `dropped-with-http-transport` | pre-adopt secret latched out after adopt | Latching behaviour of the observer-secret handoff across an adopt boundary. |
| `test_adopt_requeue_returns_in_flight_rows_and_leaves_other_owners_alone` (:1731) | `dropped-with-http-transport` | adopt in_flight=requeue policy, owner-scoped | in_flight requeue/drop policy is an adopt-RPC parameter with no S8 transport-level adopt flow to attach to. |
| `test_adopt_drop_policy_retires_the_in_flight_rows` (:1757) | `dropped-with-http-transport` | adopt in_flight=drop policy | Same adopt-RPC in_flight policy parameter, discarded with the adopt mechanism. |
| `test_adopt_started_journal_records_the_in_flight_choice` (:1776) | `dropped-with-http-transport` | delivery_adopt_started journal fields | Journaling detail of the discarded adopt RPC. |
| `test_old_sidecar_confirm_after_adopt_is_fenced_not_idempotent` (:1799) | `dropped-with-http-transport` | adopt fencing takes precedence over confirm idempotence | Interaction between the discarded adopt-generation fence and confirm; S8's unfenced record_ack has no adopt boundary to interact with. |
| `test_second_adopt_needs_force_and_force_supersedes_the_first` (:1819) | `dropped-with-http-transport` | concurrent adopt force/supersede semantics | Concurrency control for the discarded adopt RPC. |
| `test_adopt_unknown_owner_installs_no_lease` (:1846) | `dropped-with-http-transport` | adopt_delivery unknown-owner rejection | Input validation for the discarded adopt RPC. |
| `test_adopt_owner_without_delivery_credential_is_refused` (:1864) | `dropped-with-http-transport` | adopt_delivery requires existing delivery credential | Precondition check tied to the discarded delivery-credential/adopt machinery. |
| `test_adopt_rejects_invalid_in_flight_and_arming_seconds` (:1879) | `dropped-with-http-transport` | adopt_delivery parameter validation, side-effect-free | Parameter validation for the discarded adopt RPC. |
| `test_adopt_expires_when_the_adopting_session_never_registers` (:1897) | `dropped-with-http-transport` | adopt arming-window expiry on no adopting register | Failure-mode handling for the discarded adopt RPC's arming window. |
| `test_expired_adopt_restores_the_previous_sidecars_delivery_path` (:1920) | `dropped-with-http-transport` | expired-adopt restoration of prior (generation, instance) | Rollback semantics of the discarded adopt mechanism. |
| `test_closing_the_superseded_pane_does_not_kill_the_adopted_session` (:1948) | `dropped-with-pane` | close_pane detachment for adopted owners | Interaction between pane close teardown and the adopt mechanism, both pane/HTTP-transport specific. |
| `test_adopt_does_not_leave_two_live_processes_sharing_one_bind` (:1993) | `dropped-with-http-transport` | adopt invalidates the previous process's full token | MCP session/bind ownership transfer on adopt, tied to the discarded full-token/bind model. |
| `test_expired_adopt_gives_the_previous_process_its_token_back` (:2019) | `dropped-with-http-transport` | expired-adopt token rollback | Rollback of the token-ownership transfer performed by the discarded adopt RPC. |
| `test_expired_adopt_does_not_claim_it_restored_a_closed_pane` (:2040) | `dropped-with-pane` | expired-adopt honesty when the detached pane is already closed | Depends on pane-close bookkeeping (_pane_meta) to avoid a false restored=True claim. |
| `test_forced_adopt_carries_the_whole_rollback_state_not_just_the_generation` (:2073) | `dropped-with-pane` | forced-adopt full rollback state (generation, instance, token, detached pane) | Rollback state explicitly includes detached-pane bookkeeping from the pane-spawn path. |
| `test_forced_adopt_expiry_restores_the_original_incumbent_not_the_fence` (:2117) | `dropped-with-http-transport` | forced-adopt expiry restores pre-fence incumbent | Rollback-chain correctness for the discarded force-supersede adopt mechanism. |
| `test_expired_adopt_does_not_clobber_a_lease_installed_after_the_deadline` (:2140) | `dropped-with-http-transport` | compare-and-delete on expired-adopt lease cleanup | Compare-and-delete guard for the discarded observer-lease/adopt-expiry interaction. |
| `test_expired_adopt_does_not_restore_the_fence_after_someone_registered` (:2160) | `dropped-with-http-transport` | compare-and-restore on expired-adopt generation rollback | Compare-and-restore guard for the discarded adopt-expiry sweep. |
| `test_an_expired_adoption_does_not_block_the_next_adopt` (:2190) | `dropped-with-http-transport` | adopt sweep at adopt entry point | Sweep-on-entry behaviour of the discarded adopt RPC. |
| `test_expired_adopt_is_swept_by_check_messages_when_nothing_polls` (:2210) | `dropped-with-http-transport` | adopt sweep at check_messages entry point | Sweep-on-entry behaviour via the pull fallback, still scoped to the discarded adopt mechanism. |
| `test_adopt_arms_an_owner_that_never_registered_a_sidecar` (:2228) | `dropped-with-http-transport` | adopt succeeds without prior generation/lease state | Precondition-free adopt case for the discarded mechanism. |
| `test_reset_delivery_state_cancels_a_pending_adoption` (:2251) | `dropped-with-http-transport` | reset_delivery_state cancels pending adopt | Teardown interaction between reset and the discarded adopt mechanism. |
| `test_delivery_dump_exposes_the_pending_adoption_without_the_secret` (:2273) | `dropped-with-http-transport` | delivery_dump pending-adoption fields, secret excluded | Admin-diagnostic exposure of the discarded adopt mechanism's pending state. |
| `test_adopt_never_writes_the_observer_secret_to_the_journal` (:2292) | `dropped-with-http-transport` | adopt journal secret exclusion | Secret-hygiene guard for journaling of the discarded adopt mechanism. |
| `test_scrub_secrets_redacts_a_pending_adoptions_secret_after_a_rotate` (:2312) | `dropped-with-http-transport` | scrub_secrets redacts a superseded pending-adopt secret | Secret-scrubbing edge case tied to the discarded adopt/observer-lease rotation interaction. |
| `test_adopt_arming_seconds_defaults_to_the_broker_tunable` (:2332) | `dropped-with-http-transport` | adopt_arming_seconds tunable default wiring | Configuration-plumbing test for the discarded adopt RPC. |
| `test_claim_owner_observer_and_bg_over_http` (:2349) | `dropped-with-http-transport` | /claim-owner wiring of observer + bg_hosted over HTTP | HTTP wire test for the discarded observer-lease and bg_hosted machinery. |
| `test_claim_owner_rejects_bad_observer_and_bg_types` (:2371) | `dropped-with-http-transport` | /claim-owner request-body type validation | HTTP request validation for the discarded /claim-owner endpoint. |
| `test_sidecar_subprocess_claims_emits_and_confirms` (:2383) | `dropped-with-http-transport` | channel_sidecar subprocess over real HTTP daemon | End-to-end wire test of the discarded HTTP daemon plus channel_sidecar push-emit mechanism. |
| `test_spawn_lease_end_to_end_over_the_wire` (:2499) | `dropped-with-pane` | spawn + observer-lease + channel_sidecar end-to-end over HTTP | Composes pane spawn, observer-lease handoff via pane env, and the HTTP channel sidecar in one wire test; pane-spawn machinery dominates the fixture. |
| `test_spawn_threads_semantic_kind_to_capable_backend` (:2577) | `dropped-with-pane` | spawn(kind=) threading to backend | Terminal-backend spawn-signature detail (herdr agent.start kind argument). |
| `test_spawn_omits_kind_for_backends_without_the_capability` (:2604) | `dropped-with-pane` | tmux/wezterm spawn signature unchanged | Explicitly named in PORTING_LEDGER.md as pinning old backend (tmux/wezterm/herdr) spawn signatures verbatim. |
| `test_venv_pane_env_backend_gets_virtual_env_without_argv_rewrite` (:2614) | `dropped-with-pane` | venv_path_via_pane_env backend branch | Backend-specific venv-activation strategy for pane spawn (herdr 0.7.5). |
| `test_venv_wrapper_backend_is_unchanged` (:2635) | `dropped-with-pane` | default wrapper-based venv activation unchanged | Explicitly named in PORTING_LEDGER.md as pinning the tmux/wezterm/herdr-legacy spawn signature. |

## `tests/broker/test_store.py`

Ledger class: **carry (invariant) / rewrite (mechanism)** (PORTING_LEDGER.md:220). 6 test functions: 3 dropped-with-pane, 2 carried-by-s8, 1 failing-spec.

| Test | Disposition | Successor / goes with | Rationale |
|---|---|---|---|
| `test_enqueue_only_to_registered` (:61) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_message_is_sent_only_to_a_registered_recipient | Given fixed point: this is the send-to-registered-recipient-only invariant, now enforced by MessageBus over the HandlerRegistry instead of broker bind registration. |
| `test_enqueue_matches_by_name` (:73) | `failing-spec` | tests/messagebus/test_carried_specifications.py::test_a_send_to_a_registered_alias_reaches_the_canonical_recipient | Recipient aliasing is not part of the new contract yet; landed failing (xfail strict) rather than driving the deleted module. |
| `test_drain_is_at_most_once` (:81) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_settled_message_is_never_presented_again | Given fixed point: pull-then-ack in MessageBus replaces drain-then-gone, pinning the same at-most-once presentation invariant. |
| `test_nudge_injected_once_when_idle` (:92) | `dropped-with-pane` | v1 idle-gated pane nudge injection (classify_pane_state + send_line adapter) | Pins pane-terminal idle detection and text injection into a tmux pane, which has no transport-neutral successor in the pull-based MessageBus. |
| `test_nudge_skips_when_no_pane` (:106) | `dropped-with-pane` | v1 pane_id-gated nudge suppression | Given fixed point group (the three nudge tests): pane presence gating a UI nudge has no meaning without a pane. |
| `test_nudge_single_flight_under_concurrent_sends` (:116) | `dropped-with-pane` | v1 single-flight nudge worker thread dedup (_nudge_threads) | Given fixed point group (the three nudge tests): the nudge worker thread machinery is pane/adapter delivery machinery with no MessageBus counterpart (pull model has no push-side nudge thread to dedup). |

## `tests/broker/test_control_plane.py`

Ledger class: **carry (invariant) / rewrite (mechanism)** (PORTING_LEDGER.md:221). 50 test functions: 41 dropped-with-http-transport, 8 carried-deferred, 1 dropped-with-pane.

| Test | Disposition | Successor / goes with | Rationale |
|---|---|---|---|
| `test_sidecar_roundtrip_and_fields` (:85) | `carried-deferred` | a broker.server successor's discovery metadata (the sidecar.py daemon.json contract) | Ledger carve: the discovery-metadata round trip and state_dir absolutisation carry; the backend="tmux" field assertion inside it drops with the pane. |
| `test_sidecar_backend_none_for_no_nudge` (:104) | `dropped-with-pane` | nudge adapter backend field (tmux/None) | backend records the nudge/tmux adapter choice, a pane-transport concept S8 has no analog for. |
| `test_remove_sidecar_is_idempotent` (:113) | `carried-deferred` | a broker.server successor's discovery metadata (the sidecar.py daemon.json contract) | Idempotent removal of the discovery file is resume-condition material, per the ledger carve. |
| `test_admin_token_written_atomically_and_0600` (:127) | `carried-deferred` | a broker.server successor's discovery metadata (the sidecar.py daemon.json contract) | Secret-file hygiene (atomic write, 0600) carries with the discovery-metadata/secret separation. |
| `test_read_admin_token_empty_is_none` (:142) | `carried-deferred` | a broker.server successor's discovery metadata (the sidecar.py daemon.json contract) | The secret-absence read contract, same carve. |
| `test_read_journal_since_avoids_prior_run_false_positive` (:148) | `carried-deferred` | a broker.server successor's verification probe | Offset-scoped journal verification that stops a prior run's line from reading as a false positive -- verification-probe material, per the ledger carve. |
| `test_admin_mint_token_reflects_tier` (:180) | `dropped-with-http-transport` | admin mint_token RPC / auth_role tiers | Token minting and role-tiered MCP tool surfaces are v1 HTTP admin-plane machinery. |
| `test_admin_mint_token_secretary_is_full_surface` (:199) | `dropped-with-http-transport` | admin mint_token RPC tier surface count | Same mint_token/tier machinery. |
| `test_admin_mint_token_carries_cwd` (:206) | `dropped-with-http-transport` | admin mint_token cwd binding | cwd-on-bind is a spawn-anchor concept tied to the v1 token/bind model, no S8 analog. |
| `test_admin_mint_token_absolutizes_relative_cwd` (:214) | `dropped-with-http-transport` | admin mint_token cwd absolutization | Same bind/cwd mechanism as above. |
| `test_admin_mint_token_default_agent_id_is_unique` (:226) | `dropped-with-http-transport` | admin mint_token default agent_id uniqueness | agent_id/bind uniqueness is part of the discarded token-bind model. |
| `test_admin_mint_token_honors_explicit_name` (:237) | `dropped-with-http-transport` | admin mint_token explicit name binding | Same bind/name mechanism. |
| `test_admin_mint_token_rejects_duplicate_explicit_name` (:246) | `dropped-with-http-transport` | admin mint_token duplicate-name rejection | Guards the discarded bind/queue-sharing model, not outbox/messagebus semantics. |
| `test_admin_mint_token_channel_wires_sidecar` (:259) | `dropped-with-http-transport` | channel=True sidecar + delivery-scoped credential wiring | Wires the v1 push channel sidecar and delivery credential, both discarded transport machinery. |
| `test_admin_mint_token_without_channel_has_no_sidecar` (:283) | `dropped-with-http-transport` | channel sidecar absence when channel omitted | Negative case of the same discarded channel/credential wiring. |
| `test_admin_mint_token_rejects_non_bool_channel` (:293) | `dropped-with-http-transport` | channel param strict-bool validation | Validates a param of the discarded channel-credential RPC. |
| `test_admin_mint_token_rejects_unknown_role` (:306) | `dropped-with-http-transport` | admin mint_token role validation | Role validation for the discarded token-tier RPC. |
| `test_admin_rejects_missing_token` (:315) | `dropped-with-http-transport` | admin HTTP bearer-token auth gate | Admin RPC bearer-auth is HTTP daemon plumbing with no MCP stdio counterpart. |
| `test_admin_rejects_wrong_token` (:322) | `dropped-with-http-transport` | admin HTTP bearer-token auth gate | Same admin auth gate. |
| `test_admin_disabled_when_no_admin_token` (:328) | `dropped-with-http-transport` | admin route 404-hiding when unconfigured | HTTP route-hiding behavior of the discarded admin plane. |
| `test_admin_unknown_method_rejected` (:335) | `dropped-with-http-transport` | admin RPC unknown-method dispatch | Dispatch table of the discarded admin JSON-RPC surface. |
| `test_admin_flip_mode_advances_epoch` (:342) | `dropped-with-http-transport` | per-agent PUSH/PULL delivery_mode + epoch flip RPC | The dual PUSH/PULL mode flip and its epoch are v1 transport machinery; S8's MessageBus is pull-only with no mode flip. |
| `test_admin_flip_mode_rejects_bad_params` (:355) | `dropped-with-http-transport` | flip_mode param validation | Validates params of the discarded flip_mode RPC. |
| `test_admin_delivery_dump` (:361) | `dropped-with-http-transport` | admin delivery_dump diagnostic RPC | Cross-cutting admin snapshot RPC over the discarded delivery_mode/generation model. |
| `test_admin_adopt_delivery_returns_handover_payload` (:406) | `dropped-with-http-transport` | adopt_delivery RPC / generation+observer_secret handover | Adopt/generation/observer-secret handover is the v1 competing-sidecar exclusivity mechanism S7/S8 replaces with lease epochs; recovery.recover() is the transport-neutral successor. |
| `test_admin_adopt_delivery_does_not_leak_internal_keys` (:432) | `dropped-with-http-transport` | adopt_delivery internal key redaction | Guards internals of the discarded adopt RPC response shape. |
| `test_admin_adopt_delivery_rekeys_the_bind_instead_of_minting_a_second` (:445) | `dropped-with-http-transport` | adopt bind rekeying vs re-mint | Bind rekeying is a property of the discarded token-bind/adopt model. |
| `test_admin_adopt_delivery_keeps_observer_secret_out_of_mcp_config` (:474) | `dropped-with-http-transport` | observer_secret exclusion from mcp_config | observer_secret is v1's non-replayable secret for the adopt mechanism, dropped with it. |
| `test_admin_adopt_delivery_echoes_in_flight_policy` (:488) | `dropped-with-http-transport` | adopt in_flight requeue/drop policy echo | In-flight requeue/drop policy is specific to the adopt takeover RPC response, not a standalone delivery invariant. |
| `test_admin_adopt_delivery_arming_default_follows_daemon_tunable` (:500) | `dropped-with-http-transport` | adopt arming_seconds daemon tunable default | Arming window is part of the discarded adopt handshake. |
| `test_admin_adopt_delivery_honors_explicit_arming_seconds` (:517) | `dropped-with-http-transport` | adopt arming_seconds param normalization | Same adopt arming-window mechanism. |
| `test_admin_adopt_delivery_requires_admin_token` (:530) | `dropped-with-http-transport` | adopt admin-auth gate + no-side-effect-on-401 | Auth gate for the discarded adopt RPC. |
| `test_admin_adopt_delivery_rejects_agent_and_delivery_credentials` (:544) | `dropped-with-http-transport` | adopt admin-only reachability (full/delivery creds refused) | Authorization boundary of the discarded adopt RPC and delivery credential model. |
| `test_adopt_is_absent_from_the_mcp_tool_surface` (:559) | `dropped-with-http-transport` | tools/list exclusion of adopt tools | Asserts absence of adopt from the discarded MCP tool catalogue/tier model. |
| `test_admin_adopt_delivery_rejects_bad_params` (:589) | `dropped-with-http-transport` | adopt_delivery param validation matrix | Input validation for the discarded adopt RPC. |
| `test_admin_adopt_delivery_unknown_owner_is_400` (:603) | `dropped-with-http-transport` | adopt unknown-owner rejection | Owner lookup against the discarded bind/owner registry. |
| `test_admin_adopt_delivery_without_delivery_credential_is_400` (:616) | `dropped-with-http-transport` | adopt requires delivery credential precondition | Delivery credential precondition of the discarded channel/adopt model. |
| `test_admin_adopt_delivery_conflict_requires_force` (:631) | `dropped-with-http-transport` | adopt in-flight conflict + force supersede, last-rotate-wins | Concurrent-adopt conflict resolution is specific to the discarded single-owner-rotation RPC, not a transport-neutral write-exclusivity fact (that's covered by lease epoch fencing in test_outbox.py). |
| `test_admin_adopt_status_reports_pending_without_secret` (:650) | `dropped-with-http-transport` | adopt_status pending report / secret non-disclosure | Status introspection of the discarded adopt handshake. |
| `test_admin_adopt_status_pending_is_none_when_idle` (:674) | `dropped-with-http-transport` | adopt_status idle pending=None | Same discarded adopt_status RPC. |
| `test_admin_adopt_status_rejects_bad_owner` (:688) | `dropped-with-http-transport` | adopt_status owner param validation | Validation for the discarded adopt_status RPC. |
| `test_admin_adopt_delivery_exception_is_rendered_as_400` (:700) | `dropped-with-http-transport` | adopt_delivery handler exception -> 400 with secret scrubbing | HTTP error-rendering and secret-scrubbing for the discarded admin route. |
| `test_admin_adopt_status_exception_is_rendered_as_400` (:722) | `dropped-with-http-transport` | adopt_status handler exception -> 400 | Same discarded admin route's error handling. |
| `test_admin_near_adopt_method_names_are_unknown` (:737) | `dropped-with-http-transport` | admin RPC method-name exact matching | Dispatch-table strictness for the discarded admin JSON-RPC surface. |
| `test_adopt_hidden_when_no_admin_token` (:749) | `dropped-with-http-transport` | admin route 404-hiding covers adopt too | Route-hiding behavior of the discarded admin plane. |
| `test_sigterm_handler_requests_shutdown` (:763) | `dropped-with-http-transport` | SIGTERM -> request_shutdown daemon lifecycle wiring | Signal-driven graceful shutdown is specific to the long-running HTTP daemon process; MessageBus is an MCP stdio endpoint with no equivalent daemon lifecycle. |
| `test_admin_shutdown_clean_stop_via_run` (:790) | `dropped-with-http-transport` | admin shutdown RPC end-to-end clean stop + sidecar teardown | End-to-end HTTP daemon shutdown/sidecar teardown, entirely discarded transport machinery. |
| `test_pid_alive_true_for_self` (:856) | `carried-deferred` | sidecar.pid_alive (module still shipped) | Pins sidecar.pid_alive, the process-liveness contract any resume path needs; not admin-RPC surface and not a MessageBus invariant. |
| `test_pid_alive_false_for_reaped_child` (:863) | `carried-deferred` | sidecar.pid_alive (module still shipped) | Same liveness contract, the reaped-child case. |
| `test_pid_alive_false_for_nonpositive_or_nonint` (:875) | `carried-deferred` | sidecar.pid_alive (module still shipped) | Same liveness contract, the malformed-input case. |

## `tests/broker/test_notify.py`

Ledger class: **rewrite** (PORTING_LEDGER.md:223). 21 test functions: 18 dropped-with-http-transport, 3 carried-by-s8.

| Test | Disposition | Successor / goes with | Rationale |
|---|---|---|---|
| `test_send_delivers_to_registered_recipient` (:79) | `carried-by-s8` | tests/messagebus/test_endpoint.py::test_poll_then_ack_over_the_tool_surface | The reachability half of the bridge contract, re-expressed as send-then-poll on the new surface. |
| `test_send_delivers_unicode_body` (:91) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_non_ascii_payload_survives_delivery_byte_for_byte | Payload fidelity is transport-neutral and is re-pinned against the bus directly. |
| `test_send_close_failure_does_not_invert_success` (:103) | `dropped-with-http-transport` | v1 _McpClient de-register/close RPC to the HTTP daemon | Pins that a cleanup-RPC failure on the ephemeral sender token's close() must not flip the send result, which is machinery of the deleted admin-mint HTTP client, not a delivery invariant. |
| `test_send_diagnostic_is_ascii_even_with_unicode_path` (:118) | `dropped-with-http-transport` | broker_send CLI's ASCII-safe stderr diagnostic normalization | Pins CLI stderr formatting for the deleted notify.broker_send helper, not a MessageBus delivery/ack invariant. |
| `test_send_unknown_recipient_is_undelivered` (:130) | `carried-by-s8` | tests/messagebus/test_carried_specifications.py::test_a_message_is_sent_only_to_a_registered_recipient | The refusal half of send-to-registered-only, now refused before the durable write. |
| `test_send_does_not_leak_registered_sender` (:136) | `dropped-with-http-transport` | v1 admin-mint ephemeral sender token + broker._binds registration/revocation bookkeeping | Pins that the disposable admin-* sender bind used only for this send is de-registered, which is v1's bind/registration machinery with no MessageBus counterpart (no worker registration binding beyond HandlerRegistry). |
| `test_send_no_sidecar_is_noop_nonzero` (:150) | `dropped-with-http-transport` | v1 sidecar discovery (daemon.json absence -> noop) | Pins sidecar-file-based daemon discovery, explicitly out of scope for the sidecar/daemon-less MessageBus. |
| `test_send_missing_admin_token_is_nonzero` (:156) | `dropped-with-http-transport` | v1 admin.token mint precondition | Pins the admin-token minting credential flow that MessageBus has no equivalent for. |
| `test_send_rejected_admin_token_is_undelivered` (:167) | `dropped-with-http-transport` | v1 admin RPC 401/auth-mismatch handling | Pins admin-token authentication against the HTTP daemon, a delivery-credential concept the MCP stdio endpoint does not have. |
| `test_send_malformed_sidecar_is_caught_no_raise` (:181) | `dropped-with-http-transport` | v1 sidecar JSON parsing / read_sidecar catch-all | Pins resilience of the daemon-discovery sidecar file reader, which no longer exists. |
| `test_send_unreachable_daemon_is_nonzero_no_raise` (:202) | `dropped-with-http-transport` | v1 HTTP daemon reachability / URLError handling | Pins network-reachability handling to a stopped HTTP daemon process that MessageBus (in-process outbox + MCP stdio) does not have. |
| `test_top_level_cli_routes_to_broker_send` (:214) | `dropped-with-http-transport` | v1 `claude-org-runtime broker send` CLI subcommand wiring | Pins CLI routing to the deleted broker_send helper/daemon path; MessageBus is an MCP endpoint, not this CLI subcommand. |
| `test_resolve_state_dir_precedence` (:224) | `dropped-with-http-transport` | v1 ORG_BROKER_STATE_DIR env / --state-dir precedence for locating the daemon's sidecar | Pins state-dir resolution used only to find the v1 daemon's sidecar file, irrelevant to the outbox-backed MessageBus. |
| `test_send_uses_env_state_dir_when_flag_omitted` (:240) | `dropped-with-http-transport` | same v1 state-dir/env sidecar-discovery mechanism as test_resolve_state_dir_precedence | Env-based daemon discovery for the deleted broker daemon; no MessageBus counterpart. |
| `test_send_flag_beats_env` (:256) | `dropped-with-http-transport` | same v1 state-dir/env sidecar-discovery mechanism | Pins CLI-flag-vs-env precedence for locating the v1 daemon, not a delivery invariant. |
| `test_send_stale_hint_when_pid_dead` (:267) | `dropped-with-http-transport` | v1 stale-sidecar stderr hint keyed on sidecar.pid_alive() | Diagnostic-hint machinery tied to the v1 daemon's recorded pid; the project explicitly keeps stale-readout out of delivery decisions and this test pins a CLI hint string, not a delivery outcome. |
| `test_send_no_stale_hint_when_pid_alive` (:284) | `dropped-with-http-transport` | same v1 stale-sidecar hint mechanism | Negative case of the same pid-liveness-driven CLI diagnostic, deleted with the daemon. |
| `test_send_stale_hint_on_mcp_surface_leg` (:297) | `dropped-with-http-transport` | same v1 stale-sidecar hint mechanism, on the MCP-surface leg of the send path | Pins a second call site of the same deleted diagnostic machinery (MCP client to the HTTP daemon). |
| `test_send_parser_defaults_and_required` (:320) | `dropped-with-http-transport` | v1 broker_cli `send` argparse subparser defaults | Pins CLI argument wiring for the deleted broker send subcommand; MessageBus has no such CLI surface. |
| `test_send_requires_to_and_message` (:333) | `dropped-with-http-transport` | v1 broker_cli `send` required-argument validation | Same deleted CLI parser, just the required-args negative case. |
| `test_send_help_is_ascii_only` (:341) | `dropped-with-http-transport` | v1 broker_cli `send` --help ASCII/cp932-safety text | Pins help text of the deleted CLI subcommand, not a MessageBus delivery contract (the ASCII-help convention itself still applies wherever a new CLI surface is added, but this specific test targets the removed parser). |

## `tests/attention/test_broker_journal_contract.py`

Ledger class: **carry (invariant) / rewrite (mechanism)** (PORTING_LEDGER.md:243). 6 test functions: 3 carried-deferred, 3 dropped-with-http-transport.

| Test | Disposition | Successor / goes with | Rationale |
|---|---|---|---|
| `test_real_broker_duplicate_reaches_the_attention_layer` (:65) | `carried-deferred` | the outbox/incident anomaly surface (D-0007) | The end-to-end discipline -- a detected delivery anomaly must actually reach a consumer, not just a journal line -- carries (the ledger's accident-derived fixture); the anomaly driving it here, two generation-fenced HTTP sidecars, does not exist in the new model. |
| `test_healthy_single_sidecar_produces_no_attention_event` (:88) | `carried-deferred` | the outbox/incident anomaly surface (D-0007) | The no-false-positive half of the same producer-consumer discipline; re-pin it against whatever anomaly the successor surface detects. |
| `test_store_cooldown_survives_the_consumer` (:106) | `carried-deferred` | the outbox/incident anomaly surface (D-0007) | Noise hygiene of the carried discipline (a persistent anomaly must not flood the consumer); the cooldown it exercises is v1's, the requirement is not. |
| `test_real_broker_adopt_expiry_reaches_the_attention_layer` (:143) | `dropped-with-http-transport` | adopt_delivery/adopt_status arming-deadline + adoption_id + observer_secret handover flow | Adopt-window expiry is v1's push-transport handover machinery (adoption_id, observer secret, arming seconds); Q-0023's transport-neutral successor is lease expiry plus outbox.recover(), which does not have an 'adopt window' to expire. |
| `test_real_broker_superseded_session_reaches_the_attention_layer` (:183) | `dropped-with-http-transport` | observer_secret-based supersession of a registering delivery instance after a second adopt_delivery | Session supersession here is fenced by the observer secret handed out by adopt_delivery, an HTTP delivery-credential concept with no MessageBus equivalent (write exclusivity comes from writer_epoch fencing instead). |
| `test_completed_adopt_produces_no_attention_event` (:221) | `dropped-with-http-transport` | adopt_delivery completion cancelling the arming deadline, negative case of the adopt-expiry test | Asserts silence for a successfully completed v1 adopt handover, the same discarded credential/observer/arming-deadline machinery as its positive counterpart. |

