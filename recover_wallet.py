#!/usr/bin/env python3
"""Try focused candidates against a Base64 OpenSSL enc backup."""

import argparse
import base64
import binascii
import datetime
import itertools
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def generated_candidates():
    roots = {
        "hellohello": ("hello", "hello"),
        "fuckyou": ("fuck", "you"),
        "fuckyouall": ("fuck", "you", "all"),
        "abc": ("abc",),
        "ryanisgay": ("ryan", "is", "gay"),
        "fua": ("fua",),
    }
    seen = set()

    for name, words in roots.items():
        for capitalization in itertools.product((False, True), repeat=len(words)):
            if len(words) > 1 and all(capitalization) and name != "ryanisgay":
                continue
            cased_words = tuple(
                word.capitalize() if capitalize else word
                for word, capitalize in zip(words, capitalization)
            )
            delimiters = (
                itertools.product(("", "-"), repeat=len(words) - 1)
                if name == "ryanisgay"
                else (("",) * (len(words) - 1),)
            )

            for separators in delimiters:
                if (len(words) > 1 and all(capitalization)
                        and name == "ryanisgay" and not all(separators)):
                    continue
                root = cased_words[0] + "".join(
                    separator + word
                    for separator, word in zip(separators, cased_words[1:])
                )
                if root not in seen:
                    seen.add(root)
                    yield root

                    for digit_count in (1, 2, 3):
                        digit_choices = ("1",) + ("123",) * (digit_count - 1)
                        for digits in itertools.product(*digit_choices):
                            for digit_separators in itertools.product(("", "."), repeat=digit_count):
                                candidate = root + "".join(
                                    separator + digit
                                    for separator, digit in zip(digit_separators, digits)
                                )
                                if candidate not in seen:
                                    seen.add(candidate)
                                    yield candidate


def candidates_from_wordlist(path):
    with path.open("r", encoding="utf-8", errors="strict") as wordlist:
        for line in wordlist:
            yield line.rstrip("\r\n")


def validate_envelope(path):
    with path.open("rb") as backup:
        prefix = b"".join(backup.read(256).split())[:64]
    try:
        decoded_prefix = base64.b64decode(prefix, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("backup does not start with valid Base64 data") from error
    if not decoded_prefix.startswith(b"Salted__"):
        raise ValueError("decoded backup does not have the OpenSSL Salted__ header")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Offline password recovery for a Base64 OpenSSL enc backup."
    )
    parser.add_argument("backup", type=Path, help="encrypted backup file")
    parser.add_argument(
        "--cipher", default="aes-256-cbc",
        help="OpenSSL cipher (Schildbach backup default: aes-256-cbc)",
    )
    parser.add_argument(
        "--digest", default="md5",
        help="OpenSSL legacy digest (Schildbach backup default: md5)",
    )
    parser.add_argument(
        "--kdf", choices=("legacy", "pbkdf2"), default="legacy",
        help="key derivation mode (Schildbach backup default: legacy, not PBKDF2)",
    )
    parser.add_argument(
        "--iterations", type=int,
        help="PBKDF2 iteration count, if non-default; requires --kdf pbkdf2",
    )
    parser.add_argument(
        "--validator",
        help="optional local validator command; include {file}, return 0 only for a valid wallet",
    )
    parser.add_argument(
        "--wallet-marker", default="org.bitcoin.production",
        help="plaintext marker expected in a protobuf backup when --validator is omitted",
    )
    parser.add_argument(
        "--wordlist", type=Path,
        help="optional additional candidate file (generated candidates are tried first)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("recovered-wallet.dat"),
        help="validated plaintext output (must not already exist; default: recovered-wallet.dat)",
    )
    parser.add_argument(
        "--log-file", type=Path, default=Path("wallet-recovery-attempts.txt"),
        help="append-only attempt log; candidate passwords are stored in clear text",
    )
    parser.add_argument(
        "--rate", type=float, default=1.0,
        help="maximum average attempts per second (default: 1)",
    )
    parser.add_argument(
        "--report-every", type=int, default=100,
        help="print progress every N attempts (default: 100)",
    )
    parser.add_argument(
        "--max-attempts", type=int,
        help="optional cap on attempts in this session",
    )
    args = parser.parse_args()

    if args.rate <= 0:
        parser.error("--rate must be greater than zero")
    if args.report_every <= 0:
        parser.error("--report-every must be greater than zero")
    if args.max_attempts is not None and args.max_attempts < 0:
        parser.error("--max-attempts must not be negative")
    if args.iterations is not None and (args.kdf != "pbkdf2" or args.iterations <= 0):
        parser.error("--iterations requires --kdf pbkdf2 and a positive count")
    if args.validator is not None:
        try:
            validator = shlex.split(args.validator)
        except ValueError as error:
            parser.error(f"invalid --validator command: {error}")
        if not validator or not any("{file}" in part for part in validator):
            parser.error("--validator must include a {file} argument")
        args.validator = validator
    elif not args.wallet_marker:
        parser.error("--wallet-marker must not be empty")
    return args


