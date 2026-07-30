"""Rendering findings for humans: terminal, markdown, and pull-request comments."""

from .markdown import MARKER, render_pr_comment, render_report
from .terminal import print_result

__all__ = ["MARKER", "print_result", "render_pr_comment", "render_report"]
