# AGENTS.md

- VPS development/debugging uses SSH alias `nnnnn`.
- Live Docker container: `tg-ai-manager`;
- Do not keep a VPS git checkout; use the local repo as the source of truth.
- Live debugging: `docker logs tg-ai-manager`, then `docker cp` and restart.
- Container edits do not persist in the image.
- Use external authentication in production, not in-app web authentication.
- Never expose the production app port publicly.