def decrypt_candidate(args, candidate, output_path):
    command = [
        "openssl", "enc", "-d", f"-{args.cipher}", "-a", "-md", args.digest,
        "-pass", "stdin", "-in", str(args.backup), "-out", str(output_path),
    ]
    if args.kdf == "pbkdf2":
        command.append("-pbkdf2")
    if args.iterations is not None:
        command.extend(("-iter", str(args.iterations)))

    try:
        result = subprocess.run(
            command,
            input=candidate.encode("utf-8") + b"\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("openssl executable was not found") from error
    return result.returncode == 0


def check_candidate(args, candidate, output_path):
    if not decrypt_candidate(args, candidate, output_path):
        return False

    if args.validator is None:
        marker = args.wallet_marker.encode("ascii")
        overlap = b""
        with output_path.open("rb") as plaintext:
            while chunk := plaintext.read(65536):
                block = overlap + chunk
                if marker in block:
                    return True
                overlap = block[-len(marker) + 1:]
        return False

    validator_command = [
        part.replace("{file}", str(output_path)) for part in args.validator
    ]
    try:
        result = subprocess.run(
            validator_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def save_without_overwrite(source, destination):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as plaintext:
            shutil.copyfileobj(plaintext, output)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def open_attempt_log(path, protected_paths):
    path = path.resolve()
    if any(path == protected for protected in protected_paths):
        raise SystemExit("Attempt log path must differ from the backup, wordlist, and output")
    for protected in protected_paths:
        if path.exists() and protected.exists() and os.path.samefile(path, protected):
            raise SystemExit("Attempt log must not be the same file as the backup, wordlist, or output")

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise SystemExit(f"Cannot open attempt log {path}: {error}") from error

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SystemExit(f"Attempt log is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        log = os.fdopen(descriptor, "a", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise

    log.write(json.dumps({
        "type": "session",
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "backup": str(protected_paths[0]),
        "warning": "candidate passwords are logged in clear text",
    }, ensure_ascii=True) + "\n")
    log.flush()
    return path, log


def write_attempt_log(log, attempt, candidate, outcome):
    log.write(json.dumps({
        "type": "attempt",
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "attempt": attempt,
        "candidate": candidate,
        "outcome": outcome,
    }, ensure_ascii=True) + "\n")
    log.flush()


def read_attempted_candidates(path):
    attempted = set()
    if not path.exists():
        return attempted

    try:
        with path.open("r", encoding="utf-8") as existing_log:
            for line_number, line in enumerate(existing_log, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SystemExit(
                        f"Cannot safely resume: invalid JSON in attempt log at line {line_number}"
                    ) from error
                if record.get("type") == "attempt" and isinstance(record.get("candidate"), str):
                    attempted.add(record["candidate"])
    except OSError as error:
        raise SystemExit(f"Cannot read attempt log {path}: {error}") from error
    return attempted


def main():
    args = parse_args()
    args.backup = args.backup.resolve()
    args.output = args.output.resolve()
    if args.wordlist is not None:
        args.wordlist = args.wordlist.resolve()
    args.log_file = args.log_file.resolve()

    if not args.backup.is_file():
        raise SystemExit(f"Backup file not found: {args.backup}")
    if args.output.exists():
        raise SystemExit(f"Output already exists; refusing to overwrite: {args.output}")
    if args.log_file == args.backup or args.log_file == args.output:
        raise SystemExit("Attempt log path must differ from the backup and output")
    if args.wordlist is not None and not args.wordlist.is_file():
        raise SystemExit(f"Wordlist not found: {args.wordlist}")
    if args.validator is not None and shutil.which(args.validator[0]) is None:
        raise SystemExit(f"Validator executable not found: {args.validator[0]}")
    try:
        validate_envelope(args.backup)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Backup validation failed: {error}") from error

    args.output.parent.mkdir(parents=True, exist_ok=True)
    protected_paths = [args.backup, args.output]
    if args.wordlist is not None:
        protected_paths.append(args.wordlist)
    previously_attempted = read_attempted_candidates(args.log_file)
    args.log_file, attempt_log = open_attempt_log(args.log_file, protected_paths)

    candidates = generated_candidates()
    if args.wordlist is not None:
        candidates = itertools.chain(candidates, candidates_from_wordlist(args.wordlist))

    attempts = 0
    skipped = 0
    seen_this_session = set()
    started = time.monotonic()
    next_attempt_at = started
    temporary_path = None
    print(f"Starting local recovery. Attempt log: {args.log_file}", flush=True)

    try:
        for candidate in candidates:
            if candidate in previously_attempted or candidate in seen_this_session:
                skipped += 1
                continue
            seen_this_session.add(candidate)

            if args.max_attempts is not None and attempts >= args.max_attempts:
                break

            delay = next_attempt_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)

            with tempfile.NamedTemporaryFile(
                prefix=".wallet-recovery-", dir=args.output.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)

            attempts += 1
            next_attempt_at = time.monotonic() + 1.0 / args.rate
            write_attempt_log(attempt_log, attempts, candidate, "started")
            print(f"Attempt {attempts}: testing {candidate!r}", flush=True)
            try:
                valid = check_candidate(args, candidate, temporary_path)
            except RuntimeError as error:
                write_attempt_log(attempt_log, attempts, candidate, "error")
                raise SystemExit(str(error)) from error

            if valid:
                write_attempt_log(attempt_log, attempts, candidate, "validated")
                save_without_overwrite(temporary_path, args.output)
                elapsed = time.monotonic() - started
                print(
                    f"Validated wallet found after {attempts} attempts in {elapsed:.1f}s.\n"
                    f"Password: {candidate!r}\n"
                    f"Recovered file: {args.output}",
                    flush=True,
                )
                return 0

            write_attempt_log(attempt_log, attempts, candidate, "rejected")
            temporary_path.unlink(missing_ok=True)
            temporary_path = None
            if attempts % args.report_every == 0:
                elapsed = time.monotonic() - started
                print(f"Session attempts: {attempts}; elapsed: {elapsed:.1f}s", flush=True)

        elapsed = time.monotonic() - started
        print(
            f"No validated wallet found. Session attempts: {attempts}; "
            f"previously tried/skipped: {skipped}; elapsed: {elapsed:.1f}s",
            flush=True,
        )
        return 1
    except KeyboardInterrupt:
        elapsed = time.monotonic() - started
        print(
            f"\nInterrupted. Session attempts: {attempts}; elapsed: {elapsed:.1f}s",
            flush=True,
        )
        return 130
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        attempt_log.close()


if __name__ == "__main__":
    sys.exit(main())