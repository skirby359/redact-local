"""Errors that are safe to show on the console.

The CLI prints the message of a SafeError and nothing else from it. So a
SafeError message must never contain document text, a detected value, a line
of names.txt, or a file name from inside a matter folder. Use fixed wording,
counts, and paths the caller gave us. Put details in the private run.log.

Every other exception is reported by type name only.
"""


class SafeError(Exception):
    """An error whose message is safe to print (no document-derived text)."""

    exit_code = 1


class UsageError(SafeError):
    exit_code = 2
