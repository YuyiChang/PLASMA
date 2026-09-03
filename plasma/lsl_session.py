import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionInfo:
    """Subject/session identity handed to every device on init. Devices and the
    dashboard read it both as attributes and, for legacy call sites, by key."""
    sub_id: str
    ses_id: str
    participant_enc: int
    log_root: str

    @property
    def log_dir(self):
        return os.path.join(self.log_root, self.sub_id, self.ses_id)

    def __getitem__(self, key):
        return self.log_dir if key == "log_dir" else getattr(self, key)


def encode_participant(sub, ses):
    sub_number = re.search(r'\d+', sub)
    if sub_number:
        sub_number = sub_number.group()
    else:
        sub_number = 0

    ses_number = re.search(r'\d+', ses)
    if ses_number:
        ses_number = ses_number.group()
    else:
        ses_number = 0

    integer_representation = int(sub_number) * 100 + int(ses_number)
    # print(sub_number, ses_number, integer_representation)

    return integer_representation

