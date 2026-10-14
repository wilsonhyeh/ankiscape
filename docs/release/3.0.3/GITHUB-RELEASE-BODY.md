# GitHub release body draft — v3.0.3

AnkiScape 3.0.3 fixes switching skills. Nothing else changes, and installing
over 3.0.2 keeps your progress.

- You can switch back to a skill you trained before: its remembered resource
  no longer shows "Currently training" with a disabled Train button
- The Skills screen marks the skill you are training, and the "Training"
  badge only appears on the resource actually being trained
- Cooking now says it earns Cooked fish, which is what it always gave

Verified artifact: `ankiscape-3.0.3.ankiaddon`, SHA-256
`f6c287c9c3aba15f64b2342e5199083b3e18684fe1a6c61be5125892d9a0a84f`, from release
verification
[run 38444772317](https://github.com/wilsonhyeh/ankiscape/actions/runs/38444772317)
(seven desktop targets on macOS, Windows and Linux; the fix passed on all
seven). One macOS target missed a performance budget on a single sample in a
code path this release does not change; see READINESS.md.

Install: download `ankiscape-3.0.3.ankiaddon` and use
Tools → Add-ons → Install from file. The AnkiWeb listing (1808450369) gets the
same version.

Thanks to the AnkiWeb reviewer who wrote down the exact steps.

Not affiliated with or endorsed by Jagex Ltd. RuneScape/Old School RuneScape
and the relevant artwork belong to Jagex Ltd; image sourcing is attributed to
the Old School RuneScape Wiki.

**Full changelog:** see `CHANGELOG.md` in `docs/release/3.0.3/`.
