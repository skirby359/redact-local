"""Allow ``python -m redact_local``."""

from redact_local import netguard

netguard.install()

from redact_local.cli import main  # noqa: E402  (guard must be installed first)

raise SystemExit(main())
