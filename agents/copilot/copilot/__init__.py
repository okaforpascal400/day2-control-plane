"""Observability Copilot: answers about the running system, with cited evidence.

`receipts.py` and `verify.py` are deliberately independent of the rest — the
receipt format is meant to outlive this project, and the verifier must run for
someone with no access to the chat, the cluster, or an API key.
"""
