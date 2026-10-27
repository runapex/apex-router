"""apex flow studio: a local web UI that shows the request flow from source to sink.

``apex-router studio`` serves one page on 127.0.0.1 (``server``). The page shows the flow graph
(``flow``), live counts from the local sinks (``live``), a dry-run "Test a prompt" through the real
resolver, and a reviewed Draft for settings (``drafts``). Stdlib only. See docs/STUDIO.md.
"""
