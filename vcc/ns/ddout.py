import os.path
from threading import Thread, Event
from datetime import datetime, timedelta
from threading import Event
import re
import logging

from psutil import process_iter, AccessDenied, NoSuchProcess

from vcc import VCCError
from vcc.messaging import RMQclientException

logger = logging.getLogger('vcc')


# Read records from log file opened by ddout
class DDoutScanner(Thread):

    is_pcfs = re.compile(r'^(?P<time>^\d{4}\.\d{3}\.\d{2}:\d{2}:\d{2}\.\d{2})(?P<data>.*)$').match
    is_header = re.compile(r'(?P<key>#onoff#    source)(?P<data>.*)$').match
    is_onoff = re.compile(r'(?P<key>#onoff#VAL)(?P<data>.*)$').match
    keys = [(re.compile(f'{separator}(?P<key>{key})(?P<data>.*)$').match, msg) for (separator, key, msg)
            in [(':', 'exper_initi', 'schedule loaded {ses_id}'),
                (':', 'sched_end', 'schedule ended'),
                (';', 'halt', 'schedule halted'),
                (';', 'contstatus', None),
                (';', 'cont', 'schedule continue'),
                (':', 'scan_name=[a-zA-Z0-9-]*', '{key}'),
                (':', 'source=[a-zA-Z0-9-+]*', '{key}'),
                (';', 'Log Opened:', 'log opened')]
            ]

    def __init__(self, sta_id, vcc):
        super().__init__()

        self.stopped = Event()
        self.sta_id, self.vcc = sta_id, vcc
        self.rmq = self.vcc.get_rmq_connection()
        self.log = self.active = self.ses_id = None
        self.last_time = {}
        self.onoff, self.header = [], []

    # Close the log file
    def close_log(self):
        if self.log:
            self.log.close()
        self.active = self.log = None

    # Open log file if different that active file
    def open_log(self, path):
        if path != self.active:
            self.close_log()
            self.active, self.log = path, open(path, 'r', encoding="utf8", errors="ignore")
            self.ses_id = os.path.basename(path)[:-6] if path.endswith(f'{self.sta_id.lower()}.log') else None
            logger.debug(f'OPEN LOG {path} SES_ID {self.ses_id}')
            logger.debug(self.last_time.get(self.active, datetime.utcnow() - timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S'))
        return self.last_time.get(self.active, datetime.utcnow() - timedelta(seconds=2))

    # Get the path of the file opened by ddout
    @staticmethod
    def get_ddout_log():
        for proc in process_iter(['name', 'pid']):
            if proc.info['name'] == 'ddout':
                try:
                    files = [file.path for file in proc.open_files() if file.path.startswith('/usr2/log')]
                    return files[0] if files else None
                except (NoSuchProcess, AccessDenied):
                    return None
        return None

    # Check if ONOFF header
    def is_onoff_header(self, info):
        rec = self.is_header(info)
        if not rec:
            return False
        self.header = ['source'] + rec['data'].split()
        self.send_onoff()  # Send existing onoff records to VCC
        return True

    # Check if ONOFF VAL record
    def is_onoff_record(self, timestamp, info):
        rec = self.is_onoff(info)
        if not rec:
            return False
        record = {name: value for name, value in zip(self.header, rec['data'].split())}
        self.onoff.append(dict(**{'time': timestamp}, **record))
        return True

    # Send ONOFF record to VCC
    def send_onoff(self):
        if self.onoff:
            rsp = self.vcc.get_api().post('onoff', data=self.onoff)
            if rsp:
                logger.info(f'onoff records uploaded')
            else:
                logger.warning(f'fail uploading onoff records {rsp.text}')
            self.onoff = []

    # Send station status to VCC Messenger
    def send_status(self, info):
        for (is_key, status) in self.keys:
            rec = is_key(info)
            if rec:
                if status:
                    msg = {'status': status.format(ses_id=self.ses_id, key=rec['key']), 'session': self.ses_id}
                    logger.info(f'{self.sta_id} sending {msg}')
                    # publisher = self.vcc.get_rmq_connection()
                    self.rmq.send(self.sta_id, 'sta_info', 'msg', msg)
                    # publisher.close()
                return

    # The continuous function
    def run(self):
        try:
            logger.info('start ddout scanner')
            while not self.stopped.wait(0.5):
                self.rmq.keep_connection_alive()
                path = self.get_ddout_log()
                if not path:
                    self.send_onoff()
                    self.close_log()
                else:
                    last = self.open_log(path)
                    for line in self.log:
                        rec = self.is_pcfs(line)
                        if rec:
                            timestamp, info = datetime.strptime(rec['time'], '%Y.%j.%H:%M:%S.%f'), rec['data']
                            if timestamp >= last:
                                if not self.is_onoff_header(info) and not self.is_onoff_record(timestamp, info):
                                    self.send_onoff()
                                    self.send_status(info)
                                self.last_time[self.active] = timestamp

            self.send_onoff()
            self.close_log()
        except (RMQclientException, VCCError) as exc:
            logger.warning(f'log scanner failed! - {str(exc)}')

    def stop(self):
        logger.info(f'stop ddout scanner')
        self.stopped.set()

