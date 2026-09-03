"""Fail-closed cache migrations for non-decision screening source changes."""

from __future__ import annotations

import re
from typing import Mapping

from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)


_ORCHESTRATION_ONLY_SOURCE_PATHS = frozenset(
    {
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py",
    }
)
_REVIEWED_PRIORITY_MONITOR_STATE_SOURCE_TRANSITIONS: frozenset[
    tuple[str, str]
] = frozenset(
    {
        # The deployed opening-event release wrote a fully authenticated
        # priority queue without embedding its source manifest.  Preserve that
        # exact runtime state across this transport/startup-only release, then
        # immediately rewrite it under the current identity.
        (
            "sha256:01eddbef608cfef200aba7a7f4d1ed90f70fd37f794efd4f83f8afdeeb7234cc",
            "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d",
        ),
        # The live-alert admission gate changes only process scheduling.  The
        # deployed queue already contains authenticated decision documents and
        # observations; validate it in full, then rewrite it under this exact
        # coordinated service/process release instead of rebuilding the queue.
        (
            "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d",
            "sha256:0e242b05dee654f8d99e97e4c2eb91dd2304811c971575f4900175e0bad70c3f",
        ),
        # Retaining the newest completed intraday projection through lunch and
        # shortening browser revalidation do not alter any persisted monitor
        # document.  Preserve the deployed authenticated queue and immediately
        # rewrite it under this exact presentation-only source identity.
        (
            "sha256:0e242b05dee654f8d99e97e4c2eb91dd2304811c971575f4900175e0bad70c3f",
            "sha256:abcac87b2319d28a1d57cd29e4c24cba63085e69db1621b7898324a41670b03a",
        ),
        # Streaming monitor batches already carry authenticated per-symbol
        # observation times.  Using those times for the page cutoff changes no
        # persisted document or schema, so retain the queue written by the
        # immediately preceding intraday-projection deployment.
        (
            "sha256:abcac87b2319d28a1d57cd29e4c24cba63085e69db1621b7898324a41670b03a",
            "sha256:d3712db30fdc0ec2ff920d1a171f33df9ea322776ab8a2dccb975b8242320624",
        ),
        # Defer authenticated next-epoch coverage repairs while the realtime
        # priority monitor owns the structure pool.  This is scheduling-only:
        # persisted observations and decision documents are unchanged, so the
        # immediately deployed queue can be retained and re-signed exactly.
        (
            "sha256:d3712db30fdc0ec2ff920d1a171f33df9ea322776ab8a2dccb975b8242320624",
            "sha256:9fc76471f0fe57e5fd6b3e83907a87500d1d5c021dd377136d83b588eaee0034",
        ),
        # Keep the same scheduling-only queue across the close-handoff hardening
        # that extends resource protection through the exact 15:05 boundary.
        (
            "sha256:9fc76471f0fe57e5fd6b3e83907a87500d1d5c021dd377136d83b588eaee0034",
            "sha256:504d37341745d9a201d07d1e89975431ee2218fb41a042641696aa3a4df523d9",
        ),
        # Production 5m-boundary evidence showed the final singleton wave was
        # being withheld with unused SLA headroom.  Extending only the bounded
        # priority admission budget preserves every persisted decision/state.
        (
            "sha256:504d37341745d9a201d07d1e89975431ee2218fb41a042641696aa3a4df523d9",
            "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5",
        ),
        # The standalone bar clock, fixed finalization reserve and dynamic
        # release of completed sector-build workers change only when an
        # authenticated monitor row is revisited. Preserve the exact durable
        # monitor queue and re-sign it under this coordinated runtime release.
        (
            "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5",
            "sha256:2d70261f1ab2c3160cbd74161f54d85f114cfbbbdfdfea7653ca3495656c08c3",
        ),
        # If today's authenticated checkpoint has started before any complete
        # generation exists, retain only the independently validated previous-
        # close candidate continuity.  Priority documents and observations are
        # untouched, so the deployed durable queue can be validated and re-signed.
        (
            "sha256:2d70261f1ab2c3160cbd74161f54d85f114cfbbbdfdfea7653ca3495656c08c3",
            "sha256:8b6b4c66a6628d081d44529b24412deb3ead24b9b040e1acfca1cb79f6847ca8",
        ),
        # Rejecting an out-of-epoch generation must also roll back the probe's
        # migration audit state.  The deployed continuity release wrote no new
        # monitor semantics, so retain its authenticated queue across the fix.
        (
            "sha256:8b6b4c66a6628d081d44529b24412deb3ead24b9b040e1acfca1cb79f6847ca8",
            "sha256:7f5060a02826eaff56c713b06229c026e7650cb3435566c056006e22d790f342",
        ),
        # Replace the global 1m wave barrier with independent, affinity-stable
        # per-worker streams. This changes only request admission/order inside
        # the same absolute deadline; persisted decisions and observations keep
        # their exact schemas and are revalidated before the queue is re-signed.
        (
            "sha256:7f5060a02826eaff56c713b06229c026e7650cb3435566c056006e22d790f342",
            "sha256:978b540e92386151a2db3d1430a30b744e6677f26dcc0f88358979dc0dd41869",
        ),
        # Widening the decoded 1m L1 and its operational admission ceiling does
        # not change a frame, structure, signal, or persisted monitor document.
        # Revalidate the authenticated queue and re-sign it under the exact
        # runtime-capacity release so a deployment does not discard live state.
        (
            "sha256:978b540e92386151a2db3d1430a30b744e6677f26dcc0f88358979dc0dd41869",
            "sha256:71a798322866a5d67d698b49a162e1e30e122de01f608584128efb4fda06acd1",
        ),
        # The exact 320-to-384 full-market capacity migration changes one
        # authenticated operational field and re-signs the otherwise identical
        # complete snapshot. Monitor documents and observations remain intact.
        (
            "sha256:71a798322866a5d67d698b49a162e1e30e122de01f608584128efb4fda06acd1",
            "sha256:5d94e0e5134f3b8f293b391aee0f0d53c45687e666034e07aeee9b99f2f7e792",
        ),
        # Use the authenticated per-code 1m observation timestamps as the
        # fairness cursor when cold throughput spans three or more physical
        # waves. This changes only revisit order; retained decisions,
        # observations and their schemas remain byte-for-byte compatible.
        (
            "sha256:5d94e0e5134f3b8f293b391aee0f0d53c45687e666034e07aeee9b99f2f7e792",
            "sha256:5405cf3d282e051a50bf30212f593de2d44ab9265958faad86d8ddfff6974e8d",
        ),
        # The final warmed symbol now uses the immediately preceding request
        # duration instead of the fixed admission guard. This affects only
        # physical scheduling near the deadline; authenticated observations,
        # decisions and monitor documents remain schema-compatible.
        (
            "sha256:5405cf3d282e051a50bf30212f593de2d44ab9265958faad86d8ddfff6974e8d",
            "sha256:e3f2bb6ffc56a16901582a22b4759a1e3df720670b3fb01126191043248e51d3",
        ),
        # Remove source-proven unreachable helpers and configuration without
        # changing any decision, structure, market-data or persisted-state
        # byte.  The coordinated file transition below authenticates the
        # exact no-op cleanup before the durable queue is re-signed.
        (
            "sha256:e3f2bb6ffc56a16901582a22b4759a1e3df720670b3fb01126191043248e51d3",
            "sha256:7343aa38677a66b48f5777404c8698c3f92c8318701241cffa4f4742ee18418b",
        ),
    }
)
_REVIEWED_COMPOSABLE_ORCHESTRATION_SOURCE_ROWS = frozenset(
    {
        # Realtime admission capacity changes only which already-selected
        # subjects can be revisited inside the fixed live deadline.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
            "sha256:2023dacd160858ea22539e051328699e65f287345c9efb1f88c5471b022cfcd6",
        ),
        # Restore the most recent authenticated priority queue across an exact
        # runtime-state loader change.  Signal documents, screening rules and
        # market facts are unchanged; the queue is validated in full and
        # immediately persisted under the current source identity.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:2023dacd160858ea22539e051328699e65f287345c9efb1f88c5471b022cfcd6",
            "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
        ),
        # The immutable coverage snapshot, strict structures, decisions and
        # monitor-state schema are unchanged.  Only the page projection keeps
        # the newest completed intraday observations visible outside the
        # notification session, so this exact suffix can compose with already
        # reviewed cache migrations without widening their file sets.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:21391c06f6ff6b87863dc14676c2ddfda0818fd3545e4f452ed91b23fa5881d9",
            "sha256:89e6d3fa2d7855089c3187161890d5c5d450124b85d608e67e6b1614d54c8609",
        ),
        # Advance the display cutoff from each successfully published monitor
        # batch rather than the previous whole-round timestamp.  This remains
        # presentation-only and is safe as an exact composable suffix.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:89e6d3fa2d7855089c3187161890d5c5d450124b85d608e67e6b1614d54c8609",
            "sha256:0d22ca4b0ef09cb2257f16da4df67d2a76ca9242595ba888277fe91545e5d69e",
        ),
        # Give the realtime priority phase exclusive access during the live
        # compute window and resume unfinished coverage repair post-close.
        # Structure and decision bytes do not change, so this exact operational
        # suffix may compose with the reviewed cache migrations above.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:0d22ca4b0ef09cb2257f16da4df67d2a76ca9242595ba888277fe91545e5d69e",
            "sha256:5ca05713eadcc473f56fe095e2f5100b2995fd6edf5fc3a50968cffff7caa273",
        ),
        # Close the 15:01-15:05 scheduling gap and explicitly exempt automatic
        # cache recovery from deferral.  This changes no persisted decisions.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:5ca05713eadcc473f56fe095e2f5100b2995fd6edf5fc3a50968cffff7caa273",
            "sha256:f68e7eab264e0d818a2ddaf4fd4a5bee003f943c9dc6e5110bd20d13f52c6824",
        ),
        # Increase the live priority admission budget from 55s to 58s while
        # retaining the fixed 60s cadence and absolute native deadlines.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:f68e7eab264e0d818a2ddaf4fd4a5bee003f943c9dc6e5110bd20d13f52c6824",
            "sha256:befa02220a62df34a24eeb76e41f7f56132ed9d6eb2a2f1b87dea082d474c7c8",
        ),
        # A new session can own a valid in-progress checkpoint before it owns a
        # complete generation.  Falling back only to the previous close's
        # validated sector/code identities keeps realtime discovery continuous;
        # no legacy signal, structure or current-session result is published.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:f8b50b590300f6d507c4d84ec82ce86b03a4c94f9e9e8ffeca546cc767707d20",
            "sha256:6769cc39632997717a5569a52ce5020949c8766afe8ea550ebbb0109a8a31c8f",
        ),
        # Generation probing is now transactional: a valid but wrong-epoch
        # legacy publication cannot mark itself adopted or be retired during
        # startup.  This changes no cached decision or evidence byte.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:6769cc39632997717a5569a52ce5020949c8766afe8ea550ebbb0109a8a31c8f",
            "sha256:e3c105f18edb5351012125f35bc8c6b4e900925e09059b1e7cd995b599893c59",
        ),
        # Independent per-affinity 1m streams remove accumulated wave-tail
        # latency without changing any structure input, decision rule, durable
        # document, or cache schema. The exact authenticated snapshot is safe
        # to retain and re-sign under this operational-only suffix.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:e3c105f18edb5351012125f35bc8c6b4e900925e09059b1e7cd995b599893c59",
            "sha256:36bfcb7c5f7b0cd6613ec592653ba0cad465c569d533141d66a57d8a7805c13f",
        ),
        # Register only the reviewed 320-to-384 full-market monitor-capacity
        # projection. It changes no structure input, signal, or ranking and is
        # independently validated before the cached publication is re-signed.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:36bfcb7c5f7b0cd6613ec592653ba0cad465c569d533141d66a57d8a7805c13f",
            "sha256:f438ca0e6638b5d055df197c478949a081b8d34a3eef34fcbcd2c7d8068f3763",
        ),
        # Advance a cold locator by its validated per-code 1m observation age
        # instead of alternating only the last two partial waves. The signal
        # engine, market inputs and persisted document contract are unchanged.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:f438ca0e6638b5d055df197c478949a081b8d34a3eef34fcbcd2c7d8068f3763",
            "sha256:5f5305a74353dcb637f71ea24f13913902cb29cb31cf18c1422bbeeba83a8e30",
        ),
        # Admit the final hot symbol when its own shard has just demonstrated
        # that it fits before the absolute deadline. No market input, strict
        # structure, signal rule, or persisted payload changes.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:5f5305a74353dcb637f71ea24f13913902cb29cb31cf18c1422bbeeba83a8e30",
            "sha256:8dae5e9e3172bac95e10a6d6581b6842185bfaa0983516c4267f4fa02a472679",
        ),
        # Completed-epoch reuse and a larger decoded runtime L1 alter only the
        # physical cache path; strict frame and decision bytes are identical.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:781bdae926d461b657def096fc9994f013eaa29c6b97e74d31c06b07b8749637",
            "sha256:24e563bfd172730d504c854bd191256da18f8381563a75eae5eff437881e4752",
        ),
        # The first live deployment already persisted the completed-epoch
        # reuse release before the decoded 5m L1 was widened. Preserve that
        # exact intermediate cache as well as the direct pre-release edge.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:5a42f1c112c80a1138f1d1d2182aa620dcb2ba993741e1a0bf32beec99731387",
            "sha256:24e563bfd172730d504c854bd191256da18f8381563a75eae5eff437881e4752",
        ),
        # Retain the completed-epoch fast path but return decoded 5m residency
        # to the measured memory-safe bound after production pressure testing.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:24e563bfd172730d504c854bd191256da18f8381563a75eae5eff437881e4752",
            "sha256:c713b6774c60c49cafa4dafddf0421252a97a2b3f13881b68984e9a83ff47d86",
        ),
        # Querying a retained canonical 09:31 row must also include QMT's
        # source-only 09:30 opening event.  The event is folded away by the
        # existing normalizer, so this only restores the intended incremental
        # transport path and leaves the analyzed frame and decisions unchanged.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:c713b6774c60c49cafa4dafddf0421252a97a2b3f13881b68984e9a83ff47d86",
            "sha256:68be97ca72de7e69920d80b45f21229299e4ed974424e15e3c9a5eee20623706",
        ),
        # Hot states already own an authenticated complete prefix.  Read only
        # the exact overlapping market tail, then reconstruct and validate the
        # same full frame in memory instead of retransferring thousands of
        # unchanged QMT rows on every minute.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:68be97ca72de7e69920d80b45f21229299e4ed974424e15e3c9a5eee20623706",
            "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74",
        ),
        # The 384-subject production ceiling can place more than 32 symbols on
        # one deterministic affinity shard. Retaining 48 decoded 1m runtimes
        # removes LRU restore thrash; all market inputs and decision bytes stay
        # identical, so this exact cache-only source suffix may compose.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74",
            "sha256:9953acc53171ab81e2c32533bc243627548bba1ff450081b8bd7b8e0715235ba",
        ),
        # Recover the immutable complete generation when a reviewed deployment
        # lands while the main pointer contains an authenticated in-progress
        # checkpoint.  This changes only startup cache selection; neither the
        # checkpoint nor a legacy decision is published without current
        # contract validation.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:e45c252d40dab5845b80532c1ee7a0dd99919d0bb23c594be8fd66c02a3b069c",
            "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
        ),
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:adcae28654104f00a33d99d621eae3cb59dfe2b0d69fece45754bb1041eb8f7f",
            "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
        ),
        # Stripe the already-balanced whole-sector queues by their installed
        # worker slot.  This changes only physical request order and audit
        # wording; every symbol keeps the same worker, inputs and decision.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce",
        ),
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce",
            "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d",
        ),
        # A full-market process can restore a content-addressed publication
        # while the mutable main pointer still belongs to a bounded validation
        # scope.  Route large review validation to the exact recovered file;
        # this changes only which identical durable bytes the validator reads,
        # never a structure, signal, ranking, or coverage decision.
        (
            "web/chanlun_chart/cl_app/services/trading_screening.py",
            "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d",
            "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
        ),
        # Preserve the original stale-history cause in fallback telemetry.
        # Frame validation, subscription renewal, downloads and analysis are
        # unchanged.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:ae039de5d26ff45b1d3d97362ed86e86b7840744859a795e90cf0be395e5dae8",
            "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
        ),
        # Parallel sector execution and the wider native read batch are an
        # operational suffix that can safely compose with every already
        # reviewed exact migration into the preceding release.
        (
            "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
            "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            "sha256:781bdae926d461b657def096fc9994f013eaa29c6b97e74d31c06b07b8749637",
        ),
        (
            "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
            "sha256:4ef9af14e4500ed7dbf55af575e510b8b02178dbfc25101ac21f547518778fcf",
            "sha256:52e60874bf524a58c53dbc3b549e78bd5766112b3c56d7c81e764bd335c268f4",
        ),
        (
            "web/chanlun_chart/cl_app/services/trading_screening_process.py",
            "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
        ),
        # A process-local MiniQMT subscription only keeps the same requested
        # 1m/5m stream current between disk downloads.  The gateway still
        # applies the identical adjustment, causal close cutoff, full frame
        # validation, content revision, and exact-symbol download fallback.
        (
            "src/chanlun/exchange/exchange_qmt.py",
            "sha256:31d023aab9d8f5ef951a8ea16866fffdb9c01f50a35e0063a438bd32ea25d617",
            "sha256:aab8af80fad0e489271e4762f28fc731e6fcacef07e73e1fbb023f33de7452ea",
        ),
        (
            "src/chanlun/exchange/exchange_qmt.py",
            "sha256:aab8af80fad0e489271e4762f28fc731e6fcacef07e73e1fbb023f33de7452ea",
            "sha256:006cf995b1ecde54355cbb9df63de044130967a96d200f60fe3bdd0a82b8a857",
        ),
        (
            "src/chanlun/exchange/exchange_qmt.py",
            "sha256:31d023aab9d8f5ef951a8ea16866fffdb9c01f50a35e0063a438bd32ea25d617",
            "sha256:006cf995b1ecde54355cbb9df63de044130967a96d200f60fe3bdd0a82b8a857",
        ),
    }
)
_REVIEWED_COMPOSABLE_ORCHESTRATION_SOURCE_TRANSITIONS = frozenset(
    {
        # Static reachability analysis plus the full regression suite prove
        # these helpers have no callers.  Keep all four removals atomic so no
        # subset (or unrelated edit in the same file) can borrow this reviewed
        # cache migration.
        (
            (
                "src/chanlun/core/strict_structure/strength.py",
                "sha256:b6eea83b5b04013e07daf15b8e561c579f071dfa5421309d78729ad6e2b0e53f",
                "sha256:6dd15451597fe36c0d13ebef26d2256b6688794f5bc08e1d7cf703b3bfcf5d9a",
            ),
            (
                "src/chanlun/decision_support/trading_system/human_review_screening.py",
                "sha256:2aa7f811c55128422e789497c3aa9d550d1cb7d3c37574b9dfa1dc1668b5f77a",
                "sha256:a3b2ae6413df5b286f41b9e8c0b32c1dcbe060e16f0a3962e63e8a6b93da053c",
            ),
            (
                "src/chanlun/decision_support/trading_system/qmt_same_base_stream.py",
                "sha256:d1f656f3c58498aef87a0adfb7a2bc0c8bddf7c3702034e8a782ca0c9136b13c",
                "sha256:952400669e3d4c8bde6a796e1fa363cb95c24be48ffc9ea4ab4dc5d5ea52186d",
            ),
            (
                "src/chanlun/exchange/qmt_screening_sector_source.py",
                "sha256:94f16f459cbf9b1f5cd2a587729726a8ee8ad77b531b93507a7aa1f01a010cac",
                "sha256:2516025c59ae3b676d31e26b30a8bf1647217ff867c0f8a4f4bbae3736348365",
            ),
        ),
        # Keep the realtime scheduler independent of long native progress
        # calls, stop every live lane before the atomic-publish reserve, and
        # return each completed sector shard to the urgent pool immediately.
        # These three files form one exact operational release; authorizing a
        # partial combination could recreate the missed-minute failure.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:befa02220a62df34a24eeb76e41f7f56132ed9d6eb2a2f1b87dea082d474c7c8",
                "sha256:f8b50b590300f6d507c4d84ec82ce86b03a4c94f9e9e8ffeca546cc767707d20",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:e55b855f2553e5f358462add507cc16d1150fa8576e0400b3061ad1e3ade5bbe",
                "sha256:092fb536a4cfcc0e6a3fe4d493082e9494680d07628ee859c9bb0017e0eacc8f",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py",
                "sha256:fb0386a7a36802d11eb8f80de82d4917fed6f01e73ab3259b1ade498f5bdd688",
                "sha256:e074b522c6f7dd02a4091737f0397880f848bac5df3d77618e26174c6777484b",
            ),
        ),
        # This scheduling release may be a suffix of an older reviewed cache
        # migration, but its two rows must move together.  Treating either row
        # as an independently composable edge would authorize impossible mixed
        # service/process deployments.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
                "sha256:21391c06f6ff6b87863dc14676c2ddfda0818fd3545e4f452ed91b23fa5881d9",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
                "sha256:e55b855f2553e5f358462add507cc16d1150fa8576e0400b3061ad1e3ade5bbe",
            ),
        ),
    }
)
_REVIEWED_ORCHESTRATION_SOURCE_TRANSITIONS = frozenset(
    {
        # Reuse an already-validated completed 5m analysis during the remaining
        # one-minute polls of that same immutable epoch, retain the decoded 5m
        # runtime for the exact affinity workset, and widen only the explicitly
        # authorized realtime locator ceiling. The strict frame, structure,
        # decision rows and full-market coverage epoch do not change.
        # Keep both rows in one exact transition so neither source edit can be
        # used independently to authorize an unrelated cached decision.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
                "sha256:2023dacd160858ea22539e051328699e65f287345c9efb1f88c5471b022cfcd6",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:781bdae926d461b657def096fc9994f013eaa29c6b97e74d31c06b07b8749637",
                "sha256:24e563bfd172730d504c854bd191256da18f8381563a75eae5eff437881e4752",
            ),
        ),
        # At a just-completed 1m/5m boundary, renew the process-local
        # subscription once before using the serialized history-download
        # fallback.  The stale check and all downstream facts are unchanged.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:d4944b72ac12081713638aa5964c1d7423b58ab0ea30cec6d3bb2f600e152445",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            ),
        ),
        # Remove the phase-wide predownload RPC and authorize local-first 1m/5m
        # reads instead.  The gateway verifies the exact expected completed bar
        # and falls back to the original per-symbol download for short or stale
        # history, so frozen structures and decisions remain unchanged.  The
        # exact composable ExchangeQMT row above supplies live cache continuity.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:abc71aed21fa9eab2e8be21edbc01fc828e4e2157f2e263646efb9d065fff7d2",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:5851ef90c2f970c5748bfa6dacc87646284dc61819743944ca42b77c6f283821",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # Tighten only the live-alert freshness gate and reduce bounded QMT
        # predownload lookback to the mutable tail.  Local frames still need the
        # full authenticated warmup history; an incomplete tail automatically
        # falls back to the original exact-symbol download.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:abe4dfabb07cdf94826979c3c06f5518a9cf682697e1e3427a6f70cc0907c721",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:6acf4ebc19f4027253616b38d215075354306104f3c65506c1a63a9a0bfce4c0",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # Prepare the complete admitted phase in one bounded call and ask QMT
        # for the alert-critical 1m base before 5m.  This only removes repeated
        # transport round trips; the prepared facts and validation path below
        # this boundary are unchanged.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:31b91d64700632fd9fbbacdc43caf537c47157f984e288bb305fc38162feb488",
                "sha256:abe4dfabb07cdf94826979c3c06f5518a9cf682697e1e3427a6f70cc0907c721",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:d7d3a160f8c5c18eb5b75df3304e182fa24e31d2a67020f5ac8ce651e68f7634",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            ),
        ),
        # Replace per-symbol serialized QMT downloads with a short cancellable
        # batch refresh of the same 1m/5m bases.  Every prepared frame still
        # passes the existing strict validation and automatically falls back to
        # the original per-symbol refresh when local history is incomplete.
        # Service/native/process changes only propagate deadlines and partial
        # preparation scope; no frozen decision field or rule changes.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9407da9bd22a530b3573f28fb8e97b5ebf756b2202bfd7b81d969ab0a67458b6",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:7e83375a8b8170913dc1f171c971c7a2cff1c220d4840549098562159347d654",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
                "sha256:8901b2dcfb32978a17bc6636f50eff6b03dca59ece299d145421610910a56e47",
                "sha256:4ef9af14e4500ed7dbf55af575e510b8b02178dbfc25101ac21f547518778fcf",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:381270d91c7e24ba176cfa4095aba941f779f1d5d44c9f2eaa2c8ee609287737",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # Realtime capacity sizing, lunch-session 1m warmup, and alert-health
        # telemetry change only when/how an already-authenticated candidate is
        # monitored.  They do not alter frozen coverage rows or decisions.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
        ),
        # The deployed complete snapshot also predates the gateway's bounded
        # hot-cache sizing and same-completed-5m-epoch fast path.  Both return
        # the identical authenticated analysis for an identical close; retain
        # that snapshot across the exact two-file operational transition.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:d2496f15ec376b68f4fb1135ced452093c1714d2e34dc516fba650ccfefa9433",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            ),
        ),
        # Preserve a valid snapshot when a deployment skips both the affinity
        # alignment and the realtime-capacity release.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # The balanced affinity plan already routes whole sectors to exact
        # workers; this release makes the service's coverage-wave order use
        # that installed plan instead of the pre-balance hash.  Only physical
        # scheduling and its audit contract change, so a completed or resumable
        # decision snapshot remains byte-for-byte valid.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:381270d91c7e24ba176cfa4095aba941f779f1d5d44c9f2eaa2c8ee609287737",
            ),
        ),
        # Daily completion dispatch observes an already-finalized snapshot and
        # only queues an operational DingTalk receipt. It does not change the
        # frozen universe, structure evaluation, signal rows, or scan result.
        # Direct edges retain reviewed deploy-skipping states that may still be
        # present in a complete or resumable cache.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:c7bf29c6b9d56da88d037acd77da808217828c590a62815bf69acec01e7605b6",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        # A multi-minute atomic sector rebuild owns the first candidate shard.
        # Excluding that already-busy shard from the 1m burst router changes
        # only physical scheduling and health telemetry; fixed authenticated
        # structure bundles and every resulting decision remain identical.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:cbfd8b3c23680a2b604bae14d0c2baf8a8dc14fb537824bcb38184b5572fb0a7",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
            ),
        ),
        # Realtime approaching/preconfirmation delivery changes only monitor
        # orchestration and requested live frequencies. It does not alter a
        # structure document, candidate decision, or frozen coverage result.
        # Keep direct edges from the immediately preceding build and reviewed
        # deploy-skipping states so an operational notification release cannot
        # discard a valid decision snapshot.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:c7bf29c6b9d56da88d037acd77da808217828c590a62815bf69acec01e7605b6",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:f314a453febeb7c5eaa63f73e74384d3c3f394cb267853098ac2ed0a278f84a5",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
                "sha256:f314a453febeb7c5eaa63f73e74384d3c3f394cb267853098ac2ed0a278f84a5",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa",
                "sha256:3cd8d938d16a422000dd7f6ea307645bed15c4a30094bd2302c845392b23cc85",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
                "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:ec204210c310ca0ca1f87057e1b41b13648062be48910b9b116a2c607a524434",
                "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:43e1a04db1d82ef7a81de2002752d93e8a2ee22e0c6d23b2a5a0a5b7512469fa",
                "sha256:cbfd8b3c23680a2b604bae14d0c2baf8a8dc14fb537824bcb38184b5572fb0a7",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
                "sha256:43e1a04db1d82ef7a81de2002752d93e8a2ee22e0c6d23b2a5a0a5b7512469fa",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:745fbf8abdc2864c06b2467f08d9fcda49f101385aaa7adc8e4cdc635e62e0c7",
                "sha256:ec204210c310ca0ca1f87057e1b41b13648062be48910b9b116a2c607a524434",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
                "sha256:745fbf8abdc2864c06b2467f08d9fcda49f101385aaa7adc8e4cdc635e62e0c7",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:b554ac6c931cea904e0660dff27fe57537adddd9cd25f2cf4cc3285464966f03",
                "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
                "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
                "sha256:4b5223d73c250f293940556ec858622b4e44fc8762fb2ff9e8893320dbb0bb56",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:f4d1bf3f5030621a03c590946d28b318bbb715d4c9ad187b9b463324d7f81d25",
                "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:34bad75e736608383eae305e0979f50e71cd884330d3d947fed2945967d678ed",
                "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
            ),
        ),
        # Split only the physical sector-build schedule: authenticated catalog
        # partitions are merged before the unchanged global strength ranking,
        # parent gate and atomic publication.  The wider QMT daily read batch
        # changes request/GC count, not bars, adjustment, cutoff or decisions.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
                "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
                "sha256:781bdae926d461b657def096fc9994f013eaa29c6b97e74d31c06b07b8749637",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
                "sha256:4ef9af14e4500ed7dbf55af575e510b8b02178dbfc25101ac21f547518778fcf",
                "sha256:52e60874bf524a58c53dbc3b549e78bd5766112b3c56d7c81e764bd335c268f4",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
                "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
            ),
        ),
        # Stop admitting full-coverage structure requests while the exact 1m
        # alert phase drains and runs.  Service and process rows form one
        # inseparable scheduling transition: neither changes frozen market
        # facts, strict structures, signal documents, or coverage decisions.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
                "sha256:21391c06f6ff6b87863dc14676c2ddfda0818fd3545e4f452ed91b23fa5881d9",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
                "sha256:e55b855f2553e5f358462add507cc16d1150fa8576e0400b3061ad1e3ade5bbe",
            ),
        ),
    }
)
_REVIEWED_RESUMABLE_CHECKPOINT_SOURCE_TRANSITIONS = frozenset(
    {
        # Preserve an authenticated unfinished coverage ledger across the same
        # exact two-file live-alert admission release.  Keeping the pair here
        # prevents either scheduling row from authorizing a partial migration.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
                "sha256:21391c06f6ff6b87863dc14676c2ddfda0818fd3545e4f452ed91b23fa5881d9",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
                "sha256:e55b855f2553e5f358462add507cc16d1150fa8576e0400b3061ad1e3ade5bbe",
            ),
        ),
        # The worker-queue stripe changes only which already-fixed symbol is
        # submitted first.  Preserve an authenticated unfinished ledger so a
        # performance deployment does not throw away completed symbol work.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
                "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce",
                "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
                "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d",
            ),
        ),
        # The exact-file review validator changes only how a later completed
        # publication is located.  Preserve unfinished worker ledgers across
        # that release, including deployments that skip either queue-stripe
        # intermediate, so an operational restart never repeats completed
        # symbol work.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d",
                "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce",
                "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
                "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
            ),
        ),
    }
)
_REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS = frozenset(
    {
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5",
            ),
        ),
    }
)
_REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS = frozenset(
    {
        # A pure monitor-scheduling release must not discard an authenticated
        # unfinished retry queue from the immediately preceding deployment.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
        ),
        # Compose the reviewed retry repair, affinity alignment, and current
        # monitor-only release for a deployment that skips all three.
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # Preserve the reviewed retry-document repair when a deployment skips
        # directly over the scheduling-only affinity alignment above.
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:381270d91c7e24ba176cfa4095aba941f779f1d5d44c9f2eaa2c8ee609287737",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:f314a453febeb7c5eaa63f73e74384d3c3f394cb267853098ac2ed0a278f84a5",
            ),
        ),
    }
)
_REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS = frozenset(
    {
        # Preserve an exact completed-retry cleanup across the current
        # monitor-only release.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
        ),
        # Compose the completed-retry cleanup, affinity alignment, and current
        # monitor-only release without widening the accepted file set.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:97e36449b7b30ecbdb6d9cc2c15108f4cc7d197d8cc7bce7fddbf513474d5842",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            ),
        ),
        # Compose the exact completed-retry cleanup with the physical
        # affinity-order deployment without widening either migration.
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6",
                "sha256:381270d91c7e24ba176cfa4095aba941f779f1d5d44c9f2eaa2c8ee609287737",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:ba7d42d155bd5a45936f8bee9f6224ca9278bde0e684ac52130a075de564989e",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:32cb16b0d5cc0201617b9ca39b7a1d1245008a697f9ad0ae4509bc21f8049a8e",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:f314a453febeb7c5eaa63f73e74384d3c3f394cb267853098ac2ed0a278f84a5",
            ),
        ),
    }
)
_REVIEWED_SECTOR_SNAPSHOT_SOURCE_TRANSITIONS = frozenset(
    {
        # Dead helper/configuration removal changes the producer identity but
        # cannot change an already-authenticated sector payload.  Preserve the
        # exact deployed snapshot and re-sign it under this cleanup revision.
        (
            "sha256:2fbb5a59c874c65e51d82be910ac1d34b9fc18ae7b602324a3aa769392c614cf",
            "sha256:c7f38b622c9f5d88402ff8621a513cdd606a5d8d74c9d141810c7144600a041e",
        ),
        # Process-local quote subscriptions change only how already-requested
        # QMT bars reach the strict gateway.  A completed authenticated sector
        # snapshot contains the same causal facts and can be safely retagged.
        (
            "sha256:a0cdd42545120b187e5dddb9969a159326c7ab6a62b171c3c9f504e1715e1b71",
            "sha256:40c19ebe7856759765a45552fe327c80e90df7fd0ba5b1997ea3b56be3fe5609",
        ),
        (
            "sha256:57d9a704161f119f9800b6c90bf5e46b74cc2f6dbe40342ce696d0aab261a05a",
            "sha256:40c19ebe7856759765a45552fe327c80e90df7fd0ba5b1997ea3b56be3fe5609",
        ),
        # The process producer now exposes its already-installed affinity map
        # to the Web scheduler.  It does not alter QMT reads, composites,
        # structure facts, or the authenticated sector snapshot payload.
        (
            "sha256:3fe81e67380576efdc3ada6ed2dfc8e20cf0492483b3c8a740be817f0110e511",
            "sha256:5452b04068be4ab56e822c2871717ff0124040b6865fb2ae3eb015fd26834467",
        ),
        # The cached sector snapshot was already produced successfully. The
        # new gateway branch only rebuilds one stock's poisoned incremental
        # runtime after an exact lock-time rejection, so it cannot change that
        # authenticated, completed sector payload.
        (
            "sha256:909bc520565bdae72196f32c407cb254a4db725cc56f0f57d21e89fe69dd4a9b",
            "sha256:3fe81e67380576efdc3ada6ed2dfc8e20cf0492483b3c8a740be817f0110e511",
        ),
        # The producer bytes changed only in Web-side live worker routing and
        # health reporting. The authenticated atomic sector payload is
        # unchanged, so a completed cache with exact scope proof is reusable.
        (
            "sha256:835e8fd2046f70f882bd0f611cd2f64d63fc9857875b2110d466933df07dbc8d",
            "sha256:909bc520565bdae72196f32c407cb254a4db725cc56f0f57d21e89fe69dd4a9b",
        ),
        (
            "sha256:bb88417a5a59aafc1891512071d40f0f0432f4a26469b26aba709146b10216ab",
            "sha256:fcb531d1e2940880845580d169999c5be7bc7d45875147c54605b38fc613bd9a",
        ),
        (
            "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4",
            "sha256:2a5e1822092334582e3480e6908e909f3bf5b9625ab273fd59b137d017f818b1",
        ),
        (
            "sha256:544bc1e62b74d754771c8764114d8c754f5fd4c91b9dededaa83e036538c1ac8",
            "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4",
        ),
        # The persisted sector bytes were completed before this release. Only
        # Web-side shard availability and live-deadline routing changed; the
        # catalog inputs, merge, ranking and atomic payload are identical.
        (
            "sha256:c4a0c23c76bca1f1108f18d342b47a08c91d6270f188df73ba8a54f145ec1cc8",
            "sha256:fce0b2ae3d9fbdbe10ecf32bd6fa80dcc994155fae51d4eab7086985e29593ab",
        ),
    }
)
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _authenticated_source_changed_rows(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> tuple[tuple[str, str | None, str | None], ...] | None:
    if not isinstance(cached_decision_source_snapshot_id, str) or not isinstance(
        current_decision_source_snapshot_id,
        str,
    ):
        return None
    if not isinstance(cached_decision_source_snapshot, Mapping) or not isinstance(
        current_decision_source_snapshot,
        Mapping,
    ):
        return None
    try:
        if (
            decision_source_snapshot_id(cached_decision_source_snapshot)
            != cached_decision_source_snapshot_id
            or decision_source_snapshot_id(current_decision_source_snapshot)
            != current_decision_source_snapshot_id
        ):
            return None
        cached_rows = cached_decision_source_snapshot["files"]
        current_rows = current_decision_source_snapshot["files"]
        if not isinstance(cached_rows, (list, tuple)) or not isinstance(
            current_rows,
            (list, tuple),
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    cached_files = {
        str(row["path"]): str(row["sha256"])
        for row in cached_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    current_files = {
        str(row["path"]): str(row["sha256"])
        for row in current_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    if len(cached_files) != len(cached_rows) or len(current_files) != len(current_rows):
        return None
    changed_paths = {
        path
        for path in set(cached_files).union(current_files)
        if cached_files.get(path) != current_files.get(path)
    }
    return tuple(
        sorted(
            (
                path,
                cached_files.get(path),
                current_files.get(path),
            )
            for path in changed_paths
        )
    )


def _reviewed_transition_with_composable_rows_allowed(
    changed_rows: tuple[tuple[str, str | None, str | None], ...],
    transitions: frozenset[
        tuple[tuple[str, str | None, str | None], ...]
    ],
) -> bool:
    # Compose only exact, directed suffix edges.  For example, if A->B is an
    # existing coordinated migration and B->C is explicitly marked as a
    # composable operational release, A->C is accepted by first reducing it to
    # A->B.  Independent per-file paths are never inferred and a reverse edge
    # cannot be traversed.
    pending = [changed_rows]
    visited = {changed_rows}
    while pending:
        state = pending.pop()
        if not state or state in transitions:
            return True
        for index, row in enumerate(state):
            path, old_digest, current_digest = row
            for suffix in _REVIEWED_COMPOSABLE_ORCHESTRATION_SOURCE_ROWS:
                suffix_path, suffix_old, suffix_current = suffix
                if suffix_path != path or suffix_current != current_digest:
                    continue
                replacement = list(state)
                if old_digest == suffix_old:
                    replacement.pop(index)
                else:
                    replacement[index] = (path, old_digest, suffix_old)
                candidate = tuple(sorted(replacement, key=lambda item: item[0]))
                if candidate not in visited:
                    visited.add(candidate)
                    pending.append(candidate)
        # Coordinated suffixes are reduced atomically.  Every path and current
        # digest must be present before any row is rewritten, so a partial
        # service/process release cannot borrow the authorization of the pair.
        state_by_path = {row[0]: row for row in state}
        for suffix_transition in (
            _REVIEWED_COMPOSABLE_ORCHESTRATION_SOURCE_TRANSITIONS
        ):
            if not all(
                path in state_by_path
                and state_by_path[path][2] == current_digest
                for path, _old_digest, current_digest in suffix_transition
            ):
                continue
            replacement_by_path = dict(state_by_path)
            for path, suffix_old, _suffix_current in suffix_transition:
                _state_path, cached_digest, _current_digest = replacement_by_path[
                    path
                ]
                if cached_digest == suffix_old:
                    del replacement_by_path[path]
                else:
                    replacement_by_path[path] = (
                        path,
                        cached_digest,
                        suffix_old,
                    )
            candidate = tuple(
                sorted(replacement_by_path.values(), key=lambda item: item[0])
            )
            if candidate not in visited:
                visited.add(candidate)
                pending.append(candidate)
    return False


def orchestration_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize an authenticated, byte-exact reviewed cache transition."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    changed_paths = set() if changed_rows is None else {row[0] for row in changed_rows}
    return bool(
        changed_rows
        and (
            changed_paths <= _ORCHESTRATION_ONLY_SOURCE_PATHS
            or _reviewed_transition_with_composable_rows_allowed(
                changed_rows,
                _REVIEWED_ORCHESTRATION_SOURCE_TRANSITIONS,
            )
            or _reviewed_transition_with_composable_rows_allowed(
                changed_rows,
                _REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS,
            )
            or _reviewed_transition_with_composable_rows_allowed(
                changed_rows,
                _REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS,
            )
            or _reviewed_transition_with_composable_rows_allowed(
                changed_rows,
                _REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS,
            )
        )
    )


def resumable_checkpoint_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize exact scheduling-only migration of an unfinished ledger."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and _reviewed_transition_with_composable_rows_allowed(
            changed_rows,
            _REVIEWED_RESUMABLE_CHECKPOINT_SOURCE_TRANSITIONS,
        )
    )


def suspension_evidence_recheck_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize only the reviewed status-hint/5m-evidence transition."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and _reviewed_transition_with_composable_rows_allowed(
            changed_rows,
            _REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS,
        )
    )


