#!/bin/sh

# An unsigned JWT whose payload is {"exp":1000000000} (2001-09-09), so it is
# always past its expiry and must trigger a proactive refresh.
expired_jwt=eyJhbGciOiJub25lIn0.eyJleHAiOjEwMDAwMDAwMDB9.c2ln

# expect_process only compares process results; these cases also assert that
# the refreshed token was persisted back to auth.json.
check_saved_token() {
    desc=$1
    file=$2
    tests_run=$((tests_run + 1))
    if grep -q '"access_token": "refreshed-access-token"' "$file"; then
        printf 'ok %03d - %s\n' "$tests_run" "$desc"
    else
        tests_failed=$((tests_failed + 1))
        printf 'not ok %03d - %s\n' "$tests_run" "$desc"
    fi
}

# T9.1: the OPENAI_API_KEY environment variable authenticates the request when
# no auth.json exists. MICROCODEX_TEST_BEARER is exported (not passed through
# `env`) because the mock server process starts before the CLI and only sees
# the exported environment.
key_home=$TEST_WORKDIR/key-home
mkdir -p "$key_home" || exit 1
export MICROCODEX_TEST_BEARER="env-api-key"

expect_process "T9.1: OPENAI_API_KEY environment variable authenticates the request" 0 \
    run_with_mock api-key env CODEX_HOME="$key_home" \
        OPENAI_API_KEY="env-api-key" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Api key prompt <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
STDERR
unset MICROCODEX_TEST_BEARER

# T9.2: an OPENAI_API_KEY stored in auth.json is used when the variable is
# unset, taking precedence over the OAuth token set in the same file.
file_home=$TEST_WORKDIR/file-key-home
mkdir -p "$file_home" || exit 1
cat > "$file_home/auth.json" <<'EOF'
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "file-api-key",
  "tokens": {
    "id_token": "test-id-token",
    "access_token": "test-access-token",
    "refresh_token": "test-refresh-token",
    "account_id": "test-account"
  }
}
EOF
chmod 600 "$file_home/auth.json"
export MICROCODEX_TEST_BEARER="file-api-key"

expect_process "T9.2: auth.json OPENAI_API_KEY authenticates the request" 0 \
    run_with_mock api-key env -u OPENAI_API_KEY CODEX_HOME="$file_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Api key prompt <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
STDERR
unset MICROCODEX_TEST_BEARER

# T9.3: an HTTP 401 refreshes the OAuth token and retries the request with the
# new token; the refreshed token is saved back to auth.json.
refresh_home=$TEST_WORKDIR/refresh-home
write_test_credentials "$refresh_home" || exit 1

expect_process "T9.3: HTTP 401 refreshes the token and retries the request" 0 \
    run_with_mock token-refresh-401 env -u OPENAI_API_KEY CODEX_HOME="$refresh_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Refresh the token <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
STDERR
check_saved_token "T9.3: refreshed token is persisted to auth.json" "$refresh_home/auth.json"

# T9.4: an access token that is already past its JWT expiry is refreshed
# before the first request goes out; the model catalog and the turn both use
# the new token, which is saved back to auth.json.
expired_home=$TEST_WORKDIR/expired-home
mkdir -p "$expired_home" || exit 1
cat > "$expired_home/auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "id_token": "test-id-token",
    "access_token": "$expired_jwt",
    "refresh_token": "test-refresh-token",
    "account_id": "test-account"
  }
}
EOF
chmod 600 "$expired_home/auth.json"

expect_process "T9.4: expired access token is refreshed before the request" 0 \
    run_with_mock token-refresh-expired env -u OPENAI_API_KEY CODEX_HOME="$expired_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Refresh the token <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
STDERR
check_saved_token "T9.4: refreshed token is persisted to auth.json" "$expired_home/auth.json"

# T9.5: when the proactive refresh fails (the refresh endpoint is down), the
# turn still goes out with the stored token instead of failing. The stored
# token is left untouched in auth.json; the startup warning is the only trace
# of the failed refresh.
fallback_home=$TEST_WORKDIR/fallback-home
mkdir -p "$fallback_home" || exit 1
cat > "$fallback_home/auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "id_token": "test-id-token",
    "access_token": "$expired_jwt",
    "refresh_token": "test-refresh-token",
    "account_id": "test-account"
  }
}
EOF
chmod 600 "$fallback_home/auth.json"

expect_process "T9.5: failed proactive refresh falls back to the stored token" 0 \
    run_with_mock token-refresh-fallback env -u OPENAI_API_KEY CODEX_HOME="$fallback_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Fallback after failed refresh <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
Warning: OAuth token endpoint returned HTTP 400: refresh_failed
STDERR
tests_run=$((tests_run + 1))
if grep -q '"access_token": "refreshed-access-token"' "$fallback_home/auth.json"; then
    tests_failed=$((tests_failed + 1))
    printf 'not ok %03d - %s\n' "$tests_run" "T9.5: auth.json was overwritten despite the failed refresh"
