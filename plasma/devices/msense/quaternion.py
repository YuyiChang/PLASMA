"""Shared quaternion math, kept in one place so every consumer (delta-rotation
composition in msense.py, 3D rendering in integrated_panel.py) agrees on the
same (x, y, z, w) component convention — the MSense IMU stream firmware keeps
q3 (the scalar/w term) non-negative when reconstructing it, which is the
standard sign-normalization for the *real* part of a unit quaternion, so wire
order (q0, q1, q2, q3) maps to (x, y, z, w), not (w, x, y, z)."""

IDENTITY_QUAT = (0.0, 0.0, 0.0, 1.0)


def quat_multiply(a, b):
    """Hamilton product a⊗b; quaternions as (x, y, z, w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_normalize(q):
    x, y, z, w = q
    n = (x * x + y * y + z * z + w * w) ** 0.5
    if n == 0:
        return IDENTITY_QUAT
    return (x / n, y / n, z / n, w / n)


def quat_to_axes(q):
    """Rotated local X/Y/Z unit axes for a unit quaternion (x, y, z, w).

    Returned axes double as the columns of the rotation matrix, so callers
    can also use them to rotate an arbitrary local-frame point p=(px,py,pz)
    via px*axes[0] + py*axes[1] + pz*axes[2]."""
    x, y, z, w = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)),
        (2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)),
        (2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)),
    )
