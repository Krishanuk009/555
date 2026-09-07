# Unified IPTV Playlist Auto-Updater

This repository is a clean, single-workflow reimplementation of the provider
and playlist behavior from the reference workflow repository.

It keeps the original project's useful outputs and transformations, but does
not copy its fragmented "one Python file + one workflow per playlist" design.

## What is included

The repository contains the **35 M3U filenames present in the reference
repository**, plus the existing `Extraz.m3u` file from this repository.

### Automatically regenerated M3U outputs

| Output | Source / transformation |
|---|---|
| `playlists/jtv.m3u` | StreamStar Worker |
| `playlists/jtv2.m3u` | `m3u-86e` JSON → M3U |
| `playlists/jtv3.m3u` | SixPG JioTV → Kodi-style M3U |
| `playlists/jtv4.m3u` | Alex GitHub M3U |
| `playlists/jtv5.m3u` | Ayush Worker |
| `playlists/jtvplus.m3u` | STBPLUS `Zio.m3u` |
| `playlists/jtvplus2.m3u` | AllInOne Reborn |
| `playlists/jtvplus3.m3u` | GeoPlus + biscuit + sportsbiscuit → Kodi-style M3U |
| `playlists/jtvplus4.m3u` | Alex Plus |
| `playlists/jtvplus5.m3u` | PremiumPlugX VIP |
| `playlists/jtvplus6.m3u` | Qwerty Geo `jiotv_cf.m3u` → Kodi-style M3U |
| `playlists/jtvplus7.m3u` | SixPG JioTV → Kodi-style M3U |
| `playlists/jtvplus8.m3u` | STBPLUS `ZioMobile.m3u` |
| `playlists/mixiptv.m3u` | PremiumPlugX VIP |
| `playlists/playlist.m3u` | DRMLive/Fusion |
| `playlists/pocket.m3u` | Pocket TV |
| `playlists/Star.m3u` | Sports JSON → Star Sports M3U |
| `playlists/Star2.m3u` | SixPG → Star Sports filter |
| `playlists/Star3.m3u` | PremiumPlugX VIP → Star Sports filter |
| `playlists/hotstar.m3u` | PremiumPlugX Hot endpoint |
| `playlists/digital.m3u` | PremiumPlugX Hot endpoint → Digital filter |

### JSON companion outputs

- `data/cookie.json`
- `data/star.json`
- `data/star2.json`

These reproduce the reference repository's JSON-generation behavior while
keeping generated data in a dedicated directory.

### Snapshot/static M3Us

The reference repository also contained M3Us that had no updater script or
workflow associated with them. Their snapshot files are included so the
repository starts with the same output set:

- `IPL2026.m3u`
- `Sport.m3u`
- `Sports.m3u`
- `Tnt.m3u`
- `airteltv.m3u`
- `sony.m3u`
- `sony2.m3u`
- `sony3.m3u`
- `sony4.m3u`
- `sports.m3u`
- `sun.m3u`
- `waves.m3u`
- `zee.m3u`
- `zee2.m3u`

These are **not automatically regenerated**, because the reference repository
did not provide a source/updater for them.

## Update schedule

`.github/workflows/update.yml` runs every 30 minutes (UTC) and can also be
started manually with **Run workflow**.

Only one workflow is used. It does not trigger itself from its own commits,
which avoids the workflow-loop problem that can occur when a playlist workflow
also listens to pushes to `main`.

## Reliability features

The updater has several protections beyond the reference implementation:

- one failed provider does not stop the remaining providers
- retries with backoff
- shared HTTP session
- response validation
- M3U entry validation
- atomic file replacement
- last-known-good preservation
- suspicious channel-count-drop protection
- unchanged files are not rewritten
- unchanged files are not committed
- derived outputs reuse cached downloads where possible
- Git push retries
- workflow concurrency protection

### Last-known-good behavior

If a provider returns an error, HTML, an empty playlist, an invalid M3U, or an
unexpectedly tiny playlist, the existing output is left untouched.

For example:

```text
Previous: 1,400 channels
New:        250 channels
Result:     reject update and keep previous file
```

This is particularly useful for temporary provider outages.

## Architecture

```text
                  GitHub Actions
                        |
                        v
              scripts/update_playlists.py
                        |
          +-------------+-------------+
          |             |             |
       Fetch          Parse         Transform
          |             |             |
          +-------------+-------------+
                        |
                 Validate output
                        |
                 Atomic replacement
                        |
                 Git commit + push
```

The source/output mapping is also recorded in:

`config/providers.json`

## Important note

The M3U files may contain provider-supplied playback metadata such as
`#KODIPROP`, `#EXTVLCOPT`, cookies, headers, or DRM-related fields. Support for
those directives varies by player. This updater preserves/transforms metadata
in the same general manner as the reference project; it cannot guarantee that
every IPTV player will interpret every directive identically.

Use only playlist sources and playback credentials/metadata that you are
authorized to access and redistribute.
