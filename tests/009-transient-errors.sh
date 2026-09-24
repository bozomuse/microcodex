#!/bin/sh

transient_home=$TEST_WORKDIR/transient-home
write_test_credentials "$transient_home" || exit 1

expect_process "T9.1: transient HTTP 503s are retried and the turn completes" 0 \
    run_with_mock transient-503 env CODEX_HOME="$transient_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex --model test-model Survive some blips <<'STDOUT' 3<<'STDERR'
Recovered after retry
STDOUT
STDERR

expect_process "T9.2: persistent HTTP 503s fail gracefully after retries" 1 \
    run_with_mock persistent-503 env CODEX_HOME="$transient_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex --model test-model Trigger persistent failure <<'STDOUT' 3<<'STDERR'
STDOUT
Agent failed: Codex API request failed after 4 attempts: Codex API returned HTTP 503: service unavailable
STDERR

retry_home=$TEST_WORKDIR/retry-home
write_test_credentials "$retry_home" || exit 1

expect_process "T9.3: a seed turn is saved before the failing turn" 0 \
    run_with_mock conversation-first env CODEX_HOME="$retry_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex --model test-model Remember alpha <<'STDOUT' 3<<'STDERR'
Alpha stored
STDOUT
STDERR

set -- "$retry_home"/conversations/*.jsonl
[ "$#" -eq 1 ] && [ -f "$1" ] || exit 1
retry_id=$(basename "$1" .jsonl)

expect_process "T9.4: an exhausted turn fails without touching saved context" 1 \
    run_with_mock persistent-503 env CODEX_HOME="$retry_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex resume "$retry_id" Trigger persistent failure <<'STDOUT' 3<<'STDERR'
STDOUT
Agent failed: Codex API request failed after 4 attempts: Codex API returned HTTP 503: service unavailable
STDERR

expect_process "T9.5: the durable transcript still holds only the seed turn" 0 \
    env CODEX_HOME="$retry_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex show "$retry_id" <<'STDOUT' 3<<'STDERR'
user: Remember alpha
assistant: Alpha stored
STDOUT
STDERR

expect_process "T9.6: the conversation continues normally after the failure" 0 \
    run_with_mock conversation-resume env CODEX_HOME="$retry_home" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex resume "$retry_id" Recall beta <<'STDOUT' 3<<'STDERR'
Alpha and beta recalled
STDOUT
STDERR
