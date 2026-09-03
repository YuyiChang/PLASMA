# PLASMA

PLASMA: Platform for LSL-based Acquisition of Sensor Metrics and Analytics

## quickstart

- `conda create -n plasma python=3.12`
- `conda activate plasma`
- `pip install -r requirements.txt`
- `python -m plasma`

## Known issue

- [ ] need manually set lidar ip addr


## developing a sensor plugin

Every sensor is a plugin: a `PlasmaDevice` subclass plus a registration entry in
`plasma/plugins.py`. Core code never imports a concrete device.

> full guide: [`plasma/devices/README.md`](plasma/devices/README.md)
> base class: `PlasmaDevice` in `plasma/devices/template.py`
> reference package (config section + tabs + multi-unit): `plasma/devices/msense/`

## references

- qb2: https://docs.blickfeld.com/qb2/Qb2/v1.10/guides/api.html
- pupil-labs: https://pupil-labs.github.io/pl-realtime-api/dev/