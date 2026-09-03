import pylsl
from pylsl import StreamInfo, StreamOutlet
import time
import os
import re 

class SessionInfo():
    def __init__(self, sub_id, ses_id, log_root):
        self.sub_id = sub_id
        self.ses_id = ses_id
        self.log_dir = os.path.join(log_root, sub_id, ses_id)

        os.makedirs(self.log_dir, exist_ok=True)


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

