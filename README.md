# hornlab-fusion-addin

WGLink is the maintained Fusion add-in for sending Fusion geometry to Waveguide Generator (WG), solving it there, and inserting or updating native WG geometry in Fusion. The add-in lives in `fusion-addins/WGLink/`. WG's platform installers package WGLink for macOS and Windows.

The former WG Metal pipeline is maintained separately as a frozen fallback. It is not part of this repository or WGLink's updater.

## Docs

- [WGLink user guide](docs/WGLINK-GUIDE.md)
- [WGLink implementation and development guide](fusion-addins/WGLink/README.md)
- [Naming a WGLink link](docs/WGLINK-LINK-NAMING.md)
- [Validating a WGLink change without a release](docs/WGLINK-DEV-LOOP.md)
- [Headless workflow status](docs/HEADLESS.md)
- [Retired pipeline guide](docs/WGMETAL-PIPELINE-GUIDE.md)

WGLink's Send and Solve dialogs use an automatic model domain; there is no manual Model dropdown. With a current WG, explicit exports also record surviving Fusion origin-plane cut history and the Y-up/Z-up modelling orientation. Older WG versions receive neither new manifest feature and retain their previous absent-domain behaviour.

## Install

The WG platform installer installs the packaged add-in and connects it to WG's runtime. For development from this checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/install_wglink_addin.py --symlink
```

Restart Fusion, then enable WGLink under Utilities > Add-Ins. A symlinked development install uses this repository's active environment for Update resampling. It does not depend on a sibling or legacy checkout.

## Dependencies

`requirements.txt` installs WGLink's development and test dependencies into the active environment. The packaged add-in uses WG's pinned runtime.

## Tests

```bash
.venv/bin/python -m pytest tests -v
```

## License

AGPL-3.0-or-later
