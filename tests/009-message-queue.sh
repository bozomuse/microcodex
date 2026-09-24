#!/bin/sh

queue_home=$TEST_WORKDIR/queue-home
write_test_credentials "$queue_home" || exit 1

# expect_process increments tests_run before run_with_mock allocates its fixture.
mock_number=$((tests_run + 2))
mock_dir=$TEST_WORKDIR/mock-$mock_number
expect_process "T9.1: messages typed mid-turn are queued and processed in order after the turn completes" 0 \
    run_with_mock message-queue env CODEX_HOME="$queue_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        "$RUBY" "$TEST_DIR/queue-ui.rb" microcodex "Start message queue test" \
        "$mock_dir/first-request" "$mock_dir/queued-done" <<'STDOUT' 3<<'STDERR'
STDOUT
STDERR
