# PLASMA

PLASMA: Platform for LSL-based Acquisition of Sensor Metrics and Analytics

## quickstart

- `conda create -n plasma python=3.12`
- `conda activate plasma`
- `pip install -e ".[all]"` — or a lean subset, e.g. `".[msense]"` / `".[qb2,pupil]"`
- `python -m plasma` (or the `plasma` console script)

`pip install -r requirements.txt` still works — it's a shim for `-e .[all,build,test]`.

Writable state (device config, gyro-bias calibration, `data/` recordings, session
log) location, in order: `$PLASMA_HOME` if set → a per-user app-data dir when
running as a packaged app (`~/Library/Application Support/PLASMA` on macOS,
`%LOCALAPPDATA%\PLASMA` on Windows, `~/.local/share/plasma` on Linux) → the
working directory when running from source. Resolved once at import — set
`PLASMA_HOME` before launching to override.

### use PLASMA from another project

```
pip install "plasma-app[msense] @ git+https://github.com/YuyiChang/PLASMA@v1.0.0"
```

The distribution is `plasma-app`; the import package is `plasma`. Extras map to
plugins: `msense`, `qb2`, `pupil`, `shimmer`, `obs` (and `all`).

## Known issue

- [ ] need manually set lidar ip addr
- [ ] the built-in LSL→XDF recorder captures **every** stream on the default
      liblsl session, so a concurrent `pytest` run or another lab tool leaks
      into the recording; re-Initialize doesn't fully tear down old outlets.
      Direction (SessionID scoping, outlet lifecycle): [`docs/lsl-lifecycle-and-scoping.md`](docs/lsl-lifecycle-and-scoping.md)


## developing a sensor plugin

Every sensor is a plugin: a `PlasmaDevice` subclass plus a registration entry in
`plasma/plugins.py`. Core code never imports a concrete device.

> full guide: [`plasma/devices/README.md`](plasma/devices/README.md)
> base class: `PlasmaDevice` in `plasma/devices/template.py`
> reference package (config section + tabs + multi-unit): `plasma/devices/msense/`

## references

- qb2: https://docs.blickfeld.com/qb2/Qb2/v1.10/guides/api.html
- pupil-labs: https://pupil-labs.github.io/pl-realtime-api/dev/