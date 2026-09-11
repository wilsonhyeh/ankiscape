# Asset rights: bundled game art

Maintainer record. Not user-facing copy. Recheck primary sources at each
release; this file records what is known and what remains unresolved.

## What ships

AnkiScape bundles Old School RuneScape game artwork (skill icons, item art,
textures) together with original generated UI icons, an original stone
texture, and the Press Start 2P font under the SIL Open Font License 1.1.
Provenance, hashes, source pages, revisions and retrieval dates live in
`assets/manifest.json`; every file's `rights` category resolves to
`assets/manifest.json → rights_notices`.

## Verified source

- [Jagex Fan Content Policy](https://legal.jagex.com/docs/policies/fan-content-policy),
  checked 2026-09-10; the page listed v1.3 at that time.
- Section 6.1.3 restricts software/game uses.
- Section 8.1 specifies attribution for covered uses.
- Section 14.1 calls for a separate agreement for uses beyond the policy.

The Old School RuneScape Wiki notice (recorded in the manifest) states that
its media is licensed from Jagex and that the Wiki uses it with permission.
That permission does not automatically license redistribution inside a
third-party add-on.

## Unresolved

Redistribution of the bundled Jagex game art inside the AnkiScape package is
**not cleared** by this repository. Continued development with this art
direction is settled; publication clearance is not established. If applicable
permission is obtained later, record its scope and any required wording here
and update the credits page; do not infer permission from personal preference
or from the Wiki's notice.

## Rules for maintainers

- Do not add new third-party game art without recording a `rights` category,
  source page, revision, retrieval date, dimensions and SHA-256 in the
  manifest (`scripts/fetch_osrs_assets.py --download` enforces this flow).
- Do not claim a license that has not been established, state a fair-use
  conclusion, or describe the art as "not copied" while it is bundled.
- Keep the font's OFL text packaged (`fonts/OFL.txt`).
- The credits page keeps the attribution language; keep this file out of the
  user-facing page.
