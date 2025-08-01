# lsl_integrated_sensor

## quickstart

- `conda create -n plasma python=3.12`
- `conda activate plasma`
- `pip install -r requirements.txt`
- `python -m plasma`

## Known issue

- [ ] need manually set lidar ip addr


## device template

for developing device-specific interface to be integrayed with `plasma.integrated_panel`

> refer to `PlasmaDevice` in `plasma/devices/template.py`

## references

- qb2: https://docs.blickfeld.com/qb2/Qb2/v1.10/guides/api.html
- pupil-labs: https://pupil-labs.github.io/pl-realtime-api/dev/