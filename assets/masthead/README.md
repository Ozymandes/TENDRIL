# TENDRIL masthead

The mark above the `remote-agents` selector, derived programmatically from the
original logo master (`TENDRIL_LOGO.ans`, 200×56 truecolor half-blocks). No
glyph is hand-drawn: the master is treated as a raster and re-rendered with
Unicode quadrant blocks (`▘▝▖▗▀▄▌▐▛▜▙▟█`, 2×2 per cell), grid-fitted the way a
font hinter snaps stems to pixels.

| tier | size | used when the selector box is |
|---|---|---|
| `tendril_full.ans` | 50×8 | ≥ 50 columns (the 54-column phone) |
| `tendril_compact.ans` | 42×7 | 44–49 columns |
| plain `TENDRIL` caption + slogan | 2 rows | narrower |

Every tier carries the slogan **THE EDGE IS YOURS**, letter-spaced, in the
selector's muted gray, centred under the wordmark one row below it (in the
full tier it fits beside the icon's tail, so it adds no height). A tested
single down-right offset layer was rejected: at quadrant resolution it
cannot be applied without bridging strokes or leaving unrenderable cells.

The masthead is skipped when colour is off (non-tty, `NO_COLOR`, `TERM=dumb`)
and steps down a tier, or disappears, whenever it would push the selector or
prompt past the terminal height. The selector itself is never touched.

## Files

- `build_masthead.py` – deterministic converter + verifier (build time only)
- `master_coverage.txt` – lime coverage of the master, cropped to the logo
  (the 317 KB `.ans` is only needed to refresh this)
- `tendril_full.ans`, `tendril_compact.ans` – generated assets (`cat` them)
- `preview.py` – terminal-accurate PNG of the real selector screen
  (needs Pillow; `--masthead FILE` previews an asset in place of the built-in one)

Runtime cost is zero: `bin/remote-agents` embeds the glyph lines as plain
strings and only wraps them in colour when printing.

## Regenerate

```sh
python3 build_masthead.py --extract ~/Downloads/TENDRIL_LOGO.ans   # only if the master changed
python3 build_masthead.py --embed      # build + verify, prints the _MASTHEAD block
```

Paste the printed `_MASTHEAD` block over the one in `bin/remote-agents`, then:

```sh
python3 preview.py --cols 54 --out /tmp/screen.png        # eyeball it
python3 -m unittest discover -s ../../tests               # from here, or -s tests from the repo root
```

The tests fail if the committed assets, the embedded block and a fresh build
ever disagree, and they check that everything below the masthead is
byte-identical to the selector printed without it (and to `origin/main`'s
selector while origin/main predates the masthead).