else
    printf 'ok %03d - %s\n' "$tests_run" "T9.5: auth.json keeps the stored token after a failed refresh"
fi

# T9.6: a failed proactive refresh is not retried on later requests. The
# session performs two model requests (the first triggers a tool call); the
# mock asserts the token endpoint saw exactly two attempts (startup + first
# turn) rather than three.
skip_home=$TEST_WORKDIR/skip-home
mkdir -p "$skip_home" || exit 1
cat > "$skip_home/auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "id_token": "test-id-token",
    "access_token": "$expired_jwt",
    "refresh_token": "test-refresh-token",
    "account_id": "test-account"
  }
}
EOF
chmod 600 "$skip_home/auth.json"

skip_cwd=$case_cwd
case_cwd=$TEST_PROJECT
# 003-tool-loop.sh already created result.txt via the same tool call; the
# write tool refuses to overwrite, so start from a clean slate.
rm -f "$TEST_PROJECT/result.txt"
expect_process "T9.6: failed proactive refresh is not retried on later requests" 0 \
    run_with_mock token-refresh-fallback-skip env -u OPENAI_API_KEY CODEX_HOME="$skip_home" \
        PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Trigger a tool call after failed refresh <<'STDOUT' 3<<'STDERR'
Created result.txt
STDOUT
Warning: OAuth token endpoint returned HTTP 400: refresh_failed

[tool write] {"path":"result.txt","content":"made by tool\n"}
[tool write completed] Created result.txt
STDERR
case_cwd=$skip_cwd

# T9.7: when persisting the refreshed token fails, the turn still goes out
# with the refreshed token instead of discarding it. A tiny LD_PRELOAD /
# DYLD_INSERT_LIBRARIES shim fails the credential temp-file creation with
# EACCES, which breaks the save deterministically even for privileged users.
# auth.json must keep the stale token afterwards.
savefail_home=$TEST_WORKDIR/savefail-home
mkdir -p "$savefail_home" || exit 1
cat > "$savefail_home/auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "id_token": "test-id-token",
    "access_token": "$expired_jwt",
    "refresh_token": "test-refresh-token",
    "account_id": "test-account"
  }
}
EOF
chmod 600 "$savefail_home/auth.json"

shim_dir=$TEST_WORKDIR/savefail-shim
mkdir -p "$shim_dir" || exit 1
cat > "$shim_dir/nosave.c" <<'EOF'
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/types.h>

static int fail_credential_tmp(const char *path) {
    return path != NULL && strstr(path, "auth.json.tmp.") != NULL;
}

int open(const char *path, int flags, ...) {
    if (fail_credential_tmp(path)) {
        errno = EACCES;
        return -1;
    }
    int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open");
    va_list ap;
    va_start(ap, flags);
    mode_t mode = va_arg(ap, mode_t);
    va_end(ap);
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    if (fail_credential_tmp(path)) {
        errno = EACCES;
        return -1;
    }
    int (*real_open64)(const char *, int, ...) = dlsym(RTLD_NEXT, "open64");
    va_list ap;
    va_start(ap, flags);
    mode_t mode = va_arg(ap, mode_t);
    va_end(ap);
    return real_open64(path, flags, mode);
}
EOF
if [ "$(uname -s)" = "Darwin" ]; then
    shim_lib=$shim_dir/nosave.dylib
    cc -dynamiclib -o "$shim_lib" "$shim_dir/nosave.c" || exit 1
    shim_preload="DYLD_INSERT_LIBRARIES=$shim_lib"
else
    shim_lib=$shim_dir/nosave.so
    cc -shared -fPIC -o "$shim_lib" "$shim_dir/nosave.c" -ldl || exit 1
    shim_preload="LD_PRELOAD=$shim_lib"
fi

expect_process "T9.7: failed credential save keeps the refreshed token for the session" 0 \
    run_with_mock token-refresh-save-fail env -u OPENAI_API_KEY CODEX_HOME="$savefail_home" \
        "$shim_preload" PATH="$TEST_BIN_DIR:$PATH" \
        microcodex Refresh despite an unwritable credential file <<'STDOUT' 3<<'STDERR'
Hello, world!
STDOUT
Warning: Could not create temporary credentials file: Permission denied; using the refreshed token for this session only
STDERR
tests_run=$((tests_run + 1))
if grep -q "\"access_token\": \"$expired_jwt\"" "$savefail_home/auth.json"; then
    printf 'ok %03d - %s\n' "$tests_run" "T9.7: auth.json keeps the stale token after the failed save"
else
    tests_failed=$((tests_failed + 1))
    printf 'not ok %03d - %s\n' "$tests_run" "T9.7: auth.json was modified despite the failed save"
fi
