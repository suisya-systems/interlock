"""Dispatcher state-machine helpers ported from claude-org-ja ``tools/dispatcher_runner.py``.

The package hosts :mod:`runner`. The lazy re-export shim that used to expose
``runner`` / ``LocaleConfig`` as package attributes was the discard content of
this file (PORTING_LEDGER.md D-0014); it is stripped rather than the file
deleted, because ``runner.py`` is a ``rewrite`` row and still lives here.
Import it by path: ``from claude_org_runtime.dispatcher import runner``.
"""
