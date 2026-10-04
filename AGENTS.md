# Maintainer instructions

This repository distributes an Agent Skill, not a desktop application. The skill is self-contained under `.agents/skills/video-archive/`; keep its runtime Python standard-library-only. User-facing instructions and examples are in Chinese; communicate in the user's language when applying the skill.

Do not read or modify any parent project's video files, task state, control files or logs. Develop and test only in this repository and disposable temporary directories. Never use personal footage as test fixtures. Use short synthetic clips with one encoding thread for integration checks.

Do not turn this project's historical CRF choice, source paths, exclusions, or archival decisions into another user's defaults. Preserve the scan → goal → proposal → sample → human acceptance → batch → archival guidance sequence. Scripts cannot authenticate user consent; the agent must record actual user decisions.

Keep original media, existing outputs, 10-bit depth and known color metadata protected. Test changes to publication, recovery, path handling and approval invalidation. Do not publish a repository or upload media as part of local development.

Run `.venv/bin/python -B -m unittest discover -s tests -v`. Native macOS is the first validated platform. Mark other systems and AI clients unverified until tested.
