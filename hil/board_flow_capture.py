"""Read-only kernel capture when existing permissions allow it; no escalation."""
import json
from pathlib import Path
import subprocess
import time


class BoardFlowCapture:
    def __init__(self,directory,board_ip='192.168.4.29'):
        self.directory=Path(directory);self.board_ip=board_ip;self.process=None
    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.stream=(self.directory/'tcpdump.log').open('w')
        try:
            self.process=subprocess.Popen(['tcpdump','-i','wlp8s0','-U','-w',str(self.directory/'kernel-flow.pcap'),
                'host',self.board_ip],stdout=self.stream,stderr=subprocess.STDOUT)
            time.sleep(.3)
            self.available=self.process.poll() is None
        except OSError as exc:
            self.stream.write(str(exc)+'\n');self.available=False
        (self.directory/'kernel-status.json').write_text(json.dumps(dict(available=self.available,
            escalation=False,board_filter=self.board_ip,
            fallback='Opt-in server UDP callback pcap plus endpoint DTLS/SCTP events; synthesized headers, not kernel timestamps'),indent=2)+'\n')
        return self
    def __exit__(self,*exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.stream.close()
