# AGENTS.md

- VPS development/debugging uses SSH alias `nnnnn`.
- Live Docker container: `tg-ai-manager`;
- Do not keep a VPS git checkout for this project; use the local repo as source of truth.
- For fast live debugging, inspect logs with `docker logs tg-ai-manager`, copy changed files into the container with `docker cp`, then restart `tg-ai-manager`; these container edits are not image-persistent.
