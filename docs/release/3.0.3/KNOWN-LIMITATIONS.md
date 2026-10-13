# AnkiScape 3.0.3 — known limitations

Everything in `docs/release/3.0.2/KNOWN-LIMITATIONS.md` (and through it 3.0.1
and 3.0.0) still applies unchanged. 3.0.3 adds only the following.

- **The reporter has not confirmed the fix.** The report came as an AnkiWeb
  review, with no way to reply on the listing. The fix is verified by four unit
  tests and the extended `ui-training` native journey, which fails on the
  3.0.2 code with the reported "Currently training" button and passes on 3.0.3.
- **Not swept beyond the training switch.** The review said "bugs
  everywhere" but named only this one. The related sweep covered the Skills
  screen, the Training screen and how scoring picks the skill and resource.
  Bank, Feats, Hiscores and sync were not re-reviewed for 3.0.3, and GitHub
  issues were not read (the CLI was signed out).
