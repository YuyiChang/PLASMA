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


# class IntegratedOutlet(StreamOutlet):
#     def __init__(self, name, peripheral, chunk_size=32, max_buffered=360, use_lsl=True):
#         self.name = name.replace(':', '-')
#         self.use_lsl = use_lsl

#         lsl_status = "OK" if self.use_lsl else "disabled"
#         self.msg = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."
#         self.msg_fun = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."

#         if self.use_lsl:
#             info = StreamInfo(name, "MotionSenSE", 3, 2, cf_double64, peripheral.address())
#             super().__init__(info, chunk_size, max_buffered)

#         self.log_dir = os.path.join(yams_dir, "default")


#     def tic(self):
#         now = datetime.datetime.now()
#         return now.strftime("%H:%M:%S")

#     def save_data(self, data):
#         self.log_path = os.path.join(self.log_dir, f"{self.name}.txt")
#         # Ensure the file exists
#         if not os.path.exists(self.log_path):
#             with open(self.log_path, 'w') as f: pass

#         # Append NumPy array as a line
#         with open(self.log_path, 'a') as f:
#             np.savetxt(f, [data], fmt='%s')

#     def push_sample(self, x):
#         if self.use_lsl:
#             formatted = '\t'.join(str(num) for num in x)
#             self.msg = f"📻 {self.tic()} last LSL pushed: {formatted}"
            
#             fun_msg = "".join(["✅" for i in range(int(time.time())%10)])
#             self.msg_fun = f"📻 {self.tic()} {fun_msg}"

#             x.append(time.time())
#             super().push_sample(x)

#         self.save_data(x)