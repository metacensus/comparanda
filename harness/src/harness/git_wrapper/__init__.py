"""Harness git commit wrapper — path-based branch routing for the fork 2-branch model.

See docs/specs/harness-commit-wrapper.md for the spec.
"""

from harness.git_wrapper.commit import harness_commit, classify_paths, main

__all__ = ["harness_commit", "classify_paths", "main"]
