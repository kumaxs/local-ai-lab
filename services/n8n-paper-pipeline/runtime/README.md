# Runtime Notes

This directory is for documenting runtime facts about `n8n-paper-pipeline`.

It is not a service directory and does not contain launchd, cron, Docker Compose, or process manager configuration.

Operational directories `n8n_inbox`, `n8n_outputs`, and `n8n_state` are ignored operational data and are not canonical documentation.

Cleanup or pruning of these directories must confirm worker state first (idle or stopped, no in-flight jobs).
