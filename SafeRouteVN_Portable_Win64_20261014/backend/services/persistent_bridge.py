"""Serialized private pipe transport; timeout kills/waits before releasing ownership."""
import atexit
import json
import queue
import subprocess
import threading
import time


class PersistentBridge:
    def __init__(self):
        self.process=None;self.key=None;self.lock=threading.Lock()
        atexit.register(self.close)

    def _stop(self):
        process=self.process;self.process=None;self.key=None
        if process is None:return
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for stream in (process.stdin,process.stdout):
            if stream is not None:stream.close()

    def close(self):
        with self.lock:self._stop()

    def request(self,command,operation,fields,*,cwd,env,timeout,binding):
        deadline=time.monotonic()+timeout
        if not self.lock.acquire(timeout=max(0,timeout)):
            raise subprocess.TimeoutExpired(command,timeout)
        try:
            key=(tuple(command),str(cwd),binding,tuple(sorted(env.items())))
            if self.process is None or self.process.poll() is not None or self.key!=key:
                self._stop()
                self.process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL,text=True,encoding='utf8',bufsize=1,
                                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.key=key
            process=self.process;answer=queue.Queue(maxsize=1)
            raw=json.dumps({'operation':operation,'fields':fields},allow_nan=False)+'\n'
            if len(raw.encode('utf8'))>1048576:raise ValueError('Bridge request too large')
            def exchange():
                try:
                    process.stdin.write(raw);process.stdin.flush()
                    line=process.stdout.readline(64*1024*1024+1)
                    if not line or not line.endswith('\n') or len(line)>64*1024*1024:raise ValueError('Invalid bridge response frame')
                    answer.put((line,None))
                except Exception as error:answer.put((None,error))
            thread=threading.Thread(target=exchange,daemon=True);thread.start()
            try:
                line,error=answer.get(timeout=max(0,deadline-time.monotonic()))
            except queue.Empty:
                self._stop();thread.join(timeout=1)
                raise subprocess.TimeoutExpired(command,timeout) from None
            if error is not None:
                self._stop();raise ValueError('Bridge process stopped during request') from error
            return subprocess.CompletedProcess(command,0,stdout=line,stderr='')
        finally:self.lock.release()
