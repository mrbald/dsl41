"""The sample CLIs use the deployed worker's exit codes (CONTRACT.md "Activation is external")."""

from dsl41.file_processing_worker import EXIT_LOCK_BUSY, EXIT_REFUSED, EXIT_STOPPED, exit_codes

__all__ = ["EXIT_LOCK_BUSY", "EXIT_REFUSED", "EXIT_STOPPED", "exit_codes"]