def incomplete_retry_reconciliation_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize one reviewed repair of an unfinished frozen retry queue."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and _reviewed_transition_with_composable_rows_allowed(
            changed_rows,
            _REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS,
        )
    )


def completed_retry_residue_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize one exact cleanup of stale errors after completed coverage."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and _reviewed_transition_with_composable_rows_allowed(
            changed_rows,
            _REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS,
        )
    )


def priority_monitor_state_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
) -> bool:
    """Authorize only forward paths through reviewed runtime-state releases."""

    if not (
        isinstance(cached_decision_source_snapshot_id, str)
        and isinstance(current_decision_source_snapshot_id, str)
        and _SHA256_ID.fullmatch(cached_decision_source_snapshot_id) is not None
        and _SHA256_ID.fullmatch(current_decision_source_snapshot_id) is not None
    ):
        return False
    pending = [cached_decision_source_snapshot_id]
    visited = {cached_decision_source_snapshot_id}
    while pending:
        source_id = pending.pop()
        for previous_id, next_id in (
            _REVIEWED_PRIORITY_MONITOR_STATE_SOURCE_TRANSITIONS
        ):
            if previous_id != source_id:
                continue
            if next_id == current_decision_source_snapshot_id:
                return True
            if next_id not in visited:
                visited.add(next_id)
                pending.append(next_id)
    return False


def sector_snapshot_source_migration_allowed(
    *,
    cached_source_revision: object,
    current_source_revision: object,
) -> bool:
    """Authorize one reviewed non-sector change by exact producer identities."""

    return bool(
        isinstance(cached_source_revision, str)
        and isinstance(current_source_revision, str)
        and _SHA256_ID.fullmatch(cached_source_revision) is not None
        and _SHA256_ID.fullmatch(current_source_revision) is not None
        and (cached_source_revision, current_source_revision)
        in _REVIEWED_SECTOR_SNAPSHOT_SOURCE_TRANSITIONS
    )


__all__ = (
    "completed_retry_residue_source_migration_allowed",
    "incomplete_retry_reconciliation_source_migration_allowed",
    "orchestration_source_migration_allowed",
    "priority_monitor_state_source_migration_allowed",
    "resumable_checkpoint_source_migration_allowed",
    "sector_snapshot_source_migration_allowed",
    "suspension_evidence_recheck_source_migration_allowed",
)
