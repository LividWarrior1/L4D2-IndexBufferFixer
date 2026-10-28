# Testing

## 1. What has already been verified (development, 2026-10-27)

| check | result |
|---|---|
| Unit tests (`python -m unittest discover tests`) | 12/12 pass (VPK write/read/CRC round trip, VMT patching incl. `patch` shaders, CRLF/BOM, addonlist editing, stock model parsing) |
| VPK reader on every add-on of the real install | 327/327 VPKs parsed, 0 errors, 1.8 s |
| MDL/VTX/VVD parser | stock survivors and special infected (`survivor_gambler`, `coach`, `hunter`, `witch`, `smoker`, `charger`), three custom survivors, two Workshop survivors/infected, and a 582-model map pack (`3366491323.vpk`, 817 MB, 5 s). Checksums of .mdl/.vtx/.vvd agree in every case. The VTX strip-group layout is auto-detected (L4D2 models are MDL v49 *without* the extra topology fields) |
| Full scan of the 40 enabled add-ons on the real install | 2.1 s; flags exactly the three custom survivors; detects three Workshop survivors already shipping `$nodecal` as MITIGATED |
| `fix --apply` (replace mode) on a copy of the install | repacked 313 MB and 42 MB VPKs; **every entry CRC-verified**, all non-VMT files byte-identical to the source, only the selected VMTs + `addoninfo.txt` differ; `addonlist.txt` updated |
| `fix --apply --mode inplace` | original backed up, rewritten in place, patched VMT confirmed |
| `revert --all` | outputs deleted, `addonlist.txt` byte-identical to the pre-fix backup, in-place original restored with identical SHA-1 |
| dry run | writes nothing (verified by directory listing and addonlist diff) |

After these checks the fix was applied for real on that install (replace mode)
and a full five-map campaign was played with the three fixed survivors active:
zero index-buffer errors in `console.log`.

## 2. In-game verification procedure

### 2.1 Set up logging (once)

1. Steam → Left 4 Dead 2 → Properties → Launch Options: add `-condebug`.
   The game now writes `left4dead2\console.log`.
2. `ibfix probe` then, in the game console: `exec ibfix_probe`. Quit.
3. `ibfix diagnose` - prints the real flags/defaults of `r_drawmodeldecals`,
   `r_maxmodeldecal`, `cl_player_max_decal_count`, … for your build.

### 2.2 Reproduce the crash (baseline)

Use a local game (`map c1m1_hotel` from the console, or any campaign) and
play as or next to the flagged model. Decals pile onto a survivor when:

* a Hunter pounces / a Jockey rides / a Witch or Tank hits you,
* you kill commons with melee at point-blank range (blood splatter lands on
  the nearest surfaces - including the survivor models),
* the custom infected itself gets shot (impact decals on *its* dense skin).

With a model like the custom Nick above, a single large splat on the skin material is
estimated above the limit, so a handful of melee kills in a horde should do
it. Note the time; `ibfix diagnose` afterwards should show
`Too many indices ... (N>32768)` and the map.

### 2.3 Apply and re-test

1. `ibfix scan` → `ibfix fix` (dry run) → `ibfix fix --apply`.
2. Start the game. Extras → Add-ons: the original shows **disabled**, the copy
   shows the same title with ` [IBFix]` **enabled** (replace mode).
   Overlay mode only: run `path` in the console; the `ibfix_*_overlay.vpk`
   line must appear **above** the add-on it overrides.
3. Repeat 2.2 for at least as long as it previously took to crash.
   Expected: no blood/bullet decals appear on the patched materials (that is
   the visible proof the VMT change took effect), decals still appear on the
   untouched materials, and no index-buffer error.
4. `ibfix diagnose` - no new `Too many indices` entries since the fix.

Success criterion: zero index-buffer errors over several sessions in which the
previously crashing situations occurred. If an error still appears, the log
context tells you whether it is the same model (then use
`--all-materials`, or a lower `--threshold`) or a different producer
(particles, another add-on - see `models mentioned shortly before`).

### 2.4 After a Workshop update

`ibfix status` marks fixes whose source add-on changed as **stale**;
`ibfix fix --apply --refresh` reverts and re-creates them.

### 2.5 Undo

`ibfix revert --all` restores everything (addonlist entries, deletes the
copies, puts back in-place originals from `backups/`).

## 3. Isolating an unknown culprit

If the log points at nothing obvious: `ibfix bisect start` disables half of
the enabled add-ons; play; `ibfix bisect bad` (crashed) or `good`; repeat
until one add-on is left; `ibfix bisect finish` restores the original list.

## 4. Running the unit tests on Windows

```
cd ibfix_tool
py -3 -m unittest discover tests -v
```

The two `RealFiles` tests need the game installed (they auto-detect it or use
`L4D2_DIR`).
